"""Adversarial regressions for selection, normalization, ownership and I/O."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch
from zipfile import ZipFile

from test_epub_blocks import AUXILIARY, finalize_recipe, make_epub, minimal_recipe

from epub_blocks import (
    EpubBlocksError,
    ExtractedBlock,
    Fragment,
    SafetyLimits,
    compile_recipe,
    extract_blocks,
    extract_fragments,
    extract_recipe,
    extract_recipe_candidates,
    inspect_epub,
    write_tsv,
)
from epub_blocks.safety import EpubArchive


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def document(self, xml: str) -> tuple[Path, dict[str, object]]:
        epub = make_epub(
            self.directory,
            documents={"text/chapter.xhtml": xml, "text/aux.xhtml": AUXILIARY},
        )
        return epub, minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )

    def sample(self, body: str) -> tuple[Path, dict[str, object]]:
        return self.document(
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><body>' + body + "</body></html>"
        )

    def read(self, epub: Path, recipe: dict[str, object]) -> list[ExtractedBlock]:
        finalize_recipe(epub, recipe)
        return extract_recipe(epub, recipe)

    def readers(
        self, epub: Path, recipe: dict[str, object]
    ) -> tuple[Callable[[], object], ...]:
        return (
            lambda: extract_blocks(epub),
            lambda: extract_fragments(epub, [Fragment("text/chapter.xhtml", "1")]),
            lambda: extract_recipe_candidates(epub, recipe),
            lambda: compile_recipe(epub, recipe, verify_digest=False),
            lambda: compile_recipe(epub, recipe),
            lambda: extract_recipe(epub, recipe),
        )

    def test_coverage_and_content_policies_do_not_change_semantic_omissions(
        self,
    ) -> None:
        epub, recipe = self.sample(
            '<p>A<a epub:type="noteref">1</a>B</p>'
            '<p epub:type="noteref">Excluded root</p>'
            '<aside epub:type="other noteref"><div><p>Excluded nested</p></div></aside>'
            "<p>After</p>"
        )
        expected = [
            ExtractedBlock("001", "paragraph", "AB"),
            ExtractedBlock("002", "paragraph", "After"),
        ]
        self.assertEqual(self.read(epub, recipe), expected)
        self.assertEqual([b.element_path for b in extract_blocks(epub)], ["1", "4"])
        policies: tuple[dict[str, object], ...] = (
            {},
            {"markup": {}},
            {"block_boundaries": {"tags": ["td"]}},
        )
        for strict in (False, True):
            for text in policies:
                with self.subTest(strict=strict, text=text):
                    changed = copy.deepcopy(recipe)
                    cast(dict[str, object], changed["source_blocks"])[
                        "strict_coverage"
                    ] = strict
                    changed["text"] = text
                    self.assertEqual(self.read(epub, changed), expected)
                    self.assertEqual(
                        [
                            b.element_path
                            for b in extract_recipe_candidates(epub, changed)
                        ],
                        ["1", "4"],
                    )
        # Explicit fragment extraction still omits descendants, not its root.
        self.assertEqual(
            extract_fragments(epub, [Fragment("text/chapter.xhtml", "2")]),
            ["Excluded root"],
        )
        recipe["omit_epub_types"] = []
        before = self.read(epub, recipe)
        cast(dict[str, object], recipe["source_blocks"])["strict_coverage"] = True
        self.assertEqual(self.read(epub, recipe), before)
        self.assertEqual(len(before), 4)

    def cleaned_recipe(self, recipe: dict[str, object], fmt: str) -> dict[str, object]:
        recipe["normalization"] = {"collapse_whitespace": False}
        markup: dict[str, object] = {"format": fmt, "remove_source_newlines": True}
        recipe["text"] = {"markup": markup}
        return markup

    def test_cleanup_normalizes_before_heading_matching_and_group_generation(
        self,
    ) -> None:
        for fmt in ("xml", "delimiters", "literal"):
            with self.subTest(format=fmt):
                epub, recipe = self.sample("<h1>e\n\u0301 Heading</h1>")
                self.cleaned_recipe(recipe, fmt)
                output = cast(dict[str, object], recipe["output"])
                output["groups"] = {"source_marker": {"pattern": "^(é) "}}
                output["rules"] = [
                    {
                        "match": {"text_pattern": "^é Heading$"},
                        "type": "heading",
                        "role": "fixed",
                        "id": "{group}.title",
                    }
                ]
                self.assertEqual(
                    [b.text for b in extract_recipe_candidates(epub, recipe)],
                    ["é Heading"],
                )
                self.assertEqual(
                    self.read(epub, recipe),
                    [ExtractedBlock("é.title", "heading", "é Heading")],
                )

    def test_cleanup_prefixes_and_slices_use_stable_code_point_offsets(self) -> None:
        for fmt in ("xml", "delimiters", "literal"):
            for sliced in (False, True):
                with self.subTest(format=fmt, sliced=sliced):
                    epub, recipe = self.sample("<p>e\n\u0301 Heading</p>")
                    self.cleaned_recipe(recipe, fmt)
                    output = cast(dict[str, object], recipe["output"])
                    if sliced:
                        output["replacements"] = [
                            {
                                "anchor": "text/chapter.xhtml#1",
                                "outputs": [
                                    {
                                        "type": "heading",
                                        "parts": [
                                            {
                                                "document": "text/chapter.xhtml",
                                                "element_path": "1",
                                                "slice": {"start": 2, "end": 9},
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                    else:
                        output["rules"] = [
                            {
                                "match": {"tag": "p"},
                                "type": "heading",
                                "remove_prefix": {"pattern": "^é "},
                            }
                        ]
                    self.assertEqual(
                        self.read(epub, recipe),
                        [ExtractedBlock("001", "heading", "Heading")],
                    )

    def test_cleanup_rejects_new_cross_markup_composition_before_compilation(
        self,
    ) -> None:
        for fmt in ("xml", "delimiters", "literal"):
            with self.subTest(format=fmt):
                epub, recipe = self.sample("<p><em>e</em>\n\u0301x</p>")
                markup = self.cleaned_recipe(recipe, fmt)
                markup.update(
                    rules=[{"match": {"tag": "em"}, "kind": "span", "name": "em"}],
                    delimiters={"em": ["⟬", "⟭"]},
                )
                for operation in self.readers(epub, recipe)[2:]:
                    with self.assertRaisesRegex(
                        EpubBlocksError, "normalization crosses"
                    ):
                        operation()

    def test_cleanup_does_not_change_collapse_order_or_opaque_labels(self) -> None:
        epub, recipe = self.sample('<p>A\nB<span id="e&#10;&#x301;"/></p>')
        markup = self.cleaned_recipe(recipe, "xml")
        recipe["normalization"] = {"collapse_whitespace": True}
        markup["rules"] = [
            {
                "match": {"tag": "span"},
                "kind": "milestone",
                "name": "page",
                "label_attribute": "id",
            }
        ]
        self.assertEqual(
            self.read(epub, recipe)[0].text, 'A B<page label="e&#10;\u0301"/>'
        )

    def marked_sample(self, body: str) -> tuple[Path, dict[str, object]]:
        epub, recipe = self.sample(body)
        recipe["text"] = {
            "markup": {
                "format": "xml",
                "attachment_order": "source",
                "between_blocks": "next",
                "trailing": "previous",
                "rules": [
                    {
                        "match": {"tag": "span"},
                        "kind": "milestone",
                        "name": "page",
                        "label_attribute": "id",
                    }
                ],
            }
        }
        return epub, recipe

    def test_source_attachment_rejects_ordinary_and_inserted_duplicate_owners(
        self,
    ) -> None:
        epub, recipe = self.marked_sample('<p>First</p><p>Second</p><span id="3"/>')
        cast(dict[str, object], recipe["output"])["insertions"] = [
            {
                "after": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "note",
                        "parts": [
                            {"document": "text/chapter.xhtml", "element_path": "2"},
                        ],
                    }
                ],
            }
        ]
        markup = cast(
            dict[str, object], cast(dict[str, object], recipe["text"])["markup"]
        )
        for order in ("source", "output"):
            with self.subTest(order=order):
                markup["attachment_order"] = order
                with self.assertRaisesRegex(
                    EpubBlocksError, "milestone attachment.*reused"
                ):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_source_reuse_without_detached_markers_remains_allowed(self) -> None:
        epub, recipe = self.marked_sample("<p>Anchor</p><p>Reused</p>")
        cast(dict[str, object], recipe["output"])["insertions"] = [
            {
                "after": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "note",
                        "parts": [
                            {"document": "text/chapter.xhtml", "element_path": "2"}
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(
            [b.text for b in self.read(epub, recipe)], ["Anchor", "Reused", "Reused"]
        )

    def test_source_attachment_checks_inserted_slices_and_accepts_adjacent_ones(
        self,
    ) -> None:
        for second_start in (2, 3):
            with self.subTest(second_start=second_start):
                epub, recipe = self.marked_sample(
                    '<p>Anchor</p><span id="2"/><p>ABCD</p>'
                )
                output = cast(dict[str, object], recipe["output"])
                output["skip_source"] = ["text/chapter.xhtml#3"]
                output["insertions"] = [
                    {
                        "after": "text/chapter.xhtml#1",
                        "outputs": [
                            {
                                "type": "note",
                                "parts": [
                                    {
                                        "document": "text/chapter.xhtml",
                                        "element_path": "3",
                                        "slice": {"start": start, "end": end},
                                    }
                                ],
                            }
                            for start, end in ((second_start, 4), (0, 3))
                        ],
                    }
                ]
                if second_start == 2:
                    with self.assertRaisesRegex(EpubBlocksError, "overlapping slices"):
                        compile_recipe(epub, recipe, verify_digest=False)
                else:
                    self.assertEqual(
                        [b.text for b in self.read(epub, recipe)],
                        ["Anchor", "D", '<page label="2"/>ABC'],
                    )

    def test_auxiliary_reuse_is_not_an_in_scope_marker_owner(self) -> None:
        epub, recipe = self.marked_sample('<p>Anchor</p><span id="2"/>')
        cast(dict[str, object], recipe["output"])["insertions"] = [
            {
                "after": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "note",
                        "parts": [
                            {"document": "text/aux.xhtml", "element_path": "1"},
                        ],
                    }
                    for _ in range(2)
                ],
            }
        ]
        self.assertEqual(
            [b.text for b in self.read(epub, recipe)],
            ['Anchor<page label="2"/>', "Auxiliary material.", "Auxiliary material."],
        )

    def test_malformed_xhtml_body_structure_is_rejected_by_all_text_readers(
        self,
    ) -> None:
        namespace = 'xmlns="http://www.w3.org/1999/xhtml"'
        documents = (
            f"<html {namespace}><body><p>One</p></body><body><p>Lost</p></body></html>",
            f"<html {namespace}><body><p>One</p><body><p>Nested</p></body></body></html>",
            f"<html {namespace}><div><body><p>Nested</p></body></div></html>",
        )
        for xml in documents:
            epub, recipe = self.document(xml)
            cast(dict[str, object], recipe["source_blocks"])["strict_coverage"] = True
            for index, operation in enumerate(self.readers(epub, recipe)):
                with (
                    self.subTest(xml=xml, reader=index),
                    self.assertRaisesRegex(EpubBlocksError, "exactly one XHTML body"),
                ):
                    operation()

    def test_xhtml_root_must_be_html_in_the_xhtml_namespace(self) -> None:
        for root in ("section", "html xmlns=''", "html xmlns='urn:other'"):
            tag = root.split()[0]
            epub, _ = self.document(
                f'<{root}><body xmlns="http://www.w3.org/1999/xhtml"><p>Text</p></body></{tag}>'
            )
            with self.assertRaisesRegex(EpubBlocksError, "unexpected XHTML root"):
                extract_blocks(epub)
        epub, recipe = self.document(
            '<h:html xmlns:h="http://www.w3.org/1999/xhtml"><h:body>'
            "<h:p>Text</h:p></h:body></h:html>"
        )
        self.assertEqual(self.read(epub, recipe)[0].text, "Text")

    def corrupt_filename(self, *, central: bool) -> tuple[Path, dict[str, object]]:
        epub, _ = self.sample("<p>Text</p>")
        data = bytearray(epub.read_bytes())
        with ZipFile(epub) as archive:
            local_offset = archive.getinfo("text/chapter.xhtml").header_offset
        offsets = [(local_offset, 6, 30)]
        if central:
            offsets.append((data.index(b"PK\x01\x02"), 8, 46))
        for offset, flags_offset, name_offset in offsets:
            flags = struct.unpack_from("<H", data, offset + flags_offset)[0]
            struct.pack_into("<H", data, offset + flags_offset, flags | 0x800)
            data[offset + name_offset] = 0xFF
        epub.write_bytes(data)
        return epub, minimal_recipe(epub_sha256=hashlib.sha256(data).hexdigest())

    def test_corrupt_zip_names_have_context_and_cause_across_public_apis(self) -> None:
        for central in (False, True):
            epub, recipe = self.corrupt_filename(central=central)
            operations = self.readers(epub, recipe)
            if central:
                operations += (lambda epub=epub: inspect_epub(epub),)
            for index, operation in enumerate(operations):
                with self.subTest(central=central, reader=index):
                    with self.assertRaises(EpubBlocksError) as caught:
                        operation()
                    self.assertIsInstance(
                        caught.exception.__cause__, UnicodeDecodeError
                    )
                    self.assertIn(
                        "ZIP container" if central else "text/chapter.xhtml",
                        str(caught.exception),
                    )

    def test_invalid_local_filename_does_not_charge_read_budget(self) -> None:
        epub, _ = self.corrupt_filename(central=False)
        with ZipFile(epub) as archive:
            reader = EpubArchive(
                archive,
                SafetyLimits(
                    max_total_read_bytes=archive.getinfo(
                        "text/chapter.xhtml"
                    ).file_size,
                ),
            )
            for _ in range(2):
                with self.assertRaises(EpubBlocksError):
                    reader.read("text/chapter.xhtml")
            self.assertEqual(reader.read("mimetype"), b"application/epub+zip")

    def test_bad_zip_name_cli_preserves_output_and_has_no_traceback(self) -> None:
        for central in (False, True):
            epub, recipe = self.corrupt_filename(central=central)
            source, output = (
                self.directory / "recipe.json",
                self.directory / "output.tsv",
            )
            source.write_text(json.dumps(recipe), encoding="utf-8")
            output.write_text("Original", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "epub_blocks.cli",
                    str(epub),
                    str(source),
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("epub-blocks: error:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "Original")

    def test_epub_hash_is_checked_before_a_corrupt_directory_is_decoded(self) -> None:
        epub, recipe = self.corrupt_filename(central=True)
        recipe["epub"] = {"identifier": "sample-edition", "sha256": "0" * 64}
        with self.assertRaisesRegex(EpubBlocksError, "source SHA-256"):
            compile_recipe(epub, recipe, verify_digest=False)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_tsv_replacement_preserves_modes_and_new_files_remain_private(self) -> None:
        target = self.directory / "output.tsv"
        write_tsv(target, [ExtractedBlock("1", "p", "Old")])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        for mode in (0o644, 0o640, 0o600, 0o444):
            with self.subTest(mode=oct(mode)):
                target.chmod(mode)
                write_tsv(target, [ExtractedBlock("1", "p", "New")])
                self.assertEqual(target.stat().st_mode & 0o777, mode)
                self.assertEqual(target.read_text(encoding="utf-8"), "1\tp\tNew\n")

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_permission_copy_failure_leaves_existing_tsv_untouched(self) -> None:
        target = self.directory / "output.tsv"
        target.write_text("Original", encoding="utf-8")
        target.chmod(0o640)
        with (
            patch.object(Path, "chmod", side_effect=PermissionError("denied")),
            self.assertRaises(PermissionError),
        ):
            write_tsv(target, [ExtractedBlock("1", "p", "New")])
        self.assertEqual(target.read_text(encoding="utf-8"), "Original")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.directory.glob(".output.tsv.*.tmp")), [])
