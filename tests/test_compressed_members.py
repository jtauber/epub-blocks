"""Real corrupt-DEFLATE regressions across the reader, public APIs, and CLI."""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from zlib import error as ZlibError

from test_epub_blocks import make_epub, minimal_recipe

from epub_blocks import (
    EpubBlocksError,
    Fragment,
    SafetyLimits,
    compile_recipe,
    extract_blocks,
    extract_fragments,
    extract_recipe,
    extract_recipe_candidates,
    inspect_epub,
)
from epub_blocks.safety import EpubArchive


class CompressedMemberTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def corrupted(
        self, member_name: str = "text/chapter.xhtml"
    ) -> tuple[Path, dict[str, object]]:
        epub = make_epub(self.directory)
        with ZipFile(epub) as archive:
            member = archive.getinfo(member_name)
            self.assertEqual(member.compress_type, ZIP_DEFLATED)
            offset = member.header_offset
        data = bytearray(epub.read_bytes())
        name_length, extra_length = struct.unpack_from("<HH", data, offset + 26)
        payload = offset + 30 + name_length + extra_length
        # Invalid DEFLATE BTYPE=3; leave the ZIP directory and headers readable.
        data[payload] = (data[payload] & ~7) | 7
        epub.write_bytes(data)
        return epub, minimal_recipe(epub_sha256=hashlib.sha256(data).hexdigest())

    def test_corrupt_member_has_context_cause_and_does_not_charge_read_budget(
        self,
    ) -> None:
        epub, _ = self.corrupted()
        with ZipFile(epub) as archive:
            member = archive.getinfo("text/chapter.xhtml")
            reader = EpubArchive(
                archive, SafetyLimits(max_total_read_bytes=member.file_size)
            )
            for _ in range(2):
                with self.assertRaisesRegex(
                    EpubBlocksError, "text/chapter.xhtml.*could not be read"
                ) as caught:
                    reader.read("text/chapter.xhtml")
                self.assertIsInstance(caught.exception.__cause__, ZlibError)
            # The failed member must not consume the budget needed by this read.
            self.assertEqual(reader.read("mimetype"), b"application/epub+zip")

    def test_corrupt_chapter_raises_domain_errors_across_public_apis(self) -> None:
        epub, recipe = self.corrupted()
        operations: tuple[Callable[[], object], ...] = (
            lambda: extract_blocks(epub),
            lambda: extract_fragments(epub, [Fragment("text/chapter.xhtml", "1")]),
            lambda: extract_recipe_candidates(epub, recipe),
            lambda: compile_recipe(epub, recipe, verify_digest=False),
            lambda: compile_recipe(epub, recipe),
            lambda: extract_recipe(epub, recipe),
        )
        for index, operation in enumerate(operations):
            with (
                self.subTest(operation=index),
                self.assertRaisesRegex(
                    EpubBlocksError, "text/chapter.xhtml.*could not be read"
                ) as caught,
            ):
                operation()
            self.assertIsInstance(caught.exception.__cause__, ZlibError)

    def test_corrupt_package_member_is_also_contextual(self) -> None:
        for member in ("META-INF/container.xml", "content.opf"):
            epub, _ = self.corrupted(member)
            with (
                self.subTest(member=member),
                self.assertRaisesRegex(
                    EpubBlocksError, f"{member}.*could not be read"
                ) as caught,
            ):
                inspect_epub(epub)
            self.assertIsInstance(caught.exception.__cause__, ZlibError)

    def test_cli_reports_normal_error_and_preserves_existing_output(self) -> None:
        epub, recipe = self.corrupted()
        source, output = epub.parent / "recipe.json", epub.parent / "output.tsv"
        source.write_text(json.dumps(recipe), encoding="utf-8")
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    output.write_text("existing\n", encoding="utf-8")
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
                self.assertIn("text/chapter.xhtml", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(list(epub.parent.glob(".output.tsv.*.tmp")), [])
                if existing:
                    self.assertEqual(output.read_text(encoding="utf-8"), "existing\n")
                else:
                    self.assertFalse(output.exists())

    def test_hash_mismatch_precedes_decoding_a_corrupt_member(self) -> None:
        epub, recipe = self.corrupted()
        recipe["epub"] = {"identifier": "sample-edition", "sha256": "0" * 64}
        with self.assertRaisesRegex(EpubBlocksError, "source SHA-256"):
            compile_recipe(epub, recipe, verify_digest=False)
