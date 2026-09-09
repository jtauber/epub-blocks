"""Whole-document fragments and rich-text boundary contracts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from test_epub_blocks import make_epub

from epub_blocks import (
    ContentOptions,
    ElementSelector,
    EpubBlocksError,
    Fragment,
    MarkupOptions,
    MarkupRule,
    NormalizationOptions,
)
from epub_blocks._content import build_rich_text, render_rich_text
from epub_blocks.safety import EpubArchive
from epub_blocks.xhtml import (
    extract_rich_fragment,
    fragment_markup_rules,
    read_document_body,
)
from epub_blocks.xml import XmlElement, parse_xml

DOCUMENT = "text/chapter.xhtml"


class FragmentEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_whole_body_fragment_with_plain_cache_keeps_original_list_ordinals(
        self,
    ) -> None:
        epub = make_epub(
            self.directory,
            documents={
                DOCUMENT: '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                '<ol start="7"><li>First.</li><li>Second.</li></ol></body></html>'
            },
        )
        body_rule = MarkupRule(ElementSelector(tag="body"), "span", "body")
        number_rule = MarkupRule(
            ElementSelector(tag="li"),
            "milestone",
            "number",
            position="before",
            label_counter="ordered-list",
        )
        markup = MarkupOptions(rules=(body_rule, number_rule))
        content = ContentOptions(markup=markup)
        cache: dict[str, XmlElement] = {}
        with ZipFile(epub) as archive:
            source = EpubArchive(archive)
            body = read_document_body(source, DOCUMENT, cache)
            self.assertIs(read_document_body(source, DOCUMENT, cache), body)
            self.assertEqual(
                fragment_markup_rules(Fragment(DOCUMENT), cache, content), (body_rule,)
            )
            tree = extract_rich_fragment(
                source,
                Fragment(DOCUMENT),
                cache=cache,
                normalization=NormalizationOptions(),
                omit_epub_types=frozenset(),
                content=content,
            )
        self.assertEqual(
            render_rich_text(tree, markup),
            '<body><number label="7"/>First.<number label="8"/>Second.</body>',
        )
        self.assertEqual(
            [n.get("value") for n in body.findall(".//{*}li")], [None, None]
        )

    def test_non_milestone_attachment_is_rejected_not_silently_dropped(self) -> None:
        epub = make_epub(
            self.directory,
            documents={
                DOCUMENT: '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Keep.</p></body></html>'
            },
        )
        with (
            ZipFile(epub) as archive,
            self.assertRaisesRegex(
                EpubBlocksError, "attached fragment must be a milestone"
            ),
        ):
            extract_rich_fragment(
                EpubArchive(archive),
                Fragment(DOCUMENT),
                cache={},
                normalization=NormalizationOptions(),
                omit_epub_types=frozenset(),
                content=ContentOptions(markup=MarkupOptions()),
                milestone_only=True,
            )

    def test_ordered_list_label_requires_original_context(self) -> None:
        item = parse_xml(b"<li>Reading.</li>", "test")
        content = ContentOptions(
            markup=MarkupOptions(
                rules=(
                    MarkupRule(
                        ElementSelector(tag="li"),
                        "milestone",
                        "number",
                        position="before",
                        label_counter="ordered-list",
                    ),
                )
            )
        )
        with self.assertRaisesRegex(
            EpubBlocksError, "requires original document context"
        ):
            build_rich_text(item, DOCUMENT + "#1.1", content, frozenset())

    def test_explicit_root_is_retained_while_semantic_descendants_are_omitted(
        self,
    ) -> None:
        root = parse_xml(
            b'<div xmlns:epub="http://www.idpf.org/2007/ops" epub:type="noteref">'
            b'Root<span epub:type="noteref">Hidden</span> tail.</div>',
            "test",
        )
        tree = build_rich_text(
            root, DOCUMENT + "#1", ContentOptions(), frozenset({"noteref"})
        )
        self.assertEqual(render_rich_text(tree, None), "Root tail.")
