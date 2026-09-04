from __future__ import annotations

import copy
import re
import unicodedata

from .errors import EpubBlocksError
from .models import Fragment, NormalizationOptions
from .safety import EpubArchive
from .xml import XmlElement, local_name, parse_xml

EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
XHTML_BODY = "{http://www.w3.org/1999/xhtml}body"
_DEFAULT_NORMALIZATION = NormalizationOptions()
_ELEMENT_PATH = re.compile(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*")


def _path_components(element_path: str, location: str) -> tuple[int, ...]:
    if not _ELEMENT_PATH.fullmatch(element_path):
        raise EpubBlocksError(f"{location}: invalid element path {element_path!r}")
    try:
        return tuple(int(component) for component in element_path.split("."))
    except ValueError as error:
        raise EpubBlocksError(
            f"{location}: invalid element path {element_path!r}"
        ) from error


def child_at(
    element: XmlElement,
    element_path: str,
    location: str = "XHTML",
) -> XmlElement:
    """Select a descendant by a one-based, dot-separated child path."""

    current = element
    if not element_path:
        return current
    for component in _path_components(element_path, location):
        index = component - 1
        children = list(current)
        if index < 0 or index >= len(children):
            raise EpubBlocksError(
                f"{location}: element path {element_path!r} is outside the document"
            )
        current = children[index]
    return current


def remove_at(
    element: XmlElement,
    element_path: str,
    location: str = "XHTML",
) -> None:
    """Remove a descendant while preserving its tail text."""

    if not element_path:
        raise EpubBlocksError(f"{location}: cannot omit the selected root")
    components = _path_components(element_path, location)
    parent = child_at(
        element, ".".join(str(component) for component in components[:-1]), location
    )
    index = components[-1] - 1
    children = list(parent)
    if index < 0 or index >= len(children):
        raise EpubBlocksError(f"{location}: omit path {element_path!r} is invalid")
    child = children[index]
    tail = child.tail or ""
    parent.remove(child)
    if tail:
        if index:
            previous = list(parent)[index - 1]
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail


def remove_descendants_by_epub_type(
    element: XmlElement, omitted_types: set[str] | frozenset[str]
) -> None:
    """Remove descendants whose EPUB type intersects omitted_types."""

    targets: list[tuple[int, int, XmlElement, XmlElement]] = []

    def collect(parent: XmlElement, depth: int) -> None:
        for index, child in enumerate(list(parent)):
            if not set(child.get(EPUB_TYPE, "").split()) & omitted_types:
                collect(child, depth + 1)
            else:
                targets.append((depth, index, parent, child))

    collect(element, 1)
    for _depth, _index, parent, child in sorted(
        targets, key=lambda target: (target[0], target[1]), reverse=True
    ):
        current_index = list(parent).index(child)
        tail = child.tail or ""
        parent.remove(child)
        if tail:
            if current_index:
                previous = list(parent)[current_index - 1]
                previous.tail = (previous.tail or "") + tail
            else:
                parent.text = (parent.text or "") + tail


def normalize_text(text: str, options: NormalizationOptions) -> str:
    """Normalize extracted text according to explicit, reproducible options."""

    if options.collapse_whitespace:
        text = re.sub(r"\s+", " ", text)
    if options.strip:
        text = text.strip()
    if options.unicode_normalization != "none":
        try:
            text = unicodedata.normalize(options.unicode_normalization, text)
        except ValueError as error:
            raise EpubBlocksError(
                f"unknown Unicode normalization {options.unicode_normalization!r}"
            ) from error
    return text


def flatten_text(element: XmlElement) -> str:
    """Flatten an XHTML subtree while retaining explicit line boundaries."""

    pieces: list[str] = []

    def collect(current: XmlElement) -> None:
        if local_name(current.tag) == "br":
            pieces.append("\n")
        if current.text:
            pieces.append(current.text)
        for child in current:
            collect(child)
            if child.tail:
                pieces.append(child.tail)

    collect(element)
    return "".join(pieces)


def read_document_body(
    epub: EpubArchive,
    document_path: str,
    cache: dict[str, XmlElement] | None = None,
) -> XmlElement:
    """Read and cache the XHTML body for one archive document."""

    cache = cache if cache is not None else {}
    if document_path in cache:
        return cache[document_path]
    data = epub.read(document_path)
    root = parse_xml(data, document_path, epub.limits)
    body = root.find(f"./{XHTML_BODY}")
    if body is None:
        raise EpubBlocksError(f"{document_path!r} has no XHTML body")
    cache[document_path] = body
    return body


def extract_fragment(
    epub: EpubArchive,
    fragment: Fragment,
    *,
    cache: dict[str, XmlElement] | None = None,
    normalization: NormalizationOptions = _DEFAULT_NORMALIZATION,
    omit_epub_types: set[str] | frozenset[str] = frozenset(),
) -> str:
    """Extract one XHTML fragment for joining by a consuming application."""

    location = f"{fragment.document_path}#{fragment.element_path}"
    body = read_document_body(epub, fragment.document_path, cache)
    selected = copy.deepcopy(child_at(body, fragment.element_path, location))
    parsed_paths = [
        (_path_components(path, location), path) for path in fragment.omit_paths
    ]
    for index, (components, path) in enumerate(parsed_paths):
        for other_components, other_path in parsed_paths[:index]:
            shared_length = min(len(components), len(other_components))
            if components[:shared_length] == other_components[:shared_length]:
                raise EpubBlocksError(
                    f"{location}: omit paths {other_path!r} and {path!r} "
                    "duplicate or overlap"
                )
    omit_paths = [
        path
        for _components, path in sorted(
            parsed_paths, key=lambda item: item[0], reverse=True
        )
    ]
    for element_path in omit_paths:
        remove_at(selected, element_path, location)
    remove_descendants_by_epub_type(selected, omit_epub_types)

    text = flatten_text(selected)
    if fragment.start is None and fragment.end is None:
        return text
    if not isinstance(fragment.start, int) or not isinstance(fragment.end, int):
        raise EpubBlocksError(
            f"{location}: fragment start and end must both be integers"
        )
    text = normalize_text(text, normalization)
    if fragment.start < 0 or fragment.end <= fragment.start or fragment.end > len(text):
        raise EpubBlocksError(
            f"{location}: slice [{fragment.start}, {fragment.end}) is outside a "
            f"{len(text)}-code-point fragment"
        )
    return text[fragment.start : fragment.end]
