from __future__ import annotations

import copy
import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .errors import EpubBlocksError
from .models import NormalizationOptions, TextBlock
from .package import matches, read_epub_package, select_spine_documents
from .safety import DEFAULT_SAFETY_LIMITS, EpubArchive, SafetyLimits
from .xhtml import normalize_text, read_document_body, remove_descendants_by_epub_type
from .xml import XmlElement, local_name

PRIMARY_BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "pre"})
FALLBACK_BLOCK_TAGS = frozenset({"blockquote", "li"})
DEFAULT_OMITTED_EPUB_TYPES = frozenset({"noteref", "pagebreak"})
_DEFAULT_NORMALIZATION = NormalizationOptions()
StrPath = str | os.PathLike[str]


def _contains_primary_block(
    element: XmlElement, primary_tags: frozenset[str]
) -> bool:
    return any(
        descendant is not element and local_name(descendant.tag) in primary_tags
        for descendant in element.iter()
    )


def _candidate_elements(
    parent: XmlElement,
    primary_tags: frozenset[str],
    fallback_tags: frozenset[str],
    parent_path: tuple[int, ...] = (),
) -> Iterator[tuple[XmlElement, tuple[int, ...]]]:
    for index, child in enumerate(list(parent), 1):
        element_path = (*parent_path, index)
        tag = local_name(child.tag)
        if tag in primary_tags or (
            tag in fallback_tags
            and not _contains_primary_block(child, primary_tags)
        ):
            yield child, element_path
        else:
            yield from _candidate_elements(
                child, primary_tags, fallback_tags, element_path
            )


def extract_blocks(
    epub_path: StrPath,
    *,
    expected_identifier: str | None = None,
    include_documents: Sequence[str] | None = None,
    exclude_documents: Sequence[str] | None = None,
    exclude_classes: Sequence[str] | None = None,
    include_locators: Sequence[str] | None = None,
    exclude_locators: Sequence[str] | None = None,
    include_non_linear: bool = False,
    primary_tags: frozenset[str] = PRIMARY_BLOCK_TAGS,
    fallback_tags: frozenset[str] = FALLBACK_BLOCK_TAGS,
    omit_epub_types: frozenset[str] = DEFAULT_OMITTED_EPUB_TYPES,
    normalization: NormalizationOptions = _DEFAULT_NORMALIZATION,
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> list[TextBlock]:
    """Extract selected text blocks in EPUB spine and XHTML document order."""

    path = Path(epub_path)
    blocks: list[TextBlock] = []
    excluded_classes = {value.casefold() for value in (exclude_classes or ())}
    try:
        with ZipFile(path) as zip_file:
            epub = EpubArchive(zip_file, limits)
            package = read_epub_package(epub)
            if expected_identifier is not None:
                if not expected_identifier:
                    raise EpubBlocksError(
                        "expected_identifier must be a non-empty string"
                    )
                if expected_identifier not in package.identifiers:
                    raise EpubBlocksError(
                        f"{path}: expected package identifier "
                        f"{expected_identifier!r} not found"
                    )
            documents = select_spine_documents(
                package,
                include_documents,
                exclude_documents,
                include_non_linear=include_non_linear,
            )
            document_cache: dict[str, XmlElement] = {}
            for document in documents:
                body = read_document_body(epub, document.path, document_cache)
                for element, address in _candidate_elements(
                    body, primary_tags, fallback_tags
                ):
                    classes = frozenset(element.get("class", "").split())
                    if {value.casefold() for value in classes} & excluded_classes:
                        continue
                    element_path = ".".join(str(component) for component in address)
                    locator = f"{document.path}#{element_path}"
                    if include_locators and not matches(locator, include_locators):
                        continue
                    if exclude_locators and matches(locator, exclude_locators):
                        continue
                    selected = copy.deepcopy(element)
                    remove_descendants_by_epub_type(selected, omit_epub_types)
                    text = normalize_text("".join(selected.itertext()), normalization)
                    if not text:
                        continue
                    blocks.append(
                        TextBlock(
                            spine_position=document.position,
                            document_path=document.path,
                            element_path=element_path,
                            tag=local_name(element.tag),
                            text=text,
                            classes=classes,
                        )
                    )
    except BadZipFile as error:
        raise EpubBlocksError(f"{path}: not a valid ZIP container") from error

    if not blocks:
        raise EpubBlocksError(f"{path}: no text blocks matched the selection")
    return blocks
