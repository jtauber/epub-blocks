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
from importlib.resources import files
from pathlib import Path
from typing import cast
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from jsonschema import (  # pyright: ignore[reportMissingModuleSource]
    Draft202012Validator,
)

from epub_blocks import (
    CompiledBlock,
    CompiledRecipe,
    EpubBlocksError,
    ExtractedBlock,
    Fragment,
    NormalizationOptions,
    SafetyLimits,
    __version__,
    compile_recipe,
    compile_recipe_file,
    compiled_recipe_digest,
    extract_blocks,
    extract_fragments,
    extract_recipe,
    extract_recipe_file,
    inspect_epub,
    load_recipe,
    write_tsv,
)
from epub_blocks.cli import main
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
        epub.writestr("META-INF/container.xml", container, compress_type=ZIP_DEFLATED)
        epub.writestr("content.opf", package, compress_type=ZIP_DEFLATED)
        for name, value in document_values.items():
            epub.writestr(name, value, compress_type=ZIP_DEFLATED)
        for name, value in extra_members or []:
            epub.writestr(name, value, compress_type=ZIP_DEFLATED)
    return epub_path


def minimal_recipe(
    *,
    epub_sha256: str = "0" * 64,
    **overrides: object,
) -> dict[str, object]:
    recipe: dict[str, object] = {
        "recipe_version": "1",
        "epub": {"identifier": "sample-edition", "sha256": epub_sha256},
        "normalization": {
            "collapse_whitespace": True,
            "strip": True,
            "unicode_normalization": "NFC",
        },
        "omit_epub_types": ["noteref", "pagebreak"],
        "source_blocks": {
            "include_documents": ["text/chapter.xhtml"],
            "exclude_documents": [],
            "exclude_classes": [],
            "include_locators": [],
            "exclude_locators": [],
            "include_non_linear": False,
        },
        "output": {
            "groups": {},
            "identifiers": {
                "block": {"template": "{number:03d}", "start": 1},
                "line": {"template": "{block:03d}.{number:02d}", "start": 1},
            },
            "default": {"type": "paragraph", "role": "block"},
            "rules": [],
            "skip_source": [],
            "replacements": [],
            "insertions": [],
            "compiled_sha256": "0" * 64,
        },
    }
    recipe.update(overrides)
    return recipe


def finalize_recipe(epub_path: Path, recipe: dict[str, object]) -> CompiledRecipe:
    compiled = compile_recipe(epub_path, recipe, verify_digest=False)
    output = cast(dict[str, object], recipe["output"])
    output["compiled_sha256"] = compiled_recipe_digest(compiled)
    return compiled


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

    def test_nested_structure_and_explicit_line_breaks(self) -> None:
        chapter = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><main><section>
  <h1><span>Part</span><br/>Alpha</h1>
  <section>
    <h2>Chapter One</h2>
    <blockquote>
      <p class="verse">First line.</p>
      <p class="verse">Second line.</p>
    </blockquote>
    <blockquote><p class="quotation">Quoted paragraph.</p></blockquote>
    <p class="trailer">Trailer.</p>
  </section>
