from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import cast
from unittest.mock import patch

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
from epub_blocks.cli import main


class ComposedMarkupTests(unittest.TestCase):
    def sample(self, body: str) -> tuple[Path, dict[str, object]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        epub = make_epub(
            Path(temporary.name),
            documents={
                "text/chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml" '
                'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                + body
                + "</body></html>"
            },
        )
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest(),
            omit_epub_types=[],
            source_blocks={"strict_coverage": True},
        )
        recipe["text"] = {
            "markup": {
                "between_blocks": "next",
                "trailing": "previous",
                "rules": [self.page(), self.em(), self.image()],
                "delimiters": {
                    "page": ["⟦", "⟧"],
                    "em": ["⧼", "⧽"],
                    "image": ["⟮", "⟯"],
                },
            }
        }
        return epub, recipe

    def page(self, **overrides: object) -> dict[str, object]:
        return {
            "match": {"attribute_prefixes": {"id": "page_"}},
            "kind": "milestone",
            "name": "page",
            "label_attribute": "id",
            "position": "before",
            "continue_matching": True,
            **overrides,
        }

    def em(self, **overrides: object) -> dict[str, object]:
        return {
            "match": {"classes_all": ["italic"]},
            "kind": "span",
            "name": "em",
            **overrides,
        }

    def image(self, **overrides: object) -> dict[str, object]:
        return {
            "match": {"tag": "img"},
            "kind": "milestone",
            "name": "image",
            "label_attribute": "src",
            **overrides,
        }

    def markup(self, recipe: dict[str, object]) -> dict[str, object]:
        return cast(
            dict[str, object], cast(dict[str, object], recipe["text"])["markup"]
        )

    def output(self, epub: Path, recipe: dict[str, object]) -> list[str]:
        finalize_recipe(epub, recipe)
        return [b.text for b in extract_recipe(epub, recipe)]

    def test_page_and_image_on_same_inline_element_in_both_formats(self) -> None:
        epub, recipe = self.sample(
            '<p>H<span class="italic">m<img id="page_103" src="letter.jpg"/>n</span>.</p>'
        )
        for format, wanted in [
            ("xml", 'H<em>m<page label="page_103"/><image label="letter.jpg"/>n</em>.'),
            ("delimiters", "H⧼m⟦page_103⟧⟮letter.jpg⟯n⧽."),
        ]:
            self.markup(recipe)["format"] = format
            self.assertEqual(self.output(epub, recipe), [wanted])
            self.assertEqual(
                [b.text for b in extract_recipe_candidates(epub, recipe)], ["Hmn."]
            )
            self.assertEqual(
                [b.block_id for b in extract_recipe(epub, recipe)], ["001"]
            )

    def test_leading_page_is_outside_same_element_span(self) -> None:
        epub, recipe = self.sample(
            '<p id="page_5" class="italic">A <span class="italic">nested</span> word.</p>'
        )
        self.assertEqual(
            self.output(epub, recipe),
            ['<page label="page_5"/><em>A <em>nested</em> word.</em>'],
        )
        self.markup(recipe)["format"] = "delimiters"
        self.assertEqual(self.output(epub, recipe), ["⟦page_5⟧⧼A ⧼nested⧽ word.⧽"])

    def test_multiple_prefixes_in_rule_order_then_first_terminal_rule(self) -> None:
        epub, recipe = self.sample(
            '<p id="page_5" data-label="section" class="italic">Reading.</p>'
        )
        markup = self.markup(recipe)
        markup["rules"] = [
            self.page(),
            self.image(),
            self.page(match={"tag": "p"}, name="section", label_attribute="data-label"),
            self.em(),
            self.page(match={"tag": "p"}, name="ignored"),
        ]
        markup["delimiters"] = {}
        self.assertEqual(
            self.output(epub, recipe),
            ['<page label="page_5"/><section label="section"/><em>Reading.</em>'],
        )
        # An unmatched rule never terminates an opted-in search. A matched
        # ordinary rule does terminate it, regardless of later matches.
        markup["rules"] = [self.em(), self.page()]
        self.assertEqual(self.output(epub, recipe), ["<em>Reading.</em>"])

    def test_continuing_rule_can_be_last_or_have_no_later_match(self) -> None:
        epub, recipe = self.sample('<p id="page_5">Reading.</p>')
        self.assertEqual(self.output(epub, recipe), ['<page label="page_5"/>Reading.'])
        self.markup(recipe)["rules"] = [self.page()]
        self.markup(recipe)["delimiters"] = {}
        self.assertEqual(self.output(epub, recipe), ['<page label="page_5"/>Reading.'])

    def test_false_and_absent_preserve_old_first_match_and_digest(self) -> None:
        epub, recipe = self.sample('<p id="page_5" class="italic">Reading.</p>')
        first = self.page(continue_matching=False)
        self.markup(recipe)["rules"] = [first, self.em(), self.image()]
        self.assertEqual(self.output(epub, recipe), ['<page label="page_5"/>Reading.'])
        old = compiled_recipe_digest(compile_recipe(epub, recipe))
        first.pop("continue_matching")
        self.assertEqual(compiled_recipe_digest(finalize_recipe(epub, recipe)), old)
        first["continue_matching"] = True
        self.assertNotEqual(compiled_recipe_digest(finalize_recipe(epub, recipe)), old)
        self.assertEqual(
            self.output(epub, recipe), ['<page label="page_5"/><em>Reading.</em>']
        )

    def test_option_is_hashed_even_when_no_extra_effect_matches(self) -> None:
        epub, recipe = self.sample('<p id="page_5">Reading.</p>')
        first = self.page(continue_matching=False)
        self.markup(recipe)["rules"] = [first, self.em(), self.image()]
        before = finalize_recipe(epub, recipe)
        first["continue_matching"] = True
        with self.assertRaisesRegex(EpubBlocksError, "compiled SHA-256"):
            compile_recipe(epub, recipe)
        after = finalize_recipe(epub, recipe)
        self.assertNotEqual(
            compiled_recipe_digest(before), compiled_recipe_digest(after)
        )
        self.assertEqual(before.blocks, after.blocks)

    def test_detached_composed_images_attach_once_and_keep_order(self) -> None:
        epub, recipe = self.sample(
            '<img id="page_1" src="one"/><p>A</p><p><img id="page_2" src="two"/></p><p>B</p><img id="page_3" src="three"/>'
        )
        self.assertEqual(
            self.output(epub, recipe),
            [
                '<page label="page_1"/><image label="one"/>A',
                '<page label="page_2"/><image label="two"/>B<page label="page_3"/><image label="three"/>',
            ],
        )
        plan = compile_recipe(epub, recipe)
        self.assertEqual([len(b.milestones) for b in plan.blocks], [1, 2])

    def test_leading_wrapper_bundle_belongs_only_to_retained_descendants(self) -> None:
        epub, recipe = self.sample(
            '<div id="page_5" data-label="x"><p>A</p><p>B</p></div><div id="page_6" data-label="y"><p>C</p></div><p>After</p>'
        )
        markup = self.markup(recipe)
        markup["rules"] = [
            self.page(),
            self.page(
                match={"tag": "div"}, name="section", label_attribute="data-label"
            ),
        ]
        markup["delimiters"] = {}
        recipe["source_blocks"] = {
            "include_locators": ["text/chapter.xhtml#1.2", "text/chapter.xhtml#3"]
        }
        self.assertEqual(
            self.output(epub, recipe),
            ['<page label="page_5"/><section label="x"/>B', "After"],
        )
        self.assertEqual(len(compile_recipe(epub, recipe).blocks[0].milestones), 1)

    def test_marker_only_paragraph_keeps_its_leading_label_in_both_formats(
        self,
    ) -> None:
        epub, recipe = self.sample(
            '<p id="page_5"><img src="plate"/></p><p>Caption</p>'
        )
        for format, expected in [
            ("xml", '<page label="page_5"/><image label="plate"/>Caption'),
            ("delimiters", "⟦page_5⟧⟮plate⟯Caption"),
        ]:
            self.markup(recipe)["format"] = format
            self.assertEqual(self.output(epub, recipe), [expected])
            self.assertEqual(
                [i for i, _ in compile_recipe(epub, recipe).blocks[0].milestones],
                [0, 0],
            )

    def test_nested_textless_wrappers_keep_source_order_without_duplicate_children(
        self,
    ) -> None:
        epub, recipe = self.sample(
            '<div id="page_5"><div id="page_6"><img src="a"/><img src="b"/></div></div><p>Caption</p>'
        )
        self.assertEqual(
            self.output(epub, recipe),
            [
                '<page label="page_5"/><page label="page_6"/><image label="a"/><image label="b"/>Caption'
            ],
        )
        self.assertEqual(len(compile_recipe(epub, recipe).blocks[0].milestones), 4)

    def test_empty_leading_subtrees_still_do_not_leak(self) -> None:
        epub, recipe = self.sample(
            '<p id="page_5"/><div id="page_6"><span id="page_7"/></div><img src="outside"/><p>Caption</p>'
        )
        self.assertEqual(self.output(epub, recipe), ['<image label="outside"/>Caption'])

    def test_skipped_nested_marker_does_not_rescue_its_wrapper(self) -> None:
        epub, recipe = self.sample(
            '<div id="page_5"><img src="removed"/></div><p>Keep</p>'
        )
        recipe["source_blocks"] = {
            "element_rules": [{"match": {"tag": "img"}, "action": "skip"}]
        }
        self.assertEqual(self.output(epub, recipe), ["Keep"])

    def test_filtered_marker_paragraph_does_not_donate_its_wrapper_label(self) -> None:
        epub, recipe = self.sample('<p id="page_5"><img src="removed"/></p><p>Keep</p>')
        recipe["source_blocks"] = {"exclude_locators": ["text/chapter.xhtml#1"]}
        self.assertEqual(self.output(epub, recipe), ["Keep"])

    def test_output_skip_of_an_empty_retained_child_does_not_rescue_wrapper(
        self,
    ) -> None:
        epub, recipe = self.sample(
            '<div id="page_5"><img src="removed"/></div><p>Keep</p>'
        )
        recipe["source_blocks"] = {
            "element_rules": [
                {"match": {"tag": "img"}, "action": "block", "keep_empty": True}
            ]
        }
        cast(dict[str, object], recipe["output"])["skip_source"] = [
            "text/chapter.xhtml#1.1"
        ]
        self.assertEqual(self.output(epub, recipe), ["Keep"])

    def test_textless_wrapper_follows_nested_marker_trailing_policy(self) -> None:
        epub, recipe = self.sample(
            '<p>Text</p><div id="page_5"><img src="plate"/></div>'
        )
        self.assertEqual(
            self.output(epub, recipe),
            ['Text<page label="page_5"/><image label="plate"/>'],
        )
        self.markup(recipe).pop("trailing")
        with self.assertRaisesRegex(EpubBlocksError, "trailing: previous"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_textless_wrapper_requires_the_nested_markers_between_block_policy(
        self,
    ) -> None:
        epub, recipe = self.sample(
            '<div id="page_5"><img src="plate"/></div><p>Caption</p>'
        )
        self.markup(recipe).pop("between_blocks")
        with self.assertRaisesRegex(EpubBlocksError, "between_blocks: next"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_textless_wrapper_inside_consumed_join_keeps_its_part_position(
        self,
    ) -> None:
        epub, recipe = self.sample(
            '<p class="join">A</p><div id="page_5"><img src="plate"/></div><p>B</p>'
        )
        cast(dict[str, object], recipe["output"])["rules"] = [
            {
                "match": {"classes_all": ["join"]},
                "type": "paragraph",
                "role": "block",
                "consume": 2,
            }
        ]
        self.assertEqual(
            self.output(epub, recipe),
            ['A <page label="page_5"/><image label="plate"/>B'],
        )
        self.assertEqual(
            [i for i, _ in compile_recipe(epub, recipe).blocks[0].milestones], [1, 1]
        )

    def test_marker_ancestry_does_not_confuse_numeric_path_prefixes(self) -> None:
        epub, recipe = self.sample(
            '<div id="page_1"/>'
            + "<span/>" * 8
            + '<div><img src="outside"/></div><p>Keep</p>'
        )
        self.assertEqual(self.output(epub, recipe), ['<image label="outside"/>Keep'])

    def test_added_wrapper_attachments_are_part_of_the_compiled_hash(self) -> None:
        epub, recipe = self.sample(
            '<p id="page_5"><img src="plate"/></p><p>Caption</p>'
        )
        compiled = finalize_recipe(epub, recipe)
        block = compiled.blocks[0]
        old = replace(
            compiled, blocks=(replace(block, milestones=block.milestones[1:]),)
        )
        cast(dict[str, object], recipe["output"])["compiled_sha256"] = (
            compiled_recipe_digest(old)
        )
        with self.assertRaisesRegex(EpubBlocksError, "compiled SHA-256"):
            compile_recipe(epub, recipe)

    def test_whole_item_combines_page_counter_and_span(self) -> None:
        epub, recipe = self.sample(
            '<ol start="4"><li id="page_5" class="italic"><p>A</p><p>B</p></li></ol>'
        )
        self.markup(recipe)["rules"] = [
            self.page(),
            {
                "match": {"tag": "li"},
                "kind": "milestone",
                "name": "number",
                "position": "before",
                "label_counter": "ordered-list",
                "continue_matching": True,
            },
            self.em(),
        ]
        self.markup(recipe)["delimiters"] = {}
        cast(dict[str, object], recipe["text"])["block_boundaries"] = {
            "tags": ["p"],
            "separator": " ",
        }
        recipe["source_blocks"] = {
            "element_rules": [{"match": {"tag": "li"}, "action": "block"}]
        }
        self.assertEqual(
            self.output(epub, recipe),
            ['<page label="page_5"/><number label="4"/><em>A B</em>'],
        )

    def test_text_labels_see_source_contents_not_other_marker_labels(self) -> None:
        epub, recipe = self.sample(
            '<p id="page_5" class="italic">  An <span>example</span> </p>'
        )
        first = self.page(name="reading", label_text=True)
        first.pop("label_attribute")
        self.markup(recipe)["rules"] = [first, self.page(), self.em()]
        self.markup(recipe)["delimiters"] = {}
        self.assertEqual(
            self.output(epub, recipe),
            ['<reading label="An example"/><page label="page_5"/><em>An example</em>'],
        )

    def test_label_errors_in_later_effects_are_not_hidden(self) -> None:
        epub, recipe = self.sample('<p>A<img id="page_1"/></p>')
        operations: tuple[Callable[[Path, dict[str, object]], object], ...] = (
            extract_recipe_candidates,
            lambda p, r: compile_recipe(p, r, verify_digest=False),
        )
        for operation in operations:
            with self.assertRaisesRegex(EpubBlocksError, "label attribute 'src'"):
                operation(epub, recipe)

    def test_duplicate_effect_names_on_one_element_are_rejected(self) -> None:
        epub, recipe = self.sample('<p id="page_5">Reading.</p>')
        self.markup(recipe)["rules"] = [self.page(), self.page()]
        self.markup(recipe)["delimiters"] = {}
        with self.assertRaisesRegex(
            EpubBlocksError, "composed markup repeats name 'page'"
        ):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_source_skips_remove_conflicting_rules_before_evaluation(self) -> None:
        epub, recipe = self.sample(
            '<p id="page_5" class="omit">Discard.</p><p>Keep.</p>'
        )
        self.markup(recipe)["rules"] = [self.page(), self.page()]
        self.markup(recipe)["delimiters"] = {}
        recipe["source_blocks"] = {
            "element_rules": [{"match": {"classes_all": ["omit"]}, "action": "skip"}]
        }
        self.assertEqual(self.output(epub, recipe), ["Keep."])

    def test_replacing_effect_still_cannot_swallow_nested_milestones(self) -> None:
        epub, recipe = self.sample(
            '<p>Before<span id="page_5" class="replace"><img src="nested"/></span>After</p>'
        )
        self.markup(recipe)["rules"] = [
            self.page(),
            {
                "match": {"classes_all": ["replace"]},
                "kind": "milestone",
                "name": "replacement",
            },
            self.image(),
        ]
        self.markup(recipe)["delimiters"] = {}
        with self.assertRaisesRegex(EpubBlocksError, "nested milestone"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_composition_does_not_bypass_semantic_omissions(self) -> None:
        epub, recipe = self.sample(
            '<p>A<img id="page_5" src="image" epub:type="pagebreak"/>B</p>'
        )
        recipe["omit_epub_types"] = ["pagebreak"]
        with self.assertRaisesRegex(EpubBlocksError, "semantically omitted"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_slices_keep_coincident_events_together_at_shared_boundary(self) -> None:
        epub, recipe = self.sample('<p>AB<img id="page_5" src="letter"/>CD</p>')
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
                                "slice": {"start": start, "end": end},
                            }
                        ],
                    }
                    for start, end in [(0, 2), (2, 4)]
                ],
            }
        ]
        self.assertEqual(
            self.output(epub, recipe),
            ["AB", '<page label="page_5"/><image label="letter"/>CD'],
        )

    def test_reusing_a_composed_source_from_two_fragments_still_fails(self) -> None:
        epub, recipe = self.sample(
            '<p><span>A<img id="page_5" src="letter"/>B</span></p>'
        )
        cast(dict[str, object], recipe["output"])["insertions"] = [
            {
                "after": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "paragraph",
                        "parts": [
                            {"document": "text/chapter.xhtml", "element_path": "1.1"}
                        ],
                    }
                ],
            }
        ]
        with self.assertRaisesRegex(
            EpubBlocksError, "milestone is emitted more than once"
        ):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_detached_bundle_in_consumed_join_keeps_its_part_position(self) -> None:
        epub, recipe = self.sample(
            '<p class="join">A</p><img id="page_5" src="letter"/><p>B</p>'
        )
        cast(dict[str, object], recipe["output"])["rules"] = [
            {
                "match": {"classes_all": ["join"]},
                "type": "paragraph",
                "role": "block",
                "consume": 2,
            }
        ]
        self.assertEqual(
            self.output(epub, recipe),
            ['A <page label="page_5"/><image label="letter"/>B'],
        )
        self.assertEqual(
            [i for i, _ in compile_recipe(epub, recipe).blocks[0].milestones], [1]
        )

    def test_fragment_omission_removes_whole_bundle_and_keeps_tail(self) -> None:
        epub, recipe = self.sample('<p>A<img id="page_5" src="letter"/>B</p>')
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
        self.assertEqual(self.output(epub, recipe), ["AB"])

    def test_file_cli_and_escaping_preserve_composed_effects_without_tsv_quotes(
        self,
    ) -> None:
        epub, recipe = self.sample('<p>A<img id="page_5" src="a&amp;&quot;b"/>B</p>')
        recipe_path, output_path = (
            epub.parent / "recipe.json",
            epub.parent / "output.tsv",
        )
        for format in ("xml", "delimiters"):
            self.markup(recipe)["format"] = format
            finalize_recipe(epub, recipe)
            recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
            with (
                patch.object(
                    sys,
                    "argv",
                    ["epub-blocks", str(epub), str(recipe_path), str(output_path)],
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 0)
            data = output_path.read_text(encoding="utf-8")
            expected = (
                'A<page label="page_5"/><image label="a&amp;&quot;b"/>B'
                if format == "xml"
                else 'A⟦page_5⟧⟮a&"b⟯B'
            )
            self.assertEqual(data, "001\tparagraph\t" + expected + "\n")
            self.markup(recipe)["rules"] = [
                self.page(),
                self.image(continue_matching=True),
            ]
            recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
            with (
                patch.object(
                    sys,
                    "argv",
                    ["epub-blocks", str(epub), str(recipe_path), str(output_path)],
                ),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(), 2)
            self.assertEqual(output_path.read_text(encoding="utf-8"), data)
            self.markup(recipe)["rules"] = [self.page(), self.em(), self.image()]

    def test_schema_runtime_and_typed_state_validate_option(self) -> None:
        epub, recipe = self.sample('<p id="page_5">Reading.</p>')
        validator = Draft202012Validator(
            json.loads(
                files("epub_blocks")
                .joinpath("schemas/recipe-v1.schema.json")
                .read_text()
            )
        )
        invalid: tuple[object, ...] = (None, "true", 0, 1, [], {})
        for value in (*invalid, False, True):
            candidate = copy.deepcopy(recipe)
            self.markup(candidate)["rules"] = [self.page(continue_matching=value)]
            self.markup(candidate)["delimiters"] = {}
            valid = type(value) is bool
            self.assertEqual(validator.is_valid(candidate), valid)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
            if valid:
                compile_recipe(epub, candidate, verify_digest=False)
            else:
                with self.assertRaisesRegex(EpubBlocksError, "continue_matching"):
                    compile_recipe(epub, candidate, verify_digest=False)
        for rule in [
            self.page(position="replace"),
            self.image(continue_matching=True),
            self.em(continue_matching=True),
        ]:
            candidate = copy.deepcopy(recipe)
            self.markup(candidate)["rules"] = [rule]
            self.markup(candidate)["delimiters"] = {}
            self.assertFalse(validator.is_valid(candidate))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
            with self.assertRaisesRegex(EpubBlocksError, "continue_matching"):
                compile_recipe(epub, candidate, verify_digest=False)
        plan = finalize_recipe(epub, recipe)
        markup = plan.content.markup
        assert markup is not None
        for value in invalid:
            corrupt = replace(markup.rules[0], continue_matching=cast(bool, value))
            with self.assertRaisesRegex(EpubBlocksError, "continue_matching"):
                compiled_recipe_digest(
                    replace(
                        plan,
                        content=replace(
                            plan.content, markup=replace(markup, rules=(corrupt,))
                        ),
                    )
                )
        corrupt = replace(markup.rules[0], position="replace")
        with self.assertRaisesRegex(EpubBlocksError, "continue_matching"):
            compiled_recipe_digest(
                replace(
                    plan,
                    content=replace(
                        plan.content, markup=replace(markup, rules=(corrupt,))
                    ),
                )
            )
