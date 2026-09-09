from __future__ import annotations

import unittest

from epub_blocks import EpubBlocksError
from epub_blocks.xml import parse_xml


class EmptyDoctypeTests(unittest.TestCase):
    def test_empty_subsets_with_existing_external_identifier_forms(self) -> None:
        for declaration in (
            "<!DOCTYPE html []>",
            "<!DOCTYPE html[]>",
            "<!DOCTYPE html [ \t\r\n ] >",
            '<!DOCTYPE html SYSTEM "https://invalid.example/never-fetch.dtd" []>',
            "<!DOCTYPE html PUBLIC '-//Example//XHTML' 'file:///never-read.dtd' [ ]>",
        ):
            with self.subTest(declaration=declaration):
                root = parse_xml(
                    (
                        '<?xml version="1.0"?>\n<!--before-->'
                        + declaration
                        + "<html><p>&nbsp;A &amp; B</p></html>"
                    ).encode(),
                    "sample.xhtml",
                )
                self.assertEqual(root.findtext("p"), "\u00a0A & B")

    def test_utf16_empty_subset_uses_same_safety_policy(self) -> None:
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                root = parse_xml(
                    "<!DOCTYPE html []><html><p>Invented</p></html>".encode(encoding),
                    "sample.xhtml",
                )
                self.assertEqual(root.findtext("p"), "Invented")

    def test_nonempty_or_malformed_subsets_remain_rejected(self) -> None:
        for declaration in (
            '<!DOCTYPE html [<!ENTITY custom "reading">]>',
            '<!DOCTYPE html [<!ENTITY custom SYSTEM "file:///never-read">]>',
            '<!DOCTYPE html [<!ENTITY % external SYSTEM "https://invalid.example/dtd">%external;]>',
            "<!DOCTYPE html [<!ELEMENT html ANY>]>",
            '<!DOCTYPE html [<!ATTLIST html label CDATA "injected">]>',
            "<!DOCTYPE html [<!-- not an empty subset -->]>",
            "<!DOCTYPE html [<?processor instruction?>]>",
            "<!DOCTYPE html [\u000b]>",
            "<!DOCTYPE html [\u00a0]>",
            "<!DOCTYPE html [[]>",
            "<!DOCTYPE HTML []>",
        ):
            with self.subTest(declaration=declaration):
                for encoding in ("utf-8", "utf-16"):
                    with self.assertRaisesRegex(EpubBlocksError, "DTD"):
                        parse_xml(
                            (declaration + "<html/>").encode(encoding), "sample.xhtml"
                        )

    def test_position_uniqueness_and_entity_checks_still_apply(self) -> None:
        for source, message in (
            ("<html/><!DOCTYPE html []>", "prolog"),
            ("<!DOCTYPE html []><!DOCTYPE html><html/>", "multiple"),
            ("<!DOCTYPE html><!DOCTYPE html []><html/>", "multiple"),
            ("<!DOCTYPE html []><!DOCTYPE html []><html/>", "multiple"),
            ("<!DOCTYPE html []><html>&custom;</html>", "unknown named"),
        ):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(EpubBlocksError, message),
            ):
                parse_xml(source.encode(), "sample.xhtml")

    def test_literals_in_opaque_regions_are_not_removed(self) -> None:
        root = parse_xml(
            b"<!--<!DOCTYPE html []>--><?sample <!DOCTYPE html []>?>"
            b"<!DOCTYPE html []><html><![CDATA[<!DOCTYPE html []>]]></html>",
            "sample.xhtml",
        )
        self.assertEqual(root.text, "<!DOCTYPE html []>")


if __name__ == "__main__":
    unittest.main()
