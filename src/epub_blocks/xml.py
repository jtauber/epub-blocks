from __future__ import annotations

import re
from xml.etree import ElementTree

from .errors import EpubBlocksError
from .safety import DEFAULT_SAFETY_LIMITS, SafetyLimits

type XmlElement = ElementTree.Element[str]


def local_name(tag: str) -> str:
    """Return an XML expanded name without its namespace."""

    return tag.rsplit("}", 1)[-1]


_SIMPLE_HTML_DOCTYPE = re.compile(br"<!DOCTYPE\s+html\s*>", re.IGNORECASE)
_FORBIDDEN_DECLARATION = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


def parse_xml(
    data: bytes,
    location: str,
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> XmlElement:
    if len(data) > limits.max_xml_bytes:
        raise EpubBlocksError(
            f"{location}: XML document is too large "
            f"({len(data)} > {limits.max_xml_bytes} bytes)"
        )
    declarations = _SIMPLE_HTML_DOCTYPE.sub(b"", data)
    if _FORBIDDEN_DECLARATION.search(declarations):
        raise EpubBlocksError(
            f"{location}: external/internal DTDs and entity declarations are forbidden"
        )
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise EpubBlocksError(f"{location}: malformed XML") from error

    element_count = 0
    stack: list[tuple[XmlElement, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        element_count += 1
        if element_count > limits.max_xml_elements:
            raise EpubBlocksError(
                f"{location}: XML document has too many elements"
            )
        if depth > limits.max_xml_depth:
            raise EpubBlocksError(f"{location}: XML document is too deeply nested")
        stack.extend((child, depth + 1) for child in element)
    return root
