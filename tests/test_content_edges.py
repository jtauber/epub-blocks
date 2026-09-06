from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import cast

from test_epub_blocks import finalize_recipe, make_epub, minimal_recipe

from epub_blocks import (
    EpubBlocksError,
    compile_recipe,
    extract_recipe,
    extract_recipe_candidates,
)


class ContentEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def sample(self, body: str) -> tuple[Path, dict[str, object]]:
        epub = make_epub(
            self.directory,
            documents={
                "text/chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                + body
                + "</body></html>"
            },
        )
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )
        recipe["text"] = {
            "markup": {
                "format": "xml",
                "rules": [{"match": {"tag": "em"}, "kind": "span", "name": "em"}],
            }
        }
        return epub, recipe

    def readings(self, epub: Path, recipe: dict[str, object]) -> list[str]:
        finalize_recipe(epub, recipe)
        return [b.text for b in extract_recipe(epub, recipe)]

    def test_class_attribute_and_semantic_selectors_are_conjunctive(self) -> None:
        epub, recipe = self.sample(
            '<p><span class="only" data-ok="yes">A</span>'
            '<span class="only extra" data-ok="yes">B</span>'
            '<span class="only" data-ok="no">C</span>'
            '<i epub:type="term">D</i><i epub:type="term keyword">E</i></p>'
        )
        recipe["text"] = {
            "markup": {
                "format": "xml",
                "rules": [
                    {
                        "match": {
                            "classes": ["only"],
                            "attributes": {"data-ok": "yes"},
                        },
                        "kind": "span",
                        "name": "exact",
                    },
                    {
                        "match": {"epub_types": ["term", "keyword"]},
                        "kind": "span",
                        "name": "term",
                    },
                ],
            }
        }
        self.assertEqual(
            self.readings(epub, recipe), ["<exact>A</exact>BCD<term>E</term>"]
        )

    def test_semantic_omissions_cannot_also_be_retained_by_nonsemantic_selectors(
        self,
    ) -> None:
        # These selectors match a tag/class, so the recipe parser cannot
        # detect the conflict without inspecting the actual source elements.
        for body in (
            '<p class="retained" epub:type="noteref">Text.</p>',
            '<p>Text.<span class="retained" epub:type="noteref">note</span></p>',
            '<p>Text.<span class="marker"><b><span class="retained" epub:type="noteref">note</span></b></span></p>',
        ):
            with self.subTest(body=body):
                epub, recipe = self.sample(body)
                recipe["text"] = {
                    "markup": {
                        "format": "xml",
                        "rules": [
                            {
                                "match": {"classes_all": ["marker"]},
                                "kind": "milestone",
                                "name": "marker",
                            },
                            {
                                "match": {"classes_all": ["retained"]},
                                "kind": "span",
                                "name": "retained",
                            },
                        ],
                    }
                }
                with self.assertRaisesRegex(
                    EpubBlocksError, "retained markup is also semantically omitted"
                ):
                    compile_recipe(epub, recipe, verify_digest=False)

    def test_semantically_omitted_blocks_and_empty_candidates_are_not_emitted(
        self,
    ) -> None:
        epub, recipe = self.sample(
            '<p epub:type="noteref">Omit me.</p><p> </p><p>Keep me.</p>'
        )
        self.assertEqual(self.readings(epub, recipe), ["Keep me."])
        cast(dict[str, object], recipe["source_blocks"])["include_locators"] = [
            "*#1",
            "*#2",
        ]
        with self.assertRaisesRegex(EpubBlocksError, "no text blocks matched"):
            extract_recipe_candidates(epub, recipe)

    def test_br_inside_markup_preserves_newline_when_whitespace_is_not_collapsed(
        self,
    ) -> None:
        epub, recipe = self.sample("<p><em>First<br/>Second</em></p>")
        cast(dict[str, object], recipe["normalization"])["collapse_whitespace"] = False
        self.assertEqual(self.readings(epub, recipe), ["<em>First\nSecond</em>"])

    def test_unicode_none_preserves_combining_characters_across_markup(self) -> None:
        epub, recipe = self.sample("<p><em>e</em>\u0301</p>")
        with self.assertRaisesRegex(EpubBlocksError, "normalization crosses"):
            compile_recipe(epub, recipe, verify_digest=False)
        cast(dict[str, object], recipe["normalization"])["unicode_normalization"] = (
            "none"
        )
        self.assertEqual(self.readings(epub, recipe), ["<em>e</em>\u0301"])

    def test_rich_slices_reject_out_of_bounds_and_remove_unselected_spans(self) -> None:
        epub, recipe = self.sample("<p><em>A</em>B<em>C</em></p>")
        bounds = {"start": 0, "end": 4}
        cast(dict[str, object], recipe["output"])["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "p",
                        "parts": [
                            {
                                "document": "text/chapter.xhtml",
                                "element_path": "1",
                                "slice": bounds,
                            }
                        ],
                    }
                ],
            }
        ]
        with self.assertRaisesRegex(EpubBlocksError, "outside a 3-code-point fragment"):
            compile_recipe(epub, recipe, verify_digest=False)
        bounds.update(start=1, end=2)
        self.assertEqual(self.readings(epub, recipe), ["B"])

    def test_omitting_every_character_requires_explicit_empty_output_permission(
        self,
    ) -> None:
        for markup in (True, False):
            with self.subTest(markup=markup):
                epub, recipe = self.sample("<p><em>Text.</em></p>")
                if not markup:
                    del recipe["text"]
                produced: dict[str, object] = {
                    "type": "p",
                    "parts": [
                        {
                            "document": "text/chapter.xhtml",
                            "element_path": "1",
                            "omit": ["1"],
                        }
                    ],
                }
                cast(dict[str, object], recipe["output"])["replacements"] = [
                    {"anchor": "text/chapter.xhtml#1", "outputs": [produced]}
                ]
                with self.assertRaisesRegex(
                    EpubBlocksError, "extraction produced no text"
                ):
                    compile_recipe(epub, recipe, verify_digest=False)
                produced["allow_empty"] = True
                self.assertEqual(self.readings(epub, recipe), [""])


if __name__ == "__main__":
    unittest.main()
