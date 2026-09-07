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
    CompiledBlock,
    CompiledRecipe,
    ContentOptions,
    ElementRule,
    ElementSelector,
    EpubBlocksError,
    Fragment,
    NormalizationOptions,
    compile_recipe,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_candidates,
)


class AttributePrefixTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def sample(self, body: str) -> tuple[Path, dict[str, object]]:
        epub = make_epub(
            self.directory,
            documents={
                "text/chapter.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:x="urn:example"><body>' + body + "</body></html>"
                )
            },
        )
        return epub, minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest(),
            omit_epub_types=[],
        )

    def readings(self, epub: Path, recipe: dict[str, object]) -> list[str]:
        finalize_recipe(epub, recipe)
        return [block.text for block in extract_recipe(epub, recipe)]

    def test_page_markers_inline_detached_and_trailing_in_both_formats(self) -> None:
        epub, recipe = self.sample(
            '<span id="page_1"/><span id="other"/>'
            '<p>A<span id="page_2"/>B<span id="Page_3"/>'
            '<span id="page_4">kept</span><span/><span id=""/></p>'
            '<span id="page_5&amp;6"/>'
        )
        for format_name, expected in (
            (
                "xml",
                (
                    '<page label="page_1"/>A<page label="page_2"/>Bkept'
                    '<page label="page_5&amp;6"/>'
                ),
            ),
            ("delimiters", "⟦page_1⟧A⟦page_2⟧Bkept⟦page_5&6⟧"),
        ):
            with self.subTest(format=format_name):
                recipe["text"] = {
                    "markup": {
                        "format": format_name,
                        "between_blocks": "next",
                        "trailing": "previous",
                        "rules": [
                            {
                                "match": {
                                    "tag": "span",
                                    "empty": True,
                                    "attribute_prefixes": {"id": "page_"},
                                },
                                "kind": "milestone",
                                "name": "page",
                                "label_attribute": "id",
                            }
                        ],
                        "delimiters": {"page": ["⟦", "⟧"]},
                    }
                }
                self.assertEqual(self.readings(epub, recipe), [expected])
                self.assertEqual(
                    [b.text for b in extract_recipe_candidates(epub, recipe)],
                    ["ABkept"],
                )

    def test_literal_case_sensitive_namespaced_and_conjunctive_matching(self) -> None:
        epub, recipe = self.sample(
            '<div id="keep_1" x:kind="é_*one" data-ok="yes">A</div>'
            '<div id="keep_2" x:kind="é_Xone" data-ok="yes">B</div>'
            '<div id="keep_3" x:kind="É_*one" data-ok="yes">C</div>'
            '<div id="keep_4" kind="é_*one" data-ok="yes">D</div>'
            '<div id="keep_5" x:kind="é_*one" data-ok="no">E</div>'
            '<div id="Keep_6" x:kind="é_*one" data-ok="yes">F</div>'
            '<div id="" x:kind="é_*one" data-ok="yes">G</div>'
            '<div x:kind="é_*one" data-ok="yes">H</div>'
        )
        recipe["source_blocks"] = {
            "element_rules": [
                {
                    "match": {
                        "tag": "div",
                        "attributes": {"data-ok": "yes"},
                        "attribute_prefixes": {
                            "id": "keep_",
                            "{urn:example}kind": "é_*",
                        },
                    },
                    "action": "block",
                },
                {"match": {"tag": "div"}, "action": "skip"},
            ],
            "strict_coverage": True,
        }
        self.assertEqual(self.readings(epub, recipe), ["A"])

    def test_same_attribute_exact_and_prefix_constraints_both_apply(self) -> None:
        epub, recipe = self.sample('<p id="keep_1">A</p><p id="keep_2">B</p>')
        for prefix, expected in (("keep_", ["B"]), ("other_", ["A", "B"])):
            recipe["source_blocks"] = {
                "element_rules": [
                    {
                        "match": {
                            "attributes": {"id": "keep_1"},
                            "attribute_prefixes": {"id": prefix},
                        },
                        "action": "skip",
                    }
                ]
            }
            self.assertEqual(self.readings(epub, recipe), expected)

    def test_contextual_source_rules_use_attribute_prefixes(self) -> None:
        epub, recipe = self.sample(
            '<div><span id="start_1"/>A</div><div>B</div>'
            '<div id="marker_1"/><div>C</div><div>D</div>'
        )
        recipe["source_blocks"] = {
            "element_rules": [
                {
                    "match": {
                        "tag": "div",
                        "has_child": {"attribute_prefixes": {"id": "start_"}},
                    },
                    "action": "block",
                },
                {
                    "match": {
                        "tag": "div",
                        "previous_sibling": {"attribute_prefixes": {"id": "marker_"}},
                    },
                    "action": "block",
                },
                {"match": {"tag": "div"}, "action": "skip"},
            ],
            "strict_coverage": True,
        }
        self.assertEqual(self.readings(epub, recipe), ["A", "C"])

    def test_prefix_boundary_rules_keep_ordinary_word_wrappers_transparent(
        self,
    ) -> None:
        epub, recipe = self.sample(
            '<p>in<span id="word_1">side</span><span id="space_1"/>word'
            '<span id="space_2">kept</span>end<span id="omit_1">gone</span>.</p>'
        )
        recipe["source_blocks"] = {
            "element_rules": [
                {
                    "match": {"attribute_prefixes": {"id": "omit_"}},
                    "action": "skip",
                }
            ]
        }
        recipe["text"] = {
            "block_boundaries": {
                "rules": [
                    {
                        "match": {"attribute_prefixes": {"id": "space_"}},
                        "before": " ",
                    }
                ]
            }
        }
        self.assertEqual(self.readings(epub, recipe), ["inside word keptend."])

    def test_schema_runtime_agree_in_shared_and_contextual_selectors(self) -> None:
        epub, base = self.sample('<p id="page_1">A</p><p>B</p>')
        schema = json.loads(
            files("epub_blocks").joinpath("schemas/recipe-v1.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        cases: list[tuple[dict[str, object], bool]] = [
            ({"attribute_prefixes": {"id": "page_"}}, True),
            ({"tag": "span", "attribute_prefixes": {}}, True),
            ({"attribute_prefixes": {}}, False),
            ({"attribute_prefixes": {"": "page_"}}, False),
            ({"attribute_prefixes": {"id": ""}}, False),
            ({"attribute_prefixes": {"id": 1}}, False),
            ({"attribute_prefixes": {"id": True}}, False),
            ({"attribute_prefixes": {"id": None}}, False),
            ({"attribute_prefixes": {"id": []}}, False),
            ({"attribute_prefixes": None}, False),
            ({"attribute_prefixes": []}, False),
            ({"attribute_prefixes": "page_"}, False),
        ]
        for match, valid in cases:
            for context in (None, "previous_sibling", "has_child"):
                for consumer in ("source", "markup", "boundary"):
                    with self.subTest(match=match, context=context, consumer=consumer):
                        selector = match if context is None else {context: match}
                        recipe = copy.deepcopy(base)
                        if consumer == "source":
                            recipe["source_blocks"] = {
                                "element_rules": [
                                    {"match": selector, "action": "descend"}
                                ]
                            }
                        elif consumer == "markup":
                            recipe["text"] = {
                                "markup": {
                                    "rules": [
                                        {
                                            "match": selector,
                                            "kind": "span",
                                            "name": "em",
                                        }
                                    ]
                                }
                            }
                        else:
                            recipe["text"] = {
                                "block_boundaries": {
                                    "rules": [{"match": selector, "before": " "}]
                                }
                            }
                        self.assertEqual(validator.is_valid(recipe), valid)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                        if valid:
                            compile_recipe(epub, recipe, verify_digest=False)
                        else:
                            with self.assertRaises(EpubBlocksError):
                                compile_recipe(epub, recipe, verify_digest=False)

    def test_output_rule_matcher_is_not_implicitly_extended(self) -> None:
        epub, recipe = self.sample("<p>A</p>")
        cast(dict[str, object], recipe["output"])["rules"] = [
            {
                "match": {"attribute_prefixes": {"id": "page_"}},
                "type": "paragraph",
            }
        ]
        with self.assertRaisesRegex(EpubBlocksError, "unknown"):
            compile_recipe(epub, recipe, verify_digest=False)

    def legacy_plan(self) -> CompiledRecipe:
        return CompiledRecipe(
            "sample",
            "0" * 64,
            NormalizationOptions(),
            frozenset(),
            (CompiledBlock("1", "paragraph", (Fragment("text/chapter.xhtml", "1"),)),),
            content=ContentOptions(
                element_rules=(
                    ElementRule(
                        ElementSelector(tag="p", attributes=(("id", "keep"),)), "block"
                    ),
                )
            ),
        )

    def test_pre_060_compiled_digest_is_unchanged(self) -> None:
        # Captured using the published 0.5.0 package, not the development checkout.
        self.assertEqual(
            compiled_recipe_digest(self.legacy_plan()),
            "e50c66c05d7a2f8c3b3e52382a2700ffe7794f8c27037b67564fa938b0ea656b",
        )

    def test_empty_prefix_map_preserves_digest_and_mapping_order_is_canonical(
        self,
    ) -> None:
        epub, recipe = self.sample('<p id="keep_1" data-kind="note_1">A</p><p>B</p>')
        match: dict[str, object] = {"tag": "p"}
        recipe["source_blocks"] = {
            "element_rules": [{"match": match, "action": "block"}]
        }
        before = compiled_recipe_digest(finalize_recipe(epub, recipe))
        match["attribute_prefixes"] = {}
        self.assertEqual(compiled_recipe_digest(finalize_recipe(epub, recipe)), before)
        match["attribute_prefixes"] = {"id": "keep_", "data-kind": "note_"}
        first = finalize_recipe(epub, recipe)
        match["attribute_prefixes"] = {"data-kind": "note_", "id": "keep_"}
        self.assertEqual(
            compiled_recipe_digest(finalize_recipe(epub, recipe)),
            compiled_recipe_digest(first),
        )
        match["attribute_prefixes"] = {"data-kind": "note_", "id": "keep"}
        second = finalize_recipe(epub, recipe)
        self.assertEqual(first.blocks, second.blocks)
        self.assertNotEqual(
            compiled_recipe_digest(first), compiled_recipe_digest(second)
        )
        self.assertNotEqual(before, compiled_recipe_digest(first))

    def test_compiled_prefixes_require_canonical_valid_immutable_pairs(self) -> None:
        original = self.legacy_plan()
        invalid: list[object] = [
            (("id", ""),),
            (("", "page_"),),
            (("id", 1),),
            (("id", "a"), ("id", "b")),
            (("z", "a"), ("a", "b")),
            [("id", "page_")],
            (("id",),),
            "id",
            None,
        ]
        for prefixes in invalid:
            with self.subTest(prefixes=prefixes), self.assertRaises(EpubBlocksError):
                content = ContentOptions(
                    element_rules=(
                        ElementRule(
                            ElementSelector(
                                attribute_prefixes=cast(
                                    tuple[tuple[str, str], ...], prefixes
                                )
                            ),
                            "block",
                        ),
                    )
                )
                compiled_recipe_digest(replace(original, content=content))
