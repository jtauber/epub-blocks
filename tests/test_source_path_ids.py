from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from typing import cast

from jsonschema import (  # pyright: ignore[reportMissingModuleSource]
    Draft202012Validator,
)
from test_epub_blocks import AUXILIARY, finalize_recipe, make_epub, minimal_recipe

from epub_blocks import EpubBlocksError, compile_recipe, extract_recipe


class SourcePathIdTests(unittest.TestCase):
    def sample(self, body: str) -> tuple[Path, dict[str, object], dict[str, object]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        epub = make_epub(
            Path(temporary.name),
            documents={
                "text/chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml">'
                f"<body>{body}</body></html>",
                "text/aux.xhtml": AUXILIARY,
            },
        )
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )
        output = cast(dict[str, object], recipe["output"])
        output["groups"] = {"source_pattern": "(chapter)"}
        output["default"] = {
            "type": "paragraph",
            "role": "fixed",
            "id": "{group}.{element_path}",
        }
        return epub, recipe, output

    def ids(self, epub: Path, recipe: dict[str, object]) -> list[str]:
        finalize_recipe(epub, recipe)
        return [row.block_id for row in extract_recipe(epub, recipe)]

    def test_nested_paths_and_skips_do_not_renumber_sources(self) -> None:
        epub, recipe, output = self.sample(
            "<div><p>A.</p><p>Skipped.</p><div><p>B.</p></div></div>"
        )
        output["skip_source"] = ["text/chapter.xhtml#1.2"]
        self.assertEqual(self.ids(epub, recipe), ["chapter.1.1", "chapter.1.3.1"])

    def test_join_uses_first_emitted_not_first_consumed_path(self) -> None:
        epub, recipe, output = self.sample("<p>Drop.</p><p>A.</p><p>B.</p>")
        output["rules"] = [
            {
                "match": {"locators": ["text/chapter.xhtml#1"]},
                "type": "joined",
                "role": "fixed",
                "id": "{element_path}",
                "consume": 3,
                "emit": [2, 3],
            }
        ]
        self.assertEqual(self.ids(epub, recipe), ["2"])
        self.assertEqual(extract_recipe(epub, recipe)[0].text, "A. B.")

    def test_replacement_uses_its_first_part_not_anchor(self) -> None:
        epub, recipe, output = self.sample("<p>A.</p><p>B.</p>")
        output["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "joined",
                        "role": "fixed",
                        "id": "{group}.{element_path}",
                        "parts": [
                            {"document": "text/chapter.xhtml", "element_path": "2"},
                            {"document": "text/chapter.xhtml", "element_path": "1"},
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(self.ids(epub, recipe), ["chapter.2"])

    def test_inserted_note_path_is_its_own_fragment_and_keeps_anchor_group(
        self,
    ) -> None:
        epub, recipe, output = self.sample("<p>A.</p>")
        output["insertions"] = [
            {
                "after": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "note",
                        "role": "fixed",
                        "id": "{group}.note.{element_path}",
                        "parts": [{"document": "text/aux.xhtml", "element_path": "1"}],
                    }
                ],
            }
        ]
        self.assertEqual(self.ids(epub, recipe), ["chapter.1", "chapter.note.1"])

    def test_splits_need_explicit_unique_suffixes(self) -> None:
        epub, recipe, output = self.sample("<p>AB</p>")
        produced: list[dict[str, object]] = [
            {
                "type": "part",
                "role": "fixed",
                "id": "{element_path}",
                "parts": [
                    {
                        "document": "text/chapter.xhtml",
                        "element_path": "1",
                        "slice": {"start": i, "end": i + 1},
                    }
                ],
            }
            for i in range(2)
        ]
        output["replacements"] = [
            {"anchor": "text/chapter.xhtml#1", "outputs": produced}
        ]
        with self.assertRaisesRegex(EpubBlocksError, "duplicate generated identifier"):
            compile_recipe(epub, recipe, verify_digest=False)
        produced[0]["id"] = "{element_path}.a"
        produced[1]["id"] = "{element_path}.b"
        self.assertEqual(self.ids(epub, recipe), ["1.a", "1.b"])

    def test_empty_source_blocks_keep_structural_paths(self) -> None:
        epub, recipe, output = self.sample("<div><p/><p>A.</p></div>")
        recipe["source_blocks"] = {
            "include_documents": ["text/chapter.xhtml"],
            "element_rules": [
                {"match": {"tag": "p"}, "action": "block", "keep_empty": True}
            ],
        }
        cast(dict[str, object], output["default"])["allow_empty"] = True
        self.assertEqual(self.ids(epub, recipe), ["chapter.1.1", "chapter.1.2"])

    def test_equivalent_literal_ids_keep_digest(self) -> None:
        epub, recipe, output = self.sample("<p>A.</p>")
        first = compile_recipe(epub, recipe, verify_digest=False)
        cast(dict[str, object], output["default"])["id"] = "{group}.1"
        second = compile_recipe(epub, recipe, verify_digest=False)
        self.assertEqual(first, second)

    def test_counter_templates_still_reject_source_paths(self) -> None:
        epub, recipe, output = self.sample("<p>A.</p>")
        output["identifiers"] = {"block": {"template": "{element_path}.{number}"}}
        with self.assertRaisesRegex(EpubBlocksError, "unsupported field"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_unsafe_and_invalid_field_formats_remain_rejected(self) -> None:
        for template in (
            "{element_path.__class__}",
            "{element_path[0]}",
            "{element_path!r}",
            "{element_path:{group}}",
            "{element_path:03d}",
        ):
            with self.subTest(template=template):
                epub, recipe, output = self.sample("<p>A.</p>")
                cast(dict[str, object], output["default"])["id"] = template
                with self.assertRaises(EpubBlocksError):
                    compile_recipe(epub, recipe, verify_digest=False)

    @staticmethod
    def set_root(output: dict[str, object], root: object = "1.1") -> None:
        cast(dict[str, object], output["identifiers"])["element_path_root"] = root

    def test_relative_paths_keep_full_source_provenance_and_skips(self) -> None:
        epub, recipe, output = self.sample(
            "<div><div><p>A.</p><p>Skip.</p><div><p>B.</p></div></div></div>"
        )
        self.set_root(output)
        output["skip_source"] = ["text/chapter.xhtml#1.1.2"]
        self.assertEqual(self.ids(epub, recipe), ["chapter.1", "chapter.3.1"])
        compiled = compile_recipe(epub, recipe)
        self.assertEqual(
            [b.parts[0].element_path for b in compiled.blocks], ["1.1.1", "1.1.3.1"]
        )

    def test_root_is_component_aligned_and_strictly_above_fragment(self) -> None:
        for root in ("1", "1.1", "1.10", "2"):
            with self.subTest(root=root):
                epub, recipe, output = self.sample("<p>A.</p>")
                self.set_root(output, root)
                with self.assertRaisesRegex(
                    EpubBlocksError, "not below element_path_root"
                ):
                    compile_recipe(epub, recipe, verify_digest=False)
        epub, recipe, output = self.sample("<div>" + "<p>A.</p>" * 10 + "</div>")
        self.set_root(output)
        output["skip_source"] = [f"text/chapter.xhtml#1.{i}" for i in range(1, 10)]
        with self.assertRaisesRegex(EpubBlocksError, "'1.10' is not below"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_root_applies_to_first_emitted_join_and_replacement_fragments(self) -> None:
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                epub, recipe, output = self.sample(
                    "<div><div><p>A.</p><p>B.</p></div></div>"
                )
                self.set_root(output)
                if replacement:
                    output["replacements"] = [
                        {
                            "anchor": "text/chapter.xhtml#1.1.1",
                            "outputs": [
                                {
                                    "type": "joined",
                                    "role": "fixed",
                                    "id": "{group}.{element_path}",
                                    "parts": [
                                        {
                                            "document": "text/chapter.xhtml",
                                            "element_path": "1.1.2",
                                        },
                                        {
                                            "document": "text/chapter.xhtml",
                                            "element_path": "1.1.1",
                                        },
                                    ],
                                }
                            ],
                        }
                    ]
                else:
                    output["rules"] = [
                        {
                            "match": {"locators": ["text/chapter.xhtml#1.1.1"]},
                            "type": "joined",
                            "role": "fixed",
                            "id": "{group}.{element_path}",
                            "consume": 2,
                            "emit": [2],
                        }
                    ]
                self.assertEqual(self.ids(epub, recipe), ["chapter.2"])

    def test_literal_inserted_note_ids_do_not_rebase_or_require_root(self) -> None:
        epub, recipe, output = self.sample("<div><div><p>A.</p></div></div>")
        self.set_root(output)
        output["insertions"] = [
            {
                "after": "text/chapter.xhtml#1.1.1",
                "outputs": [
                    {
                        "type": "note",
                        "role": "fixed",
                        "id": "{group}.1.fn1",
                        "parts": [{"document": "text/aux.xhtml", "element_path": "1"}],
                    }
                ],
            }
        ]
        self.assertEqual(self.ids(epub, recipe), ["chapter.1", "chapter.1.fn1"])
        insertion = cast(list[dict[str, object]], output["insertions"])[0]
        note = cast(list[dict[str, object]], insertion["outputs"])[0]
        note["id"] = "{group}.note.{element_path}"
        with self.assertRaisesRegex(EpubBlocksError, "not below element_path_root"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_relative_inserted_note_uses_own_path_not_anchor(self) -> None:
        epub, recipe, output = self.sample("<div><p>A.</p><p>Note.</p></div>")
        self.set_root(output, "1")
        output["insertions"] = [
            {
                "after": "text/chapter.xhtml#1.1",
                "outputs": [
                    {
                        "type": "note",
                        "role": "fixed",
                        "id": "{group}.note.{element_path}",
                        "parts": [
                            {"document": "text/chapter.xhtml", "element_path": "1.2"}
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(
            self.ids(epub, recipe), ["chapter.1", "chapter.note.2", "chapter.2"]
        )

    def test_root_does_not_change_counters_or_escaped_literal_fields(self) -> None:
        for emission, expected in (
            ({"type": "p", "role": "block"}, "001"),
            (
                {"type": "p", "role": "fixed", "id": "{{element_path}}"},
                "{element_path}",
            ),
        ):
            with self.subTest(emission=emission):
                epub, recipe, output = self.sample("<p>A.</p>")
                output["default"] = emission
                baseline = compile_recipe(epub, recipe, verify_digest=False)
                self.set_root(output)
                self.assertEqual(self.ids(epub, recipe), [expected])
                self.assertEqual(compile_recipe(epub, recipe), baseline)

    def test_root_changes_digest_when_it_changes_ids(self) -> None:
        epub, recipe, output = self.sample("<div><div><p>A.</p></div></div>")
        baseline = compile_recipe(epub, recipe, verify_digest=False)
        self.set_root(output)
        changed = compile_recipe(epub, recipe, verify_digest=False)
        self.assertNotEqual(baseline, changed)
        cast(dict[str, object], output["default"])["id"] = "{group}.1"
        self.assertEqual(compile_recipe(epub, recipe, verify_digest=False), changed)

    def test_root_schema_and_runtime_validation(self) -> None:
        schema = json.loads(
            files("epub_blocks").joinpath("schemas/recipe-v1.schema.json").read_text()
        )
        validator = Draft202012Validator(schema)
        invalid_roots: tuple[object, ...] = (
            None,
            False,
            1,
            [],
            {},
            "",
            "0",
            "01",
            "1.0",
            "1.",
            ".1",
            "1..2",
            "-1",
            "1\n",
        )
        for root in invalid_roots:
            with self.subTest(root=root):
                epub, recipe, output = self.sample("<div><p>A.</p></div>")
                finalize_recipe(epub, recipe)
                self.set_root(output, root)
                self.assertFalse(validator.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                with self.assertRaises(EpubBlocksError):
                    compile_recipe(epub, recipe, verify_digest=False)
        epub, recipe, output = self.sample("<div><div><p>A.</p></div></div>")
        self.set_root(output)
        finalize_recipe(epub, recipe)
        self.assertTrue(validator.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
