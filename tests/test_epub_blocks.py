from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unicodedata
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from epub_blocks import (
    EpubBlocksError,
    ExtractedBlock,
    NormalizationOptions,
    SafetyLimits,
    __version__,
    extract_blocks,
    extract_recipe,
    extract_recipe_file,
    inspect_epub,
    load_recipe,
    write_tsv,
)
from epub_blocks.cli import main
from epub_blocks.models import Fragment
from epub_blocks.package import matches, select_spine_documents
from epub_blocks.safety import EpubArchive
from epub_blocks.xhtml import (
    child_at,
    extract_fragment,
    normalize_text,
    read_document_body,
    remove_at,
    remove_descendants_by_epub_type,
)
from epub_blocks.xml import parse_xml

CONTAINER = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="content.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

PACKAGE = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf"
         xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0"
         unique-identifier="pub-id">
  <metadata>
    <dc:identifier id="pub-id">sample-edition</dc:identifier>
    <dc:title>Sample Book</dc:title>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2026-09-04T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="chapter" href="text/chapter.xhtml"
          media-type="application/xhtml+xml"/>
    <item id="aux" href="text/aux.xhtml"
          media-type="application/xhtml+xml"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"
          properties="nav"/>
    <item id="cover" href="images/cover.svg" media-type="image/svg+xml"/>
  </manifest>
  <spine>
    <itemref idref="chapter" properties="page-spread-left"/>
    <itemref idref="aux" linear="no"/>
    <itemref idref="cover"/>
  </spine>
  <guide><identifier>forged-identifier</identifier></guide>
</package>"""

CHAPTER = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops"><body>
  <h1>Chapter One</h1>
  <p>One <span epub:type="pagebreak">9</span> two
     <a epub:type="noteref">1</a> three.</p>
  <blockquote><p>Nested quotation.</p></blockquote>
  <li>Standalone item.</li>
  <p class="image-caption">A caption</p>
  <div><p>Alpha <span>discard</span> omega</p></div>
  <p>A<span epub:type="remove">X</span>B<span epub:type="remove">Y</span>C</p>
  <p>e\u0301  spaced</p>
</body></html>"""

AUXILIARY = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
  <p>Auxiliary material.</p>
</body></html>"""

NAVIGATION = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops"><body>
  <nav epub:type="toc"><ol><li><a href="text/chapter.xhtml">Chapter One</a></li></ol></nav>
</body></html>"""


def make_epub(
    directory: Path,
    *,
    filename: str = "sample.epub",
    container: str = CONTAINER,
    package: str = PACKAGE,
    documents: dict[str, str | bytes] | None = None,
    extra_members: list[tuple[str, str | bytes]] | None = None,
) -> Path:
    epub_path = directory / filename
    document_values = documents or {
        "text/chapter.xhtml": CHAPTER,
        "text/aux.xhtml": AUXILIARY,
        "nav.xhtml": NAVIGATION,
        "images/cover.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
    }
    with ZipFile(epub_path, "w") as epub:
        epub.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        epub.writestr(
            "META-INF/container.xml", container, compress_type=ZIP_DEFLATED
        )
        epub.writestr("content.opf", package, compress_type=ZIP_DEFLATED)
        for name, value in document_values.items():
            epub.writestr(name, value, compress_type=ZIP_DEFLATED)
        for name, value in extra_members or []:
            epub.writestr(name, value, compress_type=ZIP_DEFLATED)
    return epub_path


def minimal_recipe(**overrides: object) -> dict[str, object]:
    recipe: dict[str, object] = {
        "recipe_version": "1",
        "blocks": [
            {
                "id": "01.001",
                "type": "{p}",
                "parts": [
                    {
                        "document": "text/chapter.xhtml",
                        "element_path": "2",
                    }
                ],
            }
        ],
    }
    recipe.update(overrides)
    return recipe


