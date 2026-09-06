from __future__ import annotations

import contextlib
import hashlib
import io
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch
from zipfile import BadZipFile, ZipFile

from test_epub_blocks import PACKAGE, make_epub

from epub_blocks import (
    EpubBlocksError,
    Fragment,
    NormalizationOptions,
    SafetyLimits,
    __version__,
    extract_blocks,
    extract_fragments,
    inspect_epub,
)
from epub_blocks.models import UnicodeNormalization
from epub_blocks.safety import EpubArchive
from epub_blocks.xhtml import child_at, normalize_text
from epub_blocks.xml import parse_xml


class SafetyRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_archive_rejects_encryption_flags_before_reading_members(self) -> None:
        epub = make_epub(self.directory)
        data = bytearray(epub.read_bytes())
        # Mark the first local and central ZIP headers as encrypted; no
        # decryption or password lookup should ever be attempted.
        local = data.index(b"PK\x03\x04")
        central = data.index(b"PK\x01\x02")
        data[local + 6] |= 1
        data[central + 8] |= 1
        epub.write_bytes(data)
        with self.assertRaisesRegex(EpubBlocksError, "is encrypted"):
            inspect_epub(epub)

    def test_archive_directory_cannot_be_read_as_a_document(self) -> None:
        epub = make_epub(self.directory, extra_members=[("empty/", b"")])
        with (
            ZipFile(epub) as archive,
            self.assertRaisesRegex(EpubBlocksError, "is a directory"),
        ):
            EpubArchive(archive).read("empty/")

    def test_malformed_zip_directory_errors_are_wrapped(self) -> None:
        epub = make_epub(self.directory)
        with ZipFile(epub) as archive:
            with (
                patch.object(
                    archive, "infolist", side_effect=BadZipFile("bad directory")
                ),
                self.assertRaisesRegex(
                    EpubBlocksError, "malformed ZIP directory"
                ) as caught,
            ):
                EpubArchive(archive)
            self.assertIsInstance(caught.exception.__cause__, BadZipFile)

    def test_short_archive_read_is_rejected_without_charging_the_budget(self) -> None:
        epub = make_epub(self.directory)
        expected = b"application/epub+zip"
        with ZipFile(epub) as archive:
            bounded = EpubArchive(
                archive, SafetyLimits(max_total_read_bytes=len(expected))
            )
            with (
                patch.object(archive, "read", return_value=expected[:-1]),
                self.assertRaisesRegex(EpubBlocksError, "inconsistent size"),
            ):
                bounded.read("mimetype")
            self.assertEqual(bounded.read("mimetype"), expected)

    def test_total_read_budget_counts_each_member_only_once(self) -> None:
        epub = make_epub(self.directory, extra_members=[("one", b"ab"), ("two", b"cd")])
        with ZipFile(epub) as archive:
            bounded = EpubArchive(archive, SafetyLimits(max_total_read_bytes=3))
            self.assertEqual(bounded.read("one"), b"ab")
            self.assertEqual(bounded.read("one"), b"ab")
            with self.assertRaisesRegex(EpubBlocksError, "total uncompressed"):
                bounded.read("two")
            self.assertEqual(bounded.read("one"), b"ab")

    def test_package_rejects_fragment_only_backslash_and_invalid_linear_values(
        self,
    ) -> None:
        for package, message in (
            (
                PACKAGE.replace('href="text/chapter.xhtml"', 'href="#chapter"'),
                "invalid EPUB manifest path",
            ),
            (
                PACKAGE.replace(
                    'href="text/chapter.xhtml"', 'href="text%5Cchapter.xhtml"'
                ),
                "invalid EPUB manifest path",
            ),
            (PACKAGE.replace('linear="no"', 'linear="maybe"'), "invalid linear value"),
        ):
            with self.subTest(message=message):
                epub = make_epub(self.directory, package=package)
                with self.assertRaisesRegex(EpubBlocksError, message):
                    inspect_epub(epub)

    def test_utf16_without_an_encoding_attribute_and_with_leading_whitespace(
        self,
    ) -> None:
        for codec in ("utf-16-le", "utf-16-be"):
            for text in ('<?xml version="1.0"?><p>é</p>', " \t\r\n<p>é</p>"):
                with self.subTest(codec=codec, text=text):
                    self.assertEqual(parse_xml(text.encode(codec), "sample").text, "é")

    def test_utf16_declared_byte_order_must_match_detected_bytes(self) -> None:
        for actual, declared in (("utf-16-le", "UTF-16BE"), ("utf-16-be", "UTF-16LE")):
            with self.subTest(actual=actual):
                data = f'<?xml version="1.0" encoding="{declared}"?><p/>'.encode(actual)
                with self.assertRaisesRegex(
                    EpubBlocksError, "conflicts with detected UTF-16"
                ):
                    parse_xml(data, "sample")

    def test_xml_size_is_checked_again_after_utf16_transcoding(self) -> None:
        data = ("<p>" + "漢" * 30 + "</p>").encode("utf-16-le")
        with self.assertRaisesRegex(
            EpubBlocksError, "normalized XML document is too large"
        ):
            parse_xml(data, "sample", SafetyLimits(max_xml_bytes=len(data)))

    def test_whitespace_only_utf16_and_unterminated_xml_are_rejected(self) -> None:
        for data in (" \t".encode("utf-16-le"), " \t".encode("utf-16-be")):
            with (
                self.subTest(data=data),
                self.assertRaisesRegex(EpubBlocksError, "unsupported NUL"),
            ):
                parse_xml(data, "sample")
        for data in (b"<!--never closed", b"<![CDATA[never closed", b"<?never closed"):
            with (
                self.subTest(data=data),
                self.assertRaisesRegex(EpubBlocksError, "malformed XML"),
            ):
                parse_xml(data, "sample")

    def test_doctype_after_whitespace_and_comments_is_still_in_the_prolog(self) -> None:
        data = (
            b"\xef\xbb\xbf<?xml version='1.0'?><!--prolog--> \n<!DOCTYPE html><html/>"
        )
        self.assertEqual(parse_xml(data, "sample").tag, "html")

    def test_low_level_fragments_validate_identity_and_slice_bounds(self) -> None:
        epub = make_epub(self.directory)
        fragment = Fragment("text/chapter.xhtml", "1")
        with self.assertRaisesRegex(EpubBlocksError, "non-empty string"):
            extract_blocks(epub, expected_identifier="")
        with self.assertRaisesRegex(EpubBlocksError, "expected package identifier"):
            extract_fragments(epub, [fragment], expected_identifier="another edition")
        with self.assertRaisesRegex(EpubBlocksError, "both be integers"):
            extract_fragments(epub, [Fragment("text/chapter.xhtml", "1", start=0)])
        self.assertEqual(extract_fragments(epub, [fragment]), ["Chapter One"])
        corrupt = self.directory / "corrupt.epub"
        corrupt.write_bytes(b"not a zip")
        with self.assertRaisesRegex(EpubBlocksError, "not a valid ZIP"):
            extract_fragments(
                corrupt,
                [fragment],
                expected_sha256=hashlib.sha256(corrupt.read_bytes()).hexdigest(),
            )

    def test_low_level_normalization_can_preserve_whitespace_and_reject_bad_forms(
        self,
    ) -> None:
        options = NormalizationOptions(
            collapse_whitespace=False, strip=False, unicode_normalization="none"
        )
        self.assertEqual(normalize_text(" \te\u0301 \n", options), " \te\u0301 \n")
        with self.assertRaisesRegex(EpubBlocksError, "unknown Unicode normalization"):
            normalize_text(
                "é",
                NormalizationOptions(
                    unicode_normalization=cast(UnicodeNormalization, "UTF-8")
                ),
            )

    def test_oversized_numeric_element_path_is_reported_as_an_extraction_error(
        self,
    ) -> None:
        root = parse_xml(b"<p><i/></p>", "sample")
        limit = sys.get_int_max_str_digits()
        if limit == 0:
            self.skipTest("Python integer conversion limit is disabled")
        with self.assertRaisesRegex(EpubBlocksError, "invalid element path"):
            child_at(root, "9" * (limit + 1))

    def test_module_entry_point_supports_version(self) -> None:
        stdout = io.StringIO()
        # Run the real __main__ path in-process so it remains in coverage.
        with (
            patch.object(sys, "argv", ["epub-blocks", "--version"]),
            patch.dict(sys.modules),
            contextlib.redirect_stdout(stdout),
        ):
            sys.modules.pop("epub_blocks.cli", None)
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("epub_blocks.cli", run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(__version__, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
