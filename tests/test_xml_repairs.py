from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import cast

from jsonschema import (  # pyright: ignore[reportMissingModuleSource]
    Draft202012Validator,
)
from test_epub_blocks import AUXILIARY, finalize_recipe, make_epub, minimal_recipe

from epub_blocks import (
    EpubBlocksError,
    SafetyLimits,
    XmlRepair,
    compile_recipe,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_candidates,
)


class XmlRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.schema = Draft202012Validator(
            json.loads(
                files("epub_blocks")
                .joinpath("schemas/recipe-v1.schema.json")
                .read_text()
            )
        )

    def sample(
        self, body: str = "<p>First.<p><p>Second.</p>"
    ) -> tuple[Path, dict[str, object], bytes]:
        source = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            + body
            + "</body></html>"
        )
        epub = make_epub(
            self.directory,
            documents={"text/chapter.xhtml": source, "text/aux.xhtml": AUXILIARY},
        )
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )
        recipe["source_blocks"] = {
            "include_documents": ["text/chapter.xhtml"],
            "strict_coverage": True,
        }
        return epub, recipe, source.encode()

    def repair(
        self,
        recipe: dict[str, object],
        data: bytes,
        expected: str = "<p><p>",
        replacement: str = "</p><p>",
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "document": "text/chapter.xhtml",
            "offset": data.index(expected.encode()),
            "expected": expected,
            "replacement": replacement,
        }
        recipe["xml_repairs"] = [value]
        return value

    def test_explicit_guarded_repair_retains_readings_and_original_archive(
        self,
    ) -> None:
        epub, recipe, data = self.sample()
        original = epub.read_bytes()
        with self.assertRaisesRegex(EpubBlocksError, "malformed XML"):
            compile_recipe(epub, recipe, verify_digest=False)
        self.repair(recipe, data)
        self.assertTrue(self.schema.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        self.assertEqual(
            [b.text for b in extract_recipe_candidates(epub, recipe)],
            ["First.", "Second."],
        )
        finalize_recipe(epub, recipe)
        self.assertEqual(
            [b.text for b in extract_recipe(epub, recipe)], ["First.", "Second."]
        )
        self.assertEqual(epub.read_bytes(), original)

    def test_byte_offsets_and_order_independent_original_coordinates(self) -> None:
        epub, recipe, data = self.sample("<p>é.</p><bad>Second.</bad>")
        repairs = [
            {
                "document": "text/chapter.xhtml",
                "offset": data.index(b"<bad>"),
                "expected": "<bad>",
                "replacement": "<p>",
            },
            {
                "document": "text/chapter.xhtml",
                "offset": data.index(b"</bad>"),
                "expected": "</bad>",
                "replacement": "</p>",
            },
        ]
        recipe["xml_repairs"] = repairs
        first = compile_recipe(epub, recipe, verify_digest=False)
        recipe["xml_repairs"] = list(reversed(repairs))
        self.assertEqual(
            compiled_recipe_digest(first),
            compiled_recipe_digest(compile_recipe(epub, recipe, verify_digest=False)),
        )
        finalize_recipe(epub, recipe)
        self.assertEqual(
            [b.text for b in extract_recipe(epub, recipe)], ["é.", "Second."]
        )

    def test_absent_and_empty_repairs_keep_original_digest(self) -> None:
        epub, recipe, _ = self.sample("<p>Fine.</p>")
        before = compiled_recipe_digest(
            compile_recipe(epub, recipe, verify_digest=False)
        )
        recipe["xml_repairs"] = []
        self.assertEqual(
            before,
            compiled_recipe_digest(compile_recipe(epub, recipe, verify_digest=False)),
        )

    def test_policy_is_pinned_even_when_reading_and_structure_agree(self) -> None:
        epub, recipe, data = self.sample("<p>Fine.</p><!--x-->")
        before = compiled_recipe_digest(
            compile_recipe(epub, recipe, verify_digest=False)
        )
        self.repair(recipe, data, "<!--x-->", "")
        self.assertNotEqual(
            before,
            compiled_recipe_digest(compile_recipe(epub, recipe, verify_digest=False)),
        )

    def test_wrong_offset_and_expectation_fail_closed(self) -> None:
        epub, recipe, data = self.sample()
        value = self.repair(recipe, data)
        for offset in [0, len(data) + 100]:
            value["offset"] = offset
            with self.assertRaisesRegex(EpubBlocksError, "expectation failed"):
                compile_recipe(epub, recipe, verify_digest=False)

    def test_overlap_and_unused_document_rejected(self) -> None:
        epub, recipe, data = self.sample()
        value = self.repair(recipe, data)
        recipe["xml_repairs"] = [value, dict(value)]
        with self.assertRaisesRegex(EpubBlocksError, "must not overlap"):
            compile_recipe(epub, recipe, verify_digest=False)
        value["document"] = "text/aux.xhtml"
        recipe["xml_repairs"] = [value]
        epub, base, _ = self.sample("<p>Fine.</p>")
        base["xml_repairs"] = [value]
        with self.assertRaisesRegex(EpubBlocksError, "not used"):
            compile_recipe(epub, base, verify_digest=False)

    def test_invalid_json_fields_match_schema(self) -> None:
        epub, recipe, data = self.sample()
        value = self.repair(recipe, data)
        for field, bad in [
            ("offset", True),
            ("offset", -1),
            ("offset", "1"),
            ("expected", ""),
            ("replacement", None),
            ("document", "../x"),
            ("document", "/x"),
            ("document", "x//y"),
            ("document", "x/./y"),
            ("document", "x/"),
            ("document", "x\\y"),
            ("unknown", 0),
        ]:
            candidate = copy.deepcopy(recipe)
            candidate["xml_repairs"] = [dict(value, **{field: bad})]
            self.assertFalse(self.schema.is_valid(candidate), (field, bad))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
            with self.assertRaises(EpubBlocksError):
                compile_recipe(epub, candidate, verify_digest=False)
        for bad_array in cast(list[object], [None, {}, "repairs"]):
            recipe["xml_repairs"] = bad_array
            with self.assertRaisesRegex(EpubBlocksError, "must be an array"):
                compile_recipe(epub, recipe, verify_digest=False)

    def test_invalid_utf8_recipe_text_rejected(self) -> None:
        epub, recipe, data = self.sample()
        value = self.repair(recipe, data)
        value["replacement"] = "\ud800"
        with self.assertRaisesRegex(EpubBlocksError, "valid UTF-8"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_compiled_model_validation(self) -> None:
        epub, recipe, data = self.sample()
        self.repair(recipe, data)
        compiled = compile_recipe(epub, recipe, verify_digest=False)
        for bad in cast(
            list[object],
            [[], (None,), (XmlRepair("text/chapter.xhtml", True, "x", "y"),)],
        ):
            invalid = replace(compiled, xml_repairs=cast(tuple[XmlRepair, ...], bad))
            with self.assertRaises(EpubBlocksError):
                compiled_recipe_digest(invalid)

    def test_repaired_size_bound_and_normal_xml_security_still_apply(self) -> None:
        epub, recipe, data = self.sample("<p>Fine.</p>")
        value = self.repair(recipe, data, "Fine.", "x" * 5000)
        with self.assertRaisesRegex(EpubBlocksError, "exceeds XML size limit"):
            compile_recipe(
                epub,
                recipe,
                verify_digest=False,
                limits=SafetyLimits(max_xml_bytes=2000),
            )
        value["replacement"] = '<!DOCTYPE bad [<!ENTITY x "bad">]>'
        with self.assertRaises(EpubBlocksError):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_repair_cannot_shrink_an_oversized_source_past_the_input_limit(
        self,
    ) -> None:
        epub, recipe, data = self.sample("<p>" + "x" * 5000 + "</p>")
        self.repair(recipe, data, "x" * 5000, "Short.")
        original = epub.read_bytes()
        with self.assertRaisesRegex(EpubBlocksError, "too large before repair"):
            compile_recipe(
                epub,
                recipe,
                verify_digest=False,
                limits=SafetyLimits(max_xml_bytes=2000),
            )
        self.assertEqual(epub.read_bytes(), original)

    def test_non_utf8_source_is_readable_but_cannot_use_utf8_repairs(self) -> None:
        source = (
            '<?xml version="1.0" encoding="ISO-8859-1"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>caf\u00e9</p></body></html>'
        ).encode("latin-1")
        epub = make_epub(self.directory, documents={"text/chapter.xhtml": source})
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )
        self.assertEqual(
            [b.text for b in extract_recipe_candidates(epub, recipe)], ["café"]
        )
        self.repair(recipe, source, "<p>", '<p class="body">')
        original = epub.read_bytes()
        with self.assertRaisesRegex(EpubBlocksError, "XML repairs require UTF-8"):
            compile_recipe(epub, recipe, verify_digest=False)
        self.assertEqual(epub.read_bytes(), original)

    def test_source_hash_checked_before_repairs(self) -> None:
        epub, recipe, data = self.sample()
        self.repair(recipe, data)
        cast(dict[str, object], recipe["epub"])["sha256"] = "0" * 64
        with self.assertRaisesRegex(EpubBlocksError, "SHA-256"):
            compile_recipe(epub, recipe, verify_digest=False)


if __name__ == "__main__":
    unittest.main()
