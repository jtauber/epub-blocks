from __future__ import annotations

import re
from collections.abc import Iterator
from html.entities import html5
from xml.etree import ElementTree

from .errors import EpubBlocksError
from .safety import DEFAULT_SAFETY_LIMITS, SafetyLimits

type XmlElement = ElementTree.Element[str]


def local_name(tag: str) -> str:
    """Return an XML expanded name without its namespace."""

    return tag.rsplit("}", 1)[-1]


_QUOTED_LITERAL = rb'(?:"[^"]*"|\'[^\']*\')'
_SAFE_XHTML_DOCTYPE = re.compile(
    rb"<!DOCTYPE\s+html(?:\s+(?:SYSTEM\s+"
    + _QUOTED_LITERAL
    + rb"|PUBLIC\s+"
    + _QUOTED_LITERAL
    + rb"\s+"
    + _QUOTED_LITERAL
    + rb"))?(?:[ \t\r\n]*\[[ \t\r\n]*\])?\s*>",
)
_FORBIDDEN_DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_NAMED_CHARACTER_REFERENCE = re.compile(rb"&([A-Za-z_:][A-Za-z0-9_.:-]*);")
_OPAQUE_START = re.compile(rb"<!--|<!\[CDATA\[|<\?")
_TEXT_XML_DECLARATION = re.compile(r"\A<\?xml\b.*?\?>", re.IGNORECASE | re.DOTALL)
_TEXT_XML_ENCODING = re.compile(
    r"\bencoding\s*=\s*(?P<quote>['\"])(?P<value>[A-Za-z][A-Za-z0-9._-]*)"
    r"(?P=quote)",
    re.IGNORECASE,
)
_UTF32_SIGNATURES = (
    b"\x00\x00\xfe\xff",
    b"\xff\xfe\x00\x00",
    b"\x00\x00\x00<",
    b"<\x00\x00\x00",
)
_XML_WHITESPACE = b" \t\r\n"


def _utf16_byte_order(data: bytes) -> str | None:
    if data.startswith(b"\xff\xfe"):
        return "little"
    if data.startswith(b"\xfe\xff"):
        return "big"

    index = 0
    while index + 1 < len(data):
        little_unit = data[index : index + 2]
        big_unit = little_unit[::-1]
        if little_unit in (b" \x00", b"\t\x00", b"\r\x00", b"\n\x00"):
            index += 2
            continue
        if big_unit in (b" \x00", b"\t\x00", b"\r\x00", b"\n\x00"):
            index += 2
            continue
        if little_unit == b"<\x00":
            return "little"
        if big_unit == b"<\x00":
            return "big"
        return None
    return None


def _normalize_utf16(data: bytes, location: str) -> bytes:
    """Transcode UTF-16 XML so declaration checks are encoding-independent."""

    if any(data.startswith(signature) for signature in _UTF32_SIGNATURES):
        raise EpubBlocksError(f"{location}: unsupported UTF-32 XML encoding")

    byte_order = _utf16_byte_order(data)
    if byte_order is None:
        return data
    has_bom = data.startswith((b"\xff\xfe", b"\xfe\xff"))
    codec = "utf-16-le" if byte_order == "little" else "utf-16-be"
    if has_bom:
        codec = "utf-16"
    try:
        text = data.decode(codec)
    except UnicodeDecodeError as error:
        raise EpubBlocksError(f"{location}: malformed UTF-16 XML") from error

    declaration = _TEXT_XML_DECLARATION.match(text)
    if declaration is not None:
        encoding = _TEXT_XML_ENCODING.search(declaration.group(0))
        if encoding is not None:
            declared = encoding.group("value").casefold().replace("_", "-")
            generic_names = {"utf-16", "utf16"}
            endian_names = {
                "little": {"utf-16le", "utf16le"},
                "big": {"utf-16be", "utf16be"},
            }
            if declared in generic_names and not has_bom:
                raise EpubBlocksError(
                    f"{location}: generic UTF-16 XML encoding requires a byte-order mark"
                )
            if declared not in generic_names | endian_names[byte_order]:
                raise EpubBlocksError(
                    f"{location}: XML encoding {encoding.group('value')!r} "
                    f"conflicts with detected UTF-16 {byte_order}-endian input"
                )
            start, end = encoding.span("value")
            text = text[:start] + "UTF-8" + text[end:]
    return text.encode("utf-8")


