from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from typing import cast
from unittest.mock import patch

from jsonschema import (  # pyright: ignore[reportMissingModuleSource]
    Draft202012Validator,
)
from test_epub_blocks import AUXILIARY, finalize_recipe, make_epub, minimal_recipe

from epub_blocks import EpubBlocksError, compile_recipe, extract_recipe
from epub_blocks.cli import main


class RecipeRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def make_recipe(self, body: str) -> tuple[Path, dict[str, object]]:
        epub = make_epub(
            self.directory,
            documents={
                "text/chapter.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                    + body
                    + "</body></html>"
                ),
                "text/aux.xhtml": AUXILIARY,
            },
        )
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )
        return epub, recipe

    def readings(self, epub: Path, recipe: dict[str, object]) -> list[tuple[str, str]]:
        finalize_recipe(epub, recipe)
        return [(block.block_id, block.text) for block in extract_recipe(epub, recipe)]

    def test_reused_slices_reject_different_omissions(self) -> None:
        epub, recipe = self.make_recipe("<p><span>JUNK</span>KEEP</p>")
        output = cast(dict[str, object], recipe["output"])
        output["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "p",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1",
                                "omit": ["1"],
                                "slice": {"start": 0, "end": 4},
                            }
                        ],
                    },
                    {
                        "type": "p",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1",
                                "slice": {"start": 4, "end": 8},
                            }
                        ],
                    },
                ],
            }
        ]
        with self.assertRaisesRegex(EpubBlocksError, "different omissions"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_reused_slices_share_omissions_regardless_of_order(self) -> None:
        epub, recipe = self.make_recipe("<p><span>X</span><span>Y</span>AlphaBeta</p>")
        output = cast(dict[str, object], recipe["output"])
        second_slice = {"start": 5, "end": 9}
        output["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "p",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1",
                                "omit": ["1", "2"],
                                "slice": {"start": 0, "end": 5},
                            }
                        ],
                    },
                    {
                        "type": "p",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1",
                                "omit": ["2", "1"],
                                "slice": second_slice,
                            }
                        ],
                    },
                ],
            }
        ]
        self.assertEqual(
            self.readings(epub, recipe), [("001", "Alpha"), ("002", "Beta")]
        )
        second_slice["start"] = 4
        with self.assertRaisesRegex(EpubBlocksError, "overlapping slices"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_source_maps_require_all_captures_for_paths_and_markers(self) -> None:
        cases: tuple[tuple[str, dict[str, object], list[tuple[str, str]]], ...] = (
            (
                "<p>First</p><p>Second</p>",
                {"source_pattern": r"#(\d+)$"},
                [("two.1", "First"), ("three.1", "Second")],
            ),
            (
                "<h1>CHAPTER 1</h1><p>First</p><h1>CHAPTER 2</h1><p>Second</p>",
                {"source_marker": {"pattern": r"^CHAPTER (\d+)$"}},
                [
                    ("two.1", "CHAPTER 1"),
                    ("two.2", "First"),
                    ("three.1", "CHAPTER 2"),
                    ("three.2", "Second"),
                ],
            ),
        )
        for body, groups, expected in cases:
            with self.subTest(groups=groups):
                epub, recipe = self.make_recipe(body)
                output = cast(dict[str, object], recipe["output"])
                groups["source_map"] = {"1": "2"}
                output["groups"] = groups
                output["identifiers"] = {"block": {"template": "{group}.{number}"}}
                with self.assertRaisesRegex(
                    EpubBlocksError, "capture '2' has no mapping"
                ):
                    compile_recipe(epub, recipe, verify_digest=False)
                groups["source_map"] = {"1": "two", "2": "three"}
                self.assertEqual(self.readings(epub, recipe), expected)

    def test_fixed_insertions_preserve_verse_state(self) -> None:
        for split in (False, True):
            with self.subTest(replacement=split):
                epub, recipe = self.make_recipe(
                    '<p class="first">AlphaBeta</p><p class="line">Gamma</p><p>Prose</p>'
                )
                output = cast(dict[str, object], recipe["output"])
                output["rules"] = [
                    {
                        "match": {"classes": ["first"]},
                        "type": "verse",
                        "role": "line-start",
                    },
                    {"match": {"classes": ["line"]}, "type": "verse", "role": "line"},
                ]
                if split:
                    output["replacements"] = [
                        {
                            "anchor": "text/chapter.xhtml#1",
                            "outputs": [
                                {
                                    "type": "verse",
                                    "role": role,
                                    "parts": [
                                        {
                                            "document": "text/chapter.xhtml",
                                            "element_path": "1",
                                            "slice": {"start": start, "end": end},
                                        }
                                    ],
                                }
                                for role, start, end in (
                                    ("line-start", 0, 5),
                                    ("line", 5, 9),
                                )
                            ],
                        }
                    ]
                output["insertions"] = [
                    {
                        "after": "text/chapter.xhtml#1",
                        "outputs": [
                            {
                                "type": "footnote",
                                "role": "fixed",
                                "id": identifier,
                                "parts": [
                                    {"document": "text/aux.xhtml", "element_path": "1"}
                                ],
                            }
                            for identifier in ("note-a", "note-b")
                        ],
                    }
                ]
                expected = (
                    [("001.01", "Alpha"), ("001.02", "Beta")]
                    if split
                    else [("001.01", "AlphaBeta")]
                )
                expected += [
                    ("note-a", "Auxiliary material."),
                    ("note-b", "Auxiliary material."),
                    ("001.03" if split else "001.02", "Gamma"),
                    ("002", "Prose"),
                ]
                self.assertEqual(self.readings(epub, recipe), expected)

    def test_fixed_headings_still_end_verse_sequences(self) -> None:
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                epub, recipe = self.make_recipe(
                    '<p class="first">First</p><h1>Heading</h1><p class="line">Second</p>'
                )
                output = cast(dict[str, object], recipe["output"])
                output["rules"] = [
                    {
                        "match": {"classes": ["first"]},
                        "type": "verse",
                        "role": "line-start",
                    },
                    {"match": {"classes": ["line"]}, "type": "verse", "role": "line"},
                    {
                        "match": {"tag": "h1"},
                        "type": "heading",
                        "role": "fixed",
                        "id": "heading",
                    },
                ]
                if replacement:
                    output["replacements"] = [
                        {
                            "anchor": "text/chapter.xhtml#2",
                            "outputs": [
                                {
                                    "type": "heading",
                                    "role": "fixed",
                                    "id": "heading",
                                    "parts": [
                                        {
                                            "document": "text/chapter.xhtml",
                                            "element_path": "2",
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                with self.assertRaisesRegex(EpubBlocksError, "no preceding line-start"):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_insertion_anchor_must_be_emitted_by_consume_rule(self) -> None:
        epub, recipe = self.make_recipe("<h1>Chapter label</h1><p>Title</p>")
        output = cast(dict[str, object], recipe["output"])
        rule: dict[str, object] = {
            "match": {"tag": "h1"},
            "type": "heading",
            "consume": 2,
            "emit": [1],
        }
        output["rules"] = [rule]
        output["insertions"] = [
            {
                "after": "text/chapter.xhtml#2",
                "outputs": [
                    {
                        "type": "footnote",
                        "role": "fixed",
                        "id": "note",
                        "parts": [{"document": "text/aux.xhtml", "element_path": "1"}],
                    }
                ],
            }
        ]
        with self.assertRaisesRegex(EpubBlocksError, "discards insertion anchor"):
            compile_recipe(epub, recipe, verify_digest=False)
        for emit, text in (([2], "Title"), ([1, 2], "Chapter label Title")):
            with self.subTest(emit=emit):
                rule["emit"] = emit
                self.assertEqual(
                    self.readings(epub, recipe),
                    [("001", text), ("note", "Auxiliary material.")],
                )

    def test_schema_and_runtime_agree_on_optional_fields_and_match_criteria(
        self,
    ) -> None:
        schema_path = files("epub_blocks").joinpath("schemas", "recipe-v1.schema.json")
        schema = cast(
            dict[str, object], json.loads(schema_path.read_text(encoding="utf-8"))
        )
        validator = Draft202012Validator(schema)
        cases: list[tuple[str, dict[str, object], bool]] = []
        for member in ("id", "separator"):
            cases.append(
                (
                    member,
                    {"rules": [{"match": {"tag": "p"}, "type": "p", member: None}]},
                    False,
                )
            )
        cases.append(("default id", {"default": {"type": "p", "id": None}}, False))
        for member in ("tag", "text_pattern"):
            cases.append(
                (
                    member,
                    {"rules": [{"match": {"classes": [], member: None}, "type": "p"}]},
                    False,
                )
            )
        for member in ("classes_any", "classes_all", "locators"):
            for has_tag in (False, True):
                match: dict[str, object] = {member: []}
                if has_tag:
                    match["tag"] = "p"
                cases.append(
                    (
                        f"{member} empty, tag={has_tag}",
                        {"rules": [{"match": match, "type": "p"}]},
                        has_tag,
                    )
                )
            cases.append(
                (
                    member + " nonempty",
                    {"rules": [{"match": {member: ["verse"]}, "type": "p"}]},
                    True,
                )
            )
        cases.extend(
            [
                (
                    "empty exact classes",
                    {"rules": [{"match": {"classes": []}, "type": "p"}]},
                    True,
                ),
                (
                    "flags only",
                    {"rules": [{"match": {"case_insensitive": True}, "type": "p"}]},
                    False,
                ),
                ("empty match", {"rules": [{"match": {}, "type": "p"}]}, False),
                ("no group pattern", {"groups": {"source_offset": 0}}, False),
                (
                    "offset on transitions",
                    {
                        "groups": {
                            "transitions": {"text/chapter.xhtml#1": "chapter"},
                            "source_offset": 0,
                        }
                    },
                    False,
                ),
                (
                    "offset with pattern",
                    {"groups": {"source_pattern": r"#(\d+)$", "source_offset": 0}},
                    True,
                ),
            ]
        )
        for label, changes, valid in cases:
            with self.subTest(case=label):
                epub, recipe = self.make_recipe("<p>First</p>")
                cast(dict[str, object], recipe["output"]).update(changes)
                schema_valid = validator.is_valid(recipe)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                self.assertEqual(schema_valid, valid)
                if valid:
                    self.assertEqual(len(self.readings(epub, recipe)), 1)
                else:
                    with self.assertRaises(EpubBlocksError):
                        compile_recipe(epub, recipe, verify_digest=False)

    def test_empty_exact_classes_match_only_classless_elements(self) -> None:
        epub, recipe = self.make_recipe('<p>Plain</p><p class="verse">Verse</p>')
        output = cast(dict[str, object], recipe["output"])
        output["rules"] = [{"match": {"classes": []}, "type": "classless"}]
        finalize_recipe(epub, recipe)
        self.assertEqual(
            [(block.block_type, block.text) for block in extract_recipe(epub, recipe)],
            [("classless", "Plain"), ("paragraph", "Verse")],
        )

    def test_identifier_overflow_reports_a_recipe_error_and_preserves_output(
        self,
    ) -> None:
        epub, recipe = self.make_recipe("<p>First</p>")
        cast(dict[str, object], recipe["output"])["identifiers"] = {
            "block": {"template": "{number:c}", "start": 0x110000}
        }
        with self.assertRaisesRegex(
            EpubBlocksError, "identifier template could not be rendered"
        ):
            compile_recipe(epub, recipe, verify_digest=False)
        recipe_path = self.directory / "recipe.json"
        recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
        output = self.directory / "output.tsv"
        output.write_text("existing output\n", encoding="utf-8")
        stderr = io.StringIO()
        with (
            patch.object(
                sys, "argv", ["epub-blocks", str(epub), str(recipe_path), str(output)]
            ),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 2)
        self.assertIn("identifier template could not be rendered", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(output.read_text(encoding="utf-8"), "existing output\n")
