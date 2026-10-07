from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import cast
from unittest.mock import patch

from jsonschema import (  # pyright: ignore[reportMissingModuleSource]
    Draft202012Validator,
)
from test_epub_blocks import AUXILIARY, finalize_recipe, make_epub, minimal_recipe

from epub_blocks import (
    EpubBlocksError,
    Erratum,
    compile_recipe,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_candidates,
)
from epub_blocks.cli import main


class ErrataTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.schema = Draft202012Validator(
            json.loads(
                files("epub_blocks")
                .joinpath("schemas/recipe-v1.schema.json")
                .read_text()
            )
        )

    def sample(
        self, body: str = "<p>Old word.</p><p>Old word.</p>"
    ) -> tuple[Path, dict[str, object]]:
        epub = make_epub(
            self.directory,
            documents={
                "text/chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                + body
                + "</body></html>",
                "text/aux.xhtml": AUXILIARY,
            },
        )
        return epub, minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )

    def test_ordered_scoped_literal_edits_preserve_source_and_candidates(self) -> None:
        epub, recipe = self.sample()
        original = epub.read_bytes()
        recipe["errata"] = [
            {"id": "001", "find": "Old", "replace": "New"},
            {"id": "001", "find": "New word.", "replace": "“New” \\1 [word]."},
        ]
        compiled = finalize_recipe(epub, recipe)
        self.assertEqual(compiled.errata[0], Erratum("001", "Old", "New"))
        self.assertEqual(
            [b.text for b in extract_recipe(epub, recipe)],
            ["“New” \\1 [word].", "Old word."],
        )
        self.assertEqual(
            [b.text for b in extract_recipe_candidates(epub, recipe)],
            ["Old word.", "Old word."],
        )
        self.assertEqual(epub.read_bytes(), original)
        self.assertTrue(self.schema.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        self.assertEqual(extract_recipe(epub, recipe), extract_recipe(epub, recipe))

    def test_edits_follow_joining_and_normalization_without_renormalizing(self) -> None:
        epub, recipe = self.sample("<p>  Café </p><p> word. </p>")
        output = cast(dict[str, object], recipe["output"])
        output["rules"] = [
            {
                "match": {"locators": ["text/chapter.xhtml#1"]},
                "consume": 2,
                "separator": " ",
                "type": "paragraph",
            }
        ]
        recipe["errata"] = [
            {"id": "001", "find": "Café word", "replace": " Café  word "}
        ]
        finalize_recipe(epub, recipe)
        self.assertEqual(extract_recipe(epub, recipe)[0].text, " Café  word .")

    def test_empty_replacement_and_explicitly_allowed_empty_output(self) -> None:
        epub, recipe = self.sample("<p>Old word.</p>")
        recipe["errata"] = [{"id": "001", "find": "Old ", "replace": ""}]
        finalize_recipe(epub, recipe)
        self.assertEqual(extract_recipe(epub, recipe)[0].text, "word.")
        recipe["errata"] = [{"id": "001", "find": "Old word.", "replace": ""}]
        finalize_recipe(epub, recipe)
        with self.assertRaisesRegex(EpubBlocksError, "correction produced no text"):
            extract_recipe(epub, recipe)
        output = cast(dict[str, object], recipe["output"])
        cast(dict[str, object], output["default"])["allow_empty"] = True
        finalize_recipe(epub, recipe)
        self.assertEqual(extract_recipe(epub, recipe)[0].text, "")

    def test_missing_repeated_and_overlapping_matches_fail_closed(self) -> None:
        epub, recipe = self.sample("<p>aaa Old Old</p>")
        for find, message in [
            ("old", "found 0"),
            ("missing", "found 0"),
            ("Old", "multiple"),
            ("aa", "multiple"),
        ]:
            with self.subTest(find=find):
                recipe["errata"] = [{"id": "001", "find": find, "replace": "new"}]
                finalize_recipe(epub, recipe)
                with self.assertRaisesRegex(EpubBlocksError, message):
                    extract_recipe(epub, recipe)

    def test_unknown_generated_id_rejected_at_compilation(self) -> None:
        epub, recipe = self.sample()
        recipe["errata"] = [{"id": "999", "find": "Old", "replace": "New"}]
        with self.assertRaisesRegex(EpubBlocksError, "unknown block ID '999'"):
            finalize_recipe(epub, recipe)

    def test_digest_pins_edits_and_order_but_empty_preserves_existing_pin(self) -> None:
        epub, recipe = self.sample()
        before = compiled_recipe_digest(finalize_recipe(epub, recipe))
        recipe["errata"] = []
        self.assertEqual(compiled_recipe_digest(finalize_recipe(epub, recipe)), before)
        edits = [
            {"id": "001", "find": "Old", "replace": "New"},
            {"id": "002", "find": "Old", "replace": "New"},
        ]
        recipe["errata"] = edits
        with self.assertRaisesRegex(EpubBlocksError, "compiled SHA-256 does not match"):
            extract_recipe(epub, recipe)
        corrected = compiled_recipe_digest(finalize_recipe(epub, recipe))
        self.assertNotEqual(before, corrected)
        recipe["errata"] = list(reversed(edits))
        self.assertNotEqual(
            corrected, compiled_recipe_digest(finalize_recipe(epub, recipe))
        )
        edits[0]["replace"] = "Newest"
        recipe["errata"] = edits
        self.assertNotEqual(
            corrected, compiled_recipe_digest(finalize_recipe(epub, recipe))
        )

    def test_invalid_members_and_types_match_schema(self) -> None:
        epub, recipe = self.sample()
        value: dict[str, object] = {"id": "001", "find": "Old", "replace": "New"}
        bad_values: list[object] = [None, {}, "errata", [None]]
        for field in value:
            for bad in cast(list[object], [None, 1, True, [], {}, "\t", "x\r", "x\n"]):
                bad_values.append([dict(value, **{field: bad})])
            missing = dict(value)
            del missing[field]
            bad_values.append([missing])
        bad_values.extend(
            [[dict(value, id="")], [dict(value, find="")], [dict(value, unknown=1)]]
        )
        for bad in bad_values:
            with self.subTest(bad=bad):
                candidate = copy.deepcopy(recipe)
                candidate["errata"] = bad
                self.assertFalse(self.schema.is_valid(candidate))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                with self.assertRaises(EpubBlocksError):
                    compile_recipe(epub, candidate, verify_digest=False)

    def test_surrogates_and_no_op_edits_rejected(self) -> None:
        epub, recipe = self.sample()
        value = {"id": "001", "find": "Old", "replace": "New"}
        for field in value:
            recipe["errata"] = [dict(value, **{field: "\ud800"})]
            with self.assertRaisesRegex(EpubBlocksError, "valid UTF-8"):
                finalize_recipe(epub, recipe)
        recipe["errata"] = [dict(value, replace="Old")]
        with self.assertRaisesRegex(EpubBlocksError, "must differ"):
            finalize_recipe(epub, recipe)

    def test_markup_rejected_even_when_edits_appear_safe(self) -> None:
        epub, recipe = self.sample()
        for format_name in ["xml", "delimiters", "literal"]:
            recipe["text"] = {"markup": {"format": format_name, "rules": []}}
            recipe["errata"] = []
            original = finalize_recipe(epub, recipe)
            recipe["errata"] = [{"id": "001", "find": "Old", "replace": "New"}]
            with self.assertRaisesRegex(EpubBlocksError, "plain-text output"):
                finalize_recipe(epub, recipe)
            with self.assertRaisesRegex(EpubBlocksError, "plain-text output"):
                compiled_recipe_digest(
                    replace(original, errata=(Erratum("001", "Old", "New"),))
                )

    def test_programmatically_constructed_models_validated(self) -> None:
        epub, recipe = self.sample()
        compiled = finalize_recipe(epub, recipe)
        for bad in cast(list[object], [[], (None,), (Erratum("001", "", "x"),)]):
            with self.assertRaises(EpubBlocksError):
                compiled_recipe_digest(
                    replace(compiled, errata=cast(tuple[Erratum, ...], bad))
                )

    def test_cli_failure_keeps_existing_output_and_success_is_literal_tsv(self) -> None:
        epub, recipe = self.sample()
        recipe_path = self.directory / "recipe.json"
        output = self.directory / "output.tsv"
        output.write_text("KEEP ME")
        recipe["errata"] = [{"id": "001", "find": "missing", "replace": "“New”"}]
        finalize_recipe(epub, recipe)
        recipe_path.write_text(json.dumps(recipe))
        argv = ["epub-blocks", str(epub), str(recipe_path), str(output)]
        stderr = io.StringIO()
        with patch("sys.argv", argv), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(), 2)
        self.assertEqual(output.read_text(), "KEEP ME")
        self.assertIn("errata[1]", stderr.getvalue())
        recipe["errata"] = [{"id": "001", "find": "Old", "replace": "“New”"}]
        finalize_recipe(epub, recipe)
        recipe_path.write_text(json.dumps(recipe))
        with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertEqual(
            output.read_text(),
            "001\tparagraph\t“New” word.\n002\tparagraph\tOld word.\n",
        )
