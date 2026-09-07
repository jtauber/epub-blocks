"""Regressions for markup validation after selection and explicit label ownership."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import test_composed_markup as composed
import test_ordered_list_labels as ordered
from test_epub_blocks import finalize_recipe

from epub_blocks import (
    EpubBlocksError,
    compile_recipe,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_candidates,
)
from epub_blocks.cli import main


class RetainedMarkupTests(unittest.TestCase):
    def sample(self, body: str) -> tuple[Path, dict[str, object]]:
        helper = composed.ComposedMarkupTests()
        self.addCleanup(helper.doCleanups)
        return helper.sample(body)

    def list_sample(self, body: str) -> tuple[Path, dict[str, object]]:
        helper = ordered.OrderedListTests()
        self.addCleanup(helper.doCleanups)
        return helper.sample(body)

    @staticmethod
    def markup(recipe: dict[str, object]) -> dict[str, object]:
        return cast(
            dict[str, object], cast(dict[str, object], recipe["text"])["markup"]
        )

    @staticmethod
    def output(recipe: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], recipe["output"])

    @staticmethod
    def text_label() -> dict[str, object]:
        return {
            "match": {"classes_all": ["label"]},
            "kind": "milestone",
            "name": "label",
            "position": "before",
            "label_text": True,
        }

    def labelled(self, body: str) -> tuple[Path, dict[str, object]]:
        epub, recipe = self.sample(body)
        self.markup(recipe).update(
            rules=[self.text_label()], delimiters={"label": ["⟦", "⟧"]}
        )
        return epub, recipe

    def readings(self, epub: Path, recipe: dict[str, object]) -> list[str]:
        finalize_recipe(epub, recipe)
        return [block.text for block in extract_recipe(epub, recipe)]

    def test_partial_wrapper_omission_is_rejected_in_both_serializers(self) -> None:
        epub, recipe = self.labelled(
            '<div class="label"><p>Start <span>OMITTED</span> end</p></div><p>After</p>'
        )
        self.output(recipe)["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#1.1",
                "outputs": [
                    {
                        "type": "paragraph",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1.1",
                                "omit": ["1"],
                            }
                        ],
                    }
                ],
            }
        ]
        for format in ("xml", "delimiters"):
            with self.subTest(format=format):
                self.markup(recipe)["format"] = format
                with self.assertRaisesRegex(EpubBlocksError, "selecting the wrapper"):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_inspection_rejects_ambiguous_text_label_on_retained_wrapper(self) -> None:
        epub, recipe = self.labelled('<div class="label"><p>Reading</p></div>')
        with self.assertRaisesRegex(EpubBlocksError, "selecting the wrapper"):
            extract_recipe_candidates(epub, recipe)

    def test_selecting_whole_wrapper_applies_omission_to_label_and_reading(
        self,
    ) -> None:
        epub, recipe = self.labelled(
            '<div class="label"><p>Start <span>OMITTED</span> end</p></div><p>After</p>'
        )
        recipe["source_blocks"] = {
            "element_rules": [{"match": {"tag": "div"}, "action": "block"}]
        }
        self.output(recipe)["replacements"] = [
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
        for format, first in [
            ("xml", '<label label="Start end"/>Start end'),
            ("delimiters", "⟦Start end⟧Start end"),
        ]:
            with self.subTest(format=format):
                self.markup(recipe)["format"] = format
                self.assertEqual(self.readings(epub, recipe), [first, "After"])

    def test_partial_wrapper_slice_cannot_restore_hidden_text_in_label(self) -> None:
        epub, recipe = self.labelled('<div class="label"><p>Hidden Visible</p></div>')
        self.output(recipe)["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#1.1",
                "outputs": [
                    {
                        "type": "paragraph",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1.1",
                                "slice": {"start": 7, "end": 14},
                            }
                        ],
                    }
                ],
            }
        ]
        with self.assertRaisesRegex(EpubBlocksError, "selecting the wrapper"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_filtered_sibling_cannot_return_in_text_label(self) -> None:
        epub, recipe = self.labelled(
            '<div class="label"><p>Hidden</p><p>Visible</p></div><p>After</p>'
        )
        recipe["source_blocks"] = {"exclude_locators": ["text/chapter.xhtml#1.1"]}
        with self.assertRaisesRegex(EpubBlocksError, "selecting the wrapper"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_unused_text_label_is_not_rejected(self) -> None:
        epub, recipe = self.labelled(
            '<div class="label"><p>Hidden</p></div><p>Keep</p>'
        )
        recipe["source_blocks"] = {"include_locators": ["text/chapter.xhtml#2"]}
        self.assertEqual(self.readings(epub, recipe), ["Keep"])
        self.assertEqual(
            [b.text for b in extract_recipe_candidates(epub, recipe)], ["Keep"]
        )

    def test_output_skip_can_discard_a_text_label_wrapper(self) -> None:
        epub, recipe = self.labelled(
            '<div class="label"><p>Hidden</p></div><p>Keep</p>'
        )
        self.output(recipe)["skip_source"] = ["text/chapter.xhtml#1.1"]
        self.assertEqual(self.readings(epub, recipe), ["Keep"])

    def test_replacing_text_label_bundle_still_attaches(self) -> None:
        epub, recipe = self.labelled('<div class="label">005</div><p>Keep</p>')
        rule = self.text_label()
        rule["continue_matching"] = True
        self.markup(recipe).update(
            rules=[
                rule,
                {
                    "match": {"classes_all": ["label"]},
                    "kind": "milestone",
                    "name": "marker",
                },
            ],
            delimiters={},
        )
        self.assertEqual(
            self.readings(epub, recipe), ['<label label="005"/><marker/>Keep']
        )

    def test_selected_text_label_and_nested_image_still_compose(self) -> None:
        epub, recipe = self.labelled(
            '<div class="label"><p>A<img src="plate"/>B</p></div>'
        )
        recipe["source_blocks"] = {
            "element_rules": [{"match": {"tag": "div"}, "action": "block"}]
        }
        self.markup(recipe).update(
            rules=[
                self.text_label(),
                {
                    "match": {"tag": "img"},
                    "kind": "milestone",
                    "name": "image",
                    "label_attribute": "src",
                },
            ],
            delimiters={},
        )
        self.assertEqual(
            self.readings(epub, recipe), ['<label label="AB"/>A<image label="plate"/>B']
        )

    def test_ambiguous_label_failure_preserves_existing_cli_output(self) -> None:
        epub, recipe = self.labelled(
            '<div class="label"><p>Hidden</p></div><p>Keep</p>'
        )
        self.output(recipe)["compiled_sha256"] = "0" * 64
        source, output = epub.parent / "recipe.json", epub.parent / "output.tsv"
        source.write_text(json.dumps(recipe), encoding="utf-8")
        output.write_text("existing\n", encoding="utf-8")
        with (
            patch.object(
                sys, "argv", ["epub-blocks", str(epub), str(source), str(output)]
            ),
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(main(), 2)
        self.assertIn("selecting the wrapper", error.getvalue())
        self.assertEqual(output.read_text(), "existing\n")

    def test_unused_nonnumeric_list_matches_flat_item_filtering(self) -> None:
        for item in ("Not selected", "<p>Not selected</p>"):
            epub, recipe = self.list_sample(
                f'<ol type="a"><li>{item}</li></ol><p>Keep</p>'
            )
            recipe["source_blocks"] = {"include_locators": ["text/chapter.xhtml#2"]}
            self.assertEqual(self.readings(epub, recipe), ["Keep"])
            self.assertEqual(
                [b.text for b in extract_recipe_candidates(epub, recipe)], ["Keep"]
            )

    def test_output_skipped_list_is_not_used_for_label_validation(self) -> None:
        epub, recipe = self.list_sample(
            '<ol type="a"><li><p>Skip</p></li></ol><p>Keep</p>'
        )
        self.output(recipe)["skip_source"] = ["text/chapter.xhtml#1.1.1"]
        self.assertEqual(self.readings(epub, recipe), ["Keep"])

    def test_used_list_still_validates_omitted_siblings(self) -> None:
        epub, recipe = self.list_sample(
            '<ol><li value="bad"><p>Skip</p></li><li><p>Keep</p></li></ol>'
        )
        recipe["source_blocks"] = {"exclude_locators": ["text/chapter.xhtml#1.1.1"]}
        for inspect in (True, False):
            with self.assertRaisesRegex(EpubBlocksError, "invalid ordered-list value"):
                if inspect:
                    extract_recipe_candidates(epub, recipe)
                else:
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_used_wrapper_still_validates_its_missing_attribute(self) -> None:
        epub, recipe = self.sample('<div id="page_1"><p>Keep</p></div>')
        rules = cast(list[dict[str, object]], self.markup(recipe)["rules"])
        rules[0]["label_attribute"] = "missing"
        for inspect in (True, False):
            with self.assertRaisesRegex(EpubBlocksError, "missing or empty"):
                if inspect:
                    extract_recipe_candidates(epub, recipe)
                else:
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_unused_wrapper_does_not_validate_missing_attribute(self) -> None:
        epub, recipe = self.sample('<div id="page_1"><p>Skip</p></div><p>Keep</p>')
        rules = cast(list[dict[str, object]], self.markup(recipe)["rules"])
        rules[0]["label_attribute"] = "missing"
        recipe["source_blocks"] = {"include_locators": ["text/chapter.xhtml#2"]}
        self.assertEqual(self.readings(epub, recipe), ["Keep"])
        self.assertEqual(
            [b.text for b in extract_recipe_candidates(epub, recipe)], ["Keep"]
        )

    def test_excluded_candidate_does_not_validate_duplicate_effects(self) -> None:
        for source in (
            {"exclude_classes": ["excluded"]},
            {"exclude_locators": ["text/chapter.xhtml#1"]},
            {"include_locators": ["text/chapter.xhtml#2"]},
        ):
            epub, recipe = self.sample(
                '<p class="excluded" id="page_5">Omit</p><p>Keep</p>'
            )
            rules = cast(list[dict[str, object]], self.markup(recipe)["rules"])
            self.markup(recipe).update(rules=[rules[0], rules[0]], delimiters={})
            recipe["source_blocks"] = source
            self.assertEqual(self.readings(epub, recipe), ["Keep"])
            self.assertEqual(
                [b.text for b in extract_recipe_candidates(epub, recipe)], ["Keep"]
            )

    def test_unused_wrapper_does_not_validate_duplicate_effects(self) -> None:
        epub, recipe = self.sample('<div id="page_5"><p>Omit</p></div><p>Keep</p>')
        rules = cast(list[dict[str, object]], self.markup(recipe)["rules"])
        self.markup(recipe).update(rules=[rules[0], rules[0]], delimiters={})
        recipe["source_blocks"] = {"include_locators": ["text/chapter.xhtml#2"]}
        self.assertEqual(self.readings(epub, recipe), ["Keep"])
        self.assertEqual(
            [b.text for b in extract_recipe_candidates(epub, recipe)], ["Keep"]
        )
        recipe["source_blocks"] = {}
        for inspect in (True, False):
            with self.assertRaisesRegex(EpubBlocksError, "composed markup repeats"):
                if inspect:
                    extract_recipe_candidates(epub, recipe)
                else:
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_invalid_wrapper_around_retained_image_fails_during_compile(self) -> None:
        for body in (
            '<div id="page_1"><img src="plate"/></div><p>Keep</p>',
            '<p>Keep</p><div id="page_1"><img src="plate"/></div>',
        ):
            epub, recipe = self.sample(body)
            rules = cast(list[dict[str, object]], self.markup(recipe)["rules"])
            rules[0]["label_attribute"] = "missing"
            with self.assertRaisesRegex(EpubBlocksError, "missing or empty"):
                compile_recipe(epub, recipe, verify_digest=False)
            with self.assertRaisesRegex(EpubBlocksError, "missing or empty"):
                extract_recipe_candidates(epub, recipe)

    def test_unclaimed_empty_leading_wrappers_are_ignored(self) -> None:
        epub, recipe = self.sample(
            '<div id="page_1"><div id="page_2"/></div><p>Keep</p>'
        )
        rules = cast(list[dict[str, object]], self.markup(recipe)["rules"])
        rules[0]["label_attribute"] = "missing"
        self.assertEqual(self.readings(epub, recipe), ["Keep"])
        self.assertEqual(
            [b.text for b in extract_recipe_candidates(epub, recipe)], ["Keep"]
        )

    def test_invalid_wrappers_are_reported_in_source_order(self) -> None:
        epub, recipe = self.sample(
            '<div id="page_1"><p>First</p></div><div id="page_2"><p>Second</p></div>'
        )
        rules = cast(list[dict[str, object]], self.markup(recipe)["rules"])
        rules[0]["label_attribute"] = "missing"
        for api in (extract_recipe_candidates, compile_recipe):
            with (
                self.subTest(api=api.__name__),
                self.assertRaisesRegex(
                    EpubBlocksError, r"text/chapter.xhtml#1: milestone label attribute"
                ),
            ):
                api(epub, recipe)

    def test_excluded_replacing_marker_is_not_validated_or_attached(self) -> None:
        epub, recipe = self.sample("<img/><p>Keep</p>")
        recipe["source_blocks"] = {"exclude_locators": ["text/chapter.xhtml#1"]}
        self.assertEqual(self.readings(epub, recipe), ["Keep"])
        self.assertEqual(
            [b.text for b in extract_recipe_candidates(epub, recipe)], ["Keep"]
        )

    def reorder_output(
        self, recipe: dict[str, object], first: str, second: str
    ) -> None:
        self.output(recipe)["replacements"] = [
            {
                "anchor": f"text/chapter.xhtml#{first}",
                "outputs": [
                    {
                        "type": "paragraph",
                        "parts": [
                            {"document": "text/chapter.xhtml", "element_path": path}
                        ],
                    }
                    for path in (second, first)
                ],
            }
        ]

    def test_unused_prefixes_do_not_forbid_reordering_or_change_the_plan(self) -> None:
        for serialization in ("xml", "delimiters"):
            for filtering in ("include", "exclude", "classes", "output_skip"):
                for label_source in ("attribute", "text"):
                    with self.subTest(
                        serialization=serialization,
                        filtering=filtering,
                        label_source=label_source,
                    ):
                        digests: list[str] = []
                        for labelled in (False, True):
                            attr = ' class="label" id="page_1"' if labelled else ""
                            epub, recipe = self.labelled(
                                f'<div{attr}><p class="omit">Unused</p></div>'
                                "<p>First</p><p>Second</p>"
                            )
                            rule = self.text_label()
                            if label_source == "attribute":
                                rule.pop("label_text")
                                # The unused label deliberately has no value.
                                rule["label_attribute"] = "missing"
                            self.markup(recipe).update(
                                rules=[rule], format=serialization
                            )
                            if filtering == "include":
                                recipe["source_blocks"] = {
                                    "include_locators": [
                                        "text/chapter.xhtml#2",
                                        "text/chapter.xhtml#3",
                                    ]
                                }
                            elif filtering == "exclude":
                                recipe["source_blocks"] = {
                                    "exclude_locators": ["text/chapter.xhtml#1.1"]
                                }
                            elif filtering == "classes":
                                recipe["source_blocks"] = {"exclude_classes": ["omit"]}
                            else:
                                self.output(recipe)["skip_source"] = [
                                    "text/chapter.xhtml#1.1"
                                ]
                            self.reorder_output(recipe, "2", "3")
                            compiled = finalize_recipe(epub, recipe)
                            digests.append(compiled_recipe_digest(compiled))
                            self.assertTrue(
                                all(
                                    not b.milestones and not b.milestones_after
                                    for b in compiled.blocks
                                )
                            )
                            self.assertEqual(
                                [b.text for b in extract_recipe(epub, recipe)],
                                ["Second", "First"],
                            )
                        self.assertEqual(digests[0], digests[1])

    def test_empty_nested_prefixes_do_not_forbid_reordering(self) -> None:
        epub, recipe = self.sample(
            '<div id="page_1"><div id="page_2"/></div><p>First</p><p>Second</p>'
        )
        self.reorder_output(recipe, "2", "3")
        self.assertEqual(self.readings(epub, recipe), ["Second", "First"])

    def test_unused_list_labels_do_not_forbid_reordering(self) -> None:
        epub, recipe = self.list_sample(
            '<ol type="a"><li><p>Unused</p></li></ol><p>First</p><p>Second</p>'
        )
        recipe["source_blocks"] = {"exclude_locators": ["text/chapter.xhtml#1.1.1"]}
        self.reorder_output(recipe, "2", "3")
        self.assertEqual(self.readings(epub, recipe), ["Second", "First"])

    def test_retained_prefix_still_forbids_reordering_with_unused_prefix(self) -> None:
        epub, recipe = self.sample(
            '<div id="page_0"><p>Unused</p></div>'
            '<div id="page_1"><p>First</p></div><p>Second</p>'
        )
        recipe["source_blocks"] = {"exclude_locators": ["text/chapter.xhtml#1.1"]}
        self.reorder_output(recipe, "2.1", "3")
        with self.assertRaisesRegex(EpubBlocksError, "source-ordered"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_retained_child_marker_and_its_prefix_still_forbid_reordering(self) -> None:
        epub, recipe = self.sample(
            '<div id="page_1"><img src="plate"/></div><p>First</p><p>Second</p>'
        )
        self.reorder_output(recipe, "2", "3")
        with self.assertRaisesRegex(EpubBlocksError, "source-ordered"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_unused_prefix_cannot_hide_an_unrelated_retained_marker(self) -> None:
        epub, recipe = self.sample(
            '<div id="page_1"><p>Unused</p></div>'
            '<img src="plate"/><p>First</p><p>Second</p>'
        )
        recipe["source_blocks"] = {"exclude_locators": ["text/chapter.xhtml#1.1"]}
        self.reorder_output(recipe, "3", "4")
        with self.assertRaisesRegex(EpubBlocksError, "source-ordered"):
            compile_recipe(epub, recipe, verify_digest=False)
