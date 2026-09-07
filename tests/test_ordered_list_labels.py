from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import cast

from jsonschema import (  # pyright: ignore[reportMissingModuleSource]
    Draft202012Validator,
)
from test_epub_blocks import finalize_recipe, make_epub, minimal_recipe

from epub_blocks import (
    EpubBlocksError,
    compile_recipe,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_candidates,
)


class OrderedListTests(unittest.TestCase):
    def sample(self, body: str) -> tuple[Path, dict[str, object]]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        epub = make_epub(
            Path(temp.name),
            documents={
                "text/chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                + body
                + "</body></html>"
            },
        )
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )
        recipe["text"] = {
            "block_boundaries": {"tags": ["p", "li"], "separator": " "},
            "markup": {
                "rules": [
                    self.rule(),
                    {"match": {"tag": "em"}, "kind": "span", "name": "em"},
                ],
                "delimiters": {"list-number": ["⟬", "⟭"], "em": ["⧼", "⧽"]},
            },
        }
        return epub, recipe

    def rule(self, **overrides: object) -> dict[str, object]:
        return {
            "match": {"tag": "li"},
            "kind": "milestone",
            "name": "list-number",
            "position": "before",
            "label_counter": "ordered-list",
            **overrides,
        }

    def markup(self, recipe: dict[str, object]) -> dict[str, object]:
        return cast(
            dict[str, object], cast(dict[str, object], recipe["text"])["markup"]
        )

    def readings(self, epub: Path, recipe: dict[str, object]) -> list[str]:
        finalize_recipe(epub, recipe)
        return [b.text for b in extract_recipe(epub, recipe)]

    def test_defaults_starts_resets_and_serializers(self) -> None:
        epub, recipe = self.sample(
            '<ol><li>A</li><li>B</li></ol><ol start="  +5 "><li><p><em>C</em></p><p>continued</p></li><li value="0"><p>D</p></li><li><p>E</p></li><li value="-2">F</li><li>G</li></ol>'
        )
        self.assertEqual(
            self.readings(epub, recipe),
            [
                '<list-number label="1"/>A',
                '<list-number label="2"/>B',
                '<list-number label="5"/><em>C</em>',
                "continued",
                '<list-number label="0"/>D',
                '<list-number label="1"/>E',
                '<list-number label="-2"/>F',
                '<list-number label="-1"/>G',
            ],
        )
        self.markup(recipe)["format"] = "delimiters"
        self.assertEqual(
            self.readings(epub, recipe),
            ["⟬1⟭A", "⟬2⟭B", "⟬5⟭⧼C⧽", "continued", "⟬0⟭D", "⟬1⟭E", "⟬-2⟭F", "⟬-1⟭G"],
        )
        self.assertEqual(
            [b.block_id for b in extract_recipe(epub, recipe)],
            [f"{n:03d}" for n in range(1, 9)],
        )
        self.assertEqual(
            [b.text for b in extract_recipe_candidates(epub, recipe)],
            list("ABC") + ["continued"] + list("DEFG"),
        )

    def test_whole_list_item_and_nested_paragraphs_agree(self) -> None:
        epub, recipe = self.sample(
            '<ol><li><p>A</p><ol start="8"><li><p><em>B</em></p></li><li>C</li></ol></li><li>D</li></ol>'
        )
        self.assertEqual(
            self.readings(epub, recipe),
            [
                '<list-number label="1"/>A',
                '<list-number label="8"/><em>B</em>',
                '<list-number label="9"/>C',
                '<list-number label="2"/>D',
            ],
        )
        for tag, expected in [
            (
                "li",
                [
                    '<list-number label="1"/>A <list-number label="8"/><em>B</em> <list-number label="9"/>C',
                    '<list-number label="2"/>D',
                ],
            ),
            (
                "ol",
                [
                    '<list-number label="1"/>A <list-number label="8"/><em>B</em> <list-number label="9"/>C <list-number label="2"/>D'
                ],
            ),
        ]:
            with self.subTest(tag=tag):
                recipe["source_blocks"] = {
                    "element_rules": [{"match": {"tag": tag}, "action": "block"}]
                }
                self.assertEqual(self.readings(epub, recipe), expected)

    def test_nested_prefixes_on_same_paragraph_keep_source_order(self) -> None:
        epub, recipe = self.sample(
            '<ol><li><ol start="8"><li><p>A</p></li></ol></li><li><p>B</p></li></ol>'
        )
        self.assertEqual(
            self.readings(epub, recipe),
            [
                '<list-number label="1"/><list-number label="8"/>A',
                '<list-number label="2"/>B',
            ],
        )

    def test_reversed_uses_original_direct_item_count(self) -> None:
        epub, recipe = self.sample(
            '<ol reversed="false"><li><p>A</p><ol><li><p>nested</p></li></ol></li><li value="0"><p>B</p></li><li><p>C</p></li></ol><ol reversed="reversed" start="9"><li>D</li><li>E</li></ol>'
        )
        self.assertEqual(
            self.readings(epub, recipe),
            [
                '<list-number label="3"/>A',
                '<list-number label="1"/>nested',
                '<list-number label="0"/>B',
                '<list-number label="-1"/>C',
                '<list-number label="9"/>D',
                '<list-number label="8"/>E',
            ],
        )

    def test_filters_do_not_renumber_or_leak_labels(self) -> None:
        epub, recipe = self.sample(
            '<ol><li value="5"><p>A</p></li><li><p>B</p><p>continued</p></li><li><p>C</p></li></ol><p>After</p>'
        )
        for source in [
            {
                "exclude_locators": [
                    "text/chapter.xhtml#1.1.1",
                    "text/chapter.xhtml#1.2.1",
                    "text/chapter.xhtml#1.3.1",
                ]
            },
            {"include_locators": ["text/chapter.xhtml#1.2.2", "text/chapter.xhtml#2"]},
            {
                "element_rules": [
                    {
                        "match": {
                            "locators": [
                                "text/chapter.xhtml#1.1",
                                "text/chapter.xhtml#1.2.1",
                                "text/chapter.xhtml#1.3",
                            ]
                        },
                        "action": "skip",
                    }
                ]
            },
        ]:
            with self.subTest(source=source):
                recipe["source_blocks"] = source
                self.assertEqual(
                    self.readings(epub, recipe),
                    ['<list-number label="6"/>continued', "After"],
                )
        recipe["source_blocks"] = {}
        cast(dict[str, object], recipe["output"])["skip_source"] = [
            "text/chapter.xhtml#1.1.1",
            "text/chapter.xhtml#1.2.1",
            "text/chapter.xhtml#1.2.2",
            "text/chapter.xhtml#1.3.1",
        ]
        self.assertEqual(self.readings(epub, recipe), ["After"])

    def test_whole_item_filter_and_empty_item_do_not_leak(self) -> None:
        epub, recipe = self.sample(
            '<ol><li value="5" class="omit">A</li><li></li><li>B</li></ol>'
        )
        recipe["source_blocks"] = {"exclude_classes": ["omit"]}
        self.assertEqual(self.readings(epub, recipe), ['<list-number label="7"/>B'])

    def test_fragment_omissions_keep_original_ordinals(self) -> None:
        epub, recipe = self.sample(
            '<ol start="5"><li><p>A</p></li><li><p><em>B</em></p></li><li><p>C</p></li></ol>'
        )
        recipe["source_blocks"] = {
            "element_rules": [{"match": {"tag": "ol"}, "action": "block"}]
        }
        cast(dict[str, object], recipe["output"])["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "paragraph",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1",
                                "omit": ["1"],
                            }
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(
            self.readings(epub, recipe),
            ['<list-number label="6"/><em>B</em> <list-number label="7"/>C'],
        )

    def test_slices_do_not_restore_or_duplicate_a_leading_marker(self) -> None:
        epub, recipe = self.sample(
            "<p>Intro</p><ol><li><p><em>ABCD</em></p></li></ol><p>After</p>"
        )
        cast(dict[str, object], recipe["output"])["skip_source"] = [
            "text/chapter.xhtml#2.1.1"
        ]
        for selection, expected in [
            ({"slice": {"start": 0, "end": 2}}, '<list-number label="1"/><em>AB</em>'),
            ({"slice": {"start": 2, "end": 4}}, "<em>CD</em>"),
            ({}, '<list-number label="1"/><em>ABCD</em>'),
        ]:
            with self.subTest(selection=selection):
                cast(dict[str, object], recipe["output"])["insertions"] = [
                    {
                        "after": "text/chapter.xhtml#1",
                        "outputs": [
                            {
                                "type": "paragraph",
                                "parts": [
                                    {
                                        "document": "text/chapter.xhtml",
                                        "element_path": "2.1",
                                        **selection,
                                    }
                                ],
                            }
                        ],
                    }
                ]
                self.assertEqual(
                    self.readings(epub, recipe), ["Intro", expected, "After"]
                )

    def test_decimal_style_and_non_item_children(self) -> None:
        epub, recipe = self.sample(
            '<ol type="1" reversed="reversed"><script/><li type="1">A</li><li value="+005">B</li><li>C</li></ol>'
        )
        self.assertEqual(
            self.readings(epub, recipe),
            [
                '<list-number label="3"/>A',
                '<list-number label="5"/>B',
                '<list-number label="4"/>C',
            ],
        )

    def test_large_counter_overflow_is_a_domain_error(self) -> None:
        epub, recipe = self.sample(
            '<ol start="' + "9" * 4300 + '"><li>A</li><li>B</li></ol>'
        )
        with self.assertRaisesRegex(EpubBlocksError, "ordinal is too large"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_leading_attribute_and_text_labels_retain_nested_events(self) -> None:
        epub, recipe = self.sample(
            '<div id="x&amp;y"><p><em>A</em><span id="7"/>B</p></div>'
        )
        self.markup(recipe)["rules"] = [
            {
                "match": {"tag": "div"},
                "kind": "milestone",
                "name": "list-number",
                "position": "before",
                "label_attribute": "id",
            },
            {"match": {"tag": "em"}, "kind": "span", "name": "em"},
            {
                "match": {"tag": "span"},
                "kind": "milestone",
                "name": "page",
                "label_attribute": "id",
            },
        ]
        self.assertEqual(
            self.readings(epub, recipe),
            ['<list-number label="x&amp;y"/><em>A</em><page label="7"/>B'],
        )
        rules = cast(list[dict[str, object]], self.markup(recipe)["rules"])
        rules[0].pop("label_attribute")
        rules[0]["label_text"] = True
        with self.assertRaisesRegex(EpubBlocksError, "selecting the wrapper"):
            self.readings(epub, recipe)
        recipe["source_blocks"] = {
            "element_rules": [{"match": {"tag": "div"}, "action": "block"}]
        }
        self.assertEqual(
            self.readings(epub, recipe),
            ['<list-number label="AB"/><em>A</em><page label="7"/>B'],
        )

    def test_replacing_parent_cannot_discard_leading_child(self) -> None:
        epub, recipe = self.sample('<ol id="x"><li>A</li></ol>')
        cast(list[object], self.markup(recipe)["rules"]).insert(
            0,
            {
                "match": {"tag": "ol"},
                "kind": "milestone",
                "name": "parent",
                "label_attribute": "id",
            },
        )
        with self.assertRaisesRegex(EpubBlocksError, "nested milestone"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_invalid_source_counter_context_and_values(self) -> None:
        cases = [
            "<li>A</li>",
            "<ul><li>A</li></ul>",
            '<ol type="a"><li>A</li></ol>',
            '<ol><li type="I">A</li></ol>',
        ]
        for attribute in ("start", "value"):
            for value in ("", "one", "1.5", "1x", "١", "\u00a01", "9" * 5000):
                cases.append(
                    f"<ol {attribute + '=' + chr(34) + value + chr(34) if attribute == 'start' else ''}><li {attribute + '=' + chr(34) + value + chr(34) if attribute == 'value' else ''}>A</li></ol>"
                )
        for body in cases:
            with self.subTest(body=body[:80]):
                epub, recipe = self.sample(body)
                with self.assertRaisesRegex(EpubBlocksError, "ordered-list"):
                    compile_recipe(epub, recipe, verify_digest=False)
        epub, recipe = self.sample("<p>A</p>")
        self.markup(recipe)["rules"] = [self.rule(match={"tag": "p"})]
        self.markup(recipe)["delimiters"] = {"list-number": ["⟬", "⟭"]}
        with self.assertRaisesRegex(EpubBlocksError, "direct li"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_schema_runtime_and_typed_validation(self) -> None:
        epub, recipe = self.sample("<ol><li>A</li></ol>")
        validator = Draft202012Validator(
            json.loads(
                files("epub_blocks")
                .joinpath("schemas/recipe-v1.schema.json")
                .read_text()
            )
        )
        for overrides, valid in [
            ({}, True),
            ({"label_text": False}, True),
            ({"kind": "span"}, False),
            ({"position": "replace"}, False),
            ({"label_text": True}, False),
            ({"label_attribute": "id"}, False),
            ({"label_counter": "roman"}, False),
            ({"label_counter": None}, False),
            ({"position": None}, False),
            ({"position": False}, False),
            ({"position": "after"}, False),
        ]:
            with self.subTest(overrides=overrides):
                candidate = copy.deepcopy(recipe)
                self.markup(candidate)["rules"] = [self.rule(**overrides)]
                self.markup(candidate)["delimiters"] = {"list-number": ["⟬", "⟭"]}
                self.assertEqual(validator.is_valid(candidate), valid)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                if valid:
                    compile_recipe(epub, candidate, verify_digest=False)
                else:
                    with self.assertRaises(EpubBlocksError):
                        compile_recipe(epub, candidate, verify_digest=False)
        compiled = finalize_recipe(epub, recipe)
        markup = compiled.content.markup
        assert markup is not None
        for rule in [
            replace(markup.rules[0], position=cast(str, None)),
            replace(markup.rules[0], label_counter=cast(str, False)),
            replace(markup.rules[0], position="replace"),
            replace(markup.rules[0], kind="span"),
        ]:
            with self.assertRaises(EpubBlocksError):
                compiled_recipe_digest(
                    replace(
                        compiled,
                        content=replace(
                            compiled.content, markup=replace(markup, rules=(rule,))
                        ),
                    )
                )

    def test_implicit_replace_preserves_digest_and_before_changes_it(self) -> None:
        epub, recipe = self.sample("<p><em>A</em>B</p>")
        rule = {"match": {"tag": "em"}, "kind": "milestone", "name": "em"}
        self.markup(recipe)["rules"] = [rule]
        self.markup(recipe)["delimiters"] = {"em": ["⧼", "⧽"]}
        recipe["source_blocks"] = {
            "element_rules": [
                {"match": {"tag": "p"}, "action": "block", "keep_empty": True}
            ]
        }
        digest = compiled_recipe_digest(finalize_recipe(epub, recipe))
        rule["position"] = "replace"
        self.assertEqual(compiled_recipe_digest(finalize_recipe(epub, recipe)), digest)
        rule["position"] = "before"
        self.assertNotEqual(
            compiled_recipe_digest(finalize_recipe(epub, recipe)), digest
        )
        self.assertEqual(extract_recipe(epub, recipe)[0].text, "<em/>AB")
