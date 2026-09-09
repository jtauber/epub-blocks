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


class VariableJoinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        schema = json.loads(
            files("epub_blocks").joinpath("schemas/recipe-v1.schema.json").read_text()
        )
        self.validator = Draft202012Validator(schema)

    def recipe(self, body: str) -> tuple[Path, dict[str, object], dict[str, object]]:
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
        output = cast(dict[str, object], recipe["output"])
        output["rules"] = [
            {
                "match": {"tag": "p"},
                "type": "paragraph",
                "consume_while": {"classes_all": ["cont"]},
            }
        ]
        return epub, recipe, output

    def readings(self, epub: Path, recipe: dict[str, object]) -> list[str]:
        self.assertTrue(self.validator.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        finalize_recipe(epub, recipe)
        return [block.text for block in extract_recipe(epub, recipe)]

    def test_variable_runs_and_terminal_singleton(self) -> None:
        epub, recipe, _ = self.recipe(
            '<p>A</p><p class="cont">B</p><p class="cont">C</p>'
            '<p>D</p><p class="cont">E</p><h2>Heading</h2><p>F</p>'
        )
        self.assertEqual(self.readings(epub, recipe), ["A B C", "D E", "Heading", "F"])
        compiled = compile_recipe(epub, recipe)
        self.assertEqual([len(b.parts) for b in compiled.blocks], [3, 2, 1, 1])
        self.assertEqual(
            compiled.blocks[0].consumed_locators,
            (
                "text/chapter.xhtml#1",
                "text/chapter.xhtml#2",
                "text/chapter.xhtml#3",
            ),
        )
        self.assertEqual(
            [b.block_id for b in compiled.blocks], ["001", "002", "003", "004"]
        )

    def test_text_predicate_custom_separator_and_rule_precedence(self) -> None:
        epub, recipe, output = self.recipe("<p>A</p><p>more</p><p>MORE</p><p>end</p>")
        output["rules"] = [
            {
                "match": {"tag": "p"},
                "type": "joined",
                "separator": "|",
                "consume_while": {"text_pattern": "^more$", "case_insensitive": True},
            },
            {"match": {"text_pattern": "more"}, "type": "unused"},
        ]
        self.assertEqual(self.readings(epub, recipe), ["A|more|MORE", "end"])
        self.assertEqual(
            [b.block_type for b in compile_recipe(epub, recipe).blocks],
            ["joined", "joined"],
        )

    def test_stops_before_skip(self) -> None:
        epub, recipe, output = self.recipe(
            '<p>A</p><p class="cont">X</p><p class="cont">B</p>'
        )
        output["skip_source"] = ["text/chapter.xhtml#2"]
        self.assertEqual(self.readings(epub, recipe), ["A", "B"])

    def test_stops_before_replacement_and_reserved_sources(self) -> None:
        epub, recipe, output = self.recipe(
            '<p>A</p><p class="cont">X</p><p>B</p><p class="cont">Y</p><p class="cont">C</p>'
        )
        output["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#2",
                "outputs": [
                    {
                        "type": "replacement",
                        "separator": " ",
                        "parts": [
                            {"document": "text/chapter.xhtml", "element_path": "2"},
                            {"document": "text/chapter.xhtml", "element_path": "4"},
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(self.readings(epub, recipe), ["A", "X Y", "B", "C"])

    def test_stops_after_insertion_anchor(self) -> None:
        for anchor in (1, 2):
            with self.subTest(anchor=anchor):
                epub, recipe, output = self.recipe(
                    '<p>A</p><p class="cont">B</p><p class="cont">C</p>'
                )
                output["insertions"] = [
                    {
                        "after": f"text/chapter.xhtml#{anchor}",
                        "outputs": [
                            {
                                "type": "note",
                                "parts": [
                                    {"document": "text/aux.xhtml", "element_path": "1"},
                                ],
                            }
                        ],
                    }
                ]
                readings = self.readings(epub, recipe)
                self.assertEqual(readings[0], "A" if anchor == 1 else "A B")
                self.assertEqual(readings[-1], "B C" if anchor == 1 else "C")
                self.assertEqual(len(readings), 3)

    def test_stops_at_group_boundary(self) -> None:
        epub, recipe, output = self.recipe(
            '<p>A</p><p class="cont">B</p><p class="cont">C</p>'
        )
        output["groups"] = {
            "transitions": {"text/chapter.xhtml#1": "a", "text/chapter.xhtml#2": "b"}
        }
        output["identifiers"] = {"block": {"template": "{group}.{number}"}}
        self.assertEqual(self.readings(epub, recipe), ["A", "B C"])

    def test_stops_at_document_boundary_in_same_group(self) -> None:
        epub, recipe, output = self.recipe("<p>A</p>")
        cast(dict[str, object], recipe["source_blocks"])["include_documents"] = [
            "text/*.xhtml"
        ]
        cast(dict[str, object], recipe["source_blocks"])["include_non_linear"] = True
        output["rules"] = [
            {"match": {"tag": "p"}, "type": "paragraph", "consume_while": {"tag": "p"}}
        ]
        readings = self.readings(epub, recipe)
        self.assertEqual(readings[0], "A")
        self.assertGreater(len(readings), 1)

    def test_markup_stays_at_fragment_boundaries_in_both_formats(self) -> None:
        for serialization in ("xml", "delimiters"):
            epub, recipe, _ = self.recipe(
                '<p>A<em>B</em></p><i class="label">005</i><p class="cont"><b/>C</p>'
            )
            recipe["text"] = {
                "markup": {
                    "format": serialization,
                    "between_blocks": "next",
                    "rules": [
                        {"match": {"tag": "em"}, "kind": "span", "name": "em"},
                        {
                            "match": {"classes_all": ["label"]},
                            "kind": "milestone",
                            "name": "number",
                            "label_text": True,
                        },
                        {"match": {"tag": "b"}, "kind": "milestone", "name": "line"},
                    ],
                    "delimiters": {
                        "em": ["⧼", "⧽"],
                        "number": ["⟦", "⟧"],
                        "line": ["⟬", "⟭"],
                    },
                }
            }
            expected = (
                'A<em>B</em> <number label="005"/><line/>C'
                if serialization == "xml"
                else "A⧼B⧽ ⟦005⟧⟬⟭C"
            )
            self.assertEqual(self.readings(epub, recipe), [expected])

    def test_conflicts_and_invalid_predicates_rejected_by_parser_and_schema(
        self,
    ) -> None:
        invalid: list[dict[str, object]] = [
            {"consume": 1},
            {"emit": [1]},
            {"remove_prefix": {"pattern": "^A"}},
            {"consume_while": None},
            {"consume_while": {}},
            {"consume_while": {"classes_any": []}},
            {"consume_while": {"tag": 3}},
            {"consume_while": {"unknown": True}},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                epub, recipe, output = self.recipe("<p>A</p>")
                rules = cast(list[dict[str, object]], output["rules"])
                rules[0].update(fields)
                self.assertFalse(self.validator.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                with self.assertRaises(EpubBlocksError):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_equivalent_fixed_plan_keeps_digest(self) -> None:
        epub, recipe, output = self.recipe('<p>A</p><p class="cont">B</p>')
        variable = finalize_recipe(epub, recipe)
        rules = cast(list[dict[str, object]], output["rules"])
        del rules[0]["consume_while"]
        rules[0]["consume"] = 2
        self.assertEqual(finalize_recipe(epub, recipe), variable)


if __name__ == "__main__":
    unittest.main()
