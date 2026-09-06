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
from test_epub_blocks import finalize_recipe, make_epub, minimal_recipe

from epub_blocks import (
    BoundaryRule,
    ContentOptions,
    ElementSelector,
    EpubBlocksError,
    compile_recipe,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_candidates,
    write_tsv,
)


class BoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def sample(self, body: str) -> tuple[Path, dict[str, object]]:
        epub = make_epub(
            self.directory,
            documents={
                "text/chapter.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:epub="http://www.idpf.org/2007/ops">'
                    f"<body>{body}</body></html>"
                )
            },
        )
        return epub, minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )

    def boundaries(
        self, recipe: dict[str, object], rules: list[object], **options: object
    ) -> dict[str, object]:
        boundary: dict[str, object] = {"rules": rules, **options}
        recipe["text"] = {"block_boundaries": boundary}
        return boundary

    def markup(self, recipe: dict[str, object], format_name: str) -> None:
        cast(dict[str, object], recipe["text"])["markup"] = {
            "format": format_name,
            "rules": [
                {"match": {"tag": "em"}, "kind": "span", "name": "em"},
                {
                    "match": {"tag": "span", "classes_all": ["gap"], "empty": True},
                    "kind": "milestone",
                    "name": "gap",
                },
                {
                    "match": {"tag": "span", "classes_all": ["gap"]},
                    "kind": "span",
                    "name": "spaced",
                },
            ],
            "delimiters": {"em": ["⧼", "⧽"], "gap": ["⟬", "⟭"], "spaced": ["⸤", "⸥"]},
        }

    def output(self, epub: Path, recipe: dict[str, object]) -> list[str]:
        finalize_recipe(epub, recipe)
        return [block.text for block in extract_recipe(epub, recipe)]

    def test_before_after_and_both_preserve_contents_and_transparent_words(
        self,
    ) -> None:
        epub, recipe = self.sample(
            '<p>in<span>side</span>:left<span class="gap">right</span>end'
            '<span class="gap"/>tail</p>'
        )
        for sides, expected in (
            ({"before": " "}, "inside:left rightend tail"),
            ({"after": " "}, "inside:leftright end tail"),
            ({"before": " ", "after": " "}, "inside:left right end tail"),
        ):
            with self.subTest(sides=sides):
                self.boundaries(recipe, [{"match": {"classes_all": ["gap"]}, **sides}])
                self.assertEqual(self.output(epub, recipe), [expected])
                self.assertEqual(
                    [b.text for b in extract_recipe_candidates(epub, recipe)],
                    [expected],
                )

    def test_first_matching_rule_overrides_tag_fallback_without_double_insertion(
        self,
    ) -> None:
        epub, recipe = self.sample('<p>A<span class="gap">B</span>C<span>D</span>E</p>')
        cast(dict[str, object], recipe["normalization"])["collapse_whitespace"] = False
        self.boundaries(
            recipe,
            [
                {"match": {"classes_all": ["gap"]}, "before": " "},
                {"match": {"classes_all": ["gap"]}, "after": "  "},
            ],
            tags=["span"],
            separator="\u00a0",
        )
        self.assertEqual(self.output(epub, recipe), ["A BC\u00a0D\u00a0E"])

    def test_nested_compound_blocks_and_fragment_root(self) -> None:
        epub, recipe = self.sample('<li>Open.<p>A<span class="gap"/>B</p>Tail.</li>')
        recipe["source_blocks"] = {
            "element_rules": [{"match": {"tag": "li"}, "action": "block"}],
            "strict_coverage": True,
        }
        cast(dict[str, object], recipe["normalization"])["strip"] = False
        self.boundaries(
            recipe,
            [
                {"match": {"tag": "span", "classes_all": ["gap"]}, "before": " "},
                {"match": {"tag": "li"}, "before": " ", "after": " "},
            ],
            tags=["p"],
            separator=" ",
        )
        self.assertEqual(self.output(epub, recipe), ["Open. A B Tail."])

    def test_markers_and_nonempty_spans_survive_both_serializers(self) -> None:
        epub, recipe = self.sample(
            '<p>A<em>B<span class="gap"/>C</em>D<span class="gap">E</span>F</p>'
        )
        self.boundaries(recipe, [{"match": {"classes_all": ["gap"]}, "before": " "}])
        for format_name, expected in (
            ("xml", "A<em>B <gap/>C</em>D <spaced>E</spaced>F"),
            ("delimiters", "A⧼B ⟬⟭C⧽D ⸤E⸥F"),
        ):
            with self.subTest(format=format_name):
                self.markup(recipe, format_name)
                self.assertEqual(self.output(epub, recipe), [expected])
                self.assertEqual(
                    extract_recipe_candidates(epub, recipe)[0].text, "AB CD EF"
                )

    def test_boundaries_precede_normalization_and_output_matching(self) -> None:
        epub, recipe = self.sample('<p>left<span class="gap"> \n\t right</span></p>')
        self.boundaries(
            recipe, [{"match": {"classes_all": ["gap"]}, "before": "\u00a0\t"}]
        )
        cast(dict[str, object], recipe["output"])["rules"] = [
            {"match": {"text_pattern": "^left right$"}, "type": "matched"}
        ]
        self.assertEqual(self.output(epub, recipe), ["left right"])
        self.assertEqual(extract_recipe(epub, recipe)[0].block_type, "matched")

    def test_skips_and_omissions_never_introduce_whitespace(self) -> None:
        for omission in ("structural", "semantic", "explicit"):
            with self.subTest(omission=omission):
                epub, recipe = self.sample(
                    '<p>pre<span class="gap" epub:type="noteref">X</span>fix<span class="gap"/>tail</p>'
                )
                recipe["omit_epub_types"] = (
                    ["noteref"] if omission == "semantic" else []
                )
                if omission == "structural":
                    recipe["source_blocks"] = {
                        "element_rules": [
                            {"match": {"epub_types": ["noteref"]}, "action": "skip"}
                        ]
                    }
                if omission == "explicit":
                    cast(dict[str, object], recipe["output"])["replacements"] = [
                        {
                            "anchor": "text/chapter.xhtml#1",
                            "outputs": [
                                {
                                    "type": "paragraph",
                                    "parts": [
                                        {
                                            "document": "text/chapter.xhtml",
                                            "element_path": "1",
                                            "omit": ["1"],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                self.boundaries(
                    recipe,
                    [{"match": {"classes_all": ["gap"]}, "before": " ", "after": " "}],
                )
                self.assertEqual(self.output(epub, recipe), ["prefix tail"])

    def test_contextual_selectors_use_original_structure_after_omissions(self) -> None:
        epub, recipe = self.sample(
            '<p>A<span class="old"/><span class="gap"><b/>B</span>C</p>'
        )
        self.boundaries(
            recipe,
            [
                {
                    "match": {
                        "tag": "span",
                        "classes_all": ["gap"],
                        "locators": ["text/chapter.xhtml#1.2"],
                        "previous_sibling": {"classes_all": ["old"]},
                        "has_child": {"tag": "b"},
                    },
                    "before": " ",
                }
            ],
        )
        cast(dict[str, object], recipe["output"])["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "paragraph",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1",
                                "omit": ["1", "2.1"],
                            }
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(self.output(epub, recipe), ["A BC"])

    def test_slices_and_prefix_removal_use_inserted_characters_and_marker_positions(
        self,
    ) -> None:
        for mode in ("slice", "prefix"):
            for format_name in ("xml", "delimiters"):
                with self.subTest(mode=mode, format=format_name):
                    epub, recipe = self.sample(
                        '<p>A<em>B<span class="gap"/>C</em>D</p>'
                    )
                    self.boundaries(
                        recipe, [{"match": {"classes_all": ["gap"]}, "before": " "}]
                    )
                    self.markup(recipe, format_name)
                    output = cast(dict[str, object], recipe["output"])
                    if mode == "slice":
                        output["replacements"] = [
                            {
                                "anchor": "text/chapter.xhtml#1",
                                "outputs": [
                                    {
                                        "type": "paragraph",
                                        "parts": [
                                            {
                                                "document": "text/chapter.xhtml",
                                                "element_path": "1",
                                                "slice": {"start": 1, "end": 4},
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                        expected = (
                            "<em>B <gap/>C</em>" if format_name == "xml" else "⧼B ⟬⟭C⧽"
                        )
                    else:
                        output["rules"] = [
                            {
                                "match": {"tag": "p"},
                                "type": "paragraph",
                                "remove_prefix": {"pattern": "^AB "},
                            }
                        ]
                        expected = (
                            "<em><gap/>C</em>D" if format_name == "xml" else "⧼⟬⟭C⧽D"
                        )
                    self.assertEqual(self.output(epub, recipe), [expected])

    def test_invalid_rules_rejected_by_schema_and_runtime(self) -> None:
        epub, base = self.sample("<p>Text.</p>")
        schema = json.loads(
            files("epub_blocks").joinpath("schemas/recipe-v1.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        invalid: list[object] = [
            None,
            {},
            [None],
            [{}],
            [{"match": {"tag": "span"}}],
            [{"before": " "}],
            [{"match": {}, "before": " "}],
            [{"match": {"tag": "span"}, "before": "", "after": " "}],
            [{"match": {"tag": "span"}, "before": "word"}],
            [{"match": {"tag": "span"}, "after": None}],
            [{"match": {"tag": "span"}, "before": False}],
            [{"match": {"tag": "span"}, "after": 1}],
            [{"match": {"tag": "span"}, "before": " ", "unknown": True}],
            [
                {
                    "match": {"previous_sibling": {"has_child": {"tag": "p"}}},
                    "before": " ",
                }
            ],
        ]
        for rules in invalid:
            with self.subTest(rules=rules):
                recipe = copy.deepcopy(base)
                recipe["text"] = {"block_boundaries": {"rules": rules}}
                self.assertFalse(validator.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                with self.assertRaises(EpubBlocksError):
                    compile_recipe(epub, recipe, verify_digest=False)
        for rule in (
            {"match": {"tag": "span"}, "before": " "},
            {"match": {"classes_any": ["a", "b"]}, "after": "\n"},
            {"match": {"empty": True}, "before": "\t", "after": " "},
        ):
            recipe = copy.deepcopy(base)
            self.boundaries(recipe, [rule])
            self.assertTrue(validator.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
            compile_recipe(epub, recipe, verify_digest=False)

    def test_policy_pins_side_order_and_selector_even_when_text_is_unchanged(
        self,
    ) -> None:
        epub, recipe = self.sample('<p>A<span class="gap"/>B</p>')
        original = compiled_recipe_digest(finalize_recipe(epub, recipe))
        self.boundaries(recipe, [], separator="\n")
        self.assertEqual(
            compiled_recipe_digest(finalize_recipe(epub, recipe)), original
        )
        plans: list[str] = []
        for side in ("before", "after"):
            self.boundaries(recipe, [{"match": {"classes_all": ["gap"]}, side: " "}])
            self.assertEqual(self.output(epub, recipe), ["A B"])
            plans.append(compiled_recipe_digest(finalize_recipe(epub, recipe)))
        self.assertEqual(len({original, *plans}), 3)
        rules: list[object] = [
            {"match": {"tag": "span"}, "before": " "},
            {"match": {"classes_all": ["gap"]}, "before": " "},
        ]
        self.boundaries(recipe, rules)
        before = compiled_recipe_digest(finalize_recipe(epub, recipe))
        self.boundaries(recipe, list(reversed(rules)))
        self.assertNotEqual(
            compiled_recipe_digest(finalize_recipe(epub, recipe)), before
        )

    def test_compiled_boundary_rules_are_validated_and_exported(self) -> None:
        epub, recipe = self.sample("<p>Text.</p>")
        original = finalize_recipe(epub, recipe)
        valid = BoundaryRule(ElementSelector(tag="span"), before=" ")
        plan = replace(original, content=ContentOptions(boundary_rules=(valid,)))
        self.assertEqual(len(compiled_recipe_digest(plan)), 64)
        for rule in (
            BoundaryRule(ElementSelector()),
            BoundaryRule(ElementSelector(tag="span")),
            replace(valid, before="text"),
            replace(valid, before=cast(str, False), after=" "),
            replace(valid, after=cast(str, None)),
            cast(BoundaryRule, object()),
        ):
            with self.subTest(rule=rule), self.assertRaises(EpubBlocksError):
                compiled_recipe_digest(
                    replace(original, content=ContentOptions(boundary_rules=(rule,)))
                )
        with self.assertRaises(EpubBlocksError):
            compiled_recipe_digest(
                replace(
                    original,
                    content=ContentOptions(
                        boundary_rules=cast(tuple[BoundaryRule, ...], [valid])
                    ),
                )
            )

    def test_raw_boundary_newline_is_not_silently_written_as_an_extra_tsv_row(
        self,
    ) -> None:
        epub, recipe = self.sample('<p>A<span class="gap"/>B</p>')
        self.boundaries(recipe, [{"match": {"classes_all": ["gap"]}, "before": "\n"}])
        cast(dict[str, object], recipe["normalization"])["collapse_whitespace"] = False
        self.assertEqual(self.output(epub, recipe), ["A\nB"])
        output = self.directory / "output.tsv"
        output.write_text("previous output\n")
        with self.assertRaises(EpubBlocksError):
            write_tsv(output, extract_recipe(epub, recipe))
        self.assertEqual(output.read_text(), "previous output\n")


if __name__ == "__main__":
    unittest.main()