class ExtractionTests(unittest.TestCase):
    def test_package_and_block_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            epub_path = make_epub(Path(temporary_directory))
            package = inspect_epub(epub_path)
            self.assertEqual(package.identifiers, frozenset({"sample-edition"}))
            self.assertEqual(package.spine[0].path, "text/chapter.xhtml")
            self.assertEqual(
                package.spine[0].properties,
                frozenset({"page-spread-left"}),
            )
            self.assertFalse(package.spine[1].linear)

            blocks = extract_blocks(
                str(epub_path),
                expected_identifier="sample-edition",
                exclude_classes=("IMAGE-CAPTION",),
            )
            self.assertEqual(
                [block.tag for block in blocks],
                ["h1", "p", "p", "li", "p", "p", "p"],
            )
            self.assertEqual(blocks[1].text, "One two three.")
            self.assertEqual(blocks[2].text, "Nested quotation.")
            self.assertEqual(blocks[0].source_locator, "s001:text/chapter.xhtml#1")
            self.assertEqual(blocks[4].locator, "text/chapter.xhtml#6.1")

    def test_non_linear_spine_items_are_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            epub_path = make_epub(Path(temporary_directory))
            blocks = extract_blocks(epub_path, include_non_linear=True)
            self.assertEqual(blocks[-1].text, "Auxiliary material.")
            package = inspect_epub(epub_path)
            selected = select_spine_documents(
                package, include=("*AUX*",), include_non_linear=True
            )
            self.assertEqual([item.position for item in selected], [2])
            with self.assertRaisesRegex(EpubBlocksError, "matched"):
                select_spine_documents(package, include=("*missing*",))

    def test_locator_selection_normalization_and_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            epub_path = make_epub(Path(temporary_directory))
            blocks = extract_blocks(
                epub_path,
                include_documents=("TEXT/*.XHTML",),
                include_locators=("*#6.*",),
                exclude_locators=("*#9",),
            )
            self.assertEqual([block.text for block in blocks], ["Alpha discard omega"])
            self.assertTrue(matches("A/B.xhtml", ("a/*.XHTML",)))
            self.assertFalse(matches("A/B.xhtml", ("b/*",)))
            self.assertEqual(
                normalize_text(
                    "  e\u0301\tword  ",
                    NormalizationOptions(unicode_normalization="NFD"),
                ),
                unicodedata.normalize("NFD", "é word"),
            )

    def test_expected_identifier_and_empty_selection_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            epub_path = make_epub(Path(temporary_directory))
            with self.assertRaisesRegex(EpubBlocksError, "expected package identifier"):
                extract_blocks(epub_path, expected_identifier="forged-identifier")
            with self.assertRaisesRegex(EpubBlocksError, "non-empty string"):
                extract_blocks(epub_path, expected_identifier="")
            with self.assertRaisesRegex(EpubBlocksError, "no text blocks"):
                extract_blocks(epub_path, exclude_locators=("*",))

    def test_fragment_operations_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            epub_path = make_epub(Path(temporary_directory))
            with ZipFile(epub_path) as zip_file:
                epub = EpubArchive(zip_file)
                text = extract_fragment(
                    epub,
                    Fragment(
                        "text/chapter.xhtml",
                        "6.1",
                        omit_paths=("1",),
                        start=0,
                        end=11,
                    ),
                )
                self.assertEqual(text, "Alpha omega")
                self.assertEqual(
                    extract_fragment(
                        epub,
                        Fragment("text/chapter.xhtml", "7"),
                        omit_epub_types=frozenset({"remove"}),
                    ),
                    "ABC",
                )
                body = read_document_body(epub, "text/chapter.xhtml")
                self.assertEqual(child_at(body, "1").text, "Chapter One")
                with self.assertRaisesRegex(EpubBlocksError, "outside"):
                    child_at(body, "99")
                with self.assertRaisesRegex(EpubBlocksError, "invalid element path"):
                    child_at(body, "+1")
                with self.assertRaisesRegex(EpubBlocksError, "duplicate or overlap"):
                    extract_fragment(
                        epub,
                        Fragment(
                            "text/chapter.xhtml", "6.1", omit_paths=("1", "1")
                        ),
                    )
                with self.assertRaisesRegex(EpubBlocksError, "outside"):
                    extract_fragment(
                        epub,
                        Fragment("text/chapter.xhtml", "1", start=0, end=99),
                    )

    def test_low_level_removal_preserves_tail(self) -> None:
        root = parse_xml(
            b'<p xmlns:epub="http://www.idpf.org/2007/ops">A<span>X</span>B</p>',
            "inline",
        )
        remove_at(root, "1")
        self.assertEqual("".join(root.itertext()), "AB")
        with self.assertRaisesRegex(EpubBlocksError, "selected root"):
            remove_at(root, "")
        with self.assertRaisesRegex(EpubBlocksError, "invalid"):
            remove_at(root, "2")

        nested = parse_xml(
            b'<p xmlns:epub="http://www.idpf.org/2007/ops">A<span><b epub:type="x">X</b>Y</span>Z</p>',
            "nested",
        )
        remove_descendants_by_epub_type(nested, frozenset({"x"}))
        self.assertEqual("".join(nested.itertext()), "AYZ")


