from __future__ import annotations

import copy
import hashlib
import os
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import BinaryIO
from zipfile import BadZipFile, ZipFile

from ._content import (
    EPUB_TYPE,
    build_rich_text,
    markup_rule,
    milestones,
    normalize_rich_text,
    plain_text,
    source_rule,
)
from .errors import EpubBlocksError
from .models import (
    DEFAULT_CONTENT,
    ContentOptions,
    EpubPackage,
    Fragment,
    NormalizationOptions,
    SpineDocument,
    TextBlock,
)
from .package import matches, read_epub_package, select_spine_documents
from .safety import DEFAULT_SAFETY_LIMITS, EpubArchive, SafetyLimits
from .xhtml import (
    extract_fragment,
    flatten_text,
    normalize_text,
    read_document_body,
    remove_descendants_by_epub_type,
)
from .xml import XmlElement, local_name

PRIMARY_BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "pre"})
FALLBACK_BLOCK_TAGS = frozenset({"blockquote", "li"})
DEFAULT_OMITTED_EPUB_TYPES = frozenset({"noteref", "pagebreak"})
_DEFAULT_NORMALIZATION = NormalizationOptions()
_SHA256 = re.compile(r"[0-9a-f]{64}")
StrPath = str | os.PathLike[str]


def _stream_sha256(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    while block := source.read(1024 * 1024):
        digest.update(block)
    return digest.hexdigest()


def _contains_primary_block(element: XmlElement, primary_tags: frozenset[str]) -> bool:
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
            tag in fallback_tags and not _contains_primary_block(child, primary_tags)
        ):
            yield child, element_path
        else:
            yield from _candidate_elements(
                child, primary_tags, fallback_tags, element_path
            )


def _extract_blocks_from_epub(
    epub: EpubArchive,
    package: EpubPackage,
    source_path: Path,
    *,
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
    document_cache: dict[str, XmlElement] | None = None,
    content: ContentOptions = DEFAULT_CONTENT,
    detached_milestones: list[Fragment] | None = None,
) -> list[TextBlock]:
    """Extract blocks from an already-open, bounded EPUB archive."""

    blocks: list[TextBlock] = []
    excluded_classes = {value.casefold() for value in (exclude_classes or ())}
    documents = select_spine_documents(
        package,
        include_documents,
        exclude_documents,
        include_non_linear=include_non_linear,
    )
    cache = document_cache if document_cache is not None else {}
    if content != ContentOptions():
        return _extract_content_blocks(
            epub,
            documents,
            cache,
            source_path,
            primary_tags,
            fallback_tags,
            excluded_classes,
            include_locators,
            exclude_locators,
            omit_epub_types,
            normalization,
            content,
            detached_milestones,
        )
    for document in documents:
        body = read_document_body(epub, document.path, cache)
        for element, address in _candidate_elements(body, primary_tags, fallback_tags):
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
            text = normalize_text(flatten_text(selected), normalization)
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
    if not blocks:
        raise EpubBlocksError(f"{source_path}: no text blocks matched the selection")
    return blocks


