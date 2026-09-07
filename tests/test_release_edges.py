"""Boundary cases for extraction, recipe diagnostics, and atomic output."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import test_composed_markup as composed
import test_recipe_regressions as recipes
from test_epub_blocks import PACKAGE, finalize_recipe, make_epub

from epub_blocks import (
    EpubBlocksError,
    ExtractedBlock,
    Fragment,
    compile_recipe,
    extract_blocks,
    extract_fragments,
    extract_recipe,
    inspect_epub,
    write_tsv,
)
from epub_blocks.xml import parse_xml


class ReleaseEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def sample(self, body: str) -> tuple[Path, dict[str, object]]:
        helper = recipes.RecipeRegressionTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        return helper.make_recipe(body)

    def test_plain_extraction_excludes_exact_locators(self) -> None:
        epub = make_epub(self.directory)
        original = extract_blocks(epub)
        self.assertEqual(
            extract_blocks(epub, exclude_locators=["text/chapter.xhtml#2"]),
            [block for block in original if block.element_path != "2"],
        )

    def test_empty_expected_identifier_is_rejected_by_block_api(self) -> None:
        epub = make_epub(self.directory)
        with self.assertRaisesRegex(EpubBlocksError, "non-empty string"):
            extract_blocks(epub, expected_identifier="")

    def test_plain_extraction_ignores_whitespace_and_semantically_empty_blocks(
        self,
    ) -> None:
        epub, _ = self.sample(
            '<p> \t\n </p><p xmlns:epub="http://www.idpf.org/2007/ops">'
            '<span epub:type="noteref">1</span></p><p>Keep</p>'
        )
        self.assertEqual(
            [(block.element_path, block.text) for block in extract_blocks(epub)],
            [("3", "Keep")],
        )

    def test_plain_block_api_wraps_corrupt_zip_errors(self) -> None:
        epub = self.directory / "broken.epub"
        epub.write_bytes(b"not a ZIP container")
        with self.assertRaisesRegex(EpubBlocksError, "not a valid ZIP container"):
            extract_blocks(epub)

    def test_svg_only_spine_is_not_treated_as_an_empty_text_book(self) -> None:
        package = PACKAGE.replace(
            '<itemref idref="chapter" properties="page-spread-left"/>', ""
        ).replace('<itemref idref="aux" linear="no"/>', "")
        epub = make_epub(self.directory, package=package)
        for api in (inspect_epub, extract_blocks):
            with (
                self.subTest(api=api.__name__),
                self.assertRaisesRegex(EpubBlocksError, "no XHTML spine documents"),
            ):
                api(epub)

    def test_generic_utf16_requires_bom_in_both_byte_orders(self) -> None:
        for codec in ("utf-16-le", "utf-16-be"):
            for spelling in ("UTF-16", "utf16", "UTF_16"):
                with self.subTest(codec=codec, spelling=spelling):
                    data = (
                        f'<?xml version="1.0" encoding="{spelling}"?><p>é</p>'
                    ).encode(codec)
                    with self.assertRaisesRegex(EpubBlocksError, "byte-order mark"):
                        parse_xml(data, "chapter.xhtml")
                    bom = b"\xff\xfe" if codec.endswith("le") else b"\xfe\xff"
                    self.assertEqual(parse_xml(bom + data, "chapter.xhtml").text, "é")

    def test_omitted_descendants_without_tails_are_removed(self) -> None:
        epub, _ = self.sample(
            '<p xmlns:epub="http://www.idpf.org/2007/ops">Keep'
            '<span epub:type="noteref">Gone</span></p>'
        )
        self.assertEqual(extract_blocks(epub)[0].text, "Keep")
        self.assertEqual(
            extract_fragments(epub, [Fragment("text/chapter.xhtml", "1")]),
            ["Keep"],
        )

    def test_delimiters_require_two_backslash_free_tokens(self) -> None:
        helper = composed.ComposedMarkupTests()
        self.addCleanup(helper.doCleanups)
        epub, recipe = helper.sample('<p class="italic">Keep</p>')
        for tokens in (["⧼"], ["⧼", "⧽", "|"], ["\\⧼", "⧽"], ["⧼", "⧽\\"]):
            with self.subTest(tokens=tokens):
                helper.markup(recipe)["delimiters"] = {"em": tokens}
                with self.assertRaisesRegex(EpubBlocksError, "two distinct.*tokens"):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_prefix_removal_fails_on_actual_nonmatch_empty_match_or_whole_text(
        self,
    ) -> None:
        for pattern, text, message in (
            ("^Label: ", "Plain text", "did not match"),
            ("^(?=Plain)", "Plain text", "did not match"),
            ("^Label: ", "Label: ", "remove all text"),
        ):
            with self.subTest(pattern=pattern, text=text):
                epub, recipe = self.sample(f"<p>{text}</p>")
                recipe["normalization"] = {"strip": False}
                cast(dict[str, object], recipe["output"])["rules"] = [
                    {
                        "match": {"tag": "p"},
                        "type": "p",
                        "remove_prefix": {"pattern": pattern},
                    }
                ]
                with self.assertRaisesRegex(EpubBlocksError, message):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_nonmatching_output_text_rule_falls_back_without_consuming_ids(
        self,
    ) -> None:
        epub, recipe = self.sample("<p>Ordinary prose</p><p>NOTE: text</p>")
        cast(dict[str, object], recipe["output"])["rules"] = [
            {"match": {"text_pattern": "^NOTE:"}, "type": "note"}
        ]
        finalize_recipe(epub, recipe)
        self.assertEqual(
            [(b.block_id, b.block_type) for b in extract_recipe(epub, recipe)],
            [("001", "paragraph"), ("002", "note")],
        )

    def test_insertion_after_a_reserved_nonanchor_cannot_be_silently_lost(self) -> None:
        epub, recipe = self.sample("<p>One</p><p>Two</p>")
        cast(dict[str, object], recipe["output"]).update(
            replacements=[
                {
                    "anchor": "text/chapter.xhtml#1",
                    "outputs": [
                        {
                            "type": "paragraph",
                            "parts": [
                                {"document": "text/chapter.xhtml", "element_path": p}
                                for p in ("1", "2")
                            ],
                        }
                    ],
                }
            ],
            insertions=[
                {
                    "after": "text/chapter.xhtml#2",
                    "outputs": [
                        {
                            "type": "note",
                            "parts": [
                                {"document": "text/aux.xhtml", "element_path": "1"}
                            ],
                        }
                    ],
                }
            ],
        )
        with self.assertRaisesRegex(EpubBlocksError, "insertion anchor.*not emitted"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_tempfile_creation_failure_does_not_replace_existing_tsv(self) -> None:
        output = self.directory / "records.tsv"
        output.write_text("original\n", encoding="utf-8")
        with (
            patch(
                "epub_blocks.recipe.tempfile.NamedTemporaryFile",
                side_effect=PermissionError("denied"),
            ),
            self.assertRaisesRegex(PermissionError, "denied"),
        ):
            write_tsv(output, [ExtractedBlock("001", "p", "New")])
        self.assertEqual(output.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(list(self.directory.iterdir()), [output])
