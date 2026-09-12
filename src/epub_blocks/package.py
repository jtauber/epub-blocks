from __future__ import annotations

import fnmatch
import os
import posixpath
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote
from xml.etree.ElementTree import Element
from zipfile import BadZipFile, ZipFile

from .errors import EpubBlocksError
from .models import EpubPackage, SpineDocument
from .safety import DEFAULT_SAFETY_LIMITS, EpubArchive, SafetyLimits
from .xml import parse_xml

StrPath = str | os.PathLike[str]

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
PACKAGE_MEDIA_TYPE = "application/oebps-package+xml"
XHTML_MEDIA_TYPE = "application/xhtml+xml"


def archive_path(package_path: str, href: str) -> str:
    """Resolve a manifest href safely within the EPUB archive."""

    href_path = unquote(href.split("#", 1)[0])
    if not href_path or "\\" in href_path:
        raise EpubBlocksError(f"invalid EPUB manifest path: {href!r}")
    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(package_path), href_path)
    )
    if resolved == ".." or resolved.startswith(("../", "/")):
        raise EpubBlocksError(f"EPUB manifest path escapes the archive root: {href!r}")
    return resolved


def _single_child(parent: Element[str], path: str, label: str) -> Element[str]:
    elements = parent.findall(path)
    if len(elements) != 1:
        raise EpubBlocksError(f"EPUB package must contain exactly one {label}")
    return elements[0]


def read_epub_package(epub: EpubArchive) -> EpubPackage:
    """Read identifiers and spine metadata from a bounded EPUB reader."""

    container_path = "META-INF/container.xml"
    container = parse_xml(epub.read(container_path), container_path, epub.limits)
    if container.tag != f"{{{CONTAINER_NS}}}container":
        raise EpubBlocksError("EPUB container has an unexpected root element")
    rootfiles = container.findall(
        f"./{{{CONTAINER_NS}}}rootfiles/{{{CONTAINER_NS}}}rootfile"
    )
    package_rootfiles = [
        rootfile
        for rootfile in rootfiles
        if rootfile.get("media-type") == PACKAGE_MEDIA_TYPE
    ]
    if not package_rootfiles:
        raise EpubBlocksError(
            "EPUB container has no package rootfile with the required media type"
        )
    if len(package_rootfiles) > 1:
        raise EpubBlocksError("EPUB container identifies multiple package rootfiles")
    full_path = package_rootfiles[0].get("full-path")
    if not full_path:
        raise EpubBlocksError("EPUB package rootfile has no full-path")
    package_path = archive_path("", full_path)

    package = parse_xml(epub.read(package_path), package_path, epub.limits)
    if package.tag != f"{{{OPF_NS}}}package":
        raise EpubBlocksError("EPUB package has an unexpected root element")

    metadata = _single_child(package, f"./{{{OPF_NS}}}metadata", "metadata")
    manifest_element = _single_child(package, f"./{{{OPF_NS}}}manifest", "manifest")
    spine_element = _single_child(package, f"./{{{OPF_NS}}}spine", "spine")

    identifiers = frozenset(
        (element.text or "").strip()
        for element in metadata.findall(f"./{{{DC_NS}}}identifier")
        if (element.text or "").strip()
    )

    manifest: dict[str, Element[str]] = {}
    for item in manifest_element.findall(f"./{{{OPF_NS}}}item"):
        item_id = item.get("id")
        if not item_id:
            raise EpubBlocksError("EPUB manifest item has no id")
        if item_id in manifest:
            raise EpubBlocksError(f"EPUB manifest has duplicate id {item_id!r}")
        if not item.get("href"):
            raise EpubBlocksError(f"EPUB manifest item {item_id!r} has no href")
        if not item.get("media-type"):
            raise EpubBlocksError(f"EPUB manifest item {item_id!r} has no media-type")
        manifest[item_id] = item

    spine: list[SpineDocument] = []
    for position, itemref in enumerate(
        spine_element.findall(f"./{{{OPF_NS}}}itemref"), 1
    ):
        item_id = itemref.get("idref")
        if not item_id or item_id not in manifest:
            raise EpubBlocksError(
                f"EPUB spine refers to missing manifest item {item_id!r}"
            )
        linear_value = itemref.get("linear", "yes")
        if linear_value not in {"yes", "no"}:
            raise EpubBlocksError(
                f"EPUB spine item {item_id!r} has invalid linear value {linear_value!r}"
            )
        item = manifest[item_id]
        if item.get("media-type") != XHTML_MEDIA_TYPE:
            continue
        href = item.get("href")
        if href is None:  # validated above; narrows the static type
            raise AssertionError("validated manifest href is missing")
        properties = frozenset(
            f"{item.get('properties', '')} {itemref.get('properties', '')}".split()
        )
        spine.append(
            SpineDocument(
                position=position,
                path=archive_path(package_path, href),
                properties=properties,
                linear=linear_value == "yes",
            )
        )

    if not spine:
        raise EpubBlocksError("EPUB package has no XHTML spine documents")
    return EpubPackage(package_path, identifiers, tuple(spine))


def inspect_epub(
    epub_path: StrPath,
    *,
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> EpubPackage:
    """Open an EPUB and return its validated package information."""

    path = Path(epub_path)
    try:
        with ZipFile(path) as archive:
            return read_epub_package(EpubArchive(archive, limits))
    except (BadZipFile, UnicodeDecodeError) as error:
        raise EpubBlocksError(f"{path}: not a valid ZIP container") from error


def matches(value: str, patterns: Sequence[str]) -> bool:
    """Match case-insensitive globs without operating-system path rewriting."""

    folded = value.casefold()
    return any(fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in patterns)


def select_spine_documents(
    package: EpubPackage,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    *,
    include_non_linear: bool = False,
) -> list[SpineDocument]:
    """Select spine documents, excluding auxiliary non-linear items by default."""

    include_patterns = include or ("*",)
    exclude_patterns = exclude or ()
    selected = [
        document
        for document in package.spine
        if (document.linear or include_non_linear)
        and matches(document.path, include_patterns)
        and not matches(document.path, exclude_patterns)
    ]
    if not selected:
        raise EpubBlocksError("No EPUB spine documents matched the selection")
    return selected
