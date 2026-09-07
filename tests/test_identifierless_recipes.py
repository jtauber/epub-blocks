from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import cast
from unittest.mock import patch

from jsonschema import (  # pyright: ignore[reportMissingModuleSource]
    Draft202012Validator,
)
from test_epub_blocks import PACKAGE, finalize_recipe, make_epub, minimal_recipe

from epub_blocks import (
    EpubBlocksError,
    compile_recipe,
    compile_recipe_file,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_candidates,
    extract_recipe_file,
    inspect_epub,
)
from epub_blocks.cli import main

IDENTIFIER = '<dc:identifier id="pub-id">sample-edition</dc:identifier>'
EMPTY_IDENTIFIER = '<dc:identifier id="pub-id"/>'
Operation = Callable[[Path, dict[str, object]], object]
OPERATIONS: tuple[Operation, ...] = (
    compile_recipe,
    lambda path, recipe: compile_recipe(path, recipe, verify_digest=False),
    extract_recipe,
    extract_recipe_candidates,
)


class IdentifierlessRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        schema = json.loads(
            files("epub_blocks")
            .joinpath("schemas/recipe-v1.schema.json")
            .read_text(encoding="utf-8")
        )
        self.validator = Draft202012Validator(schema)

    def schema_valid(self, recipe: dict[str, object]) -> bool:
        return self.validator.is_valid(recipe)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]

    def sample(
        self, identifier_markup: str = EMPTY_IDENTIFIER
    ) -> tuple[Path, dict[str, object]]:
        epub = make_epub(
            self.directory,
            package=PACKAGE.replace(IDENTIFIER, identifier_markup),
            documents={
                "text/chapter.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                    "<h1>A heading</h1><p>A <em>word</em>.</p></body></html>"
                )
            },
        )
        recipe = minimal_recipe(
            epub={"sha256": hashlib.sha256(epub.read_bytes()).hexdigest()},
            text={
                "markup": {
                    "format": "xml",
                    "rules": [{"match": {"tag": "em"}, "kind": "span", "name": "em"}],
                    "delimiters": {"em": ["⧼", "⧽"]},
                }
            },
        )
        return epub, recipe

    def test_absent_empty_and_whitespace_package_ids_allow_omission(self) -> None:
        for metadata in (
            "",
            EMPTY_IDENTIFIER,
            '<dc:identifier id="pub-id"> \t\n\u00a0 </dc:identifier>',
            EMPTY_IDENTIFIER + '<dc:identifier id="second"> </dc:identifier>',
        ):
            for mode, text in (
                ("xml", "A <em>word</em>."),
                ("delimiters", "A ⧼word⧽."),
            ):
                with self.subTest(metadata=metadata, mode=mode):
                    epub, recipe = self.sample(metadata)
                    markup = cast(
                        dict[str, object],
                        cast(dict[str, object], recipe["text"])["markup"],
                    )
                    markup["format"] = mode
                    self.assertFalse(inspect_epub(epub).identifiers)
                    self.assertTrue(self.schema_valid(recipe))
                    compiled = finalize_recipe(epub, recipe)
                    self.assertIsNone(compiled.epub_identifier)
                    self.assertEqual(compile_recipe(epub, recipe), compiled)
                    rows = extract_recipe(epub, recipe)
                    self.assertEqual([r.block_id for r in rows], ["001", "002"])
                    self.assertEqual([r.text for r in rows], ["A heading", text])
                    self.assertEqual(
                        [r.text for r in extract_recipe_candidates(epub, recipe)],
                        ["A heading", "A word."],
                    )

    def test_existing_package_identifiers_still_require_a_pin_everywhere(self) -> None:
        # An empty designated primary ID does not hide a usable secondary ID.
        for metadata in (
            IDENTIFIER,
            EMPTY_IDENTIFIER + '<dc:identifier id="second">secondary</dc:identifier>',
        ):
            epub, recipe = self.sample(metadata)
            self.assertTrue(self.schema_valid(recipe))
            for operation in OPERATIONS:
                with (
                    self.subTest(metadata=metadata, operation=operation),
                    self.assertRaisesRegex(EpubBlocksError, "identifier is required"),
                ):
                    operation(epub, recipe)

    def test_supplied_identifier_must_match_without_fallback(self) -> None:
        for metadata in (EMPTY_IDENTIFIER, IDENTIFIER):
            for value in ("incorrect", " ", "forged-identifier"):
                epub, recipe = self.sample(metadata)
                cast(dict[str, object], recipe["epub"])["identifier"] = value
                for operation in OPERATIONS:
                    with (
                        self.subTest(
                            metadata=metadata, value=value, operation=operation
                        ),
                        self.assertRaisesRegex(
                            EpubBlocksError, "expected package identifier"
                        ),
                    ):
                        operation(epub, recipe)

    def test_matching_secondary_id_and_source_whitespace_still_work(self) -> None:
        epub, recipe = self.sample(
            EMPTY_IDENTIFIER
            + '<dc:identifier id="second">  secondary\n</dc:identifier>'
        )
        cast(dict[str, object], recipe["epub"])["identifier"] = "secondary"
        compiled = finalize_recipe(epub, recipe)
        self.assertEqual(compiled.epub_identifier, "secondary")
        self.assertEqual(len(extract_recipe(epub, recipe)), 2)

    def test_explicit_null_empty_and_nonstring_ids_are_not_omission(self) -> None:
        invalid: tuple[object, ...] = (None, "", 0, True, [], {})
        for value in invalid:
            epub, recipe = self.sample()
            cast(dict[str, object], recipe["epub"])["identifier"] = value
            self.assertFalse(self.schema_valid(recipe))
            for operation in OPERATIONS:
                with (
                    self.subTest(value=value, operation=operation),
                    self.assertRaisesRegex(EpubBlocksError, "epub.identifier"),
                ):
                    operation(epub, recipe)

    def test_source_hash_is_still_required_and_strictly_validated(self) -> None:
        for value in (None, "", "f" * 63, "F" * 64, False):
            epub, recipe = self.sample()
            cast(dict[str, object], recipe["epub"])["sha256"] = value
            self.assertFalse(self.schema_valid(recipe))
            with self.assertRaisesRegex(EpubBlocksError, "epub.sha256"):
                compile_recipe(epub, recipe, verify_digest=False)
        epub, recipe = self.sample()
        cast(dict[str, object], recipe["epub"]).pop("sha256")
        self.assertFalse(self.schema_valid(recipe))
        with self.assertRaisesRegex(EpubBlocksError, "epub.sha256"):
            extract_recipe_candidates(epub, recipe)

    def test_incorrect_source_hash_fails_all_entry_points(self) -> None:
        epub, recipe = self.sample()
        finalize_recipe(epub, recipe)
        cast(dict[str, object], recipe["epub"])["sha256"] = "0" * 64
        for operation in OPERATIONS:
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(EpubBlocksError, "source SHA-256"),
            ):
                operation(epub, recipe)

    def test_another_identifierless_archive_is_not_interchangeable(self) -> None:
        epub, recipe = self.sample()
        finalize_recipe(epub, recipe)
        different = make_epub(
            self.directory,
            filename="another.epub",
            package=PACKAGE.replace(IDENTIFIER, EMPTY_IDENTIFIER),
        )
        self.assertFalse(inspect_epub(different).identifiers)
        for operation in OPERATIONS:
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(EpubBlocksError, "source SHA-256"),
            ):
                operation(different, recipe)

    def test_compiled_pin_remains_required_and_inspection_still_skips_it(self) -> None:
        epub, recipe = self.sample()
        finalize_recipe(epub, recipe)
        output = cast(dict[str, object], recipe["output"])
        output["compiled_sha256"] = "0" * 64
        for operation in (compile_recipe, extract_recipe):
            with self.assertRaisesRegex(EpubBlocksError, "compiled SHA-256"):
                operation(epub, recipe)
        self.assertEqual(len(extract_recipe_candidates(epub, recipe)), 2)
        output.pop("compiled_sha256")
        with self.assertRaisesRegex(EpubBlocksError, "compiled_sha256"):
            extract_recipe(epub, recipe)
        self.assertIsNone(
            compile_recipe(epub, recipe, verify_digest=False).epub_identifier
        )
        self.assertEqual(len(extract_recipe_candidates(epub, recipe)), 2)

    def test_archive_safety_is_not_disabled_by_hash_only_pinning(self) -> None:
        epub = self.directory / "invalid.epub"
        epub.write_bytes(b"not a ZIP")
        recipe = minimal_recipe(
            epub={"sha256": hashlib.sha256(epub.read_bytes()).hexdigest()}
        )
        for operation in OPERATIONS:
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(EpubBlocksError, "valid ZIP"),
            ):
                operation(epub, recipe)

    def test_source_pin_does_not_change_structural_digest_contract(self) -> None:
        epub, recipe = self.sample()
        compiled = finalize_recipe(epub, recipe)
        with_identity = replace(
            compiled, epub_identifier="a-package-id", epub_sha256="a" * 64
        )
        self.assertEqual(
            compiled_recipe_digest(compiled), compiled_recipe_digest(with_identity)
        )

    def test_file_apis_and_cli_preserve_output_on_pin_failure(self) -> None:
        epub, recipe = self.sample()
        compiled = finalize_recipe(epub, recipe)
        recipe_path, output_path = (
            self.directory / "recipe.json",
            self.directory / "out.tsv",
        )
        recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
        self.assertEqual(compile_recipe_file(epub, recipe_path), compiled)
        self.assertEqual(len(extract_recipe_file(epub, recipe_path)), 2)
        argv = ["epub-blocks", str(epub), str(recipe_path), str(output_path)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        saved = output_path.read_bytes()
        self.assertEqual(
            saved, b"001\tparagraph\tA heading\n002\tparagraph\tA <em>word</em>.\n"
        )
        for section, member, value in (
            ("epub", "identifier", "invented"),
            ("epub", "sha256", "0" * 64),
            ("output", "compiled_sha256", "0" * 64),
        ):
            invalid = copy.deepcopy(recipe)
            cast(dict[str, object], invalid[section])[member] = value
            recipe_path.write_text(json.dumps(invalid), encoding="utf-8")
            with (
                patch.object(sys, "argv", argv),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(), 2)
            self.assertEqual(output_path.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
