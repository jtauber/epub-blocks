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
from xml.etree import ElementTree

from jsonschema import (  # pyright: ignore[reportMissingModuleSource]
    Draft202012Validator,
)
from test_epub_blocks import (
    AUXILIARY,
    PACKAGE,
    finalize_recipe,
    make_epub,
    minimal_recipe,
)

from epub_blocks import (
    ElementRule,
    ElementSelector,
    EpubBlocksError,
    Fragment,
    compile_recipe,
    compiled_recipe_digest,
    extract_recipe,
    write_tsv,
)
from epub_blocks.models import ContentOptions


class ContentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def sample(
        self, body: str, auxiliary: str = "Auxiliary."
    ) -> tuple[Path, dict[str, object]]:
        def document(text: str) -> str:
            return (
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                + text
                + "</body></html>"
            )

        epub = make_epub(
            self.directory,
            documents={
                "text/chapter.xhtml": document(body),
                "text/aux.xhtml": document(auxiliary)
                if auxiliary != "Auxiliary."
                else AUXILIARY,
            },
        )
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )
        recipe["omit_epub_types"] = []
        return epub, recipe

    def output(self, epub: Path, recipe: dict[str, object]) -> list[tuple[str, str]]:
        finalize_recipe(epub, recipe)
        return [(b.block_id, b.text) for b in extract_recipe(epub, recipe)]

    def markup(
        self, recipe: dict[str, object], format_name: str = "xml"
    ) -> dict[str, object]:
        markup: dict[str, object] = {
            "format": format_name,
            "between_blocks": "next",
            "trailing": "previous",
            "rules": [
                {"match": {"tag": "em"}, "kind": "span", "name": "em"},
                {"match": {"tag": "strong"}, "kind": "span", "name": "strong"},
                {
                    "match": {"epub_types": ["pagebreak"]},
                    "kind": "milestone",
                    "name": "page",
                    "label_attribute": "title",
                },
                {
                    "match": {"tag": "div", "classes_all": ["section_break"]},
                    "kind": "milestone",
                    "name": "scene-break",
                },
            ],
            "delimiters": {
                "em": ["⧼", "⧽"],
                "strong": ["⟪", "⟫"],
                "page": ["⟦", "⟧"],
                "scene-break": ["⟬", "⟭"],
            },
        }
        recipe["text"] = {"markup": markup}
        return markup

    def test_xml_and_delimiters_share_references_and_escape_literal_text(self) -> None:
        epub, recipe = self.sample(
            '<p>A &amp; &lt;x&gt; ⧼ \\ <em>nested <strong>bold</strong></em><span epub:type="pagebreak" title="iv&amp;&quot;"/></p>'
        )
        markup = self.markup(recipe)
        self.assertEqual(
            self.output(epub, recipe),
            [
                (
                    "001",
                    'A &amp; &lt;x&gt; ⧼ \\ <em>nested <strong>bold</strong></em><page label="iv&amp;&quot;"/>',
                )
            ],
        )
        markup["format"] = "delimiters"
        self.assertEqual(
            self.output(epub, recipe),
            [("001", 'A & <x> \\⧼ \\\\ ⧼nested ⟪bold⟫⧽⟦iv&"⟧')],
        )

    def test_detached_milestones_do_not_consume_numbers(self) -> None:
        epub, recipe = self.sample(
            '<div><span epub:type="pagebreak" title="1"/></div><p>First.</p><div class="section_break"/><p><span epub:type="pagebreak" title="2"/></p><p>Second.</p><span epub:type="pagebreak" title="3"/>'
        )
        self.markup(recipe)
        self.assertEqual(
            self.output(epub, recipe),
            [
                ("001", '<page label="1"/>First.'),
                ("002", '<scene-break/><page label="2"/>Second.<page label="3"/>'),
            ],
        )

    def test_xml_round_trip_preserves_text_and_label_whitespace(self) -> None:
        epub, recipe = self.sample(
            '<p>A&#13;B<span epub:type="pagebreak" title="a&#9;b&#10;c&#13;d"/></p>'
        )
        recipe["normalization"] = {"collapse_whitespace": False}
        markup = self.markup(recipe)
        serialized = self.output(epub, recipe)[0][1]
        self.assertEqual(serialized, 'A&#13;B<page label="a&#9;b&#10;c&#13;d"/>')
        root = ElementTree.fromstring("<root>" + serialized + "</root>")
        self.assertEqual(root.text, "A\rB")
        self.assertEqual(root[0].get("label"), "a\tb\nc\rd")
        output_path = self.directory / "round-trip.tsv"
        write_tsv(output_path, extract_recipe(epub, recipe))
        self.assertEqual(
            output_path.read_text(encoding="utf-8"),
            "001\tparagraph\t" + serialized + "\n",
        )
        markup["format"] = "delimiters"
        self.assertEqual(self.output(epub, recipe)[0][1], "A\rB⟦a\tb\nc\rd⟧")
        with self.assertRaisesRegex(EpubBlocksError, "plain TSV"):
            write_tsv(output_path, extract_recipe(epub, recipe))
        self.assertEqual(
            output_path.read_text(encoding="utf-8"),
            "001\tparagraph\t" + serialized + "\n",
        )

    def test_xml_rejects_unrepresentable_join_characters(self) -> None:
        epub, recipe = self.sample("<p>One.</p><p>Two.</p>")
        self.markup(recipe)
        cast(dict[str, object], recipe["output"])["rules"] = [
            {
                "match": {"tag": "p"},
                "type": "paragraph",
                "consume": 2,
                "separator": "\x00",
            }
        ]
        with self.assertRaisesRegex(EpubBlocksError, "not allowed in XML"):
            self.output(epub, recipe)

    def test_detached_events_cross_files_in_the_selected_scope(self) -> None:
        epub, recipe = self.sample(
            '<p>First.</p><span epub:type="pagebreak" title="ii"/>',
            '<span epub:type="pagebreak" title="iii"/><p>Second.</p>',
        )
        source = cast(dict[str, object], recipe["source_blocks"])
        source["include_documents"] = ["text/*.xhtml"]
        source["include_non_linear"] = True
        self.markup(recipe)
        self.assertEqual(
            self.output(epub, recipe),
            [
                ("001", "First."),
                ("002", '<page label="ii"/><page label="iii"/>Second.'),
            ],
        )

    def test_between_parts_of_a_consuming_rule_preserves_event_position(self) -> None:
        epub, recipe = self.sample(
            '<p>First.</p><div class="section_break"/><p>Second.</p>'
        )
        self.markup(recipe)
        output = cast(dict[str, object], recipe["output"])
        output["rules"] = [{"match": {"tag": "p"}, "type": "paragraph", "consume": 2}]
        self.assertEqual(
            self.output(epub, recipe), [("001", "First. <scene-break/>Second.")]
        )

    def test_slices_assign_shared_boundary_milestone_only_to_right_slice(self) -> None:
        epub, recipe = self.sample(
            '<p><em>AB<span epub:type="pagebreak" title="1"/>CD<span epub:type="pagebreak" title="2"/></em></p>'
        )
        markup = self.markup(recipe)
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
                                "slice": {"start": start, "end": end},
                            }
                        ],
                    }
                    for start, end in ((0, 2), (2, 4))
                ],
            }
        ]
        self.assertEqual(
            self.output(epub, recipe),
            [
                ("001", "<em>AB</em>"),
                ("002", '<em><page label="1"/>CD<page label="2"/></em>'),
            ],
        )
        markup["format"] = "delimiters"
        self.assertEqual(
            self.output(epub, recipe), [("001", "⧼AB⧽"), ("002", "⧼⟦1⟧CD⟦2⟧⧽")]
        )

    def test_duplicate_milestones_and_reordered_detached_anchors_are_rejected(
        self,
    ) -> None:
        for body, paths, message in (
            (
                '<p>Text.<span epub:type="pagebreak" title="1"/></p>',
                ("1", "1"),
                "is reused",
            ),
            (
                '<p>First.</p><div class="section_break"/><p>Second.</p>',
                ("3", "1"),
                "source-ordered",
            ),
        ):
            with self.subTest(paths=paths):
                epub, recipe = self.sample(body)
                self.markup(recipe)
                cast(dict[str, object], recipe["output"])["replacements"] = [
                    {
                        "anchor": "text/chapter.xhtml#1",
                        "outputs": [
                            {
                                "type": "paragraph",
                                "parts": [
                                    {
                                        "document": "text/chapter.xhtml",
                                        "element_path": path,
                                    }
                                ],
                            }
                            for path in paths
                        ],
                    }
                ]
                with self.assertRaisesRegex(EpubBlocksError, message):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_detached_policy_is_explicit_and_missing_labels_fail(self) -> None:
        for body, update, message in (
            (
                '<span epub:type="pagebreak" title="1"/><p>Text.</p>',
                {"between_blocks": "error"},
                "between_blocks",
            ),
            (
                '<p>Text.</p><span epub:type="pagebreak" title="1"/>',
                {"trailing": "error"},
                "trailing",
            ),
            ('<p>Text.<span epub:type="pagebreak"/></p>', {}, "label attribute"),
        ):
            with self.subTest(body=body):
                epub, recipe = self.sample(body)
                self.markup(recipe).update(update)
                with self.assertRaisesRegex(EpubBlocksError, message):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_whole_list_item_and_boundaries_without_inline_word_spaces(self) -> None:
        epub, recipe = self.sample("<li>Open.<p>in<em>side</em></p>Tail.</li>")
        source = cast(dict[str, object], recipe["source_blocks"])
        source["element_rules"] = [{"match": {"tag": "li"}, "action": "block"}]
        source["strict_coverage"] = True
        self.markup(recipe)
        cast(dict[str, object], recipe["text"])["block_boundaries"] = {
            "tags": ["p"],
            "separator": " ",
        }
        self.assertEqual(
            self.output(epub, recipe), [("001", "Open. in<em>side</em> Tail.")]
        )

    def test_intentional_empty_blocks_need_both_permissions(self) -> None:
        epub, recipe = self.sample(
            '<p>First.</p><div class="section_break"/><p>Second.</p>'
        )
        source = cast(dict[str, object], recipe["source_blocks"])
        source["element_rules"] = [
            {
                "match": {"tag": "div", "classes_all": ["section_break"]},
                "action": "block",
                "keep_empty": True,
            }
        ]
        with self.assertRaisesRegex(EpubBlocksError, "allow_empty"):
            compile_recipe(epub, recipe, verify_digest=False)
        output = cast(dict[str, object], recipe["output"])
        output["rules"] = [
            {"match": {"tag": "div"}, "type": "scene-break", "allow_empty": True}
        ]
        self.assertEqual(
            self.output(epub, recipe),
            [("001", "First."), ("002", ""), ("003", "Second.")],
        )

    def test_skip_descend_coverage_and_preserved_tails(self) -> None:
        epub, recipe = self.sample(
            "<blockquote>Outside.<p>Inside.</p>Tail.</blockquote>"
        )
        source = cast(dict[str, object], recipe["source_blocks"])
        source["strict_coverage"] = True
        with self.assertRaisesRegex(EpubBlocksError, "unclaimed"):
            compile_recipe(epub, recipe, verify_digest=False)
        source["element_rules"] = [
            {"match": {"tag": "blockquote"}, "action": "block"},
            {"match": {"tag": "p"}, "action": "skip"},
        ]
        self.assertEqual(self.output(epub, recipe), [("001", "Outside.Tail.")])
        epub, recipe = self.sample("<p><li>Inside.</li></p>")
        source = cast(dict[str, object], recipe["source_blocks"])
        source["element_rules"] = [{"match": {"tag": "p"}, "action": "descend"}]
        self.assertEqual(self.output(epub, recipe), [("001", "Inside.")])

    def test_contextual_gap_rules_omit_layout_not_internal_gaps(self) -> None:
        epub, recipe = self.sample(
            "<h1>Title</h1><p>\u00a0</p><p>One.</p><p>\u00a0</p><p>Two.</p><p><br/></p>"
        )
        source = cast(dict[str, object], recipe["source_blocks"])
        source["element_rules"] = [
            {
                "match": {"tag": "p", "empty": True, "previous_sibling": {"tag": "h1"}},
                "action": "skip",
            },
            {
                "match": {"tag": "p", "empty": True, "has_child": {"tag": "br"}},
                "action": "skip",
            },
        ]
        markup = self.markup(recipe)
        cast(list[object], markup["rules"]).append(
            {"match": {"tag": "p", "empty": True}, "kind": "milestone", "name": "gap"}
        )
        self.assertEqual(
            self.output(epub, recipe),
            [("001", "Title"), ("002", "One."), ("003", "<gap/>Two.")],
        )

    def test_unicode_whitespace_nesting_and_slices(self) -> None:
        epub, recipe = self.sample("<p>  A <em>  e\u0301  B </em> C  </p>")
        self.markup(recipe)
        self.assertEqual(self.output(epub, recipe), [("001", "A <em>é B </em>C")])
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
                                "slice": {"start": 2, "end": 5},
                            }
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(self.output(epub, recipe), [("001", "<em>é B</em>")])
        epub, recipe = self.sample("<p>e<em>\u0301</em></p>")
        self.markup(recipe)
        with self.assertRaisesRegex(EpubBlocksError, "normalization crosses"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_prefix_removal_clips_spans_without_unbalanced_markup(self) -> None:
        epub, recipe = self.sample("<h1><em>CHAPTER I Title</em></h1>")
        self.markup(recipe)
        output = cast(dict[str, object], recipe["output"])
        output["rules"] = [
            {
                "match": {"tag": "h1"},
                "type": "heading",
                "remove_prefix": {"pattern": "^CHAPTER I "},
            }
        ]
        self.assertEqual(self.output(epub, recipe), [("001", "<em>Title</em>")])

    def test_omissions_keep_original_source_locators(self) -> None:
        epub, recipe = self.sample("<p><span>Delete</span><em>Keep</em> tail</p>")
        self.markup(recipe)
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
                            }
                        ],
                    }
                ],
            }
        ]
        markup = cast(
            dict[str, object], cast(dict[str, object], recipe["text"])["markup"]
        )
        markup["rules"] = [
            {
                "match": {"locators": ["text/chapter.xhtml#1.2"]},
                "kind": "span",
                "name": "em",
            }
        ]
        markup.pop("delimiters")
        self.assertEqual(self.output(epub, recipe), [("001", "<em>Keep</em> tail")])

    def test_bad_settings_and_conflicts_rejected(self) -> None:
        epub, recipe = self.sample("<p>Text.</p>")
        changes: list[dict[str, object]] = [
            {"format": "html"},
            {"trailing": "next"},
            {"rules": [{"match": {}, "kind": "span", "name": "x"}]},
            {"rules": [{"match": {"tag": "em"}, "kind": "span", "name": "x:y"}]},
            {"delimiters": {"em": ["a", "ab"], "strong": ["ab", "c"]}},
            {"format": "delimiters", "delimiters": {}},
        ]
        for change in changes:
            with self.subTest(change=change):
                candidate = copy.deepcopy(recipe)
                self.markup(candidate).update(change)
                with self.assertRaises(EpubBlocksError):
                    compile_recipe(epub, candidate, verify_digest=False)
        self.markup(recipe)
        recipe["omit_epub_types"] = ["pagebreak"]
        with self.assertRaisesRegex(EpubBlocksError, "semantically omitted"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_schema_accepts_both_profiles_and_rejects_unknown_fields(self) -> None:
        epub, recipe = self.sample(
            '<p>Text.</p><div class="section_break"/><p>More.</p>'
        )
        schema = json.loads(
            files("epub_blocks").joinpath("schemas/recipe-v1.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        source = cast(dict[str, object], recipe["source_blocks"])
        source["strict_coverage"] = True
        source["element_rules"] = [
            {
                "match": {"tag": "p", "empty": True, "has_child": {"tag": "br"}},
                "action": "skip",
            }
        ]
        markup = self.markup(recipe)
        for format_name in ("xml", "delimiters"):
            markup["format"] = format_name
            finalize_recipe(epub, recipe)
            self.assertTrue(validator.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        markup["typo"] = True
        self.assertFalse(validator.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        with self.assertRaisesRegex(EpubBlocksError, "unknown"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_more_invalid_policies(self) -> None:
        epub, base = self.sample("<p>Text.</p>")
        sources: list[dict[str, object]] = [
            {"strict_coverage": 1},
            {"element_rules": {}},
            {"element_rules": [{"match": {"tag": "p"}, "action": "drop"}]},
            {
                "element_rules": [
                    {"match": {"tag": "p"}, "action": "skip", "keep_empty": False}
                ]
            },
            {
                "element_rules": [
                    {
                        "match": {
                            "tag": "p",
                            "previous_sibling": {"has_child": {"tag": "p"}},
                        },
                        "action": "block",
                    }
                ]
            },
        ]
        for change in sources:
            recipe = copy.deepcopy(base)
            cast(dict[str, object], recipe["source_blocks"]).update(change)
            with self.subTest(change=change), self.assertRaises(EpubBlocksError):
                compile_recipe(epub, recipe, verify_digest=False)
        texts: list[object] = [
            None,
            [],
            {"markup": []},
            {"block_boundaries": {"tags": [1]}},
            {"block_boundaries": {"tags": ["p", "p"]}},
            {"block_boundaries": {"separator": "prose"}},
            {
                "markup": {
                    "rules": [
                        {
                            "match": {"attributes": {"x": 1}},
                            "kind": "span",
                            "name": "em",
                        }
                    ]
                }
            },
            {
                "markup": {
                    "rules": [
                        {
                            "match": {"tag": "em"},
                            "kind": "span",
                            "name": "em",
                            "label_attribute": "id",
                        }
                    ]
                }
            },
            {
                "markup": {
                    "rules": [
                        {"match": {"tag": "em"}, "kind": "span", "name": "same"},
                        {"match": {"tag": "span"}, "kind": "milestone", "name": "same"},
                    ]
                }
            },
            {"markup": {"delimiters": {"unused": ["a", "b"]}}},
        ]
        for value in texts:
            recipe = copy.deepcopy(base)
            recipe["text"] = value
            with self.subTest(value=value), self.assertRaises(EpubBlocksError):
                compile_recipe(epub, recipe, verify_digest=False)

    def test_selector_conjunction_filters_and_no_false_retention(self) -> None:
        epub, recipe = self.sample(
            '<p class="keep extra" data-kind="yes">Keep.</p><p class="keep" data-kind="no">Plain.</p><p class="excluded">Excluded.</p>'
        )
        source = cast(dict[str, object], recipe["source_blocks"])
        source["exclude_classes"] = ["EXCLUDED"]
        recipe["text"] = {
            "markup": {
                "rules": [
                    {
                        "match": {
                            "tag": "p",
                            "classes": ["extra", "keep"],
                            "classes_any": ["keep"],
                            "attributes": {"data-kind": "yes"},
                        },
                        "kind": "span",
                        "name": "marked",
                    }
                ]
            }
        }
        self.assertEqual(
            self.output(epub, recipe),
            [("001", "<marked>Keep.</marked>"), ("002", "Plain.")],
        )

    def test_nested_milestone_matches_are_not_silently_dropped(self) -> None:
        epub, recipe = self.sample(
            '<p>Text.</p><div class="section_break"><span epub:type="pagebreak" title="3"/></div>'
        )
        self.markup(recipe)
        with self.assertRaisesRegex(EpubBlocksError, "nested milestone"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_strict_coverage_includes_tails_and_body_text(self) -> None:
        for body in (
            "Unclaimed.<p>Text.</p>",
            "<div><p>Text.</p>Tail.</div>",
            "<p>Text.</p>Tail.",
        ):
            epub, recipe = self.sample(body)
            cast(dict[str, object], recipe["source_blocks"])["strict_coverage"] = True
            with (
                self.subTest(body=body),
                self.assertRaisesRegex(EpubBlocksError, "unclaimed"),
            ):
                compile_recipe(epub, recipe, verify_digest=False)

    def test_compiled_policy_and_empty_permissions_are_pinned(self) -> None:
        epub, recipe = self.sample("<p>Text.</p>")
        original = finalize_recipe(epub, recipe)
        recipe["text"] = {"block_boundaries": {"tags": [], "separator": "\n"}}
        self.assertEqual(
            compiled_recipe_digest(finalize_recipe(epub, recipe)),
            compiled_recipe_digest(original),
        )
        with self.assertRaisesRegex(EpubBlocksError, "boolean"):
            compiled_recipe_digest(
                replace(original, content=ContentOptions(strict_coverage=cast(bool, 1)))
            )
        self.assertNotEqual(
            compiled_recipe_digest(original),
            compiled_recipe_digest(
                replace(
                    original, blocks=(replace(original.blocks[0], allow_empty=True),)
                )
            ),
        )

    def test_explicit_selection_does_not_restore_discarded_milestones(self) -> None:
        epub, recipe = self.sample(
            '<p>Intro.</p><div><span epub:type="pagebreak" title="5"/>'
            '<span>ABCD</span></div><span epub:type="pagebreak" title="6"/>'
            "<p>End.</p>"
        )
        markup = self.markup(recipe)
        for format_name in ("xml", "delimiters"):
            markup["format"] = format_name
            page5 = '<page label="5"/>' if format_name == "xml" else "⟦5⟧"
            page6 = '<page label="6"/>' if format_name == "xml" else "⟦6⟧"
            for selection, reading in (
                ({"omit": ["1"]}, "ABCD"),
                ({"slice": {"start": 2, "end": 4}}, "CD"),
                ({}, page5 + "ABCD"),
            ):
                with self.subTest(format=format_name, selection=selection):
                    cast(dict[str, object], recipe["output"])["insertions"] = [
                        {
                            "after": "text/chapter.xhtml#1",
                            "outputs": [
                                {
                                    "type": "paragraph",
                                    "parts": [
                                        {
                                            "document": "text/chapter.xhtml",
                                            "element_path": "2",
                                            **selection,
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                    self.assertEqual(
                        self.output(epub, recipe),
                        [("001", "Intro."), ("002", reading), ("003", page6 + "End.")],
                    )

    def test_out_of_scope_notes_do_not_anchor_main_text_milestones(self) -> None:
        def document(body: str) -> str:
            return (
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                + body
                + "</body></html>"
            )

        main = document(
            '<p>Main.</p><span epub:type="pagebreak" title="3"/><p>Next.</p><span epub:type="pagebreak" title="4"/>'
        )
        note = document('<p>Note.<span epub:type="pagebreak" title="77"/></p>')
        for scope, package in (
            ("non-spine", PACKAGE.replace('<itemref idref="aux" linear="no"/>', "")),
            ("non-linear", PACKAGE),
            (
                "excluded-linear",
                PACKAGE.replace('idref="aux" linear="no"', 'idref="aux"'),
            ),
        ):
            epub = make_epub(
                self.directory,
                package=package,
                documents={"text/chapter.xhtml": main, "text/aux.xhtml": note},
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
            )
            recipe["omit_epub_types"] = []
            markup = self.markup(recipe)
            for format_name in ("xml", "delimiters"):
                markup["format"] = format_name

                def page(label: str, format_name: str = format_name) -> str:
                    return (
                        f'<page label="{label}"/>'
                        if format_name == "xml"
                        else f"⟦{label}⟧"
                    )

                for anchor in ("1", "3"):
                    with self.subTest(scope=scope, format=format_name, anchor=anchor):
                        cast(dict[str, object], recipe["output"])["insertions"] = [
                            {
                                "after": f"text/chapter.xhtml#{anchor}",
                                "outputs": [
                                    {
                                        "type": "footnote",
                                        "parts": [
                                            {
                                                "document": "text/aux.xhtml",
                                                "element_path": "1",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                        readings = ["Main.", page("3") + "Next." + page("4")]
                        readings.insert(1 if anchor == "1" else 2, "Note." + page("77"))
                        self.assertEqual(
                            self.output(epub, recipe),
                            [(f"{i:03d}", text) for i, text in enumerate(readings, 1)],
                        )

    def test_contextual_selectors_use_original_structure_after_omissions(self) -> None:
        epub, recipe = self.sample("<p><span>Delete</span><em>Keep</em></p>")
        markup = self.markup(recipe)
        markup["delimiters"] = {"marked": ["⧼", "⧽"]}
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
        for match in (
            {"tag": "p", "has_child": {"locators": ["text/chapter.xhtml#1.2"]}},
            {"tag": "p", "has_child": {"tag": "span"}},
            {
                "tag": "em",
                "previous_sibling": {
                    "locators": ["text/chapter.xhtml#1.1"],
                    "empty": False,
                },
            },
        ):
            markup["rules"] = [{"match": match, "kind": "span", "name": "marked"}]
            for format_name in ("xml", "delimiters"):
                with self.subTest(match=match, format=format_name):
                    markup["format"] = format_name
                    expected = (
                        "<marked>Keep</marked>" if format_name == "xml" else "⧼Keep⧽"
                    )
                    self.assertEqual(self.output(epub, recipe), [("001", expected)])

    def test_detached_milestones_require_forward_slice_order(self) -> None:
        epub, recipe = self.sample('<span epub:type="pagebreak" title="1"/><p>ABCD</p>')
        markup = self.markup(recipe)
        for format_name in ("xml", "delimiters"):
            markup["format"] = format_name
            for ranges in (((2, 4), (0, 2)), ((0, 2), (2, 4))):
                cast(dict[str, object], recipe["output"])["replacements"] = [
                    {
                        "anchor": "text/chapter.xhtml#2",
                        "outputs": [
                            {
                                "type": "paragraph",
                                "parts": [
                                    {
                                        "document": "text/chapter.xhtml",
                                        "element_path": "2",
                                        "slice": {"start": start, "end": end},
                                    }
                                ],
                            }
                            for start, end in ranges
                        ],
                    }
                ]
                with self.subTest(format=format_name, ranges=ranges):
                    if ranges[0][0] > ranges[1][0]:
                        with self.assertRaisesRegex(EpubBlocksError, "source-ordered"):
                            compile_recipe(epub, recipe, verify_digest=False)
                    else:
                        page = '<page label="1"/>' if format_name == "xml" else "⟦1⟧"
                        self.assertEqual(
                            self.output(epub, recipe),
                            [("001", page + "AB"), ("002", "CD")],
                        )

    def test_nested_milestone_validation_honours_semantic_omissions(self) -> None:
        epub, recipe = self.sample(
            '<p>Start.</p><div class="section_break"><aside epub:type="noteref"><span class="page" title="9"/></aside></div><p>End.</p>'
        )
        markup = self.markup(recipe)
        rules = cast(list[dict[str, object]], markup["rules"])
        rules[2]["match"] = {"tag": "span", "classes_all": ["page"]}
        recipe["omit_epub_types"] = ["noteref"]
        for format_name in ("xml", "delimiters"):
            with self.subTest(format=format_name):
                markup["format"] = format_name
                scene = "<scene-break/>" if format_name == "xml" else "⟬⟭"
                self.assertEqual(
                    self.output(epub, recipe),
                    [("001", "Start."), ("002", scene + "End.")],
                )
        recipe["omit_epub_types"] = []
        with self.assertRaisesRegex(EpubBlocksError, "nested milestone"):
            compile_recipe(epub, recipe, verify_digest=False)
        recipe["omit_epub_types"] = ["noteref"]
        rules[2]["match"] = {"tag": "aside"}
        with self.assertRaisesRegex(EpubBlocksError, "also semantically omitted"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_nested_milestone_skips_use_original_context_after_omissions(self) -> None:
        epub, recipe = self.sample(
            '<p>Start.</p><div class="section_break"><span class="container">'
            '<span>Delete</span><span epub:type="pagebreak" title="9"/>'
            "</span></div><p>End.</p>"
        )
        cast(dict[str, object], recipe["source_blocks"])["element_rules"] = [
            {
                "match": {
                    "classes_all": ["container"],
                    "has_child": {"locators": ["text/chapter.xhtml#2.1.2"]},
                },
                "action": "skip",
            }
        ]
        cast(dict[str, object], recipe["output"])["insertions"] = [
            {
                "after": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "break",
                        "allow_empty": True,
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "2",
                                "omit": ["1.1"],
                            }
                        ],
                    }
                ],
            }
        ]
        markup = self.markup(recipe)
        for format_name in ("xml", "delimiters"):
            with self.subTest(format=format_name):
                markup["format"] = format_name
                scene = "<scene-break/>" if format_name == "xml" else "⟬⟭"
                self.assertEqual(
                    self.output(epub, recipe),
                    [("001", "Start."), ("002", scene), ("003", "End.")],
                )

    def test_compiled_keep_empty_requires_real_booleans(self) -> None:
        epub, recipe = self.sample("<p>Text.</p>")
        original = finalize_recipe(epub, recipe)
        for invalid in (0, 1, 0.0, 1.0, "", "yes", None):
            content = ContentOptions(
                element_rules=(
                    ElementRule(ElementSelector(tag="p"), "block", cast(bool, invalid)),
                )
            )
            with (
                self.subTest(value=invalid),
                self.assertRaisesRegex(EpubBlocksError, "keep_empty.*boolean"),
            ):
                compiled_recipe_digest(replace(original, content=content))
        for valid in (False, True):
            content = ContentOptions(
                element_rules=(ElementRule(ElementSelector(tag="p"), "block", valid),)
            )
            self.assertEqual(
                len(compiled_recipe_digest(replace(original, content=content))), 64
            )

    def test_skipped_nested_blocks_do_not_add_boundaries(self) -> None:
        epub, base = self.sample(
            '<li>pre<div class="omit" epub:type="noteref"><em>1</em></div>'
            "fix<div>kept</div>end</li>"
        )
        for format_name in ("xml", "delimiters"):
            for separator, collapse in ((" ", True), ("\n", False)):
                for omission in ("structural", "semantic", "explicit"):
                    with self.subTest(
                        format=format_name, separator=separator, omission=omission
                    ):
                        recipe = copy.deepcopy(base)
                        self.markup(recipe, format_name)
                        cast(dict[str, object], recipe["text"])["block_boundaries"] = {
                            "tags": ["div"],
                            "separator": separator,
                        }
                        cast(dict[str, object], recipe["normalization"])[
                            "collapse_whitespace"
                        ] = collapse
                        rules: list[dict[str, object]] = [
                            {"match": {"tag": "li"}, "action": "block"}
                        ]
                        if omission == "structural":
                            rules.append(
                                {"match": {"classes": ["omit"]}, "action": "skip"}
                            )
                        elif omission == "semantic":
                            recipe["omit_epub_types"] = ["noteref"]
                        else:
                            cast(dict[str, object], recipe["output"])[
                                "replacements"
                            ] = [
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
                        cast(dict[str, object], recipe["source_blocks"])[
                            "element_rules"
                        ] = rules
                        self.assertEqual(
                            self.output(epub, recipe),
                            [("001", f"prefix{separator}kept{separator}end")],
                        )

        # An empty but retained block still contributes its configured boundaries.
        epub, recipe = self.sample("<li>Left<div/>Right</li>")
        self.markup(recipe)
        cast(dict[str, object], recipe["text"])["block_boundaries"] = {
            "tags": ["div"],
            "separator": " ",
        }
        self.assertEqual(self.output(epub, recipe), [("001", "Left Right")])

    def test_trailing_milestones_precede_joined_out_of_scope_notes(self) -> None:
        epub, base = self.sample(
            '<p>Intro.</p><span epub:type="pagebreak" title="8"/>'
            '<p><em>Main.</em></p><span epub:type="pagebreak" title="9"/>'
            '<div class="section_break"/>',
            '<p><em>Note.</em><span epub:type="pagebreak" title="77"/></p>'
            "<p>Another note.</p>",
        )
        for format_name in ("xml", "delimiters"):
            for separator in ("", " ", " | "):
                for notes in (1, 2):
                    with self.subTest(
                        format=format_name, separator=separator, notes=notes
                    ):
                        recipe = copy.deepcopy(base)
                        self.markup(recipe, format_name)
                        output = cast(dict[str, object], recipe["output"])
                        output["skip_source"] = ["text/chapter.xhtml#3"]
                        output["insertions"] = [
                            {
                                "after": "text/chapter.xhtml#1",
                                "outputs": [
                                    {
                                        "type": "paragraph",
                                        "separator": separator,
                                        "parts": [
                                            {
                                                "document": "text/chapter.xhtml",
                                                "element_path": "3",
                                            },
                                            *(
                                                {
                                                    "document": "text/aux.xhtml",
                                                    "element_path": str(i),
                                                }
                                                for i in range(1, notes + 1)
                                            ),
                                        ],
                                    }
                                ],
                            }
                        ]
                        main = (
                            '<page label="8"/><em>Main.</em><page label="9"/><scene-break/>'
                            if format_name == "xml"
                            else "⟦8⟧⧼Main.⧽⟦9⟧⟬⟭"
                        )
                        note = (
                            '<em>Note.</em><page label="77"/>'
                            if format_name == "xml"
                            else "⧼Note.⧽⟦77⟧"
                        )
                        expected = separator.join(
                            [main, note, *(["Another note."] if notes == 2 else [])]
                        )
                        self.assertEqual(
                            self.output(epub, recipe),
                            [("001", "Intro."), ("002", expected)],
                        )
                        plan = compile_recipe(epub, recipe)
                        self.assertEqual(
                            plan.blocks[1].milestones_after,
                            (
                                (0, Fragment("text/chapter.xhtml", "4")),
                                (0, Fragment("text/chapter.xhtml", "5")),
                            ),
                        )

    def test_compiled_after_part_milestones_are_validated_and_pinned(self) -> None:
        epub, recipe = self.sample(
            '<p>First.</p><p>Second.</p><span epub:type="pagebreak" title="1"/>'
        )
        self.markup(recipe)
        cast(dict[str, object], recipe["output"])["rules"] = [
            {"match": {"tag": "p"}, "type": "paragraph", "consume": 2}
        ]
        original = finalize_recipe(epub, recipe)
        block = original.blocks[0]
        event = Fragment("text/chapter.xhtml", "3")
        digests = {compiled_recipe_digest(original)}
        for position in (0, 1):
            changed = replace(
                block, milestones=(), milestones_after=((position, event),)
            )
            digests.add(compiled_recipe_digest(replace(original, blocks=(changed,))))
        self.assertEqual(len(digests), 3)
        for invalid in (-1, 2, True, 0.0, "0", None):
            with (
                self.subTest(position=invalid),
                self.assertRaisesRegex(
                    EpubBlocksError, "milestones_after: invalid milestone attachment"
                ),
            ):
                changed = replace(
                    block,
                    milestones=(),
                    milestones_after=((cast(int, invalid), event),),
                )
                compiled_recipe_digest(replace(original, blocks=(changed,)))

    def test_detached_milestones_preserve_hashes_in_document_paths(self) -> None:
        body = (
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            '<div><span epub:type="pagebreak" title="1"/></div>'
            '<p>Text.<span epub:type="pagebreak" title="2"/></p>'
            '<span epub:type="pagebreak" title="3"/></body></html>'
        )
        for path in ("text/chapter#one.xhtml", "text/chapter#one#two.xhtml"):
            epub = make_epub(
                self.directory,
                package=PACKAGE.replace(
                    'href="text/chapter.xhtml"', f'href="{path.replace("#", "%23")}"'
                ),
                documents={path: body},
            )
            for format_name in ("xml", "delimiters"):
                with self.subTest(path=path, format=format_name):
                    recipe = minimal_recipe(
                        epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
                    )
                    recipe["omit_epub_types"] = []
                    cast(dict[str, object], recipe["source_blocks"])[
                        "include_documents"
                    ] = [path]
                    self.markup(recipe, format_name)
                    expected = (
                        '<page label="1"/>Text.<page label="2"/><page label="3"/>'
                        if format_name == "xml"
                        else "⟦1⟧Text.⟦2⟧⟦3⟧"
                    )
                    self.assertEqual(self.output(epub, recipe), [("001", expected)])
                    plan = compile_recipe(epub, recipe)
                    self.assertEqual(
                        [
                            (position, fragment.document_path, fragment.element_path)
                            for position, fragment in plan.blocks[0].milestones
                        ],
                        [(0, path, "1.1"), (1, path, "3")],
                    )


if __name__ == "__main__":
    unittest.main()