def _opaque_ranges(data: bytes) -> Iterator[tuple[int, int]]:
    position = 0
    while match := _OPAQUE_START.search(data, position):
        opener = match.group(0)
        terminator = b"?>" if opener == b"<?" else b"]]>"
        if opener == b"<!--":
            terminator = b"-->"
        terminator_start = data.find(terminator, match.end())
        if terminator_start < 0:
            yield match.start(), len(data)
            return
        end = terminator_start + len(terminator)
        yield match.start(), end
        position = end


def _is_doctype_prefix(data: bytes) -> bool:
    position = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    while position < len(data):
        while position < len(data) and data[position] in _XML_WHITESPACE:
            position += 1
        if position == len(data):
            return True
        if data.startswith(b"<!--", position):
            terminator = b"-->"
        elif data.startswith(b"<?", position):
            terminator = b"?>"
        else:
            return False
        terminator_start = data.find(terminator, position + 2)
        if terminator_start < 0:
            return False
        position = terminator_start + len(terminator)
    return True


def _outside_opaque_matches(
    pattern: re.Pattern[bytes], data: bytes
) -> list[re.Match[bytes]]:
    opaque_ranges = iter(_opaque_ranges(data))
    opaque = next(opaque_ranges, None)
    outside: list[re.Match[bytes]] = []
    for match in pattern.finditer(data):
        while opaque is not None and opaque[1] <= match.start():
            opaque = next(opaque_ranges, None)
        if opaque is None or match.start() < opaque[0]:
            outside.append(match)
    return outside


def _strip_safe_xhtml_doctype(data: bytes, location: str) -> bytes:
    matches = _outside_opaque_matches(_SAFE_XHTML_DOCTYPE, data)
    if not matches:
        return data
    if len(matches) != 1:
        raise EpubBlocksError(f"{location}: multiple XHTML doctypes")
    match = matches[0]
    if not _is_doctype_prefix(data[: match.start()]):
        raise EpubBlocksError(
            f"{location}: XHTML doctype is outside the document prolog"
        )
    return data[: match.start()] + data[match.end() :]


def _replace_named_character_references(data: bytes, location: str) -> bytes:
    def replace(match: re.Match[bytes]) -> bytes:
        name = match.group(1).decode("ascii")
        replacement = html5.get(f"{name};")
        if replacement is None:
            reference = match.group(0).decode("ascii")
            raise EpubBlocksError(
                f"{location}: unknown named character reference {reference!r}"
            )
        return "".join(f"&#x{ord(character):X};" for character in replacement).encode(
            "ascii"
        )

    pieces: list[bytes] = []
    start = 0
    for opaque_start, opaque_end in _opaque_ranges(data):
        pieces.append(_NAMED_CHARACTER_REFERENCE.sub(replace, data[start:opaque_start]))
        pieces.append(data[opaque_start:opaque_end])
        start = opaque_end
    pieces.append(_NAMED_CHARACTER_REFERENCE.sub(replace, data[start:]))
    return b"".join(pieces)


def parse_xml(
    data: bytes,
    location: str,
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> XmlElement:
    """Parse bounded EPUB XML after applying the package's declaration policy."""

    if len(data) > limits.max_xml_bytes:
        raise EpubBlocksError(
            f"{location}: XML document is too large "
            f"({len(data)} > {limits.max_xml_bytes} bytes)"
        )
    data = _normalize_utf16(data, location)
    if b"\x00" in data:
        raise EpubBlocksError(
            f"{location}: XML document contains unsupported NUL bytes"
        )
    if len(data) > limits.max_xml_bytes:
        raise EpubBlocksError(
            f"{location}: normalized XML document is too large "
            f"({len(data)} > {limits.max_xml_bytes} bytes)"
        )
    declarations = _strip_safe_xhtml_doctype(data, location)
    if _outside_opaque_matches(_FORBIDDEN_DECLARATION, declarations):
        raise EpubBlocksError(f"{location}: unsupported DTD or entity declaration")
    declarations = _replace_named_character_references(declarations, location)
    if len(declarations) > limits.max_xml_bytes:
        raise EpubBlocksError(
            f"{location}: expanded XML document is too large "
            f"({len(declarations)} > {limits.max_xml_bytes} bytes)"
        )
    try:
        root = ElementTree.fromstring(declarations)
    except (ElementTree.ParseError, ValueError) as error:
        raise EpubBlocksError(f"{location}: malformed XML") from error

    element_count = 0
    stack: list[tuple[XmlElement, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        element_count += 1
        if element_count > limits.max_xml_elements:
            raise EpubBlocksError(f"{location}: XML document has too many elements")
        if depth > limits.max_xml_depth:
            raise EpubBlocksError(f"{location}: XML document is too deeply nested")
        stack.extend((child, depth + 1) for child in element)
    return root