def _extract_content_blocks(
    epub: EpubArchive,
    documents: Sequence[SpineDocument],
    cache: dict[str, XmlElement],
    source_path: Path,
    primary_tags: frozenset[str],
    fallback_tags: frozenset[str],
    excluded_classes: set[str],
    include_locators: Sequence[str] | None,
    exclude_locators: Sequence[str] | None,
    omitted_types: frozenset[str],
    normalization: NormalizationOptions,
    content: ContentOptions,
    detached: list[Fragment] | None,
) -> list[TextBlock]:
    blocks: list[TextBlock] = []

    def included(element: XmlElement, locator: str) -> bool:
        return not (
            {token.casefold() for token in element.get("class", "").split()}
            & excluded_classes
            or (include_locators and not matches(locator, include_locators))
            or (exclude_locators and matches(locator, exclude_locators))
        )

    for document in documents:
        body = read_document_body(epub, document.path, cache)

        def visit(
            element: XmlElement,
            path: str,
            previous: XmlElement | None,
            *,
            document: SpineDocument = document,
        ) -> None:
            locator = f"{document.path}#{path}"
            rule = source_rule(element, content, locator, previous)
            mark = markup_rule(element, content, locator, previous)
            if rule is not None and rule.action == "skip":
                return
            if set(element.get(EPUB_TYPE, "").split()) & omitted_types:
                if mark is not None:
                    raise EpubBlocksError(
                        f"{locator}: retained markup is also semantically omitted"
                    )
                return
            tag = local_name(element.tag)
            candidate = (
                rule.action == "block"
                if rule is not None
                else (
                    tag in primary_tags
                    or (
                        tag in fallback_tags
                        and not _contains_primary_block(element, primary_tags)
                    )
                )
            )
            event = mark is not None and mark.kind == "milestone"
            if candidate or event:
                if not included(element, locator):
                    return
                tree = normalize_rich_text(
                    build_rich_text(element, locator, content, omitted_types, previous),
                    normalization,
                )
                text = plain_text(tree)
                if text or (rule is not None and rule.keep_empty):
                    blocks.append(
                        TextBlock(
                            document.position,
                            document.path,
                            path,
                            tag,
                            text,
                            frozenset(element.get("class", "").split()),
                        )
                    )
                elif detached is not None:
                    for event_locator in milestones(tree):
                        doc, element_path = event_locator.rsplit("#", 1)
                        detached.append(Fragment(doc, element_path))
                return
            if content.strict_coverage and (element.text or "").strip():
                raise EpubBlocksError(f"{locator}: unclaimed element text")
            prev: XmlElement | None = None
            for index, child in enumerate(element, 1):
                visit(child, f"{path}.{index}", prev)
                if content.strict_coverage and (child.tail or "").strip():
                    raise EpubBlocksError(f"{locator}.{index}: unclaimed tail text")
                prev = child

        if content.strict_coverage and (body.text or "").strip():
            raise EpubBlocksError(f"{document.path}#: unclaimed body text")
        previous: XmlElement | None = None
        for index, element in enumerate(body, 1):
            visit(element, str(index), previous)
            if content.strict_coverage and (element.tail or "").strip():
                raise EpubBlocksError(f"{document.path}#{index}: unclaimed tail text")
            previous = element
    if not blocks:
        raise EpubBlocksError(f"{source_path}: no text blocks matched the selection")
    return blocks


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
            return _extract_blocks_from_epub(
                epub,
                package,
                path,
                include_documents=include_documents,
                exclude_documents=exclude_documents,
                exclude_classes=exclude_classes,
                include_locators=include_locators,
                exclude_locators=exclude_locators,
                include_non_linear=include_non_linear,
                primary_tags=primary_tags,
                fallback_tags=fallback_tags,
                omit_epub_types=omit_epub_types,
                normalization=normalization,
            )
    except BadZipFile as error:
        raise EpubBlocksError(f"{path}: not a valid ZIP container") from error


def extract_fragments(
    epub_path: StrPath,
    fragments: Sequence[Fragment],
    *,
    expected_identifier: str | None = None,
    expected_sha256: str | None = None,
    normalization: NormalizationOptions = _DEFAULT_NORMALIZATION,
    omit_epub_types: frozenset[str] = DEFAULT_OMITTED_EPUB_TYPES,
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> list[str]:
    """Extract arbitrary XHTML fragments through one validated EPUB read.

    Each result corresponds to the fragment at the same position. Descendant
    omissions precede normalization; slice offsets index normalized text.
    Callers that need a pinned input can require both a package identifier and
    complete-file SHA-256 without reopening the archive for every fragment.
    """

    if expected_identifier is not None and not expected_identifier:
        raise EpubBlocksError("expected_identifier must be a non-empty string")
    if expected_sha256 is not None and _SHA256.fullmatch(expected_sha256) is None:
        raise EpubBlocksError(
            "expected_sha256 must be 64 lowercase hexadecimal characters"
        )

    path = Path(epub_path)
    with path.open("rb") as source:
        if expected_sha256 is not None:
            if _stream_sha256(source) != expected_sha256:
                raise EpubBlocksError(f"{path}: source SHA-256 does not match")
            source.seek(0)
        try:
            zip_file = ZipFile(source)
        except BadZipFile as error:
            raise EpubBlocksError(f"{path}: not a valid ZIP container") from error
        with zip_file:
            epub = EpubArchive(zip_file, limits)
            package = read_epub_package(epub)
            if (
                expected_identifier is not None
                and expected_identifier not in package.identifiers
            ):
                raise EpubBlocksError(
                    f"{path}: expected package identifier "
                    f"{expected_identifier!r} not found"
                )
            cache: dict[str, XmlElement] = {}
            return [
                normalize_text(
                    extract_fragment(
                        epub,
                        fragment,
                        cache=cache,
                        normalization=normalization,
                        omit_epub_types=omit_epub_types,
                    ),
                    normalization,
                )
                for fragment in fragments
            ]
