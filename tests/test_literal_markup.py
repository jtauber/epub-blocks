from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from xml.etree import ElementTree as ET

import test_verbatim_and_source_order as verbatim

from epub_blocks import (
    EpubBlocksError,
    compile_recipe,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_candidates,
    write_tsv,
)


class LiteralMarkupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.helper = verbatim.VerbatimAndSourceOrderTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def sample(self, body: str) -> tuple[Path, dict[str, object], dict[str, object]]:
        epub, recipe, markup = self.helper.sample(body, "literal")
        recipe["normalization"] = {
            "collapse_whitespace": False,
            "strip": False,
            "unicode_normalization": "none",
        }
        markup.update(
            rules=[
                {"match": {"tag": "em"}, "kind": "span", "name": "em"},
                {"match": {"tag": "p"}, "kind": "span", "name": "paragraph"},
                {"match": {"tag": "br"}, "kind": "milestone", "name": "break"},
                {
                    "match": {"tag": "a"},
                    "kind": "milestone",
                    "name": "page",
                    "label_attribute": "id",
                    "label_strip_prefix": "page",
                },
            ],
            delimiters={
                "em": ["⧼", "⧽"],
                "paragraph": ["¶", "¶"],
                "break": ["∥", ""],
                "page": ["⟦", "⟧"],
            },
            strip_outer_whitespace=True,
            remove_source_newlines=True,
            between_blocks="ignore",
            trailing="ignore",
        )
        return epub, recipe, markup

    def read(self, epub: Path, recipe: dict[str, object]) -> list[str]:
        return self.helper.read(epub, recipe)

    def test_literal_notation_and_exact_whitespace(self) -> None:
        epub, recipe, _ = self.sample(
            "<pre> \n<p>\u00a0\u00a0</p>\n<p>A<br/>B</p> </pre>"
        )
        self.assertEqual(self.read(epub, recipe), ["¶\u00a0\u00a0¶¶A∥B¶"])

    def test_no_escape_layer_is_added(self) -> None:
        epub, recipe, _ = self.sample("<pre>&lt;tag&gt; \\ ⧼ ¶ &amp;</pre>")
        self.assertEqual(self.read(epub, recipe), ["<tag> \\ ⧼ ¶ &"])

    def test_trim_stops_at_markers_and_preserves_nbsp(self) -> None:
        epub, recipe, _ = self.sample(
            '<pre> \n<em> \u00a0 </em> \n</pre><pre><a id="page7"/>  text  </pre>'
        )
        self.assertEqual(self.read(epub, recipe), ["⧼ \u00a0 ⧽", "⟦7⟧  text"])

    def test_empty_marked_spans_are_trim_barriers(self) -> None:
        epub, recipe, _ = self.sample("<pre> <em/>  text  <em/> </pre>")
        self.assertEqual(self.read(epub, recipe), ["⧼⧽  text  ⧼⧽"])

    def test_remove_newlines_is_not_whitespace_collapse(self) -> None:
        epub, recipe, markup = self.sample("<pre>A\nB  C\u00a0D</pre>")
        markup["strip_outer_whitespace"] = False
        self.assertEqual(self.read(epub, recipe), ["AB  C\u00a0D"])

    def test_ignored_detached_pages_do_not_remove_inline_pages(self) -> None:
        epub, recipe, _ = self.sample(
            '<a id="page1"/><pre>A<a id="page2"/>B</pre><a id="page3"/>'
        )
        self.assertEqual(self.read(epub, recipe), ["A⟦2⟧B"])

    def test_prefix_is_exact_not_a_global_label_replacement(self) -> None:
        epub, recipe, _ = self.sample('<pre>A<a id="page07page"/>B</pre>')
        self.assertEqual(self.read(epub, recipe), ["A⟦07page⟧B"])

    def test_missing_prefix_or_empty_suffix_is_an_error(self) -> None:
        for label in ("wrong", "page"):
            with self.subTest(label=label):
                epub, recipe, _ = self.sample(f'<pre>A<a id="{label}"/>B</pre>')
                with self.assertRaisesRegex(EpubBlocksError, "label_strip_prefix"):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_xml_has_the_same_cleanup_without_literal_delimiters(self) -> None:
        epub, recipe, markup = self.sample("<pre> <em> \u00a0 </em>\nA<br/>B </pre>")
        literal = self.read(epub, recipe)[0]
        markup["format"] = "xml"
        markup["delimiters"] = {}
        xml = self.read(epub, recipe)[0]
        self.assertEqual(literal, "⧼ \u00a0 ⧽A∥B")
        self.assertEqual(xml, "<em> \u00a0 </em>A<break/>B")
        self.assertEqual(
            "".join(ET.fromstring("<root>" + xml + "</root>").itertext()), " \u00a0 AB"
        )

    def test_candidates_and_slices_use_cleaned_readings(self) -> None:
        epub, recipe, _ = self.sample("<pre> \nAB\nCD </pre>")
        self.assertEqual(
            [r.text for r in extract_recipe_candidates(epub, recipe)], ["ABCD"]
        )
        cast(dict[str, object], recipe["output"])["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "part",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1",
                                "slice": {"start": 1, "end": 3},
                            }
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(self.read(epub, recipe), ["BC"])

    def test_join_retains_marked_padding_and_uses_cleanup(self) -> None:
        epub, recipe, _ = self.sample("<pre> <em> A </em> </pre><pre> B </pre>")
        cast(dict[str, object], recipe["output"])["rules"] = [
            {
                "match": {"locators": ["text/chapter.xhtml#1"]},
                "type": "paragraph",
                "consume": 2,
            }
        ]
        self.assertEqual(self.read(epub, recipe), ["⧼ A ⧽ B"])

    def test_literal_output_is_still_unquoted_tsv(self) -> None:
        epub, recipe, _ = self.sample('<pre>"quoted"<br/>end</pre>')
        self.read(epub, recipe)
        target = self.helper.directory / "result.tsv"
        write_tsv(target, extract_recipe(epub, recipe))
        self.assertEqual(target.read_text().split("\t")[2], '"quoted"∥end\n')

    def test_tsv_still_rejects_literal_control_characters(self) -> None:
        epub, recipe, markup = self.sample("<pre>A\tB</pre>")
        markup["remove_source_newlines"] = False
        self.read(epub, recipe)
        with self.assertRaises(EpubBlocksError):
            write_tsv(self.helper.directory / "bad.tsv", extract_recipe(epub, recipe))

    def test_literal_tokens_can_share_prefixes_but_not_all_be_empty(self) -> None:
        epub, recipe, markup = self.sample("<pre><p>X</p><em>Y</em></pre>")
        cast(dict[str, object], markup["delimiters"])["em"] = ["¶x", "¶"]
        self.assertEqual(self.read(epub, recipe), ["¶X¶¶xY¶"])
        cast(dict[str, object], markup["delimiters"])["em"] = ["", ""]
        with self.assertRaises(EpubBlocksError):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_invalid_literal_options_fail_schema_and_runtime(self) -> None:
        for field, value in (
            ("strip_outer_whitespace", 1),
            ("remove_source_newlines", "yes"),
            ("delimiters", {"em": ["", ""]}),
            ("delimiters", {"em": ["a", 1]}),
            ("delimiters", {"em": ["a"]}),
            ("delimiters", {"em": ["a\n", "b"]}),
        ):
            with self.subTest(field=field, value=value):
                epub, recipe, markup = self.sample("<pre>Text</pre>")
                markup[field] = value
                self.assertFalse(self.helper.schema.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                with self.assertRaises(EpubBlocksError):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_prefix_requires_attribute_label(self) -> None:
        epub, recipe, markup = self.sample("<pre>Text</pre>")
        cast(list[dict[str, object]], markup["rules"])[0]["label_strip_prefix"] = "page"
        self.assertFalse(self.helper.schema.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        with self.assertRaisesRegex(EpubBlocksError, "requires label_attribute"):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_original_delimiter_grammar_remains_strict(self) -> None:
        epub, recipe, markup = self.sample("<pre>Text</pre>")
        markup["format"] = "delimiters"
        self.assertFalse(self.helper.schema.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        with self.assertRaises(EpubBlocksError):
            compile_recipe(epub, recipe, verify_digest=False)

    def test_options_are_pinned_and_compiled_values_validated(self) -> None:
        epub, recipe, markup = self.sample("<pre>A</pre>")
        first = compile_recipe(epub, recipe, verify_digest=False)
        for field in ("strip_outer_whitespace", "remove_source_newlines"):
            changed = copy.deepcopy(recipe)
            cast(dict[str, object], cast(dict[str, object], changed["text"])["markup"])[
                field
            ] = False
            self.assertNotEqual(
                compiled_recipe_digest(first),
                compiled_recipe_digest(
                    compile_recipe(epub, changed, verify_digest=False)
                ),
            )
            assert first.content.markup is not None
            invalid = replace(
                first,
                content=replace(
                    first.content, markup=replace(first.content.markup, **{field: 1})
                ),
            )  # type: ignore[arg-type]
            with self.assertRaises(EpubBlocksError):
                compiled_recipe_digest(invalid)
        markup.pop("strip_outer_whitespace")
        markup.pop("remove_source_newlines")
        absent = compile_recipe(epub, recipe, verify_digest=False)
        markup.update(strip_outer_whitespace=False, remove_source_newlines=False)
        self.assertEqual(absent, compile_recipe(epub, recipe, verify_digest=False))
