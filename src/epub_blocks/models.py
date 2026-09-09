from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

UnicodeNormalization = Literal["NFC", "NFD", "NFKC", "NFKD", "none"]


@dataclass(frozen=True)
class ElementSelector:
    """A conjunctive structural selector used by a compiled content policy."""

    tag: str | None = None
    classes: tuple[str, ...] | None = None
    classes_any: tuple[str, ...] = ()
    classes_all: tuple[str, ...] = ()
    locators: tuple[str, ...] = ()
    epub_types: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str], ...] = ()
    empty: bool | None = None
    previous_sibling: ElementSelector | None = None
    has_child: ElementSelector | None = None
    attribute_prefixes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ElementRule:
    """An ordered source-element selection override."""

    match: ElementSelector
    action: str
    keep_empty: bool = False


@dataclass(frozen=True)
class BoundaryRule:
    """Whitespace before/after a retained nested element; first match wins."""

    match: ElementSelector
    before: str = ""
    after: str = ""


@dataclass(frozen=True)
class MarkupRule:
    """A source selector and its span or milestone representation."""

    match: ElementSelector
    kind: str
    name: str
    label_attribute: str | None = None
    label_text: bool = False
    position: str = "replace"
    label_counter: str | None = None
    continue_matching: bool = False
    preserve_whitespace: bool = False


@dataclass(frozen=True)
class MarkupOptions:
    """Serializer configuration and explicit detached-milestone policies."""

    format: str = "xml"
    rules: tuple[MarkupRule, ...] = ()
    delimiters: tuple[tuple[str, tuple[str, str]], ...] = ()
    between_blocks: str = "error"
    trailing: str = "error"
    attachment_order: str = "output"


@dataclass(frozen=True)
class ContentOptions:
    """Optional structural and markup policies pinned in a compiled recipe."""

    element_rules: tuple[ElementRule, ...] = ()
    strict_coverage: bool = False
    boundary_tags: tuple[str, ...] = ()
    boundary_separator: str = " "
    markup: MarkupOptions | None = None
    boundary_rules: tuple[BoundaryRule, ...] = ()


DEFAULT_CONTENT = ContentOptions()


@dataclass(frozen=True)
class SpineDocument:
    """One XHTML document in EPUB spine order."""

    position: int
    path: str
    properties: frozenset[str]
    linear: bool = True


@dataclass(frozen=True)
class EpubPackage:
    """The package information needed for deterministic text extraction."""

    package_path: str
    identifiers: frozenset[str]
    spine: tuple[SpineDocument, ...]


@dataclass(frozen=True)
class NormalizationOptions:
    """Text normalization applied after XHTML text extraction."""

    collapse_whitespace: bool = True
    strip: bool = True
    unicode_normalization: UnicodeNormalization = "NFC"


@dataclass(frozen=True)
class Fragment:
    """A selectable XHTML subtree, with optional omissions and text slice."""

    document_path: str
    element_path: str = ""
    omit_paths: tuple[str, ...] = ()
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class CompiledBlock:
    """One output block in a compiled extraction plan."""

    block_id: str
    block_type: str
    parts: tuple[Fragment, ...]
    separator: str = ""
    consumed_locators: tuple[str, ...] = ()
    allow_empty: bool = False
    # Before the indexed part; len(parts) denotes the end of the joined block.
    milestones: tuple[tuple[int, Fragment], ...] = ()
    # After the indexed part, before its following join separator.
    milestones_after: tuple[tuple[int, Fragment], ...] = ()


@dataclass(frozen=True)
class XmlRepair:
    """A guarded UTF-8 byte replacement in an original content document."""

    document_path: str
    offset: int
    expected: str
    replacement: str


@dataclass(frozen=True)
class CompiledRecipe:
    """The deterministic extraction plan produced from a version 1 recipe."""

    # None denotes a hash-only pin for a package with no nonempty identifiers.
    epub_identifier: str | None
    epub_sha256: str
    normalization: NormalizationOptions
    omit_epub_types: frozenset[str]
    blocks: tuple[CompiledBlock, ...]
    skipped_locators: tuple[str, ...] = ()
    reserved_locators: tuple[str, ...] = ()
    content: ContentOptions = DEFAULT_CONTENT
    xml_repairs: tuple[XmlRepair, ...] = ()


@dataclass(frozen=True)
class TextBlock:
    """An extracted XHTML block with a stable source locator."""

    spine_position: int
    document_path: str
    element_path: str
    tag: str
    text: str
    classes: frozenset[str]

    @property
    def locator(self) -> str:
        return f"{self.document_path}#{self.element_path}"

    @property
    def source_locator(self) -> str:
        return f"s{self.spine_position:03d}:{self.locator}"


@dataclass(frozen=True)
class ExtractedBlock:
    """One identifier/type/text record produced by a recipe."""

    block_id: str
    block_type: str
    text: str