class PackageValidationTests(unittest.TestCase):
    def test_bad_zip_and_archive_escape_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            bad = directory / "bad.epub"
            bad.write_text("not zip", encoding="utf-8")
            with self.assertRaisesRegex(EpubBlocksError, "not a valid ZIP"):
                inspect_epub(bad)

            unsafe_package = PACKAGE.replace(
                'href="text/chapter.xhtml"', 'href="../../outside.xhtml"'
            )
            with self.assertRaisesRegex(EpubBlocksError, "escapes"):
                inspect_epub(make_epub(directory, package=unsafe_package))

    def test_container_validation(self) -> None:
        variants = {
            "unexpected root": CONTAINER.replace("container", "collection"),
            "required media type": CONTAINER.replace(
                "application/oebps-package+xml", "text/xml"
            ),
            "no full-path": CONTAINER.replace(' full-path="content.opf"', ""),
            "multiple package rootfiles": CONTAINER.replace(
                "</rootfiles>",
                '<rootfile full-path="other.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles>',
            ),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for index, (message, container) in enumerate(variants.items()):
                with self.subTest(message=message):
                    epub_path = make_epub(
                        directory, filename=f"container-{index}.epub", container=container
                    )
                    with self.assertRaisesRegex(EpubBlocksError, message):
                        inspect_epub(epub_path)

    def test_package_structure_validation(self) -> None:
        variants = {
            "unexpected root": PACKAGE.replace(
                '<package xmlns="http://www.idpf.org/2007/opf"',
                '<publication xmlns="http://www.idpf.org/2007/opf"',
            ).replace("</package>", "</publication>"),
            "exactly one metadata": PACKAGE.replace(
                "  <manifest>", "  <metadata></metadata>\n  <manifest>"
            ),
            "duplicate id": PACKAGE.replace('id="aux"', 'id="chapter"'),
            "has no id": PACKAGE.replace('id="aux" ', ""),
            "has no href": PACKAGE.replace('href="text/aux.xhtml"', ""),
            "has no media-type": PACKAGE.replace(
                'id="aux" href="text/aux.xhtml"\n'
                '          media-type="application/xhtml+xml"',
                'id="aux" href="text/aux.xhtml"',
            ),
            "missing manifest item": PACKAGE.replace('idref="aux"', 'idref="absent"'),
            "invalid linear": PACKAGE.replace('linear="no"', 'linear="maybe"'),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for index, (message, package) in enumerate(variants.items()):
                with self.subTest(message=message):
                    epub_path = make_epub(
                        directory, filename=f"package-{index}.epub", package=package
                    )
                    with self.assertRaisesRegex(EpubBlocksError, message):
                        inspect_epub(epub_path)

    def test_missing_members_and_xhtml_body_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            missing_container = directory / "missing-container.epub"
            with ZipFile(missing_container, "w") as epub:
                epub.writestr("mimetype", "application/epub+zip")
            with self.assertRaisesRegex(EpubBlocksError, "container.xml.*not found"):
                inspect_epub(missing_container)

            missing_package = make_epub(
                directory,
                filename="missing-package.epub",
                container=CONTAINER.replace("content.opf", "missing.opf"),
            )
            with self.assertRaisesRegex(EpubBlocksError, "missing.opf.*not found"):
                inspect_epub(missing_package)

            no_body = make_epub(
                directory,
                filename="no-body.epub",
                documents={
                    "text/chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"/>',
                    "text/aux.xhtml": AUXILIARY,
                    "nav.xhtml": NAVIGATION,
                    "images/cover.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
                },
            )
            with self.assertRaisesRegex(EpubBlocksError, "no XHTML body"):
                extract_blocks(no_body)

    def test_archive_and_xml_resource_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            epub_path = make_epub(Path(temporary_directory))
            cases = (
                (SafetyLimits(max_archive_members=1), "too many archive members"),
                (SafetyLimits(max_member_bytes=10), "too large"),
                (SafetyLimits(max_total_read_bytes=10), "total uncompressed"),
                (SafetyLimits(max_compression_ratio=1), "compression-ratio"),
                (SafetyLimits(max_xml_bytes=10), "XML document is too large"),
            )
            for limits, message in cases:
                with (
                    self.subTest(message=message),
                    self.assertRaisesRegex(EpubBlocksError, message),
                ):
                    inspect_epub(epub_path, limits=limits)

            with self.assertRaisesRegex(ValueError, "positive integer"):
                SafetyLimits(max_xml_depth=0)
            with self.assertRaisesRegex(ValueError, "positive integer"):
                SafetyLimits(max_xml_depth=1.5)  # type: ignore[arg-type]

    def test_xml_declarations_complexity_and_malformed_input(self) -> None:
        self.assertEqual(
            parse_xml(b"<!DOCTYPE html><html/>", "html").tag,
            "html",
        )
        with self.assertRaisesRegex(EpubBlocksError, "DTD"):
            parse_xml(b"<!DOCTYPE p [<!ENTITY x 'x'>]><p>&x;</p>", "dtd")
        with self.assertRaisesRegex(EpubBlocksError, "malformed XML"):
            parse_xml(b"<p>", "bad")
        with self.assertRaisesRegex(EpubBlocksError, "too many elements"):
            parse_xml(b"<p><a/><b/></p>", "wide", SafetyLimits(max_xml_elements=2))
        with self.assertRaisesRegex(EpubBlocksError, "deeply nested"):
            parse_xml(b"<p><a><b/></a></p>", "deep", SafetyLimits(max_xml_depth=2))

    def test_duplicate_and_unsafe_archive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            duplicate = directory / "duplicate.epub"
            with ZipFile(duplicate, "w") as archive:
                archive.writestr("same", "one")
                with self.assertWarns(UserWarning):
                    archive.writestr("same", "two")
            with (
                ZipFile(duplicate) as archive,
                self.assertRaisesRegex(EpubBlocksError, "duplicate archive path"),
            ):
                EpubArchive(archive)

            unsafe = directory / "unsafe.epub"
            with ZipFile(unsafe, "w") as archive:
                archive.writestr("../escape", "bad")
            with (
                ZipFile(unsafe) as archive,
                self.assertRaisesRegex(EpubBlocksError, "unsafe archive path"),
            ):
                EpubArchive(archive)


class RecipeTests(unittest.TestCase):
    def test_recipe_extracts_joins_slices_and_writes_tsv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            recipe = {
                "recipe_version": "1",
                "epub": {
                    "identifier": "sample-edition",
                    "sha256": hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                },
                "normalization": {
                    "collapse_whitespace": True,
                    "strip": True,
                    "unicode_normalization": "NFC",
                },
                "omit_epub_types": ["noteref", "pagebreak"],
                "blocks": [
                    {
                        "id": "01.001",
                        "type": "{h}",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1",
                            }
                        ],
                    },
                    {
                        "id": "01.002",
                        "type": "{p}",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "2",
                            },
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "6.1",
                                "omit": ["1"],
                                "slice": {"start": 0, "end": 11},
                            },
                        ],
                        "separator": " ",
                    },
                ],
            }
            blocks = extract_recipe(epub_path, recipe)
            self.assertEqual(
                [(block.block_id, block.block_type, block.text) for block in blocks],
                [
                    ("01.001", "{h}", "Chapter One"),
                    ("01.002", "{p}", "One two three. Alpha omega"),
                ],
            )
            recipe_path = directory / "recipe.json"
            recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
            self.assertEqual(extract_recipe_file(epub_path, recipe_path), blocks)

            output_path = directory / "output" / "records.tsv"
            write_tsv(output_path, iter(blocks))
            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "01.001\t{h}\tChapter One\n"
                "01.002\t{p}\tOne two three. Alpha omega\n",
            )

    def test_unknown_members_are_rejected_at_each_level(self) -> None:
        recipes: list[tuple[dict[str, object], str]] = [
            (minimal_recipe(seperator=" "), "recipe.*seperator"),
            (
                minimal_recipe(epub={"identifier": "x", "extra": True}),
                "recipe.epub.*extra",
            ),
            (
                minimal_recipe(normalization={"trim": True}),
                "recipe.normalization.*trim",
            ),
        ]
        block = cast(list[object], minimal_recipe()["blocks"])
        block_value = block[0]
        assert isinstance(block_value, dict)
        block_value["extra"] = True
        recipes.append((minimal_recipe(blocks=block), r"blocks\[1\].*extra"))

        part_recipe = minimal_recipe()
        parts = cast_dict_list(cast_dict_list(part_recipe["blocks"])[0]["parts"])
        parts[0]["extra"] = True
        recipes.append((part_recipe, r"parts\[1\].*extra"))

        slice_recipe = minimal_recipe()
        slice_parts = cast_dict_list(
            cast_dict_list(slice_recipe["blocks"])[0]["parts"]
        )
        slice_parts[0]["slice"] = {"start": 0, "end": 1, "extra": True}
        recipes.append((slice_recipe, "slice.*extra"))

        for recipe, message in recipes:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(EpubBlocksError, message),
            ):
                extract_recipe("unused.epub", recipe)

    def test_duplicate_json_members_and_json_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            duplicate = directory / "duplicate.json"
            duplicate.write_text(
                '{"recipe_version":"1","blocks":[],"blocks":[]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EpubBlocksError, "duplicate JSON.*blocks"):
                load_recipe(duplicate)

            nested = directory / "nested.json"
            nested.write_text(
                '{"recipe_version":"1","blocks":[{"id":"a","id":"b"}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EpubBlocksError, r"blocks\[1\].*duplicate"):
                load_recipe(nested)

            invalid = directory / "invalid.json"
            invalid.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(EpubBlocksError, r":1:2: invalid JSON"):
                load_recipe(invalid)

            array = directory / "array.json"
            array.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(EpubBlocksError, "must be an object"):
                load_recipe(array)

            binary = directory / "binary.json"
            binary.write_bytes(b"\xff")
            with self.assertRaisesRegex(EpubBlocksError, "not UTF-8"):
                load_recipe(binary)

    def test_identity_hash_and_version_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            epub_path = make_epub(Path(temporary_directory))
            cases: list[tuple[dict[str, object], str]] = [
                (minimal_recipe(recipe_version=1), "unsupported version"),
                (minimal_recipe(epub={"identifier": ""}), "non-empty string"),
                (minimal_recipe(epub={"sha256": ""}), "64 lowercase"),
                (minimal_recipe(epub={"sha256": "A" * 64}), "64 lowercase"),
                (minimal_recipe(epub={"sha256": "0" * 64}), "does not match"),
                (
                    minimal_recipe(epub={"identifier": "other-edition"}),
                    "expected package identifier",
                ),
            ]
            for recipe, message in cases:
                with (
                    self.subTest(message=message),
                    self.assertRaisesRegex(EpubBlocksError, message),
                ):
                    extract_recipe(epub_path, recipe)

    def test_field_type_and_value_validation(self) -> None:
        invalid: list[tuple[dict[str, object], str]] = [
            (minimal_recipe(epub=[]), "epub: must be an object"),
            (
                minimal_recipe(normalization={"collapse_whitespace": "yes"}),
                "collapse_whitespace.*boolean",
            ),
            (
                minimal_recipe(normalization={"strip": 1}),
                "strip.*boolean",
            ),
            (
                minimal_recipe(normalization={"unicode_normalization": "UTF-8"}),
                "unknown value",
            ),
            (minimal_recipe(omit_epub_types=[""]), "non-empty strings"),
            (minimal_recipe(blocks=[]), "non-empty array"),
            (minimal_recipe(blocks=["block"]), "must be an object"),
        ]
        for recipe, message in invalid:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(EpubBlocksError, message),
            ):
                extract_recipe("unused.epub", recipe)

    def test_block_part_omit_and_slice_validation(self) -> None:
        invalid_blocks: list[tuple[object, str]] = [
            ({"id": "", "type": "p", "parts": [{}]}, "id.*non-empty"),
            ({"id": "a", "type": "", "parts": [{}]}, "type.*non-empty"),
            ({"id": "a", "type": "p", "parts": []}, "parts.*non-empty"),
            (
                {"id": "a", "type": "p", "parts": [{}]},
                "document.*non-empty",
            ),
            (
                {
                    "id": "a",
                    "type": "p",
                    "parts": [{"document": "x", "element_path": "+1"}],
                },
                "positive integers",
            ),
            (
                {
                    "id": "a",
                    "type": "p",
                    "parts": [{"document": "x", "omit": ["1", "1.2"]}],
                },
                "duplicates or overlaps",
            ),
            (
                {
                    "id": "a",
                    "type": "p",
                    "parts": [{"document": "x", "slice": {"start": 0}}],
                },
                "start and end must be integers",
            ),
            (
                {
                    "id": "a",
                    "type": "p",
                    "parts": [
                        {"document": "x", "slice": {"start": True, "end": 2}}
                    ],
                },
                "start and end must be integers",
            ),
            (
                {
                    "id": "a",
                    "type": "p",
                    "parts": [{"document": "x", "slice": {"start": 2, "end": 1}}],
                },
                "0 <= start < end",
            ),
            (
                {"id": "a", "type": "p", "separator": 1, "parts": [{}]},
                "document.*non-empty",
            ),
        ]
        for block, message in invalid_blocks:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(EpubBlocksError, message),
            ):
                extract_recipe("unused.epub", minimal_recipe(blocks=[block]))

        duplicate_ids = minimal_recipe(
            blocks=[
                {
                    "id": "a",
                    "type": "p",
                    "parts": [{"document": "x"}],
                },
                {
                    "id": "a",
                    "type": "p",
                    "parts": [{"document": "x"}],
                },
            ]
        )
        with self.assertRaisesRegex(EpubBlocksError, "duplicate block id"):
            extract_recipe("unused.epub", duplicate_ids)

    def test_huge_element_path_component_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            epub_path = make_epub(Path(temporary_directory))
            recipe = minimal_recipe()
            blocks = cast_dict_list(recipe["blocks"])
            parts = cast_dict_list(blocks[0]["parts"])
            parts[0]["omit"] = ["1" + "0" * 5000]
            with self.assertRaisesRegex(EpubBlocksError, "invalid element path"):
                extract_recipe(epub_path, recipe)

    def test_source_and_empty_extraction_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            malformed = make_epub(
                directory,
                filename="malformed.epub",
                documents={
                    "text/chapter.xhtml": "<html>",
                    "text/aux.xhtml": AUXILIARY,
                    "nav.xhtml": NAVIGATION,
                    "images/cover.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
                },
            )
            with self.assertRaisesRegex(EpubBlocksError, "malformed XML"):
                extract_recipe(malformed, minimal_recipe())

            epub_path = make_epub(
                directory,
                filename="valid.epub",
                documents={
                    "text/chapter.xhtml": (
                        '<html xmlns="http://www.w3.org/1999/xhtml">'
                        "<body><p/></body></html>"
                    ),
                    "text/aux.xhtml": AUXILIARY,
                    "nav.xhtml": NAVIGATION,
                    "images/cover.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
                },
            )
            empty_recipe = minimal_recipe(
                blocks=[
                    {
                        "id": "a",
                        "type": "p",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1",
                            }
                        ],
                    }
                ]
            )
            with self.assertRaisesRegex(EpubBlocksError, "produced no text"):
                extract_recipe(epub_path, empty_recipe)

    def test_atomic_tsv_preserves_existing_output_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "records.tsv"
            output.write_text("original\n", encoding="utf-8")

            def failing_blocks() -> Iterator[ExtractedBlock]:
                yield ExtractedBlock("one", "p", "text")
                raise RuntimeError("stop")

            with self.assertRaisesRegex(RuntimeError, "stop"):
                write_tsv(output, failing_blocks())
            self.assertEqual(output.read_text(encoding="utf-8"), "original\n")
            self.assertEqual(list(output.parent.glob(".records.tsv.*.tmp")), [])


def cast_dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise TypeError("test fixture is not a list of dictionaries")
    values = cast(list[object], value)
    if not all(isinstance(item, dict) for item in values):
        raise TypeError("test fixture is not a list of dictionaries")
    return cast(list[dict[str, object]], values)


class CliTests(unittest.TestCase):
    def test_cli_success_error_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            recipe_path = directory / "recipe.json"
            recipe_path.write_text(json.dumps(minimal_recipe()), encoding="utf-8")
            output_path = directory / "records.tsv"

            stdout = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    ["epub-blocks", str(epub_path), str(recipe_path), str(output_path)],
                ),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(main(), 0)
            self.assertIn("1 blocks", stdout.getvalue())

            stderr = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    ["epub-blocks", "missing.epub", str(recipe_path), str(output_path)],
                ),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(main(), 2)
            self.assertIn("epub-blocks: error:", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

            version_output = io.StringIO()
            with (
                patch.object(sys, "argv", ["epub-blocks", "--version"]),
                contextlib.redirect_stdout(version_output),
                self.assertRaises(SystemExit) as raised,
            ):
                main()
            self.assertEqual(raised.exception.code, 0)
            self.assertIn(__version__, version_output.getvalue())


if __name__ == "__main__":
    unittest.main()