</section></main></body></html>"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(
                directory,
                documents={
                    "text/chapter.xhtml": chapter,
                    "text/aux.xhtml": AUXILIARY,
                    "nav.xhtml": NAVIGATION,
                    "images/cover.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
                },
            )

            blocks = extract_blocks(epub_path)
            self.assertEqual(
                [(block.tag, block.text) for block in blocks],
                [
                    ("h1", "Part Alpha"),
                    ("h2", "Chapter One"),
                    ("p", "First line."),
                    ("p", "Second line."),
                    ("p", "Quoted paragraph."),
                    ("p", "Trailer."),
                ],
            )
            self.assertEqual(
                [block.locator for block in blocks[2:4]],
                [
                    "text/chapter.xhtml#1.1.2.2.1",
                    "text/chapter.xhtml#1.1.2.2.2",
                ],
            )

            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
            )
            recipe["normalization"] = {
                "collapse_whitespace": False,
                "strip": True,
                "unicode_normalization": "NFC",
            }
            cast(dict[str, object], recipe["output"])["rules"] = [
                {"match": {"tag": "h1"}, "type": "heading"},
                {"match": {"tag": "h2"}, "type": "heading"},
                {
                    "match": {"locators": ["*#1.1.2.2.1"]},
                    "type": "verse-line",
                    "role": "line-start",
                },
                {
                    "match": {"classes": ["verse"]},
                    "type": "verse-line",
                    "role": "line",
                },
                {
                    "match": {"classes": ["quotation"]},
                    "type": "quotation",
                },
                {"match": {"classes": ["trailer"]}, "type": "trailer"},
            ]
            finalize_recipe(epub_path, recipe)
            extracted = extract_recipe(epub_path, recipe)
            self.assertEqual(extracted[0].text, "Part\nAlpha")
            self.assertEqual(
                [(block.block_id, block.block_type) for block in extracted[1:]],
                [
                    ("002", "heading"),
                    ("003.01", "verse-line"),
                    ("003.02", "verse-line"),
                    ("004", "quotation"),
                    ("005", "trailer"),
                ],
            )

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
                        Fragment("text/chapter.xhtml", "6.1", omit_paths=("1", "1")),
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
                        directory,
                        filename=f"container-{index}.epub",
                        container=container,
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

            with ZipFile(epub_path) as archive:
                bounded = EpubArchive(archive)
                with (
                    patch.object(
                        archive,
                        "read",
                        side_effect=NotImplementedError("unsupported compression"),
                    ),
                    self.assertRaisesRegex(EpubBlocksError, "could not be read"),
                ):
                    bounded.read("mimetype")

    def test_xml_declarations_complexity_and_malformed_input(self) -> None:
        self.assertEqual(
            parse_xml(b"<!DOCTYPE html><html/>", "html").tag,
            "html",
        )
        external = parse_xml(
            b'<?xml version="1.0"?>\n'
            b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            b'"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            b"<html><body><p>A&nbsp;&mdash;&nbsp;B &amp; C</p></body></html>",
            "external-xhtml",
        )
        self.assertEqual("".join(external.itertext()), "A\xa0—\xa0B & C")
        self.assertEqual(
            parse_xml(
                b"<!DOCTYPE html SYSTEM 'about:legacy-compat'><html/>",
                "system-xhtml",
            ).tag,
            "html",
        )
        cdata = parse_xml(
            b"<p><![CDATA[<!DOCTYPE html>&nbsp;]]>"
            b"<span title='&copy;'>&copy;</span></p>",
            "character-references",
        )
        self.assertEqual("".join(cdata.itertext()), "<!DOCTYPE html>&nbsp;©")
        self.assertEqual(cdata[0].get("title"), "©")
        with self.assertRaisesRegex(EpubBlocksError, "DTD"):
            parse_xml(b"<!DOCTYPE p [<!ENTITY x 'x'>]><p>&x;</p>", "dtd")
        with self.assertRaisesRegex(EpubBlocksError, "DTD"):
            parse_xml(
                b'<!DOCTYPE chapter SYSTEM "local.dtd"><chapter/>',
                "non-xhtml-dtd",
            )
        with self.assertRaisesRegex(EpubBlocksError, "DTD"):
            parse_xml(b"<!doctype HTML><html/>", "case-invalid-doctype")
        with self.assertRaisesRegex(EpubBlocksError, "unknown named"):
            parse_xml(b"<p>&publisherSpecific;</p>", "unknown-entity")
        expanding_reference = b"<p>&acE;</p>"
        with self.assertRaisesRegex(EpubBlocksError, "expanded XML.*too large"):
            parse_xml(
                expanding_reference,
                "expanded-reference",
                SafetyLimits(max_xml_bytes=len(expanding_reference)),
            )
        with self.assertRaisesRegex(EpubBlocksError, "multiple XHTML"):
            parse_xml(
                b"<!DOCTYPE html><!DOCTYPE html><html/>",
                "multiple-doctypes",
            )
        with self.assertRaisesRegex(EpubBlocksError, "document prolog"):
            parse_xml(b"<html/><!DOCTYPE html>", "late-doctype")
        with self.assertRaisesRegex(EpubBlocksError, "malformed XML"):
            parse_xml(b"<p>", "bad")
        with self.assertRaisesRegex(EpubBlocksError, "too many elements"):
            parse_xml(b"<p><a/><b/></p>", "wide", SafetyLimits(max_xml_elements=2))
        with self.assertRaisesRegex(EpubBlocksError, "deeply nested"):
            parse_xml(b"<p><a><b/></a></p>", "deep", SafetyLimits(max_xml_depth=2))

    def test_utf16_cannot_bypass_xml_declaration_policy(self) -> None:
        safe_documents = (
            (
                (
                    '<?xml version="1.0" encoding="UTF-16"?>'
                    '<!DOCTYPE html SYSTEM "about:legacy-compat">'
                    "<html><p>A&nbsp;&mdash;&nbsp;B</p></html>"
                ),
                "utf-16",
            ),
            (
                (
                    '<?xml version="1.0" encoding="UTF-16LE"?>'
                    '<!DOCTYPE html SYSTEM "about:legacy-compat">'
                    "<html><p>A&nbsp;&mdash;&nbsp;B</p></html>"
                ),
                "utf-16-le",
            ),
            (
                (
                    '<?xml version="1.0" encoding="UTF-16BE"?>'
                    '<!DOCTYPE html SYSTEM "about:legacy-compat">'
                    "<html><p>A&nbsp;&mdash;&nbsp;B</p></html>"
                ),
                "utf-16-be",
            ),
        )
        for source, encoding in safe_documents:
            with self.subTest(encoding=encoding):
                root = parse_xml(source.encode(encoding), encoding)
                self.assertEqual("".join(root.itertext()), "A\xa0—\xa0B")

        big_endian_with_bom = b"\xfe\xff" + safe_documents[0][0].encode("utf-16-be")
        root = parse_xml(big_endian_with_bom, "utf-16-big-endian-bom")
        self.assertEqual("".join(root.itertext()), "A\xa0—\xa0B")

        dangerous = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE p [<!ENTITY x "EXPANDED">]><p>&x;</p>'
        )
        dangerous_documents = (
            (dangerous, "utf-16"),
            (dangerous.replace("UTF-16", "UTF-16LE"), "utf-16-le"),
            (dangerous.replace("UTF-16", "UTF-16BE"), "utf-16-be"),
        )
        for source, encoding in dangerous_documents:
            with (
                self.subTest(encoding=encoding),
                self.assertRaisesRegex(EpubBlocksError, "DTD"),
            ):
                parse_xml(source.encode(encoding), encoding)

        dangerous_without_declaration = (
            ' \n<!DOCTYPE p [<!ENTITY x "EXPANDED">]><p>&x;</p>'
        )
        for encoding in ("utf-16-le", "utf-16-be"):
            with (
                self.subTest(encoding=f"{encoding}-leading-whitespace"),
                self.assertRaisesRegex(EpubBlocksError, "DTD"),
            ):
                parse_xml(dangerous_without_declaration.encode(encoding), encoding)

        conflicting = (
            '<?xml version="1.0" encoding="UTF-16BE"?><p>content</p>'
        ).encode("utf-16-le")
        with self.assertRaisesRegex(EpubBlocksError, "conflicts"):
            parse_xml(conflicting, "conflicting-encoding")
        with self.assertRaisesRegex(EpubBlocksError, "UTF-32"):
            parse_xml("<p>content</p>".encode("utf-32"), "utf-32")
        for encoding in ("utf-32-le", "utf-32-be"):
            with (
                self.subTest(encoding=f"{encoding}-leading-whitespace"),
                self.assertRaisesRegex(EpubBlocksError, "NUL"),
            ):
                parse_xml((" \n<p>content</p>").encode(encoding), encoding)
        with self.assertRaisesRegex(EpubBlocksError, "malformed UTF-16"):
            parse_xml(b"\xff\xfe<", "truncated-utf-16")
        with self.assertRaisesRegex(EpubBlocksError, "malformed XML"):
            parse_xml(
                b'<?xml version="1.0" encoding="UTF-7"?><p>content</p>',
                "unsupported-multibyte-encoding",
            )

    def test_declaration_scanning_is_linear_across_opaque_sections(self) -> None:
        comment = b"<!--<!DOCTYPE html><!ENTITY x 'x'>&copy;-->"
        root = parse_xml(b"<root>" + comment * 10_000 + b"</root>", "comments")
        self.assertEqual(root.tag, "root")
        for opener in (b"<!--", b"<![CDATA[", b"<?"):
            with (
                self.subTest(opener=opener),
                self.assertRaisesRegex(EpubBlocksError, "malformed XML"),
            ):
                parse_xml(b"<root>" + opener * 10_000 + b"</root>", "unterminated")

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


