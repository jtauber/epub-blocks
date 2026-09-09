from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import cast
from xml.etree import ElementTree as ET

from jsonschema import (  # pyright: ignore[reportMissingModuleSource]
    Draft202012Validator,
)
from test_epub_blocks import AUXILIARY, finalize_recipe, make_epub, minimal_recipe

from epub_blocks import (
    EpubBlocksError,
    compile_recipe,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_candidates,
    write_tsv,
)


class VerbatimAndSourceOrderTests(unittest.TestCase):
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
        self, body: str, fmt: str = "xml"
    ) -> tuple[Path, dict[str, object], dict[str, object]]:
        epub = make_epub(
            self.directory,
            documents={
                "text/chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                + body
                + "</body></html>",
                "text/aux.xhtml": AUXILIARY,
            },
        )
        recipe = minimal_recipe(
            epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
        )
        recipe["omit_epub_types"] = []
        recipe["source_blocks"] = {
            "include_documents": ["text/chapter.xhtml"],
            "strict_coverage": True,
            "element_rules": [{"match": {"tag": "pre"}, "action": "block"}],
        }
        markup: dict[str, object] = {
            "format": fmt,
            "between_blocks": "next",
            "trailing": "previous",
            "rules": [
                {
                    "match": {"tag": "pre"},
                    "kind": "span",
                    "name": "pre",
                    "preserve_whitespace": True,
                },
                {"match": {"tag": "em"}, "kind": "span", "name": "em"},
                {
                    "match": {"tag": "img"},
                    "kind": "milestone",
                    "name": "image",
                    "label_attribute": "src",
                },
            ],
            "delimiters": {"pre": ["⟬", "⟭"], "em": ["⧼", "⧽"], "image": ["⟮", "⟯"]},
        }
        recipe["text"] = {"markup": markup}
        return epub, recipe, markup

    def read(self, epub: Path, recipe: dict[str, object]) -> list[str]:
        self.assertTrue(self.schema.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        finalize_recipe(epub, recipe)
        return [b.text for b in extract_recipe(epub, recipe)]

    def test_xml_preserves_pre_whitespace_and_nested_emphasis(self) -> None:
        epub, recipe, _ = self.sample(
            "<p>  Before   prose. </p><pre>\n  if x:\n\t<em>run()</em>\n</pre><p> After   prose. </p>"
        )
        self.assertEqual(
            self.read(epub, recipe),
            [
                "Before prose.",
                "<pre>&#10;  if x:&#10;&#9;<em>run()</em>&#10;</pre>",
                "After prose.",
            ],
        )
        rows = extract_recipe(epub, recipe)
        target = self.directory / "literal.tsv"
        write_tsv(target, rows)
        self.assertEqual(len(target.read_text().splitlines()), 3)
        self.assertTrue(
            all(len(line.split("\t")) == 3 for line in target.read_text().splitlines())
        )
        self.assertEqual(
            "".join(ET.fromstring(rows[1].text).itertext()), "\n  if x:\n\trun()\n"
        )

    def test_unicode_distinguishes_literal_backslashes_and_control_escapes(
        self,
    ) -> None:
        epub, recipe, _ = self.sample(
            '<pre>\tprint("\\n")\n  \r&#13;⟬</pre>', "delimiters"
        )
        self.assertEqual(self.read(epub, recipe), ['⟬\\tprint("\\\\n")\\n  \\n\\r\\⟬⟭'])
        self.assertEqual(
            [b.text for b in extract_recipe_candidates(epub, recipe)],
            ['\tprint("\\n")\n  \n\r⟬'],
        )

    def test_preserved_whitespace_only_region_survives_strip(self) -> None:
        epub, recipe, _ = self.sample("<pre>\n\t  </pre>")
        self.assertEqual(self.read(epub, recipe), ["<pre>&#10;&#9;  </pre>"])

    def test_nested_pre_and_boundary_normalization_is_idempotent(self) -> None:
        epub, recipe, _ = self.sample(
            '<div class="whole">  Before  <pre>  x\n </pre>  after.  </div>'
        )
        recipe["source_blocks"] = {
            "strict_coverage": True,
            "element_rules": [{"match": {"tag": "div"}, "action": "block"}],
        }
        self.assertEqual(
            self.read(epub, recipe), ["Before <pre>  x&#10; </pre> after."]
        )

    def test_slicing_uses_logical_code_points_not_escape_widths(self) -> None:
        epub, recipe, _ = self.sample("<pre> A\n\tB </pre>")
        cast(dict[str, object], recipe["output"])["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "code",
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
        self.assertEqual(self.read(epub, recipe), ["<pre>&#10;&#9;B</pre>"])

    def test_join_retains_preserved_regions_and_separates_prose(self) -> None:
        epub, recipe, _ = self.sample("<pre> A\n</pre><pre>\tB </pre>")
        cast(dict[str, object], recipe["output"])["rules"] = [
            {"match": {"tag": "pre"}, "type": "code", "consume": 2, "separator": " "}
        ]
        self.assertEqual(
            self.read(epub, recipe), ["<pre> A&#10;</pre> <pre>&#9;B </pre>"]
        )

    def test_false_and_absent_preservation_have_same_digest(self) -> None:
        epub, recipe, markup = self.sample("<pre> A\n B </pre>")
        rules = cast(list[dict[str, object]], markup["rules"])
        rules[0].pop("preserve_whitespace")
        before = compiled_recipe_digest(
            compile_recipe(epub, recipe, verify_digest=False)
        )
        rules[0]["preserve_whitespace"] = False
        self.assertEqual(
            compiled_recipe_digest(compile_recipe(epub, recipe, verify_digest=False)),
            before,
        )
        self.assertEqual(self.read(epub, recipe), ["<pre>A B</pre>"])
        rules[0]["preserve_whitespace"] = True
        self.assertNotEqual(
            compiled_recipe_digest(compile_recipe(epub, recipe, verify_digest=False)),
            before,
        )

    def test_invalid_preservation_and_source_order_options_are_rejected(self) -> None:
        for value in [None, 1, "yes"]:
            epub, recipe, markup = self.sample("<pre>x</pre>")
            cast(list[dict[str, object]], markup["rules"])[0]["preserve_whitespace"] = (
                value
            )
            self.assertFalse(self.schema.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
            with self.assertRaises(EpubBlocksError):
                compile_recipe(epub, recipe, verify_digest=False)
        epub, recipe, markup = self.sample("<pre>x</pre>")
        cast(list[dict[str, object]], markup["rules"])[2]["preserve_whitespace"] = False
        self.assertFalse(self.schema.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        with self.assertRaisesRegex(EpubBlocksError, "only valid for spans"):
            compile_recipe(epub, recipe, verify_digest=False)
        cast(list[dict[str, object]], markup["rules"])[2].pop("preserve_whitespace")
        for value in [None, True, "backwards"]:
            markup["attachment_order"] = value
            self.assertFalse(self.schema.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
            with self.assertRaises(EpubBlocksError):
                compile_recipe(epub, recipe, verify_digest=False)

    def test_control_escape_delimiter_collisions_are_rejected(self) -> None:
        for opening in ["n", "r", "t", "\n", "\t", "\r"]:
            epub, recipe, markup = self.sample("<pre>x</pre>", "delimiters")
            cast(dict[str, object], markup["delimiters"])["pre"] = [opening, "⟭"]
            self.assertFalse(self.schema.is_valid(recipe))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
            with self.assertRaisesRegex(EpubBlocksError, "escapes reserve"):
                compile_recipe(epub, recipe, verify_digest=False)

    def reorder(self, recipe: dict[str, object], paths: list[str]) -> None:
        cast(dict[str, object], recipe["output"])["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#" + min(paths),
                "outputs": [
                    {
                        "type": "paragraph",
                        "parts": [
                            {"document": "text/chapter.xhtml", "element_path": p}
                        ],
                    }
                    for p in paths
                ],
            }
        ]

    def test_opt_in_source_attachment_follows_moved_text_not_output_neighbour(
        self,
    ) -> None:
        epub, recipe, markup = self.sample(
            '<p>First.</p><img src="page.png"/><p>Second.</p>'
        )
        self.reorder(recipe, ["3", "1"])
        with self.assertRaisesRegex(EpubBlocksError, "source-ordered"):
            compile_recipe(epub, recipe, verify_digest=False)
        markup["attachment_order"] = "source"
        self.assertEqual(
            self.read(epub, recipe), ['<image label="page.png"/>Second.', "First."]
        )

    def test_trailing_image_stays_after_source_last_fragment_inside_reordered_join(
        self,
    ) -> None:
        epub, recipe, markup = self.sample(
            '<p>First.</p><p>Last.</p><img src="end.png"/>'
        )
        markup["attachment_order"] = "source"
        cast(dict[str, object], recipe["output"])["replacements"] = [
            {
                "anchor": "text/chapter.xhtml#1",
                "outputs": [
                    {
                        "type": "paragraph",
                        "separator": " ",
                        "parts": [
                            {"document": "text/chapter.xhtml", "element_path": p}
                            for p in ["2", "1"]
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(
            self.read(epub, recipe), ['Last.<image label="end.png"/> First.']
        )

    def test_reordered_slices_attach_prefix_to_first_source_slice(self) -> None:
        epub, recipe, markup = self.sample('<img src="start.png"/><p>ABCD</p>')
        markup["attachment_order"] = "source"
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
                    for start, end in [(2, 4), (0, 2)]
                ],
            }
        ]
        self.assertEqual(
            self.read(epub, recipe), ["CD", '<image label="start.png"/>AB']
        )

    def test_preservation_public_compiled_values_validate_booleans(self) -> None:
        epub, recipe, _ = self.sample("<pre>x</pre>")
        plan = compile_recipe(epub, recipe, verify_digest=False)
        marks = plan.content.markup
        assert marks is not None
        invalid = replace(
            plan,
            content=replace(
                plan.content,
                markup=replace(
                    marks, rules=(replace(marks.rules[0], preserve_whitespace=1),)
                ),
            ),
        )  # pyright: ignore[reportArgumentType]
        with self.assertRaises(EpubBlocksError):
            compiled_recipe_digest(invalid)

    def test_source_order_default_digests_do_not_change(self) -> None:
        epub, recipe, markup = self.sample('<img src="x"/><p>text</p>')
        first = compiled_recipe_digest(
            compile_recipe(epub, recipe, verify_digest=False)
        )
        markup["attachment_order"] = "output"
        self.assertEqual(
            compiled_recipe_digest(compile_recipe(epub, recipe, verify_digest=False)),
            first,
        )
        markup["attachment_order"] = "source"
        self.assertNotEqual(
            compiled_recipe_digest(compile_recipe(epub, recipe, verify_digest=False)),
            first,
        )


if __name__ == "__main__":
    unittest.main()
