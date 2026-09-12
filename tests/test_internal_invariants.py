"""Fail-closed checks for guards normally protected by earlier validation.

These tests deliberately exercise internal helpers with inconsistent state.
They are not examples of supported recipe input; public validation is checked
alongside them so future refactoring cannot silently remove either safeguard.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

from test_epub_blocks import PACKAGE, make_epub, minimal_recipe

from epub_blocks import (
    CompiledBlock,
    ContentOptions,
    EpubBlocksError,
    Fragment,
    MarkupOptions,
    SpineDocument,
    compile_recipe,
    inspect_epub,
)
from epub_blocks.recipe import (
    _attach_milestones,  # pyright: ignore[reportPrivateUsage]
    _capture_key,  # pyright: ignore[reportPrivateUsage]
    _EmissionSpec,  # pyright: ignore[reportPrivateUsage]
    _IdentifierAllocator,  # pyright: ignore[reportPrivateUsage]
    _IdentifierSpec,  # pyright: ignore[reportPrivateUsage]
)
from epub_blocks.xml import (
    _is_doctype_prefix,  # pyright: ignore[reportPrivateUsage]
    parse_xml,
)


class InternalInvariantTests(unittest.TestCase):
    def test_public_validation_rejects_invalid_capture_and_emission_state(self) -> None:
        for role in ("fixed", "line-start", "line"):
            recipe = minimal_recipe()
            output = cast(dict[str, object], recipe["output"])
            output["default"] = {"type": "p", "role": role}
            output["identifiers"] = {"block": {"template": "{number}"}}
            message = (
                "required when role is 'fixed'"
                if role == "fixed"
                else "required by a line role"
            )
            with (
                self.subTest(role=role),
                self.assertRaisesRegex(EpubBlocksError, message),
            ):
                # Syntax rejection must happen before attempting to open a file.
                compile_recipe("nonexistent.epub", recipe, verify_digest=False)
        recipe = minimal_recipe()
        cast(dict[str, object], recipe["output"])["groups"] = {
            "source_pattern": "(.*)",
            "capture_kind": "hexadecimal",
        }
        with self.assertRaisesRegex(EpubBlocksError, "capture_kind: must be"):
            compile_recipe("nonexistent.epub", recipe, verify_digest=False)

    def test_internal_capture_kind_cannot_silently_fall_back(self) -> None:
        with self.assertRaisesRegex(
            AssertionError, "validated capture kind is unknown"
        ):
            _capture_key("10", "hexadecimal", "test group")
        self.assertEqual(_capture_key("10", "decimal", "test group"), "10")

    def test_invalid_allocator_state_does_not_consume_an_identifier(self) -> None:
        spec = _IdentifierSpec("{number:03d}", 1, None, 1, None)
        for role in ("fixed", "line-start", "line"):
            allocator = _IdentifierAllocator(spec)
            message = (
                "fixed emission validated with an identifier"
                if role == "fixed"
                else "line template required during parsing"
            )
            with self.subTest(role=role):
                with self.assertRaisesRegex(AssertionError, message):
                    allocator.allocate(
                        _EmissionSpec("p", role, None),
                        "chapter",
                        "test emission",
                        element_path="1",
                    )
                self.assertEqual(
                    allocator.allocate(
                        _EmissionSpec("p", "block", None),
                        "chapter",
                        "test emission",
                        element_path="1",
                    ),
                    "001",
                )

    def test_manifest_href_lost_after_validation_fails_closed(self) -> None:
        package = parse_xml(PACKAGE.encode("utf-8"), "content.opf")
        namespace = "{http://www.idpf.org/2007/opf}"
        metadata = package.find(namespace + "metadata")
        manifest = package.find(namespace + "manifest")
        spine = package.find(namespace + "spine")
        assert metadata is not None and manifest is not None and spine is not None
        item = manifest.find(namespace + "item")
        assert item is not None
        href_reads = 0

        def item_attribute(name: str, default: str | None = None) -> str | None:
            nonlocal href_reads
            if name == "href":
                href_reads += 1
                if href_reads > 1:
                    return None
            return item.get(name, default)

        # Fault injection models an inconsistent validated DOM. Ordinary EPUB
        # parsing cannot remove href between validation and spine construction.
        changing_item = Mock(wraps=item)
        changing_item.get.side_effect = item_attribute
        changing_manifest = Mock(wraps=manifest)
        changing_manifest.findall.return_value = [changing_item]
        with tempfile.TemporaryDirectory() as directory:
            epub = make_epub(Path(directory))
            with (
                patch(
                    "epub_blocks.package._single_child",
                    side_effect=[metadata, changing_manifest, spine],
                ),
                self.assertRaisesRegex(
                    AssertionError, "validated manifest href is missing"
                ),
            ):
                inspect_epub(epub)
            self.assertEqual(href_reads, 2)
            # The failure must not alter the source or poison the next read.
            self.assertEqual(inspect_epub(epub).spine[0].path, "text/chapter.xhtml")

    def test_detached_markers_require_markup_configuration_and_in_scope_owners(
        self,
    ) -> None:
        block = CompiledBlock("001", "p", (Fragment("aux.xhtml", "1"),))
        event = Fragment("chapter.xhtml", "1")
        document = SpineDocument(1, "chapter.xhtml", frozenset())
        cases = (
            (ContentOptions(), "require markup configuration"),
            (
                ContentOptions(markup=MarkupOptions()),
                "require output fragments from selected source documents",
            ),
        )
        for content, message in cases:
            with (
                self.subTest(content=content),
                self.assertRaisesRegex(EpubBlocksError, message),
            ):
                _attach_milestones((block,), (event,), (document,), content)
            self.assertEqual(block.milestones, ())
            self.assertEqual(block.milestones_after, ())
        self.assertEqual(
            _attach_milestones((block,), (), (document,), ContentOptions()),
            (block,),
        )

    def test_unterminated_prolog_tokens_are_not_valid_doctype_prefixes(self) -> None:
        for prefix in (b"<!--unfinished", b"<?unfinished", b" \n<!--ok--><?unfinished"):
            with self.subTest(prefix=prefix):
                self.assertFalse(_is_doctype_prefix(prefix))
                with self.assertRaisesRegex(EpubBlocksError, "malformed XML"):
                    parse_xml(prefix + b"<!DOCTYPE html><html/>", "sample.xhtml")
        valid = b"<?xml version='1.0'?><!--closed--> \n"
        self.assertTrue(_is_doctype_prefix(valid))
        self.assertEqual(
            parse_xml(valid + b"<!DOCTYPE html><html/>", "sample.xhtml").tag, "html"
        )