class GeneratedReferenceRecipeTests(unittest.TestCase):
    def test_packaged_v1_schema_accepts_recipe_and_rejects_unknown_fields(
        self,
    ) -> None:
        schema_path = files("epub_blocks").joinpath("schemas", "recipe-v1.schema.json")
        schema = cast(
            dict[str, object], json.loads(schema_path.read_text(encoding="utf-8"))
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        self.assertTrue(
            validator.is_valid(minimal_recipe())  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        )
        self.assertFalse(
            validator.is_valid(minimal_recipe(unknown=True))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        )

    def test_public_compiled_models_and_digest(self) -> None:
        block = CompiledBlock(
            "one",
            "paragraph",
            (Fragment("text/chapter.xhtml", "1"),),
            "",
            ("text/chapter.xhtml#1",),
        )
        compiled = CompiledRecipe(
            "edition",
            "0" * 64,
            NormalizationOptions(),
            frozenset(),
            (block,),
        )
        digest = compiled_recipe_digest(compiled)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(digest, compiled_recipe_digest(compiled))

        variants = (
            CompiledRecipe(
                "edition",
                "0" * 64,
                NormalizationOptions(strip=False),
                frozenset(),
                (block,),
            ),
            CompiledRecipe(
                "edition",
                "0" * 64,
                NormalizationOptions(),
                frozenset({"noteref"}),
                (block,),
            ),
            CompiledRecipe(
                "edition",
                "0" * 64,
                NormalizationOptions(),
                frozenset(),
                (
                    CompiledBlock(
                        "one",
                        "paragraph",
                        (Fragment("text/chapter.xhtml", "1"),),
                        "",
                        ("text/chapter.xhtml#1", "text/chapter.xhtml#2"),
                    ),
                ),
            ),
            CompiledRecipe(
                "edition",
                "0" * 64,
                NormalizationOptions(),
                frozenset(),
                (block,),
                ("text/chapter.xhtml#2",),
            ),
            CompiledRecipe(
                "edition",
                "0" * 64,
                NormalizationOptions(),
                frozenset(),
                (block,),
                (),
                ("text/chapter.xhtml#2",),
            ),
        )
        for variant in variants:
            self.assertNotEqual(digest, compiled_recipe_digest(variant))

        forward_omissions = CompiledRecipe(
            "edition",
            "0" * 64,
            NormalizationOptions(),
            frozenset(),
            (
                CompiledBlock(
                    "one",
                    "paragraph",
                    (Fragment("text/chapter.xhtml", "1", ("1", "2")),),
                ),
            ),
        )
        reverse_omissions = CompiledRecipe(
            "edition",
            "0" * 64,
            NormalizationOptions(),
            frozenset(),
            (
                CompiledBlock(
                    "one",
                    "paragraph",
                    (Fragment("text/chapter.xhtml", "1", ("2", "1")),),
                ),
            ),
        )
        self.assertEqual(
            compiled_recipe_digest(forward_omissions),
            compiled_recipe_digest(reverse_omissions),
        )

        invalid_fragments = (
            Fragment("", "1"),
            Fragment("text/chapter.xhtml", "+1"),
            Fragment("text/chapter.xhtml", "1", ("",)),
            Fragment("text/chapter.xhtml", "1", ("1", "1.2")),
            Fragment("text/chapter.xhtml", "1", start=0),
            Fragment("text/chapter.xhtml", "1", start=1, end=1),
        )
        for fragment in invalid_fragments:
            invalid = CompiledRecipe(
                "edition",
                "0" * 64,
                NormalizationOptions(),
                frozenset(),
                (CompiledBlock("one", "paragraph", (fragment,)),),
            )
            with self.assertRaises(EpubBlocksError):
                compiled_recipe_digest(invalid)

        invalid_compiled = (
            CompiledRecipe(
                "edition", "0" * 64, NormalizationOptions(), frozenset(), ()
            ),
            CompiledRecipe(
                "edition",
                "0" * 64,
                NormalizationOptions(),
                frozenset(),
                (CompiledBlock("one", "p", ()),),
            ),
            CompiledRecipe(
                "edition",
                "0" * 64,
                NormalizationOptions(),
                frozenset(),
                (block,),
                ("bad",),
            ),
            CompiledRecipe(
                "edition",
                "0" * 64,
                NormalizationOptions(),
                frozenset(),
                (block,),
                ("text/chapter.xhtml#2",),
                ("text/chapter.xhtml#2",),
            ),
            CompiledRecipe(
                "edition",
                "0" * 64,
                NormalizationOptions(),
                frozenset(),
                (
                    block,
                    CompiledBlock(
                        "two",
                        "p",
                        (Fragment("text/chapter.xhtml", "2"),),
                        "",
                        ("text/chapter.xhtml#1",),
                    ),
                ),
            ),
        )
        for invalid in invalid_compiled:
            with self.assertRaises(EpubBlocksError):
                compiled_recipe_digest(invalid)

    def test_duplicate_json_members_and_json_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            duplicate = directory / "duplicate.json"
            duplicate.write_text(
                '{"recipe_version":"1","epub":{},"epub":{}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EpubBlocksError, "duplicate JSON.*epub"):
                load_recipe(duplicate)

            nested = directory / "nested.json"
            nested.write_text(
                '{"recipe_version":"1","mapping":{"groups":{},"groups":{}}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EpubBlocksError, r"mapping.*duplicate"):
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

            for constant in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(constant=constant):
                    invalid.write_text(
                        '{"recipe_version":"1","metadata":{"value":' + constant + "}}",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        EpubBlocksError, "invalid JSON constant"
                    ):
                        load_recipe(invalid)

    def test_extract_fragments_uses_one_pinned_epub(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            digest = hashlib.sha256(epub_path.read_bytes()).hexdigest()
            texts = extract_fragments(
                epub_path,
                [
                    Fragment("text/chapter.xhtml", "6.1", omit_paths=("1",)),
                    Fragment("text/chapter.xhtml", "1", start=8, end=11),
                ],
                expected_identifier="sample-edition",
                expected_sha256=digest,
                omit_epub_types=frozenset({"noteref", "pagebreak"}),
            )
            self.assertEqual(texts, ["Alpha omega", "One"])
            with self.assertRaisesRegex(EpubBlocksError, "expected_sha256"):
                extract_fragments(epub_path, [], expected_sha256="bad")
            with self.assertRaisesRegex(EpubBlocksError, "source SHA-256"):
                extract_fragments(epub_path, [], expected_sha256="0" * 64)
            with self.assertRaisesRegex(EpubBlocksError, "identifier"):
                extract_fragments(epub_path, [], expected_identifier="")

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

    def test_tsv_writes_quotes_and_backslashes_literally(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "records.tsv"
            text = '<page label="117"/>“A question?” "An answer." \\ literal'
            blocks = [
                ExtractedBlock('id"1', 'p"', text),
                ExtractedBlock("empty", "gap", ""),
                ExtractedBlock("quoted", "paragraph", '"Quoted from the start."'),
            ]
            write_tsv(output, blocks)
            expected = (
                'id"1\tp"\t' + text + "\nempty\tgap\t\n"
                'quoted\tparagraph\t"Quoted from the start."\n'
            )
            self.assertEqual(output.read_bytes(), expected.encode("utf-8"))

    def test_tsv_rejects_field_separators_without_replacing_existing_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "records.tsv"
            output.write_text("original\n", encoding="utf-8")
            for field_index, name in enumerate(("id", "type", "text")):
                for char in ("\t", "\r", "\n"):
                    fields = ["one", "paragraph", "text"]
                    fields[field_index] += char
                    with self.subTest(field=name, char=char):
                        with self.assertRaisesRegex(
                            EpubBlocksError, f"block 2, {name}"
                        ):
                            write_tsv(
                                output,
                                [
                                    ExtractedBlock("valid", "p", "text"),
                                    ExtractedBlock(*fields),
                                ],
                            )
                        self.assertEqual(output.read_bytes(), b"original\n")
                        self.assertEqual(
                            list(output.parent.glob(".records.tsv.*.tmp")), []
                        )

    def test_recipe_generates_groups_identifiers_types_and_lines(self) -> None:
        chapter = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
  <h1>CHAPTER I</h1><p class="title">A Beginning</p>
  <p>Ordinary prose.</p>
  <p class="verse first">First line.</p><p class="verse">Second line.</p>
</body></html>"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(
                directory,
                documents={
                    "text/chapter.xhtml": chapter,
                    "text/aux.xhtml": AUXILIARY,
                    "nav.xhtml": NAVIGATION,
                    "images/cover.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
                },
            )

            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest()
            )
            output = cast(dict[str, object], recipe["output"])
            output["groups"] = {
                "source_marker": {"pattern": r"^CHAPTER ([IVX]+)$"},
                "capture_kind": "roman",
                "capture_width": 2,
            }
            output["identifiers"] = {
                "block": {"template": "{group}.{number:03d}", "start": 1},
                "line": {
                    "template": "{group}.{block:03d}.{number:02d}",
                    "start": 1,
                },
            }
            output["rules"] = [
                {
                    "match": {"tag": "h1"},
                    "type": "{h}",
                    "role": "fixed",
                    "id": "{group}.000",
                    "consume": 2,
                    "emit": [2],
                },
                {
                    "match": {"classes": ["first", "verse"]},
                    "type": "{l}",
                    "role": "line-start",
                },
                {
                    "match": {"classes": ["verse"]},
                    "type": "{l}",
                    "role": "line",
                },
            ]
            finalize_recipe(epub_path, recipe)
            self.assertNotIn("references", recipe)
            self.assertNotIn("mapping", recipe)
            self.assertEqual(
                [
                    (block.block_id, block.block_type, block.text)
                    for block in extract_recipe(epub_path, recipe)
                ],
                [
                    ("01.000", "{h}", "A Beginning"),
                    ("01.001", "paragraph", "Ordinary prose."),
                    ("01.002.01", "{l}", "First line."),
                    ("01.002.02", "{l}", "Second line."),
                ],
            )

    def test_recipe_file_apis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest()
            )
            cast(dict[str, object], recipe["source_blocks"])["include_locators"] = [
                "*#1"
            ]
            expected = finalize_recipe(epub_path, recipe)
            recipe_path = directory / "recipe.json"
            recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
            self.assertEqual(compile_recipe_file(epub_path, recipe_path), expected)
            self.assertEqual(
                extract_recipe_file(epub_path, recipe_path),
                [ExtractedBlock("001", "paragraph", "Chapter One")],
            )

    def test_source_anchored_replacement_and_insertion(self) -> None:
        chapter = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
  <p>Alpha / Beta</p><p>Tail.</p>
</body></html>"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(
                directory,
                documents={
                    "text/chapter.xhtml": chapter,
                    "text/aux.xhtml": AUXILIARY,
                    "nav.xhtml": NAVIGATION,
                    "images/cover.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
                },
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest()
            )
            output = cast(dict[str, object], recipe["output"])
            output["replacements"] = [
                {
                    "anchor": "text/chapter.xhtml#1",
                    "outputs": [
                        {
                            "type": "word",
                            "role": "block",
                            "parts": [
                                {
                                    "document": "text/chapter.xhtml",
                                    "element_path": "1",
                                    "slice": {"start": 0, "end": 5},
                                }
                            ],
                        },
                        {
                            "type": "word",
                            "role": "block",
                            "parts": [
                                {
                                    "document": "text/chapter.xhtml",
                                    "element_path": "1",
                                    "slice": {"start": 8, "end": 12},
                                }
                            ],
                        },
                    ],
                }
            ]
            output["insertions"] = [
                {
                    "after": "text/chapter.xhtml#2",
                    "outputs": [
                        {
                            "type": "note",
                            "role": "fixed",
                            "id": "note",
                            "parts": [
                                {"document": "text/aux.xhtml", "element_path": "1"}
                            ],
                        }
                    ],
                }
            ]
            compiled = finalize_recipe(epub_path, recipe)
            self.assertEqual(compiled.reserved_locators, ("text/chapter.xhtml#1",))
            self.assertEqual(
                [
                    (block.block_id, block.text)
                    for block in extract_recipe(epub_path, recipe)
                ],
                [
                    ("001", "Alpha"),
                    ("002", "Beta"),
                    ("003", "Tail."),
                    ("note", "Auxiliary material."),
                ],
            )

    def test_transition_grouping_and_rule_predicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest()
            )
            source = cast(dict[str, object], recipe["source_blocks"])
            source["include_locators"] = ["*#1", "*#2"]
            output = cast(dict[str, object], recipe["output"])
            output["groups"] = {"transitions": {"text/chapter.xhtml#1": "chapter"}}
            output["identifiers"] = {
                "block": {"template": "{group}.{number}", "start": 0}
            }
            output["rules"] = [
                {
                    "match": {
                        "tag": "h1",
                        "locators": ["TEXT/*.XHTML#1"],
                        "text_pattern": "chapter one",
                        "case_insensitive": True,
                    },
                    "type": "heading",
                }
            ]
            finalize_recipe(epub_path, recipe)
            self.assertEqual(
                [
                    (block.block_id, block.block_type)
                    for block in extract_recipe(epub_path, recipe)
                ],
                [("chapter.0", "heading"), ("chapter.1", "paragraph")],
            )

    def test_generation_validation_fails_closed(self) -> None:
        invalid_outputs: tuple[tuple[object, str], ...] = (
            ({}, "identifiers"),
            (
                {
                    "groups": {},
                    "identifiers": {"block": {"template": "{group}"}},
                    "default": {"type": "p"},
                },
                "missing required field",
            ),
            (
                {
                    "groups": {},
                    "identifiers": {"block": {"template": "{number}"}},
                    "default": {"type": "p", "role": "fixed"},
                },
                "required when role",
            ),
            (
                {
                    "groups": {},
                    "identifiers": {"block": {"template": "{number}"}},
                    "default": {"type": "p", "role": "line"},
                },
                "identifiers.line",
            ),
            (
                {
                    "groups": {"source_pattern": "no capture"},
                    "identifiers": {"block": {"template": "{number}"}},
                    "default": {"type": "p"},
                },
                "exactly one capturing group",
            ),
        )
        for output, message in invalid_outputs:
            with self.subTest(message=message):
                recipe = minimal_recipe()
                recipe["output"] = output
                with self.assertRaisesRegex(EpubBlocksError, message):
                    compile_recipe("unused.epub", recipe)

    def test_recipe_parser_rejects_malformed_members(self) -> None:
        base = minimal_recipe()

        def clone() -> dict[str, object]:
            return cast(dict[str, object], json.loads(json.dumps(base)))

        invalid: list[dict[str, object]] = []
        top_level_changes: tuple[tuple[str, object], ...] = (
            ("recipe_version", "2"),
            ("metadata", []),
            ("unknown", True),
            ("epub", {"identifier": "x"}),
            ("epub", {"identifier": "", "sha256": "0" * 64}),
            ("normalization", {"collapse_whitespace": "yes"}),
            ("normalization", {"strip": 1}),
            ("normalization", {"unicode_normalization": "UTF-8"}),
            ("omit_epub_types", "noteref"),
            ("omit_epub_types", [""]),
            ("omit_epub_types", ["noteref", "noteref"]),
            ("source_blocks", {"include_non_linear": 1}),
            ("source_blocks", {"unknown": []}),
        )
        for key, value in top_level_changes:
            recipe = clone()
            recipe[key] = value
            invalid.append(recipe)

        bad_groups: tuple[object, ...] = (
            {"source_pattern": "("},
            {"source_pattern": "no capture"},
            {
                "source_pattern": "(x)",
                "source_marker": {"pattern": "^(x)"},
            },
            {"capture_kind": "roman"},
            {"source_marker": {"pattern": "(x)"}},
            {"source_marker": {"pattern": "^(x)", "case_insensitive": 1}},
            {"source_marker": {"pattern": "^(x)"}, "capture_kind": "bad"},
            {"source_pattern": "(x)", "source_map": {}},
            {"source_pattern": "(x)", "source_map": {"": "x"}},
            {"source_pattern": "(x)", "source_map": {"x": ""}},
            {"source_pattern": "(x)", "source_map": {"x": "y"}, "source_offset": 1},
            {"source_offset": 1},
            {"capture_width": 2},
            {"source_pattern": "(x)", "capture_width": 0},
            {"transitions": {}},
            {"transitions": {"bad": "x"}},
        )
        for groups in bad_groups:
            recipe = clone()
            cast(dict[str, object], recipe["output"])["groups"] = groups
            invalid.append(recipe)

        bad_identifiers: tuple[object, ...] = (
            [],
            {},
            {"unknown": {}},
            {"block": []},
            {"block": {"template": ""}},
            {"block": {"template": "{missing}"}},
            {"block": {"template": "{number!r}"}},
            {"block": {"template": "{number:{group}}"}},
            {"block": {"template": "literal"}},
            {"block": {"template": "{number", "start": 1}},
            {"block": {"template": "{number}", "start": -1}},
            {"block": {"template": "{number}"}, "line": []},
            {"block": {"template": "{number}"}, "line": {"template": "{number}"}},
        )
        for identifiers in bad_identifiers:
            recipe = clone()
            cast(dict[str, object], recipe["output"])["identifiers"] = identifiers
            invalid.append(recipe)

        bad_rules = cast(
            tuple[object, ...],
            (
                {},
                [[]],
                [{"match": {}, "type": "p"}],
                [{"match": {"tag": "p"}, "type": ""}],
                [{"match": {"tag": "p"}, "type": "p", "role": "bad"}],
                [{"match": {"tag": "p"}, "type": "p", "id": "x"}],
                [{"match": {"tag": "p"}, "type": "p", "role": "fixed"}],
                [{"match": {"unknown": "p"}, "type": "p"}],
                [{"match": {"classes": "x"}, "type": "p"}],
                [{"match": {"text_pattern": "("}, "type": "p"}],
                [{"match": {"tag": "p", "case_insensitive": 1}, "type": "p"}],
                [{"match": {"tag": "p"}, "type": "p", "consume": 0}],
                [{"match": {"tag": "p"}, "type": "p", "consume": True}],
                [{"match": {"tag": "p"}, "type": "p", "emit": []}],
                [{"match": {"tag": "p"}, "type": "p", "emit": [1, 1]}],
                [{"match": {"tag": "p"}, "type": "p", "emit": [2]}],
                [{"match": {"tag": "p"}, "type": "p", "separator": 1}],
                [
                    {
                        "match": {"tag": "p"},
                        "type": "p",
                        "consume": 2,
                        "remove_prefix": {"pattern": "^x"},
                    }
                ],
                [
                    {
                        "match": {"tag": "p"},
                        "type": "p",
                        "remove_prefix": {"pattern": "x"},
                    }
                ],
                [
                    {
                        "match": {"tag": "p"},
                        "type": "p",
                        "remove_prefix": {"pattern": "^"},
                    }
                ],
                [
                    {
                        "match": {"tag": "p"},
                        "type": "p",
                        "remove_prefix": {"pattern": "^("},
                    }
                ],
            ),
        )
        for rules in bad_rules:
            recipe = clone()
            cast(dict[str, object], recipe["output"])["rules"] = rules
            invalid.append(recipe)

        fragment = {"document": "text/chapter.xhtml", "element_path": "2"}
        bad_replacements = cast(
            tuple[object, ...],
            (
                {},
                [[]],
                [{"anchor": "bad", "outputs": []}],
                [
                    {
                        "anchor": "text/chapter.xhtml#1",
                        "outputs": [{"type": "p", "parts": []}],
                    }
                ],
                [
                    {
                        "anchor": "text/chapter.xhtml#1",
                        "outputs": [{"type": "p", "parts": [{}]}],
                    }
                ],
                [
                    {
                        "anchor": "text/chapter.xhtml#1",
                        "outputs": [
                            {"type": "p", "parts": [{**fragment, "element_path": "+1"}]}
                        ],
                    }
                ],
                [
                    {
                        "anchor": "text/chapter.xhtml#1",
                        "outputs": [
                            {
                                "type": "p",
                                "parts": [{**fragment, "omit": ["1", "1.2"]}],
                            }
                        ],
                    }
                ],
                [
                    {
                        "anchor": "text/chapter.xhtml#1",
                        "outputs": [
                            {
                                "type": "p",
                                "parts": [
                                    {**fragment, "slice": {"start": 1, "end": 1}}
                                ],
                            }
                        ],
                    }
                ],
                [
                    {
                        "anchor": "text/chapter.xhtml#1",
                        "outputs": [{"type": "p", "parts": [fragment], "separator": 1}],
                    }
                ],
                [
                    {
                        "anchor": "text/chapter.xhtml#1",
                        "outputs": [{"type": "p", "parts": [fragment]}],
                    },
                    {
                        "anchor": "text/chapter.xhtml#1",
                        "outputs": [{"type": "p", "parts": [fragment]}],
                    },
                ],
            ),
        )
        for replacements in bad_replacements:
            recipe = clone()
            cast(dict[str, object], recipe["output"])["replacements"] = replacements
            invalid.append(recipe)

        for index, recipe in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(EpubBlocksError):
                compile_recipe("unused.epub", recipe)

    def test_line_without_start_and_compiled_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest()
            )
            cast(dict[str, object], recipe["source_blocks"])["include_locators"] = [
                "*#1"
            ]
            output = cast(dict[str, object], recipe["output"])
            output["default"] = {"type": "line", "role": "line"}
            with self.assertRaisesRegex(EpubBlocksError, "no preceding line-start"):
                compile_recipe(epub_path, recipe, verify_digest=False)
            output["default"] = {"type": "paragraph", "role": "block"}
            finalize_recipe(epub_path, recipe)
            output["compiled_sha256"] = "0" * 64
            with self.assertRaisesRegex(EpubBlocksError, "compiled SHA-256"):
                compile_recipe(epub_path, recipe)

    def test_source_pattern_mapping_and_prefix_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest()
            )
            cast(dict[str, object], recipe["source_blocks"])["include_locators"] = [
                "*#1"
            ]
            output = cast(dict[str, object], recipe["output"])
            output["groups"] = {
                "source_pattern": r"^s(\d+):",
                "capture_kind": "decimal",
                "source_offset": 1,
                "capture_width": 2,
            }
            output["identifiers"] = {
                "block": {"template": "{group}.{number:03d}", "start": 0}
            }
            output["rules"] = [
                {
                    "match": {"tag": "h1"},
                    "type": "heading",
                    "remove_prefix": {
                        "pattern": "^chapter ",
                        "case_insensitive": True,
                    },
                }
            ]
            finalize_recipe(epub_path, recipe)
            self.assertEqual(
                [
                    (block.block_id, block.text)
                    for block in extract_recipe(epub_path, recipe)
                ],
                [("02.000", "One")],
            )

            output["groups"] = {
                "source_pattern": r"^s(\d+):",
                "source_map": {"001": "mapped"},
            }
            output.pop("compiled_sha256")
            compiled = compile_recipe(epub_path, recipe, verify_digest=False)
            self.assertEqual(compiled.blocks[0].block_id, "mapped.000")

    def test_runtime_mapping_failures_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            source_hash = hashlib.sha256(epub_path.read_bytes()).hexdigest()

            def recipe() -> dict[str, object]:
                value = minimal_recipe(epub_sha256=source_hash)
                cast(dict[str, object], value["source_blocks"])["include_locators"] = [
                    "*#1",
                    "*#2",
                ]
                return value

            cases: list[tuple[dict[str, object], str]] = []

            value = recipe()
            cast(dict[str, object], value["output"])["skip_source"] = [
                "text/chapter.xhtml#999"
            ]
            cases.append((value, "not selected"))

            value = recipe()
            output = cast(dict[str, object], value["output"])
            output["skip_source"] = ["text/chapter.xhtml#1"]
            output["replacements"] = [
                {
                    "anchor": "text/chapter.xhtml#1",
                    "outputs": [
                        {
                            "type": "p",
                            "parts": [
                                {"document": "text/chapter.xhtml", "element_path": "1"}
                            ],
                        }
                    ],
                }
            ]
            cases.append((value, "both skipped and reserved"))

            value = recipe()
            cast(dict[str, object], value["output"])["insertions"] = [
                {
                    "after": "text/chapter.xhtml#999",
                    "outputs": [
                        {
                            "type": "note",
                            "role": "fixed",
                            "id": "note",
                            "parts": [
                                {"document": "text/aux.xhtml", "element_path": "1"}
                            ],
                        }
                    ],
                }
            ]
            cases.append((value, "anchor locator.*not selected"))

            value = recipe()
            cast(dict[str, object], value["output"])["replacements"] = [
                {
                    "anchor": "text/chapter.xhtml#1",
                    "outputs": [
                        {
                            "type": "p",
                            "parts": [
                                {
                                    "document": "text/chapter.xhtml",
                                    "element_path": "1",
                                },
                                {
                                    "document": "text/chapter.xhtml",
                                    "element_path": "6.1",
                                },
                            ],
                        }
                    ],
                }
            ]
            cases.append((value, "replacement locator.*not selected"))

            value = recipe()
            cast(dict[str, object], value["output"])["rules"] = [
                {"match": {"tag": "p"}, "type": "p", "consume": 3}
            ]
            cases.append((value, "needs 3 blocks"))

            value = recipe()
            output = cast(dict[str, object], value["output"])
            output["groups"] = {
                "transitions": {
                    "text/chapter.xhtml#1": "a",
                    "text/chapter.xhtml#2": "b",
                }
            }
            output["rules"] = [{"match": {"tag": "h1"}, "type": "p", "consume": 2}]
            cases.append((value, "crosses an output group"))

            value = recipe()
            output = cast(dict[str, object], value["output"])
            output["rules"] = [{"match": {"tag": "h1"}, "type": "p", "consume": 2}]
            output["skip_source"] = ["text/chapter.xhtml#2"]
            cases.append((value, "specially handled"))

            value = recipe()
            output = cast(dict[str, object], value["output"])
            output["rules"] = [{"match": {"tag": "h1"}, "type": "p", "consume": 2}]
            output["insertions"] = [
                {
                    "after": "text/chapter.xhtml#1",
                    "outputs": [
                        {
                            "type": "note",
                            "role": "fixed",
                            "id": "note",
                            "parts": [
                                {"document": "text/aux.xhtml", "element_path": "1"}
                            ],
                        }
                    ],
                }
            ]
            cases.append((value, "crosses insertion"))

            value = recipe()
            output = cast(dict[str, object], value["output"])
            output["default"] = {"type": "p", "role": "fixed", "id": "same"}
            cases.append((value, "duplicate generated identifier"))

            value = recipe()
            output = cast(dict[str, object], value["output"])
            output["skip_source"] = [
                "text/chapter.xhtml#1",
                "text/chapter.xhtml#2",
            ]
            cases.append((value, "generated no output"))

            for value, message in cases:
                with (
                    self.subTest(message=message),
                    self.assertRaisesRegex(EpubBlocksError, message),
                ):
                    compile_recipe(epub_path, value, verify_digest=False)

    def test_marker_and_group_failures_are_explicit(self) -> None:
        chapter = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
  <p>Before.</p><h1>CHAPTER I</h1><h1>CHAPTER I</h1>
