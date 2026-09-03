from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from zipfile import BadZipFile, ZipFile

from .errors import EpubBlocksError
from .models import (
    ExtractedBlock,
    Fragment,
    NormalizationOptions,
    UnicodeNormalization,
)
from .package import read_epub_package
from .safety import DEFAULT_SAFETY_LIMITS, EpubArchive, SafetyLimits
from .xhtml import extract_fragment, normalize_text
from .xml import XmlElement

StrPath = str | os.PathLike[str]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ELEMENT_PATH = re.compile(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*")


class _JsonObject(dict[str, object]):
    def __init__(self, pairs: list[tuple[str, object]]) -> None:
        super().__init__(pairs)
        seen: set[str] = set()
        duplicates: list[str] = []
        for key, _value in pairs:
            if key in seen and key not in duplicates:
                duplicates.append(key)
            seen.add(key)
        self.duplicates = tuple(duplicates)


@dataclass(frozen=True, slots=True)
class _BlockSpec:
    block_id: str
    block_type: str
    parts: tuple[Fragment, ...]
    separator: str


@dataclass(frozen=True, slots=True)
class _RecipeSpec:
    identifier: str | None
    sha256: str | None
    normalization: NormalizationOptions
    omitted_types: frozenset[str]
    blocks: tuple[_BlockSpec, ...]


def file_sha256(path: StrPath) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _reject_duplicate_members(value: object, location: str) -> None:
    if isinstance(value, _JsonObject):
        if value.duplicates:
            names = ", ".join(repr(name) for name in value.duplicates)
            raise EpubBlocksError(f"{location}: duplicate JSON member(s): {names}")
        for key, child in value.items():
            _reject_duplicate_members(child, f"{location}.{key}")
    elif isinstance(value, list):
        children = cast(list[object], value)
        for index, child in enumerate(children, 1):
            _reject_duplicate_members(child, f"{location}[{index}]")


def _mapping(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EpubBlocksError(f"{location}: must be an object")
    unknown_mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in unknown_mapping):
        raise EpubBlocksError(f"{location}: object member names must be strings")
    return cast(Mapping[str, object], value)


def _check_members(
    value: Mapping[str, object], allowed: frozenset[str], location: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        names = ", ".join(repr(name) for name in unknown)
        raise EpubBlocksError(f"{location}: unknown member(s): {names}")


def _string_array(value: object, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise EpubBlocksError(f"{location}: must be an array of non-empty strings")
    values = cast(list[object], value)
    if not all(isinstance(item, str) and item for item in values):
        raise EpubBlocksError(f"{location}: must be an array of non-empty strings")
    return tuple(cast(list[str], values))


def _normalization(value: object, location: str) -> NormalizationOptions:
    normalization = _mapping(value, location)
    _check_members(
        normalization,
        frozenset({"collapse_whitespace", "strip", "unicode_normalization"}),
        location,
    )
    collapse_whitespace = normalization.get("collapse_whitespace", True)
    strip = normalization.get("strip", True)
    if not isinstance(collapse_whitespace, bool):
        raise EpubBlocksError(f"{location}.collapse_whitespace: must be a boolean")
    if not isinstance(strip, bool):
        raise EpubBlocksError(f"{location}.strip: must be a boolean")
    unicode_normalization = normalization.get("unicode_normalization", "NFC")
    supported: tuple[UnicodeNormalization, ...] = (
        "NFC",
        "NFD",
        "NFKC",
        "NFKD",
        "none",
    )
    if not isinstance(unicode_normalization, str) or (
        unicode_normalization not in supported
    ):
        raise EpubBlocksError(
            f"{location}.unicode_normalization: unknown value "
            f"{unicode_normalization!r}"
        )
    return NormalizationOptions(
        collapse_whitespace=collapse_whitespace,
        strip=strip,
        unicode_normalization=unicode_normalization,
    )


def _validate_element_path(value: str, location: str, *, allow_empty: bool) -> None:
    if (not value and allow_empty) or _ELEMENT_PATH.fullmatch(value):
        return
    raise EpubBlocksError(
        f"{location}: must be a dot-separated sequence of positive integers"
    )


def _validate_omit_paths(paths: tuple[str, ...], location: str) -> None:
    parsed: list[tuple[str, ...]] = []
    for index, path in enumerate(paths, 1):
        path_location = f"{location}[{index}]"
        _validate_element_path(path, path_location, allow_empty=False)
        components = tuple(path.split("."))
        for previous_index, previous in enumerate(parsed, 1):
            shared_length = min(len(components), len(previous))
            if components[:shared_length] == previous[:shared_length]:
                raise EpubBlocksError(
                    f"{path_location}: duplicates or overlaps "
                    f"{location}[{previous_index}]"
                )
        parsed.append(components)


def _fragment(value: object, location: str) -> Fragment:
    part = _mapping(value, location)
    _check_members(
        part,
        frozenset({"document", "element_path", "omit", "slice"}),
        location,
    )
    document = part.get("document")
    if not isinstance(document, str) or not document:
        raise EpubBlocksError(f"{location}.document: must be a non-empty string")
    element_path = part.get("element_path", "")
    if not isinstance(element_path, str):
        raise EpubBlocksError(f"{location}.element_path: must be a string")
    _validate_element_path(
        element_path, f"{location}.element_path", allow_empty=True
    )
    omit_paths = _string_array(part.get("omit", []), f"{location}.omit")
    _validate_omit_paths(omit_paths, f"{location}.omit")

    start: int | None = None
    end: int | None = None
    slice_value = part.get("slice")
    if slice_value is not None:
        slice_spec = _mapping(slice_value, f"{location}.slice")
        _check_members(
            slice_spec, frozenset({"start", "end"}), f"{location}.slice"
        )
        start = slice_spec.get("start")  # type: ignore[assignment]
        end = slice_spec.get("end")  # type: ignore[assignment]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
        ):
            raise EpubBlocksError(
                f"{location}.slice: start and end must be integers"
            )
        if start < 0 or end <= start:
            raise EpubBlocksError(
                f"{location}.slice: must satisfy 0 <= start < end"
            )
    return Fragment(document, element_path, omit_paths, start, end)


def _parse_recipe(recipe: Mapping[str, object], location: str) -> _RecipeSpec:
    _check_members(
        recipe,
        frozenset(
            {
                "recipe_version",
                "epub",
                "normalization",
                "omit_epub_types",
                "blocks",
            }
        ),
        location,
    )
    if recipe.get("recipe_version") != "1":
        raise EpubBlocksError(f"{location}.recipe_version: unsupported version")

    epub = _mapping(recipe.get("epub", {}), f"{location}.epub")
    _check_members(epub, frozenset({"identifier", "sha256"}), f"{location}.epub")
    identifier = epub.get("identifier")
    if identifier is not None and (
        not isinstance(identifier, str) or not identifier
    ):
        raise EpubBlocksError(
            f"{location}.epub.identifier: must be a non-empty string"
        )
    sha256 = epub.get("sha256")
    if sha256 is not None and (
        not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None
    ):
        raise EpubBlocksError(
            f"{location}.epub.sha256: must be 64 lowercase hexadecimal characters"
        )

    normalization = _normalization(
        recipe.get("normalization", {}), f"{location}.normalization"
    )
    omitted_types = frozenset(
        _string_array(
            recipe.get("omit_epub_types", []), f"{location}.omit_epub_types"
        )
    )
    blocks_value = recipe.get("blocks")
    if not isinstance(blocks_value, list) or not blocks_value:
        raise EpubBlocksError(f"{location}.blocks: must be a non-empty array")
    block_values = cast(list[object], blocks_value)

    blocks: list[_BlockSpec] = []
    seen_ids: set[str] = set()
    for block_index, block_value in enumerate(block_values, 1):
        block_location = f"{location}.blocks[{block_index}]"
        block = _mapping(block_value, block_location)
        _check_members(
            block,
            frozenset({"id", "type", "parts", "separator"}),
            block_location,
        )
        block_id = block.get("id")
        block_type = block.get("type")
        if not isinstance(block_id, str) or not block_id:
            raise EpubBlocksError(f"{block_location}.id: must be a non-empty string")
        if block_id in seen_ids:
            raise EpubBlocksError(
                f"{block_location}.id: duplicate block id {block_id!r}"
            )
        if not isinstance(block_type, str) or not block_type:
            raise EpubBlocksError(
                f"{block_location}.type: must be a non-empty string"
            )
        parts_value = block.get("parts")
        if not isinstance(parts_value, list) or not parts_value:
            raise EpubBlocksError(
                f"{block_location}.parts: must be a non-empty array"
            )
        part_values = cast(list[object], parts_value)
        parts = tuple(
            _fragment(part, f"{block_location}.parts[{part_index}]")
            for part_index, part in enumerate(part_values, 1)
        )
        separator = block.get("separator", "")
        if not isinstance(separator, str):
            raise EpubBlocksError(f"{block_location}.separator: must be a string")
        blocks.append(_BlockSpec(block_id, block_type, parts, separator))
        seen_ids.add(block_id)

    return _RecipeSpec(
        identifier=identifier,
        sha256=sha256,
        normalization=normalization,
        omitted_types=omitted_types,
        blocks=tuple(blocks),
    )


def load_recipe(path: StrPath) -> dict[str, object]:
    recipe_path = Path(path)
    try:
        source = recipe_path.read_text(encoding="utf-8")
        value = cast(object, json.loads(source, object_pairs_hook=_JsonObject))
    except UnicodeDecodeError as error:
        raise EpubBlocksError(f"{recipe_path}: recipe is not UTF-8") from error
    except json.JSONDecodeError as error:
        raise EpubBlocksError(
            f"{recipe_path}:{error.lineno}:{error.colno}: invalid JSON recipe"
        ) from error
    if not isinstance(value, _JsonObject):
        raise EpubBlocksError(f"{recipe_path}: recipe must be an object")
    _reject_duplicate_members(value, str(recipe_path))
    return dict(value)


def extract_recipe(
    epub_path: StrPath,
    recipe: Mapping[str, object],
    *,
    recipe_location: str = "recipe",
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> list[ExtractedBlock]:
    """Apply a strictly validated extraction recipe."""

    path = Path(epub_path)
    spec = _parse_recipe(recipe, recipe_location)
    if spec.sha256 is not None and file_sha256(path) != spec.sha256:
        raise EpubBlocksError(f"{path}: source SHA-256 does not match recipe")

    result: list[ExtractedBlock] = []
    try:
        with ZipFile(path) as zip_file:
            epub = EpubArchive(zip_file, limits)
            package = read_epub_package(epub)
            if (
                spec.identifier is not None
                and spec.identifier not in package.identifiers
            ):
                raise EpubBlocksError(
                    f"{path}: expected package identifier "
                    f"{spec.identifier!r} not found"
                )
            cache: dict[str, XmlElement] = {}
            for block_index, block in enumerate(spec.blocks, 1):
                location = f"{recipe_location}.blocks[{block_index}]"
                extracted = [
                    extract_fragment(
                        epub,
                        part,
                        cache=cache,
                        normalization=spec.normalization,
                        omit_epub_types=spec.omitted_types,
                    )
                    for part in block.parts
                ]
                text = normalize_text(
                    block.separator.join(extracted), spec.normalization
                )
                if not text:
                    raise EpubBlocksError(
                        f"{location}: extraction produced no text"
                    )
                result.append(
                    ExtractedBlock(block.block_id, block.block_type, text)
                )
    except BadZipFile as error:
        raise EpubBlocksError(f"{path}: not a valid ZIP container") from error
    return result


def extract_recipe_file(
    epub_path: StrPath,
    recipe_path: StrPath,
    *,
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> list[ExtractedBlock]:
    """Load and apply a JSON extraction recipe."""

    recipe_file = Path(recipe_path)
    return extract_recipe(
        epub_path,
        load_recipe(recipe_file),
        recipe_location=str(recipe_file),
        limits=limits,
    )


def write_tsv(path: StrPath, blocks: Iterable[ExtractedBlock]) -> None:
    """Atomically write headerless identifier/type/text records as TSV."""

    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            writer = csv.writer(output, delimiter="\t", lineterminator="\n")
            writer.writerows(
                (block.block_id, block.block_type, block.text) for block in blocks
            )
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(output_path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
