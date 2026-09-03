from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

UnicodeNormalization = Literal["NFC", "NFD", "NFKC", "NFKD", "none"]


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
    """One recipe-produced text record."""

    block_id: str
    block_type: str
    text: str