</body></html>"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(
                directory,
                documents={
                    "text/chapter.xhtml": chapter,
                    "text/aux.xhtml": AUXILIARY,
                    "nav.xhtml": NAVIGATION,
                    "images/cover.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
                },
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest()
            )
            output = cast(dict[str, object], recipe["output"])
            output["groups"] = {
                "source_marker": {"pattern": r"^CHAPTER ([IVX]+)$"},
                "capture_kind": "roman",
            }
            with self.assertRaisesRegex(EpubBlocksError, "repeats group"):
                compile_recipe(epub_path, recipe, verify_digest=False)

            cast(dict[str, object], recipe["source_blocks"])["include_locators"] = [
                "*#1"
            ]
            with self.assertRaisesRegex(EpubBlocksError, "before the first group"):
                compile_recipe(epub_path, recipe, verify_digest=False)

            output["groups"] = {
                "source_pattern": r"chapter-(\d+)\.xhtml#",
                "capture_kind": "decimal",
            }
            with self.assertRaisesRegex(EpubBlocksError, "does not match"):
                compile_recipe(epub_path, recipe, verify_digest=False)


class CliTests(unittest.TestCase):
    def test_cli_success_error_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
            )
            cast(dict[str, object], recipe["source_blocks"])["include_locators"] = [
                "*#2"
            ]
            finalize_recipe(epub_path, recipe)
            recipe_path = directory / "recipe.json"
            recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
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
