from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

from test_epub_blocks import AUXILIARY, PACKAGE, make_epub, minimal_recipe

from epub_blocks import (
    CompiledBlock,
    CompiledRecipe,
    ContentOptions,
    EpubBlocksError,
    Fragment,
    NormalizationOptions,
    compile_recipe,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_candidates,
)
from epub_blocks.models import UnicodeNormalization


class CompiledValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fragment = Fragment("text/chapter.xhtml", "1")
        self.block = CompiledBlock(
            "001",
            "paragraph",
            (self.fragment,),
            consumed_locators=("text/chapter.xhtml#1",),
        )
        self.plan = CompiledRecipe(
            "edition", "0" * 64, NormalizationOptions(), frozenset(), (self.block,)
        )

    def test_normalization_requires_booleans_and_a_supported_form(self) -> None:
        for normalization, message in (
            (
                NormalizationOptions(collapse_whitespace=cast(bool, 1)),
                "collapse_whitespace",
            ),
            (NormalizationOptions(strip=cast(bool, "yes")), "strip"),
            (
                NormalizationOptions(
                    unicode_normalization=cast(UnicodeNormalization, None)
                ),
                "Unicode",
            ),
            (
                NormalizationOptions(
                    unicode_normalization=cast(UnicodeNormalization, "UTF-8")
                ),
                "Unicode",
            ),
        ):
            with (
                self.subTest(normalization=normalization),
                self.assertRaisesRegex(EpubBlocksError, message),
            ):
                compiled_recipe_digest(replace(self.plan, normalization=normalization))

    def test_omitted_types_require_a_frozen_set_of_nonempty_strings(self) -> None:
        for value in ({"noteref"}, frozenset({""}), frozenset({1})):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(EpubBlocksError, "omit_epub_types"),
            ):
                compiled_recipe_digest(
                    replace(self.plan, omit_epub_types=cast(frozenset[str], value))
                )

    def test_block_fields_reject_wrong_types_and_empty_names(self) -> None:
        cases = (
            (replace(self.block, block_id=""), r"\.id:"),
            (replace(self.block, block_id=cast(str, 7)), r"\.id:"),
            (replace(self.block, block_type=""), r"\.type:"),
            (replace(self.block, block_type=cast(str, None)), r"\.type:"),
            (replace(self.block, separator=cast(str, False)), r"\.separator:"),
            (replace(self.block, allow_empty=cast(bool, 1)), r"\.allow_empty:"),
        )
        for block, message in cases:
            with (
                self.subTest(block=block),
                self.assertRaisesRegex(EpubBlocksError, message),
            ):
                compiled_recipe_digest(replace(self.plan, blocks=(block,)))

    def test_distinct_fragments_cannot_share_an_output_identifier(self) -> None:
        other = CompiledBlock(
            "001", "paragraph", (Fragment("text/chapter.xhtml", "2"),)
        )
        with self.assertRaisesRegex(EpubBlocksError, r"\.id: duplicate"):
            compiled_recipe_digest(replace(self.plan, blocks=(self.block, other)))

    def test_locator_collections_reject_mutable_and_duplicate_values(self) -> None:
        locator = "text/chapter.xhtml#2"
        for value, message in (([locator], "tuple"), ((locator, locator), "unique")):
            for field in ("skipped_locators", "reserved_locators"):
                with (
                    self.subTest(value=value, field=field),
                    self.assertRaisesRegex(EpubBlocksError, message),
                ):
                    compiled_recipe_digest(replace(self.plan, **{field: value}))
            with self.assertRaisesRegex(EpubBlocksError, message):
                block = replace(
                    self.block, consumed_locators=cast(tuple[str, ...], value)
                )
                compiled_recipe_digest(replace(self.plan, blocks=(block,)))

    def test_consumed_sources_cannot_be_skipped_or_reserved(self) -> None:
        for field in ("skipped_locators", "reserved_locators"):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(EpubBlocksError, "overlaps skipped or reserved"),
            ):
                compiled_recipe_digest(
                    replace(self.plan, **{field: self.block.consumed_locators})
                )

    def test_fragment_paths_and_slices_reject_malformed_public_values(self) -> None:
        for fragment, message in (
            (replace(self.fragment, element_path=cast(str, 1)), "element_path"),
            (
                replace(self.fragment, omit_paths=cast(tuple[str, ...], ["1"])),
                r"\.omit:",
            ),
            (replace(self.fragment, start=True, end=2), "integers"),
            (replace(self.fragment, start=0, end=cast(int, 1.5)), "integers"),
            (replace(self.fragment, start=-1, end=2), "0 <= start < end"),
        ):
            with (
                self.subTest(fragment=fragment),
                self.assertRaisesRegex(EpubBlocksError, message),
            ):
                compiled_recipe_digest(
                    replace(self.plan, blocks=(replace(self.block, parts=(fragment,)),))
                )

    def test_compiled_content_requires_the_public_options_type(self) -> None:
        with self.assertRaisesRegex(EpubBlocksError, "must be ContentOptions"):
            compiled_recipe_digest(replace(self.plan, content=cast(ContentOptions, {})))


class SourceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def sample(
        self, body: str, *, package: str = PACKAGE
    ) -> tuple[Path, dict[str, object]]:
        epub = make_epub(
            self.directory,
            package=package,
            documents={
                "text/chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                + body
                + "</body></html>",
                "text/aux.xhtml": AUXILIARY,
            },
        )
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )
        return epub, recipe

    def test_invalid_group_captures_fail_with_specific_diagnostics(self) -> None:
        cases: tuple[tuple[str, dict[str, object], str], ...] = (
            (
                "CHAPTER",
                {"source_marker": {"pattern": "^CHAPTER()$"}},
                "must not be empty",
            ),
            (
                "CHAPTER ABC",
                {
                    "source_marker": {"pattern": "^CHAPTER (.+)$"},
                    "capture_kind": "decimal",
                },
                "not decimal",
            ),
            (
                "CHAPTER IIII",
                {
                    "source_marker": {"pattern": "^CHAPTER (.+)$"},
                    "capture_kind": "roman",
                },
                "noncanonical Roman",
            ),
            (
                "CHAPTER ABC",
                {"source_marker": {"pattern": "^CHAPTER (.+)$"}, "source_offset": 1},
                "requires a numeric capture",
            ),
            (
                "CHAPTER 1",
                {"source_marker": {"pattern": "^CHAPTER (.+)$"}, "source_offset": -1},
                "invalid group 0",
            ),
            (
                "CHAPTER ABC",
                {"source_marker": {"pattern": "^CHAPTER (.+)$"}, "capture_width": 2},
                "requires a numeric group",
            ),
            (
                "CHAPTER",
                {"source_marker": {"pattern": "^CHAPTER(?: (.+))?$"}},
                "capture did not participate",
            ),
            (
                "CHAPTER",
                {"source_pattern": "chapter(?:-(.+))?\\.xhtml#"},
                "capture did not participate",
            ),
        )
        for heading, groups, message in cases:
            with self.subTest(groups=groups, heading=heading):
                epub, recipe = self.sample(f"<h1>{heading}</h1><p>Text.</p>")
                cast(dict[str, object], recipe["output"])["groups"] = groups
                with self.assertRaisesRegex(EpubBlocksError, message):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_subtractive_roman_and_zero_padded_decimal_captures_generate_groups(
        self,
    ) -> None:
        for numeral, kind, expected in (
            ("iv", "roman", "05.001"),
            ("009", "decimal", "10.001"),
        ):
            with self.subTest(numeral=numeral):
                epub, recipe = self.sample(f"<h1>Chapter {numeral}</h1>")
                output = cast(dict[str, object], recipe["output"])
                output["groups"] = {
                    "source_marker": {
                        "pattern": "^chapter (.+)$",
                        "case_insensitive": True,
                    },
                    "capture_kind": kind,
                    "capture_width": 2,
                    "source_offset": 1,
                }
                output["identifiers"] = {"block": {"template": "{group}.{number:03d}"}}
                plan = compile_recipe(epub, recipe, verify_digest=False)
                output["compiled_sha256"] = compiled_recipe_digest(plan)
                self.assertEqual(extract_recipe(epub, recipe)[0].block_id, expected)

    def test_repeated_spine_documents_make_source_locators_ambiguous(self) -> None:
        # Multiple spine references can identify the same physical XHTML node.
        package = PACKAGE.replace(
            '<itemref idref="chapter" properties="page-spread-left"/>',
            '<itemref idref="chapter"/><itemref idref="chapter"/>',
        )
        epub, base = self.sample("<p>Text.</p>", package=package)
        cast(dict[str, object], base["source_blocks"])["include_documents"] = [
            "text/*.xhtml"
        ]
        cast(dict[str, object], base["source_blocks"])["include_non_linear"] = True
        chapter = "text/chapter.xhtml#1"
        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"groups": {"transitions": {chapter: "a"}}}, "transitions.*ambiguous"),
            ({"skip_source": [chapter]}, "exactly one selected"),
            (
                {
                    "insertions": [
                        {
                            "after": chapter,
                            "outputs": [
                                {
                                    "type": "note",
                                    "parts": [
                                        {
                                            "document": "text/aux.xhtml",
                                            "element_path": "1",
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
                "anchor.*ambiguous",
            ),
            (
                {
                    "replacements": [
                        {
                            "anchor": "text/aux.xhtml#1",
                            "outputs": [
                                {
                                    "type": "p",
                                    "parts": [
                                        {
                                            "document": "text/aux.xhtml",
                                            "element_path": "1",
                                        },
                                        {
                                            "document": "text/chapter.xhtml",
                                            "element_path": "1",
                                        },
                                    ],
                                }
                            ],
                        }
                    ]
                },
                "more than one selected",
            ),
        )
        for fields, message in cases:
            with self.subTest(fields=fields):
                recipe = copy.deepcopy(base)
                cast(dict[str, object], recipe["output"]).update(fields)
                with self.assertRaisesRegex(EpubBlocksError, message):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_missing_transition_and_skipped_insertion_anchor_are_rejected(self) -> None:
        epub, base = self.sample("<p>First.</p><p>Second.</p>")
        for fields, message in (
            (
                {"groups": {"transitions": {"text/chapter.xhtml#9": "a"}}},
                "not selected source",
            ),
            (
                {
                    "skip_source": ["text/chapter.xhtml#1"],
                    "insertions": [
                        {
                            "after": "text/chapter.xhtml#1",
                            "outputs": [
                                {
                                    "type": "note",
                                    "parts": [
                                        {
                                            "document": "text/aux.xhtml",
                                            "element_path": "1",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
                "also skipped",
            ),
        ):
            with self.subTest(message=message):
                recipe = copy.deepcopy(base)
                cast(dict[str, object], recipe["output"]).update(fields)
                with self.assertRaisesRegex(EpubBlocksError, message):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_fixed_identifier_must_not_render_as_an_empty_group(self) -> None:
        epub, recipe = self.sample("<p>Text.</p>")
        cast(dict[str, object], recipe["output"])["default"] = {
            "type": "p",
            "role": "fixed",
            "id": "{group}",
        }
        with self.assertRaisesRegex(
            EpubBlocksError, "identifier template rendered empty"
        ):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_recipe_apis_reject_a_corrupt_archive_even_with_a_matching_pin(
        self,
    ) -> None:
        epub = self.directory / "corrupt.epub"
        epub.write_bytes(b"not a ZIP file")
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )
        for api in (compile_recipe, extract_recipe, extract_recipe_candidates):
            with (
                self.subTest(api=api.__name__),
                self.assertRaisesRegex(EpubBlocksError, "not a valid ZIP"),
            ):
                api(epub, recipe)

    def test_extract_recipe_rejects_a_changed_archive_before_parsing_it(self) -> None:
        epub, recipe = self.sample("<p>Text.</p>")
        epub.write_bytes(b"changed archive")
        with self.assertRaisesRegex(EpubBlocksError, "source SHA-256"):
            extract_recipe(epub, recipe)

    def test_malformed_output_policies_fail_before_opening_the_epub(self) -> None:
        part = {"document": "text/chapter.xhtml", "element_path": "1"}
        replacement = {
            "anchor": "text/chapter.xhtml#1",
            "outputs": [{"type": "p", "parts": [part]}],
        }
        insertion = {
            "after": "text/chapter.xhtml#1",
            "outputs": [{"type": "p", "parts": [part]}],
        }
        cases: tuple[tuple[dict[str, object], str], ...] = (
            (
                {
                    "replacements": [
                        {
                            "anchor": "text/chapter.xhtml#1",
                            "outputs": [
                                {"type": "p", "parts": [{**part, "element_path": 1}]}
                            ],
                        }
                    ]
                },
                r"element_path: must be a string",
            ),
            (
                {
                    "rules": [
                        {
                            "match": {"tag": "p"},
                            "type": "p",
                            "remove_prefix": {
                                "pattern": "^Label: ",
                                "case_insensitive": 1,
                            },
                        }
                    ]
                },
                "case_insensitive: must be a boolean",
            ),
            (
                {
                    "groups": {
                        "source_pattern": "(chapter)",
                        "source_map": {"chapter": "1"},
                        "capture_width": 2,
                    }
                },
                "capture_width cannot be combined with source_map",
            ),
            (
                {"replacements": [{"anchor": "text/chapter.xhtml#1", "outputs": []}]},
                "outputs: must be a non-empty array",
            ),
            ({"replacements": [replacement, replacement]}, "duplicate anchor"),
            ({"insertions": {}}, "insertions: must be an array"),
            ({"insertions": [insertion, insertion]}, "duplicate anchor"),
        )
        for fields, message in cases:
            with self.subTest(message=message):
                recipe = minimal_recipe()
                cast(dict[str, object], recipe["output"]).update(fields)
                with self.assertRaisesRegex(EpubBlocksError, message):
                    compile_recipe(self.directory / "does-not-exist.epub", recipe)

    def test_python_recipe_mappings_require_string_keys_at_every_level(self) -> None:
        for key, message in (
            ("metadata", "object member names"),
            ("text", "object keys"),
        ):
            with self.subTest(key=key):
                recipe = minimal_recipe()
                recipe[key] = {1: "invalid"}
                with self.assertRaisesRegex(EpubBlocksError, message):
                    compile_recipe(self.directory / "does-not-exist.epub", recipe)


if __name__ == "__main__":
    unittest.main()
