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
    BlockReference,
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


def write_references(
    path: Path,
    rows: list[tuple[str, str]],
    *,
    header: tuple[str, str] = ("id", "type"),
) -> str:
    content = "\t".join(header) + "\n"
    content += "".join(f"{block_id}\t{block_type}\n" for block_id, block_type in rows)
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def minimal_recipe(
    *,
    epub_sha256: str = "0" * 64,
    references_path: str = "references.tsv",
    references_sha256: str = "0" * 64,
    **overrides: object,
) -> dict[str, object]:
    recipe: dict[str, object] = {
        "recipe_version": "1",
        "epub": {"identifier": "sample-edition", "sha256": epub_sha256},
        "references": {
            "path": references_path,
            "sha256": references_sha256,
        },
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
        "mapping": {
            "strategy": "ordered",
            "groups": {},
            "join_separator": " ",
            "type_rules": {},
            "skip_source": [],
            "overrides": {},
            "compiled_sha256": "0" * 64,
        },
    }
    recipe.update(overrides)
    return recipe


def finalize_recipe(
    epub_path: Path, recipe: dict[str, object], base_dir: Path
) -> CompiledRecipe:
    compiled = compile_recipe(epub_path, recipe, base_dir=base_dir, verify_digest=False)
    mapping = cast(dict[str, object], recipe["mapping"])
    mapping["compiled_sha256"] = compiled_recipe_digest(compiled)
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

            reference_path = directory / "references.tsv"
            reference_hash = write_references(
                reference_path,
                [
                    ("part-heading", "heading"),
                    ("chapter-heading", "heading"),
                    ("verse-1", "verse-line"),
                    ("verse-2", "verse-line"),
                    ("quotation", "quotation"),
                    ("trailer", "trailer"),
                ],
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_sha256=reference_hash,
            )
            recipe["normalization"] = {
                "collapse_whitespace": False,
                "strip": True,
                "unicode_normalization": "NFC",
            }
            finalize_recipe(epub_path, recipe, directory)
            extracted = extract_recipe(epub_path, recipe, base_dir=directory)
            self.assertEqual(extracted[0].text, "Part\nAlpha")
            self.assertEqual(
                [(block.block_id, block.block_type) for block in extracted[1:]],
                [
                    ("chapter-heading", "heading"),
                    ("verse-1", "verse-line"),
                    ("verse-2", "verse-line"),
                    ("quotation", "quotation"),
                    ("trailer", "trailer"),
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


class RecipeTests(unittest.TestCase):
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
        reference = BlockReference("one", "paragraph")
        self.assertEqual(
            (reference.block_id, reference.block_type), ("one", "paragraph")
        )
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

    def test_ordered_mapping_file_apis_and_tsv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            references_path = directory / "references.tsv"
            references_hash = write_references(
                references_path,
                [("01.000", "{h}"), ("01.001", "{p}")],
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_sha256=references_hash,
            )
            source = cast(dict[str, object], recipe["source_blocks"])
            source["include_locators"] = ["*#1", "*#2"]
            expected = finalize_recipe(epub_path, recipe, directory)
            self.assertEqual(
                [block.consumed_locators for block in expected.blocks],
                [
                    ("text/chapter.xhtml#1",),
                    ("text/chapter.xhtml#2",),
                ],
            )
            blocks = extract_recipe(epub_path, recipe, base_dir=directory)
            self.assertEqual(
                [(block.block_id, block.block_type, block.text) for block in blocks],
                [
                    ("01.000", "{h}", "Chapter One"),
                    ("01.001", "{p}", "One two three."),
                ],
            )

            recipe_path = directory / "recipe.json"
            recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
            self.assertEqual(compile_recipe_file(epub_path, recipe_path), expected)
            self.assertEqual(extract_recipe_file(epub_path, recipe_path), blocks)

            output_path = directory / "output" / "records.tsv"
            write_tsv(output_path, blocks)
            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "01.000\t{h}\tChapter One\n01.001\t{p}\tOne two three.\n",
            )

            with self.assertRaisesRegex(EpubBlocksError, "base directory"):
                compile_recipe(epub_path, recipe)
            cast(dict[str, object], recipe["references"])["sha256"] = "0" * 64
            with self.assertRaisesRegex(EpubBlocksError, "reference-index SHA-256"):
                compile_recipe(
                    epub_path, recipe, base_dir=directory, verify_digest=False
                )

    def test_whole_body_override_compiles_digests_and_extracts(self) -> None:
        chapter = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<p>Candidate.</p>
<div>Body tail.</div>
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
            reference_hash = write_references(
                directory / "references.tsv",
                [("whole", "body")],
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_sha256=reference_hash,
            )
            source = cast(dict[str, object], recipe["source_blocks"])
            source["include_locators"] = ["*#1"]
            mapping = cast(dict[str, object], recipe["mapping"])
            mapping["skip_source"] = ["text/chapter.xhtml#1"]
            mapping["overrides"] = {
                "whole": {"parts": [{"document": "text/chapter.xhtml"}]}
            }

            compiled = finalize_recipe(epub_path, recipe, directory)
            self.assertEqual(
                compiled.reserved_locators,
                ("text/chapter.xhtml#",),
            )
            self.assertRegex(compiled_recipe_digest(compiled), r"^[0-9a-f]{64}$")
            self.assertEqual(
                extract_recipe(epub_path, recipe, base_dir=directory),
                [ExtractedBlock("whole", "body", "Candidate. Body tail.")],
            )

    def test_type_rules_consume_emit_join_and_normalize(self) -> None:
        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<h1>CHAPTER ONE</h1><h2>  A Title  </h2>"
            "<p>  Alpha  </p><p>  Beta  </p>"
            "</body></html>"
        )
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
            reference_hash = write_references(
                directory / "references.tsv",
                [("heading", "{h}"), ("paragraph", "{p}")],
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_sha256=reference_hash,
            )
            recipe["normalization"] = {
                "collapse_whitespace": False,
                "strip": True,
                "unicode_normalization": "NFC",
            }
            mapping = cast(dict[str, object], recipe["mapping"])
            mapping["join_separator"] = "|"
            mapping["type_rules"] = {
                "{h}": {"consume": 2, "emit": [2]},
                "{p}": {"consume": 2},
            }
            compiled = finalize_recipe(epub_path, recipe, directory)
            self.assertEqual(
                compiled.blocks[0].parts, (Fragment("text/chapter.xhtml", "2"),)
            )
            self.assertEqual(
                compiled.blocks[0].consumed_locators,
                ("text/chapter.xhtml#1", "text/chapter.xhtml#2"),
            )
            self.assertEqual(compiled.blocks[1].separator, "|")
            self.assertEqual(
                [
                    block.text
                    for block in extract_recipe(epub_path, recipe, base_dir=directory)
                ],
                ["A Title", "Alpha|Beta"],
            )

    def test_type_rules_reject_unknown_reference_types(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            reference_hash = write_references(
                directory / "references.tsv",
                [("one", "paragraph")],
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_sha256=reference_hash,
            )
            cast(dict[str, object], recipe["mapping"])["type_rules"] = {
                "paragaph": {"consume": 1}
            }
            with self.assertRaisesRegex(
                EpubBlocksError,
                r"unknown reference type.*'paragaph'",
            ):
                compile_recipe(
                    epub_path,
                    recipe,
                    base_dir=directory,
                    verify_digest=False,
                )

    def test_roman_source_markers_and_prefix_removal(self) -> None:
        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<h1>CHAPTER I An Unexpected Party</h1><p>First.</p>"
            "<h1>Chapter II Roast Mutton</h1><p>Second.</p>"
            "</body></html>"
        )
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
            reference_hash = write_references(
                directory / "references.tsv",
                [
                    ("01.000", "{h}"),
                    ("01.001", "{p}"),
                    ("02.000", "{h}"),
                    ("02.001", "{p}"),
                ],
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_sha256=reference_hash,
            )
            mapping = cast(dict[str, object], recipe["mapping"])
            mapping["groups"] = {
                "reference_pattern": r"^(\d+)\.",
                "reference_capture_kind": "decimal",
                "source_marker": {
                    "pattern": r"^CHAPTER\s+([IVXLCDM]+)\b",
                    "capture_kind": "roman",
                    "case_insensitive": True,
                },
            }
            mapping["type_rules"] = {
                "{h}": {
                    "remove_prefix": {
                        "pattern": r"^CHAPTER\s+[IVXLCDM]+\s+",
                        "case_insensitive": True,
                    }
                }
            }
            compiled = finalize_recipe(epub_path, recipe, directory)
            self.assertEqual(
                (compiled.blocks[0].parts[0].start, compiled.blocks[0].parts[0].end),
                (10, 29),
            )
            self.assertEqual(
                [
                    block.text
                    for block in extract_recipe(epub_path, recipe, base_dir=directory)
                ],
                ["An Unexpected Party", "First.", "Roast Mutton", "Second."],
            )

            remove_prefix = cast(
                dict[str, object],
                cast(
                    dict[str, object],
                    cast(dict[str, object], mapping["type_rules"])["{h}"],
                )["remove_prefix"],
            )
            remove_prefix["case_insensitive"] = False
            with self.assertRaisesRegex(EpubBlocksError, "did not match"):
                compile_recipe(
                    epub_path, recipe, base_dir=directory, verify_digest=False
                )

    def test_separate_heading_marker_is_grouped_before_reserved_filter(self) -> None:
        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<h1>CHAPTER I</h1><h2>An Unexpected Party</h2><p>First.</p>"
            "</body></html>"
        )
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
            reference_hash = write_references(
                directory / "references.tsv",
                [("01.000", "{h}"), ("01.001", "{p}")],
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_sha256=reference_hash,
            )
            mapping = cast(dict[str, object], recipe["mapping"])
            mapping["groups"] = {
                "reference_pattern": r"^(\d+)\.",
                "reference_capture_kind": "decimal",
                "source_marker": {
                    "pattern": r"^CHAPTER\s+([IVXLCDM]+)$",
                    "capture_kind": "roman",
                    "case_insensitive": True,
                },
            }
            mapping["type_rules"] = {"{h}": {"consume": 2, "emit": [2]}}
            compiled = finalize_recipe(epub_path, recipe, directory)
            self.assertEqual(
                [
                    block.text
                    for block in extract_recipe(epub_path, recipe, base_dir=directory)
                ],
                ["An Unexpected Party", "First."],
            )
            self.assertEqual(
                compiled.blocks[0].consumed_locators,
                ("text/chapter.xhtml#1", "text/chapter.xhtml#2"),
            )

            mapping["type_rules"] = {}
            mapping["skip_source"] = ["text/chapter.xhtml#1"]
            mapping["overrides"] = {
                "01.000": {
                    "parts": [
                        {
                            "document": "text/chapter.xhtml",
                            "element_path": "2",
                        }
                    ]
                }
            }
            finalize_recipe(epub_path, recipe, directory)
            self.assertEqual(
                [
                    block.text
                    for block in extract_recipe(epub_path, recipe, base_dir=directory)
                ],
                ["An Unexpected Party", "First."],
            )

    def test_source_marker_failures(self) -> None:
        cases = (
            ("<p>Preface</p><h1>CHAPTER I Title</h1>", "before the first"),
            ("<h1>CHAPTER I One</h1><h1>CHAPTER I Again</h1>", "repeats group"),
            ("<h1>CHAPTER II Two</h1><h1>CHAPTER I One</h1>", "moves backward"),
            ("<h1>CHAPTER IIII Bad</h1>", "Roman numeral"),
            ("<h1>CHAPTER III Unknown</h1>", "unknown group"),
        )
        for markup, message in cases:
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                directory = Path(temporary_directory)
                chapter = (
                    '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                    + markup
                    + "</body></html>"
                )
                epub_path = make_epub(
                    directory,
                    documents={
                        "text/chapter.xhtml": chapter,
                        "text/aux.xhtml": AUXILIARY,
                        "nav.xhtml": NAVIGATION,
                        "images/cover.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
                    },
                )
                reference_hash = write_references(
                    directory / "references.tsv",
                    [("01.000", "{h}"), ("02.000", "{h}")],
                )
                recipe = minimal_recipe(
                    epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                    references_sha256=reference_hash,
                )
                cast(dict[str, object], recipe["mapping"])["groups"] = {
                    "reference_pattern": r"^(\d+)\.",
                    "reference_capture_kind": "decimal",
                    "source_marker": {
                        "pattern": r"^CHAPTER\s+(\S+)",
                        "capture_kind": "roman",
                        "case_insensitive": True,
                    },
                }
                with self.assertRaisesRegex(EpubBlocksError, message):
                    compile_recipe(
                        epub_path, recipe, base_dir=directory, verify_digest=False
                    )

    def test_locator_groups_offsets_and_maps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            reference_path = directory / "references.tsv"
            reference_hash = write_references(
                reference_path,
                [("02.a", "heading"), ("03.a", "paragraph")],
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_sha256=reference_hash,
            )
            cast(dict[str, object], recipe["source_blocks"])["include_locators"] = [
                "*#1",
                "*#2",
            ]
            mapping = cast(dict[str, object], recipe["mapping"])
            mapping["groups"] = {
                "reference_pattern": r"^(\d+)\.",
                "reference_capture_kind": "decimal",
                "source_pattern": r"#(\d+)$",
                "source_offset": 1,
            }
            finalize_recipe(epub_path, recipe, directory)
            self.assertEqual(
                [
                    block.text
                    for block in extract_recipe(epub_path, recipe, base_dir=directory)
                ],
                ["Chapter One", "One two three."],
            )

            reference_hash = write_references(
                reference_path,
                [("AL.a", "heading"), ("QS.trailer.a", "paragraph")],
            )
            cast(dict[str, object], recipe["references"])["sha256"] = reference_hash
            mapping["groups"] = {
                "reference_pattern": r"^(AL|QS\.trailer)\.",
                "source_pattern": r"#(\d+)$",
                "source_map": {"1": "AL", "2": "QS.24"},
                "reference_map": {"QS.trailer": "QS.24"},
            }
            finalize_recipe(epub_path, recipe, directory)
            self.assertEqual(
                [
                    block.block_id
                    for block in extract_recipe(epub_path, recipe, base_dir=directory)
                ],
                ["AL.a", "QS.trailer.a"],
            )

    def test_sparse_skips_overrides_and_splits(self) -> None:
        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<p>ignore</p><p>AlphaBeta</p><p>Tail</p>"
            "</body></html>"
        )
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
            reference_hash = write_references(
                directory / "references.tsv",
                [("a", "part"), ("b", "part"), ("tail", "paragraph")],
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_sha256=reference_hash,
            )
            mapping = cast(dict[str, object], recipe["mapping"])
            mapping["skip_source"] = ["text/chapter.xhtml#1"]
            mapping["overrides"] = {
                "a": {
                    "parts": [
                        {
                            "document": "text/chapter.xhtml",
                            "element_path": "2",
                            "slice": {"start": 0, "end": 5},
                        }
                    ]
                },
                "b": {
                    "parts": [
                        {
                            "document": "text/chapter.xhtml",
                            "element_path": "2",
                            "slice": {"start": 5, "end": 9},
                        }
                    ]
                },
            }
            compiled = finalize_recipe(epub_path, recipe, directory)
            self.assertEqual(compiled.skipped_locators, ("text/chapter.xhtml#1",))
            self.assertEqual(compiled.reserved_locators, ("text/chapter.xhtml#2",))
            self.assertEqual(
                [
                    block.text
                    for block in extract_recipe(epub_path, recipe, base_dir=directory)
                ],
                ["Alpha", "Beta", "Tail"],
            )

            mapping["skip_source"] = [
                "text/chapter.xhtml#1",
                "text/chapter.xhtml#2",
            ]
            with self.assertRaisesRegex(EpubBlocksError, "both skipped and reserved"):
                compile_recipe(
                    epub_path, recipe, base_dir=directory, verify_digest=False
                )
            mapping["skip_source"] = ["text/chapter.xhtml#999"]
            with self.assertRaisesRegex(EpubBlocksError, "not selected"):
                compile_recipe(
                    epub_path, recipe, base_dir=directory, verify_digest=False
                )

    def test_override_fragment_reuse_requires_disjoint_slices(self) -> None:
        fragment = {"document": "text/chapter.xhtml", "element_path": "2"}
        cases: tuple[tuple[dict[str, object], str], ...] = (
            (
                {
                    "a": {"parts": [fragment]},
                    "b": {"parts": [fragment]},
                },
                "every reuse must use non-overlapping slices",
            ),
            (
                {
                    "a": {"parts": [fragment]},
                    "b": {"parts": [{**fragment, "slice": {"start": 0, "end": 5}}]},
                },
                "every reuse must use non-overlapping slices",
            ),
            (
                {
                    "a": {"parts": [{**fragment, "slice": {"start": 0, "end": 5}}]},
                    "b": {"parts": [{**fragment, "slice": {"start": 4, "end": 9}}]},
                },
                "has overlapping slices",
            ),
            (
                {
                    "a": {
                        "parts": [
                            {**fragment, "slice": {"start": 0, "end": 5}},
                            {**fragment, "slice": {"start": 0, "end": 5}},
                        ]
                    }
                },
                "has overlapping slices",
            ),
        )
        for overrides, message in cases:
            with self.subTest(message=message):
                recipe = minimal_recipe()
                cast(dict[str, object], recipe["mapping"])["overrides"] = overrides
                with self.assertRaisesRegex(EpubBlocksError, message):
                    compile_recipe("unused.epub", recipe)

    def test_reference_index_validation_and_custom_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            reference_path = directory / "references.tsv"
            reference_hash = write_references(
                reference_path,
                [("one", "paragraph")],
                header=("identifier", "kind"),
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_path=str(reference_path),
                references_sha256=reference_hash,
            )
            cast(dict[str, object], recipe["references"])["columns"] = {
                "id": "identifier",
                "type": "kind",
            }
            cast(dict[str, object], recipe["source_blocks"])["include_locators"] = [
                "*#2"
            ]
            self.assertEqual(
                compile_recipe(epub_path, recipe, verify_digest=False)
                .blocks[0]
                .block_id,
                "one",
            )

            invalid_values: tuple[tuple[bytes, str], ...] = (
                (b"", "no header"),
                (b"id\tid\n", "duplicate columns"),
                (b"id\ttype\textra\n", "exactly two columns"),
                (b"id\tkind\none\tp\n", "missing column"),
                (b"id\ttype\none\tp\textra\n", "expected 2 fields"),
                (b"id\ttype\n\tp\n", "empty canonical reference"),
                (b"id\ttype\none\t\n", "empty block type"),
                (b'id\ttype\none\t"p\n', "invalid reference-index TSV"),
                (b"id\ttype\none\tp\none\tp\n", "duplicate canonical reference"),
                (b"id\ttype\n", "no records"),
                (b"\xff", "not UTF-8"),
            )
            references = cast(dict[str, object], recipe["references"])
            references["columns"] = {}
            for data, message in invalid_values:
                with self.subTest(message=message):
                    reference_path.write_bytes(data)
                    references["sha256"] = hashlib.sha256(data).hexdigest()
                    with self.assertRaisesRegex(EpubBlocksError, message):
                        compile_recipe(epub_path, recipe, verify_digest=False)

    def test_structural_validation_is_fail_closed(self) -> None:
        base = minimal_recipe()

        def clone() -> dict[str, object]:
            return cast(dict[str, object], json.loads(json.dumps(base)))

        invalid: list[dict[str, object]] = []
        changes: tuple[tuple[str, object], ...] = (
            ("normalization", {"collapse_whitespace": "yes"}),
            ("normalization", {"strip": 1}),
            ("normalization", {"unicode_normalization": "UTF-8"}),
            ("omit_epub_types", "noteref"),
            ("omit_epub_types", [""]),
            ("omit_epub_types", ["noteref", "noteref"]),
            ("metadata", []),
            ("unknown", True),
            ("epub", {"identifier": "x"}),
            ("references", {"path": "x", "sha256": "A" * 64}),
            ("source_blocks", {"unknown": []}),
            ("mapping", {"strategy": "other"}),
        )
        for key, value in changes:
            recipe = clone()
            recipe[key] = value
            invalid.append(recipe)

        recipe = clone()
        cast(dict[str, object], recipe["source_blocks"])["include_non_linear"] = 1
        invalid.append(recipe)
        recipe = clone()
        cast(dict[str, object], recipe["references"])["columns"] = {
            "id": "same",
            "type": "same",
        }
        invalid.append(recipe)
        recipe = clone()
        cast(dict[str, object], recipe["references"])["columns"] = {"unknown": "x"}
        invalid.append(recipe)

        bad_groups: tuple[object, ...] = (
            {"reference_pattern": "("},
            {"reference_pattern": "(x)"},
            {"source_pattern": "(x)"},
            {
                "reference_pattern": "(x)",
                "source_pattern": "(x)",
                "source_marker": {
                    "pattern": "^(x)",
                    "capture_kind": "string",
                },
            },
            {
                "reference_pattern": "(x)",
                "source_pattern": "(x)",
                "source_map": {},
            },
            {
                "reference_pattern": "(x)",
                "source_pattern": "(x)",
                "source_map": {"x": "x"},
                "source_offset": 1,
            },
            {"reference_map": {"x": "x"}},
            {"source_offset": 1},
            {
                "reference_pattern": "(x)",
                "reference_capture_kind": "roman",
                "source_pattern": "(x)",
            },
            {
                "reference_pattern": "(x)",
                "source_marker": {
                    "pattern": "(x)",
                    "capture_kind": "roman",
                },
            },
            {
                "reference_pattern": "(x)",
                "source_marker": {
                    "pattern": "^(x)",
                    "capture_kind": "bad",
                },
            },
            {
                "reference_pattern": "(x)",
                "source_marker": {
                    "pattern": "^(x)",
                    "capture_kind": "string",
                    "case_insensitive": 1,
                },
            },
        )
        for groups in bad_groups:
            recipe = clone()
            cast(dict[str, object], recipe["mapping"])["groups"] = groups
            invalid.append(recipe)

        bad_mapping_values: tuple[tuple[str, object], ...] = (
            ("join_separator", 1),
            ("skip_source", ["not-a-locator"]),
            ("skip_source", ["bad\u2028path#1"]),
            ("compiled_sha256", "sha256:" + "0" * 64),
            ("type_rules", {"": {"consume": 1}}),
            ("type_rules", {"p": []}),
            ("type_rules", {"p": {"consume": 0}}),
            ("type_rules", {"p": {"consume": True}}),
            ("type_rules", {"p": {"emit": []}}),
            ("type_rules", {"p": {"emit": [1, 1]}}),
            ("type_rules", {"p": {"consume": 1, "emit": [2]}}),
            ("type_rules", {"p": {"separator": 1}}),
            (
                "type_rules",
                {"p": {"consume": 2, "remove_prefix": {"pattern": "^x"}}},
            ),
            (
                "type_rules",
                {"p": {"remove_prefix": {"pattern": "x"}}},
            ),
            (
                "type_rules",
                {"p": {"remove_prefix": {"pattern": "^"}}},
            ),
            (
                "type_rules",
                {
                    "p": {
                        "remove_prefix": {
                            "pattern": "^x",
                            "case_insensitive": 1,
                        }
                    }
                },
            ),
        )
        for name, value in bad_mapping_values:
            recipe = clone()
            cast(dict[str, object], recipe["mapping"])[name] = value
            invalid.append(recipe)

        bad_overrides: tuple[dict[str, object], ...] = (
            {"": {"parts": [{"document": "x", "element_path": "1"}]}},
            {"a": []},
            {"a": {"parts": []}},
            {"a": {"parts": [{}]}},
            {"a": {"parts": [{"document": "x", "element_path": "+1"}]}},
            {
                "a": {
                    "parts": [
                        {
                            "document": "x",
                            "element_path": "1",
                            "omit": ["1", "1.2"],
                        }
                    ]
                }
            },
            {
                "a": {
                    "parts": [
                        {
                            "document": "x",
                            "element_path": "1",
                            "slice": {"start": 1, "end": 1},
                        }
                    ]
                }
            },
            {
                "a": {
                    "parts": [{"document": "x", "element_path": "1"}],
                    "separator": 1,
                }
            },
            {
                "a": {
                    "parts": [{"document": "x", "element_path": "1"}],
                    "remove_prefix": {"pattern": "^x"},
                }
            },
        )
        for overrides in bad_overrides:
            recipe = clone()
            cast(dict[str, object], recipe["mapping"])["overrides"] = overrides
            invalid.append(recipe)

        for index, recipe in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(EpubBlocksError):
                compile_recipe("unused.epub", recipe)

    def test_mapping_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            reference_path = directory / "references.tsv"
            reference_hash = write_references(
                reference_path,
                [("one", "paragraph")],
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_sha256=reference_hash,
            )
            cast(dict[str, object], recipe["source_blocks"])["include_locators"] = [
                "*#1",
                "*#2",
            ]
            mapping = cast(dict[str, object], recipe["mapping"])
            with self.assertRaisesRegex(EpubBlocksError, "unmapped source"):
                compile_recipe(
                    epub_path, recipe, base_dir=directory, verify_digest=False
                )
            mapping["overrides"] = {
                "unknown": {
                    "parts": [{"document": "text/chapter.xhtml", "element_path": "1"}]
                }
            }
            with self.assertRaisesRegex(EpubBlocksError, "unknown reference"):
                compile_recipe(
                    epub_path, recipe, base_dir=directory, verify_digest=False
                )
            mapping["overrides"] = {
                "one": {"parts": [{"document": "missing.xhtml", "element_path": "1"}]}
            }
            with self.assertRaises(EpubBlocksError):
                compile_recipe(
                    epub_path, recipe, base_dir=directory, verify_digest=False
                )

            mapping["overrides"] = {}
            cast(dict[str, object], recipe["source_blocks"])["include_locators"] = [
                "*#1"
            ]
            finalized = finalize_recipe(epub_path, recipe, directory)
            mapping["compiled_sha256"] = "0" * 64
            with self.assertRaisesRegex(EpubBlocksError, "compiled SHA-256"):
                compile_recipe(epub_path, recipe, base_dir=directory)
            cast(dict[str, object], recipe["epub"])["sha256"] = "0" * 64
            with self.assertRaisesRegex(EpubBlocksError, "source SHA-256"):
                compile_recipe(
                    epub_path, recipe, base_dir=directory, verify_digest=False
                )
            self.assertTrue(finalized.blocks)

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


class CliTests(unittest.TestCase):
    def test_cli_success_error_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            epub_path = make_epub(directory)
            references_hash = write_references(
                directory / "references.tsv",
                [("one", "paragraph")],
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                references_sha256=references_hash,
            )
            cast(dict[str, object], recipe["source_blocks"])["include_locators"] = [
                "*#2"
            ]
            finalize_recipe(epub_path, recipe, directory)
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
