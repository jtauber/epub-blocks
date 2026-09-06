from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from typing import cast

from jsonschema import (  # pyright: ignore[reportMissingModuleSource]
    Draft202012Validator,
)
from test_epub_blocks import make_epub, minimal_recipe

from epub_blocks import EpubBlocksError, compile_recipe, extract_recipe_candidates


class WhitespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(
            files("epub_blocks")
            .joinpath("schemas/recipe-v1.schema.json")
            .read_text(encoding="utf-8")
        )
        self.pattern = cast(str, self.schema["$defs"]["boundaryWhitespace"]["pattern"])
        self.codepoints = [cp for cp in range(0x110000) if chr(cp).isspace()]

    def test_schema_pattern_matches_python_whitespace_over_all_codepoints(self) -> None:
        pattern = re.compile(self.pattern)
        self.assertEqual(
            [cp for cp in range(0x110000) if pattern.search(chr(cp))],
            self.codepoints,
        )
        self.assertIsNone(pattern.search(""))
        self.assertIsNone(pattern.search(" \ufeff\n"))
        self.assertIsNone(pattern.search("text\n"))
        self.assertIsNotNone(pattern.search("\u0085\u001c\u00a0\n"))

    def test_javascript_pattern_matches_python_over_all_codepoints(self) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI"):
                self.fail("Node.js is required for the cross-engine schema test in CI")
            self.skipTest("Node.js is not installed")
        completed = subprocess.run(
            [
                node,
                "-e",
                """
                const fs = require('fs');
                const pattern = new RegExp(JSON.parse(fs.readFileSync(0, 'utf8')));
                const accepted = [];
                for (let cp = 0; cp < 0x110000; cp++) {
                    if (pattern.test(String.fromCodePoint(cp))) accepted.push(cp);
                }
                if (['', ' \\ufeff\\n', 'text\\n'].some(s => pattern.test(s))) {
                    throw new Error('invalid whitespace string accepted');
                }
                if (!pattern.test('\\u0085\\u001c\\u00a0\\n')) {
                    throw new Error('valid whitespace string rejected');
                }
                process.stdout.write(JSON.stringify(accepted));
                """,
            ],
            input=json.dumps(self.pattern),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        self.assertEqual(json.loads(completed.stdout), self.codepoints)

    def test_separator_and_both_rule_sides_share_schema_and_runtime_contract(
        self,
    ) -> None:
        validator = Draft202012Validator(self.schema)
        valid = [chr(cp) for cp in self.codepoints]
        valid.append("".join(valid))
        invalid: list[object] = [
            "",
            "\ufeff",
            "\u180e",
            "\u200b",
            "\x00",
            "word",
            " \ufeff\n",
            " \nword",
            None,
            True,
            1,
            [],
            {},
        ]
        with tempfile.TemporaryDirectory() as directory:
            epub = make_epub(Path(directory))
            base = minimal_recipe(
                epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
            )
            for field in ("separator", "before", "after"):
                for value in [*valid, *invalid]:
                    with self.subTest(field=field, value=value):
                        recipe = copy.deepcopy(base)
                        boundary = (
                            {"tags": ["span"], "separator": value}
                            if field == "separator"
                            else {"rules": [{"match": {"tag": "span"}, field: value}]}
                        )
                        recipe["text"] = {"block_boundaries": boundary}
                        accepted = isinstance(value, str) and value in valid
                        self.assertEqual(validator.is_valid(recipe), accepted)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                        if accepted:
                            compile_recipe(epub, recipe, verify_digest=False)
                        else:
                            with self.assertRaisesRegex(EpubBlocksError, "whitespace"):
                                compile_recipe(epub, recipe, verify_digest=False)

    def test_previously_accepted_unusual_whitespace_still_extracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            epub = make_epub(
                Path(directory),
                documents={
                    "text/chapter.xhtml": (
                        '<html xmlns="http://www.w3.org/1999/xhtml">'
                        "<body><p>A<span>B</span>C</p></body></html>"
                    )
                },
            )
            recipe = minimal_recipe(
                epub_sha256=hashlib.sha256(epub.read_bytes()).hexdigest()
            )
            recipe["text"] = {
                "block_boundaries": {
                    "rules": [{"match": {"tag": "span"}, "before": "\u001c\u0085"}]
                }
            }
            self.assertEqual(extract_recipe_candidates(epub, recipe)[0].text, "A BC")
            cast(dict[str, object], recipe["normalization"])["collapse_whitespace"] = (
                False
            )
            self.assertEqual(
                extract_recipe_candidates(epub, recipe)[0].text, "A\u001c\u0085BC"
            )
