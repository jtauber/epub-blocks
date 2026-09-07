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


class LabelTextTests(unittest.TestCase):
    def sample(self, body: str) -> tuple[Path, dict[str, object]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        epub = make_epub(
            Path(temporary.name),
            documents={
                "text/chapter.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                    + body
                    + "</body></html>"
                )
            },
        )
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest(),
            omit_epub_types=["noteref"],
        )
        recipe["text"] = {
            "markup": {
                "between_blocks": "next",
                "trailing": "previous",
                "rules": [self.rule()],
                "delimiters": {"printed-line": ["⟦", "⟧"]},
            }
        }
        return epub, recipe

    def rule(self, **overrides: object) -> dict[str, object]:
        rule: dict[str, object] = {
            "match": {"classes_any": ["number"]},
            "kind": "milestone",
            "name": "printed-line",
            "label_text": True,
        }
        rule.update(overrides)
        return rule

    def markup(self, recipe: dict[str, object]) -> dict[str, object]:
        return cast(
            dict[str, object], cast(dict[str, object], recipe["text"])["markup"]
        )

    def test_inline_and_detached_labels_do_not_consume_verse_numbers(self) -> None:
        epub, recipe = self.sample(
            '<p class="first">First</p>'
            '<div class="line"><p class="number"> 005 </p><p>Second</p></div>'
            '<div><p class="number">010</p></div><p class="line">Third</p>'
            '<p>After</p><span class="number">015</span>'
        )
        recipe["source_blocks"] = {
            "element_rules": [
                {"match": {"tag": "div", "classes_any": ["line"]}, "action": "block"}
            ],
            "strict_coverage": True,
        }
        cast(dict[str, object], recipe["output"])["rules"] = [
            {
                "match": {"classes_any": ["first"]},
                "type": "verse-line",
                "role": "line-start",
            },
            {"match": {"classes_any": ["line"]}, "type": "verse-line", "role": "line"},
        ]
        for serialization, expected in (
            (
                "xml",
                [
                    "First",
                    '<printed-line label="005"/>Second',
                    '<printed-line label="010"/>Third',
                    'After<printed-line label="015"/>',
                ],
            ),
            ("delimiters", ["First", "⟦005⟧Second", "⟦010⟧Third", "After⟦015⟧"]),
        ):
            with self.subTest(format=serialization):
                self.markup(recipe)["format"] = serialization
                finalize_recipe(epub, recipe)
                blocks = extract_recipe(epub, recipe)
                self.assertEqual(
                    [b.block_id for b in blocks], ["001.01", "001.02", "001.03", "002"]
                )
                self.assertEqual([b.text for b in blocks], expected)
                self.assertEqual(
                    [b.text for b in extract_recipe_candidates(epub, recipe)],
                    ["First", "Second", "Third", "After"],
                )

    def test_label_uses_retained_subtree_boundaries_not_tail_or_markup(self) -> None:
        epub, recipe = self.sample(
            '<p>A<span class="number"> 00<em>5</em>'
            '<i class="omit">gone</i><a epub:type="noteref">gone</a>'
            '<span class="boundary"/>6<br/>7 </span> tail</p>'
        )
        recipe["source_blocks"] = {
            "element_rules": [{"match": {"classes_any": ["omit"]}, "action": "skip"}]
        }
        cast(dict[str, object], recipe["text"])["block_boundaries"] = {
            "rules": [{"match": {"classes_any": ["boundary"]}, "before": " "}]
        }
        cast(list[object], self.markup(recipe)["rules"]).append(
            {"match": {"tag": "em"}, "kind": "span", "name": "em"}
        )
        finalize_recipe(epub, recipe)
        self.assertEqual(
            extract_recipe(epub, recipe)[0].text,
            'A<printed-line label="005 6 7"/> tail',
        )

    def test_labels_keep_unicode_and_leading_zeroes_but_collapse_whitespace(
        self,
    ) -> None:
        epub, recipe = self.sample(
            '<p><span class="number">\t 007\u00a0 e\u0301 &amp; &quot;⟧ \\ </span>e\u0301</p>'
        )
        for serialization, expected in (
            ("xml", '<printed-line label="007 e\u0301 &amp; &quot;⟧ \\"/>é'),
            ("delimiters", '⟦007 e\u0301 & "\\⟧ \\\\⟧é'),
        ):
            with self.subTest(format=serialization):
                self.markup(recipe)["format"] = serialization
                finalize_recipe(epub, recipe)
                self.assertEqual(extract_recipe(epub, recipe)[0].text, expected)

    def test_empty_retained_label_is_an_error(self) -> None:
        for content in ("", " \t\n ", '<a epub:type="noteref">omitted</a>'):
            with self.subTest(content=content):
                epub, recipe = self.sample(
                    f'<p><span class="number">{content}</span>Text</p>'
                )
                with self.assertRaisesRegex(
                    EpubBlocksError, "milestone label text is empty"
                ):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_label_whitespace_policy_is_independent_of_prose_settings(self) -> None:
        epub, recipe = self.sample(
            '<p>  A <span class="number">  00\t5  </span> B  </p>'
        )
        recipe["normalization"] = {
            "collapse_whitespace": False,
            "strip": False,
            "unicode_normalization": "none",
        }
        finalize_recipe(epub, recipe)
        self.assertEqual(
            extract_recipe(epub, recipe)[0].text, '  A <printed-line label="00 5"/> B  '
        )

    def test_fragment_omissions_keep_original_selector_context_for_labels(self) -> None:
        epub, recipe = self.sample(
            '<p>A<span class="number"><b class="before">old</b>'
            '<i class="skip">discard</i><em>005</em></span>tail</p>'
        )
        recipe["source_blocks"] = {
            "element_rules": [
                {
                    "match": {
                        "classes_any": ["skip"],
                        "previous_sibling": {"classes_any": ["before"]},
                    },
                    "action": "skip",
                }
            ]
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
                                "omit": ["1.1"],
                            }
                        ],
                    }
                ],
            }
        ]
        finalize_recipe(epub, recipe)
        self.assertEqual(
            extract_recipe(epub, recipe)[0].text, 'A<printed-line label="005"/>tail'
        )

    def test_nested_milestones_are_not_silently_lost(self) -> None:
        epub, recipe = self.sample(
            '<p><span class="number">005<span class="page"/></span>Text</p>'
        )
        cast(list[object], self.markup(recipe)["rules"]).append(
            {"match": {"classes_any": ["page"]}, "kind": "milestone", "name": "page"}
        )
        with self.assertRaisesRegex(EpubBlocksError, "nested milestone"):
            compile_recipe(epub, recipe, verify_digest=False)
        recipe["source_blocks"] = {
            "element_rules": [{"match": {"classes_any": ["page"]}, "action": "skip"}]
        }
        finalize_recipe(epub, recipe)
        self.assertEqual(
            extract_recipe(epub, recipe)[0].text, '<printed-line label="005"/>Text'
        )

    def test_schema_and_runtime_validation(self) -> None:
        epub, base = self.sample('<p><span class="number" id="005">005</span>Text</p>')
        validator = Draft202012Validator(
            json.loads(
                files("epub_blocks")
                .joinpath("schemas/recipe-v1.schema.json")
                .read_text()
            )
        )
        invalid_values: list[object] = [0, 1, None, "true", [], {}]
        cases = [
            (self.rule(), True),
            (self.rule(label_text=False), True),
            (self.rule(label_attribute="id"), False),
            (self.rule(label_text=False, label_attribute="id"), True),
            (self.rule(kind="span"), False),
            (self.rule(kind="span", label_text=False), False),
            *[(self.rule(label_text=value), False) for value in invalid_values],
        ]
        for rule, valid in cases:
            with self.subTest(rule=rule):
                recipe = copy.deepcopy(base)
                self.markup(recipe)["rules"] = [rule]
                self.assertEqual(validator.is_valid(recipe), valid)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                if valid:
                    compile_recipe(epub, recipe, verify_digest=False)
                else:
                    with self.assertRaises(EpubBlocksError):
                        compile_recipe(epub, recipe, verify_digest=False)

    def test_false_is_hash_neutral_and_true_changes_hash(self) -> None:
        epub, recipe = self.sample('<p><span class="number">005</span>Text</p>')
        self.markup(recipe)["rules"] = [self.rule(label_text=False)]
        old = compiled_recipe_digest(finalize_recipe(epub, recipe))
        rule = self.rule()
        del rule["label_text"]
        self.markup(recipe)["rules"] = [rule]
        self.assertEqual(compiled_recipe_digest(finalize_recipe(epub, recipe)), old)
        self.markup(recipe)["rules"] = [self.rule()]
        self.assertNotEqual(compiled_recipe_digest(finalize_recipe(epub, recipe)), old)

    def test_public_compiled_models_reject_non_booleans_and_conflicting_labels(
        self,
    ) -> None:
        epub, recipe = self.sample('<p><span class="number">005</span>Text</p>')
        compiled = finalize_recipe(epub, recipe)
        markup = compiled.content.markup
        assert markup is not None
        for invalid in (0, 1, None, "true"):
            with self.subTest(value=invalid):
                content = replace(
                    compiled.content,
                    markup=replace(
                        markup,
                        rules=(
                            replace(markup.rules[0], label_text=cast(bool, invalid)),
                        ),
                    ),
                )
                with self.assertRaises(EpubBlocksError):
                    compiled_recipe_digest(replace(compiled, content=content))
        for invalid_rule in (
            replace(markup.rules[0], label_attribute="id"),
            replace(markup.rules[0], kind="span"),
        ):
            content = replace(
                compiled.content, markup=replace(markup, rules=(invalid_rule,))
            )
            with self.assertRaises(EpubBlocksError):
                compiled_recipe_digest(replace(compiled, content=content))
