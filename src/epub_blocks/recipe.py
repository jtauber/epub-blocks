from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from zipfile import BadZipFile, ZipFile

from .errors import EpubBlocksError
from .extract import _extract_blocks_from_epub  # pyright: ignore[reportPrivateUsage]
from .models import (
    BlockReference,
    CompiledBlock,
    CompiledRecipe,
    EpubPackage,
    ExtractedBlock,
    Fragment,
    NormalizationOptions,
    TextBlock,
    UnicodeNormalization,
)
from .package import read_epub_package
from .safety import DEFAULT_SAFETY_LIMITS, EpubArchive, SafetyLimits
from .xhtml import extract_fragment, normalize_text
from .xml import XmlElement

StrPath = str | os.PathLike[str]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ELEMENT_PATH = re.compile(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*")
_SOURCE_LOCATOR = re.compile(r"[^\r\n\u2028\u2029]+#[1-9][0-9]*(?:\.[1-9][0-9]*)*")
_FRAGMENT_LOCATOR = re.compile(
    r"[^\r\n\u2028\u2029]+#(?:[1-9][0-9]*(?:\.[1-9][0-9]*)*)?"
)
_UNICODE_NORMALIZATIONS: tuple[UnicodeNormalization, ...] = (
    "NFC",
    "NFD",
    "NFKC",
    "NFKD",
    "none",
)
_REFERENCE_COLUMN_DEFAULTS = {"id": "id", "type": "type"}


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


class _InvalidJsonConstant(ValueError):
    pass


def _reject_json_constant(value: str) -> object:
    raise _InvalidJsonConstant(value)


@dataclass(frozen=True, slots=True)
class _SourceBlocksSpec:
    include_documents: tuple[str, ...]
    exclude_documents: tuple[str, ...]
    exclude_classes: tuple[str, ...]
    include_locators: tuple[str, ...]
    exclude_locators: tuple[str, ...]
    include_non_linear: bool


@dataclass(frozen=True, slots=True)
class _ReferenceSpec:
    path: str
    sha256: str
    columns: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _GroupSpec:
    reference_pattern: str | None
    source_pattern: str | None
    source_marker: _SourceMarker | None
    reference_capture_kind: str
    source_offset: int
    reference_map: Mapping[str, str]
    source_map: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _SourceMarker:
    pattern: str
    capture_kind: str
    case_insensitive: bool


@dataclass(frozen=True, slots=True)
class _TypeRule:
    consume: int
    emit: tuple[int, ...] | None
    separator: str | None
    remove_prefix: _RemovePrefix | None


@dataclass(frozen=True, slots=True)
class _RemovePrefix:
    pattern: str
    case_insensitive: bool


@dataclass(frozen=True, slots=True)
class _OverrideSpec:
    parts: tuple[Fragment, ...]
    separator: str


@dataclass(frozen=True, slots=True)
class _MappingSpec:
    groups: _GroupSpec
    join_separator: str
    type_rules: Mapping[str, _TypeRule]
    skip_source: tuple[str, ...]
    overrides: Mapping[str, _OverrideSpec]
    compiled_sha256: str | None


@dataclass(frozen=True, slots=True)
class _RecipeSpec:
    identifier: str
    sha256: str
    references: _ReferenceSpec
    normalization: NormalizationOptions
    omitted_types: frozenset[str]
    source_blocks: _SourceBlocksSpec
    mapping: _MappingSpec


def _stream_sha256(source: BinaryIO) -> str:
    digest = hashlib.sha256()
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
        for index, child in enumerate(cast(list[object], value), 1):
            _reject_duplicate_members(child, f"{location}[{index}]")


def load_recipe(path: StrPath) -> dict[str, object]:
    """Load a UTF-8 JSON recipe while rejecting ambiguous JSON constructs."""

    recipe_path = Path(path)
    try:
        source = recipe_path.read_text(encoding="utf-8")
        value = cast(
            object,
            json.loads(
                source,
                object_pairs_hook=_JsonObject,
                parse_constant=_reject_json_constant,
            ),
        )
    except UnicodeDecodeError as error:
        raise EpubBlocksError(f"{recipe_path}: recipe is not UTF-8") from error
    except json.JSONDecodeError as error:
        raise EpubBlocksError(
            f"{recipe_path}:{error.lineno}:{error.colno}: invalid JSON recipe"
        ) from error
    except _InvalidJsonConstant as error:
        raise EpubBlocksError(
            f"{recipe_path}: invalid JSON constant {str(error)!r}"
        ) from error
    if not isinstance(value, _JsonObject):
        raise EpubBlocksError(f"{recipe_path}: recipe must be an object")
    _reject_duplicate_members(value, str(recipe_path))
    return dict(value)


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


def _nonempty_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise EpubBlocksError(f"{location}: must be a non-empty string")
    return value


def _sha256(value: object, location: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EpubBlocksError(
            f"{location}: must be 64 lowercase hexadecimal characters"
        )
    return value


def _integer(value: object, location: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise EpubBlocksError(f"{location}: must be an integer")
    if minimum is not None and value < minimum:
        raise EpubBlocksError(f"{location}: must be at least {minimum}")
    return value


def _string_array(value: object, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise EpubBlocksError(f"{location}: must be an array of non-empty strings")
    values = cast(list[object], value)
    if not all(isinstance(item, str) and item for item in values):
        raise EpubBlocksError(f"{location}: must be an array of non-empty strings")
    strings = tuple(cast(list[str], values))
    if len(set(strings)) != len(strings):
        raise EpubBlocksError(f"{location}: values must be unique")
    return strings


def _string_map(value: object, location: str) -> dict[str, str]:
    values = _mapping(value, location)
    result: dict[str, str] = {}
    for key, item in values.items():
        if not key:
            raise EpubBlocksError(f"{location}: member names must be non-empty")
        result[key] = _nonempty_string(item, f"{location}[{key!r}]")
    return result


def _integer_array(value: object, location: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise EpubBlocksError(f"{location}: must be a non-empty array of integers")
    result = tuple(
        _integer(item, f"{location}[{index}]", minimum=1)
        for index, item in enumerate(cast(list[object], value), 1)
    )
    if len(set(result)) != len(result):
        raise EpubBlocksError(f"{location}: values must be unique")
    return result


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
    if not isinstance(unicode_normalization, str) or (
        unicode_normalization not in _UNICODE_NORMALIZATIONS
    ):
        raise EpubBlocksError(
            f"{location}.unicode_normalization: unknown value {unicode_normalization!r}"
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
    document = _nonempty_string(part.get("document"), f"{location}.document")
    element_path = part.get("element_path", "")
    if not isinstance(element_path, str):
        raise EpubBlocksError(f"{location}.element_path: must be a string")
    _validate_element_path(element_path, f"{location}.element_path", allow_empty=True)
    omit_paths = _string_array(part.get("omit", []), f"{location}.omit")
    _validate_omit_paths(omit_paths, f"{location}.omit")
    start: int | None = None
    end: int | None = None
    if "slice" in part:
        slice_spec = _mapping(part["slice"], f"{location}.slice")
        _check_members(slice_spec, frozenset({"start", "end"}), f"{location}.slice")
        start = _integer(slice_spec.get("start"), f"{location}.slice.start")
        end = _integer(slice_spec.get("end"), f"{location}.slice.end")
        if start < 0 or end <= start:
            raise EpubBlocksError(f"{location}.slice: must satisfy 0 <= start < end")
    return Fragment(document, element_path, omit_paths, start, end)


def _remove_prefix(value: object, location: str) -> _RemovePrefix:
    spec = _mapping(value, location)
    _check_members(spec, frozenset({"pattern", "case_insensitive"}), location)
    pattern = _nonempty_string(spec.get("pattern"), f"{location}.pattern")
    if not pattern.startswith("^"):
        raise EpubBlocksError(f"{location}.pattern: must be anchored with ^")
    try:
        expression = re.compile(pattern)
    except re.error as error:
        raise EpubBlocksError(
            f"{location}.pattern: invalid regular expression: {error}"
        ) from error
    empty_match = expression.match("")
    if empty_match is not None and empty_match.end() == 0:
        raise EpubBlocksError(f"{location}.pattern: must not match an empty prefix")
    case_insensitive = spec.get("case_insensitive", False)
    if not isinstance(case_insensitive, bool):
        raise EpubBlocksError(f"{location}.case_insensitive: must be a boolean")
    return _RemovePrefix(pattern, case_insensitive)


def _reference_spec(value: object, location: str) -> _ReferenceSpec:
    references = _mapping(value, location)
    _check_members(references, frozenset({"path", "sha256", "columns"}), location)
    path = _nonempty_string(references.get("path"), f"{location}.path")
    digest = _sha256(references.get("sha256"), f"{location}.sha256")
    columns_value = _mapping(references.get("columns", {}), f"{location}.columns")
    _check_members(
        columns_value,
        frozenset(_REFERENCE_COLUMN_DEFAULTS),
        f"{location}.columns",
    )
    columns = {
        name: _nonempty_string(
            columns_value.get(name, default), f"{location}.columns.{name}"
        )
        for name, default in _REFERENCE_COLUMN_DEFAULTS.items()
    }
    if len(set(columns.values())) != len(columns):
        raise EpubBlocksError(f"{location}.columns: column names must be unique")
    return _ReferenceSpec(path, digest, columns)


def _source_blocks_spec(value: object, location: str) -> _SourceBlocksSpec:
    source = _mapping(value, location)
    _check_members(
        source,
        frozenset(
            {
                "include_documents",
                "exclude_documents",
                "exclude_classes",
                "include_locators",
                "exclude_locators",
                "include_non_linear",
            }
        ),
        location,
    )
    include_non_linear = source.get("include_non_linear", False)
    if not isinstance(include_non_linear, bool):
        raise EpubBlocksError(f"{location}.include_non_linear: must be a boolean")
    return _SourceBlocksSpec(
        _string_array(
            source.get("include_documents", []), f"{location}.include_documents"
        ),
        _string_array(
            source.get("exclude_documents", []), f"{location}.exclude_documents"
        ),
        _string_array(source.get("exclude_classes", []), f"{location}.exclude_classes"),
        _string_array(
            source.get("include_locators", []), f"{location}.include_locators"
        ),
        _string_array(
            source.get("exclude_locators", []), f"{location}.exclude_locators"
        ),
        include_non_linear,
    )


def _group_pattern(value: object, location: str) -> str:
    pattern = _nonempty_string(value, location)
    try:
        expression = re.compile(pattern)
    except re.error as error:
        raise EpubBlocksError(
            f"{location}: invalid regular expression: {error}"
        ) from error
    if expression.groups != 1:
        raise EpubBlocksError(f"{location}: must contain exactly one capturing group")
    return pattern


def _group_spec(value: object, location: str) -> _GroupSpec:
    groups = _mapping(value, location)
    _check_members(
        groups,
        frozenset(
            {
                "reference_pattern",
                "reference_capture_kind",
                "source_pattern",
                "source_marker",
                "source_offset",
                "reference_map",
                "source_map",
            }
        ),
        location,
    )
    has_reference = "reference_pattern" in groups
    has_source_pattern = "source_pattern" in groups
    has_source_marker = "source_marker" in groups
    if has_source_pattern and has_source_marker:
        raise EpubBlocksError(
            f"{location}: source_pattern and source_marker are mutually exclusive"
        )
    if has_reference != (has_source_pattern or has_source_marker):
        raise EpubBlocksError(
            f"{location}: reference_pattern and exactly one source grouping "
            "mechanism must be set together or all omitted"
        )
    reference_pattern = (
        _group_pattern(groups["reference_pattern"], f"{location}.reference_pattern")
        if has_reference
        else None
    )
    source_pattern = (
        _group_pattern(groups["source_pattern"], f"{location}.source_pattern")
        if has_source_pattern
        else None
    )
    reference_capture_kind = groups.get("reference_capture_kind", "string")
    if not isinstance(reference_capture_kind, str) or reference_capture_kind not in {
        "string",
        "decimal",
    }:
        raise EpubBlocksError(
            f"{location}.reference_capture_kind: must be 'string' or 'decimal'"
        )
    if "reference_capture_kind" in groups and not has_reference:
        raise EpubBlocksError(
            f"{location}.reference_capture_kind: requires reference_pattern"
        )

    source_marker: _SourceMarker | None = None
    if has_source_marker:
        marker_location = f"{location}.source_marker"
        marker = _mapping(groups["source_marker"], marker_location)
        _check_members(
            marker,
            frozenset({"pattern", "capture_kind", "case_insensitive"}),
            marker_location,
        )
        marker_pattern = _group_pattern(
            marker.get("pattern"), f"{marker_location}.pattern"
        )
        if not marker_pattern.startswith("^"):
            raise EpubBlocksError(f"{marker_location}.pattern: must be anchored with ^")
        capture_kind = marker.get("capture_kind")
        if not isinstance(capture_kind, str) or capture_kind not in {
            "string",
            "decimal",
            "roman",
        }:
            raise EpubBlocksError(
                f"{marker_location}.capture_kind: must be 'string', 'decimal', "
                "or 'roman'"
            )
        case_insensitive = marker.get("case_insensitive", False)
        if not isinstance(case_insensitive, bool):
            raise EpubBlocksError(
                f"{marker_location}.case_insensitive: must be a boolean"
            )
        source_marker = _SourceMarker(marker_pattern, capture_kind, case_insensitive)

    source_offset = _integer(
        groups.get("source_offset", 0), f"{location}.source_offset"
    )
    reference_map = _string_map(
        groups.get("reference_map", {}), f"{location}.reference_map"
    )
    source_map = _string_map(groups.get("source_map", {}), f"{location}.source_map")
    if "reference_map" in groups and not reference_map:
        raise EpubBlocksError(f"{location}.reference_map: must not be empty")
    if "source_map" in groups and not source_map:
        raise EpubBlocksError(f"{location}.source_map: must not be empty")
    if (reference_map or source_map) and reference_pattern is None:
        raise EpubBlocksError(f"{location}: group maps require group patterns")
    if source_offset and reference_pattern is None:
        raise EpubBlocksError(f"{location}: source_offset requires group patterns")
    if source_map and source_offset:
        raise EpubBlocksError(
            f"{location}: source_offset cannot be combined with source_map"
        )
    return _GroupSpec(
        reference_pattern,
        source_pattern,
        source_marker,
        reference_capture_kind,
        source_offset,
        reference_map,
        source_map,
    )


def _mapping_spec(
    value: object, location: str, *, require_compiled_hash: bool
) -> _MappingSpec:
    mapping = _mapping(value, location)
    _check_members(
        mapping,
        frozenset(
            {
                "strategy",
                "groups",
                "join_separator",
                "type_rules",
                "skip_source",
                "overrides",
                "compiled_sha256",
            }
        ),
        location,
    )
    if mapping.get("strategy") != "ordered":
        raise EpubBlocksError(f"{location}.strategy: unsupported strategy")
    groups = _group_spec(mapping.get("groups", {}), f"{location}.groups")
    join_separator = mapping.get("join_separator", " ")
    if not isinstance(join_separator, str):
        raise EpubBlocksError(f"{location}.join_separator: must be a string")

    type_values = _mapping(mapping.get("type_rules", {}), f"{location}.type_rules")
    type_rules: dict[str, _TypeRule] = {}
    for block_type, rule_value in type_values.items():
        if not block_type:
            raise EpubBlocksError(
                f"{location}.type_rules: member names must be non-empty"
            )
        rule_location = f"{location}.type_rules[{block_type!r}]"
        rule = _mapping(rule_value, rule_location)
        _check_members(
            rule,
            frozenset({"consume", "emit", "separator", "remove_prefix"}),
            rule_location,
        )
        consume = _integer(
            rule.get("consume", 1), f"{rule_location}.consume", minimum=1
        )
        emit = (
            _integer_array(rule["emit"], f"{rule_location}.emit")
            if "emit" in rule
            else None
        )
        if emit is not None and max(emit) > consume:
            raise EpubBlocksError(
                f"{rule_location}.emit: values must not exceed consume ({consume})"
            )
        separator_value = rule.get("separator")
        if separator_value is not None and not isinstance(separator_value, str):
            raise EpubBlocksError(f"{rule_location}.separator: must be a string")
        remove_prefix = (
            _remove_prefix(rule["remove_prefix"], f"{rule_location}.remove_prefix")
            if "remove_prefix" in rule
            else None
        )
        emitted_count = len(emit) if emit is not None else consume
        if remove_prefix is not None and emitted_count != 1:
            raise EpubBlocksError(
                f"{rule_location}.remove_prefix: requires exactly one emitted part"
            )
        type_rules[block_type] = _TypeRule(
            consume,
            emit,
            separator_value,
            remove_prefix,
        )

    skip_source = _string_array(
        mapping.get("skip_source", []), f"{location}.skip_source"
    )
    for index, locator in enumerate(skip_source, 1):
        if _SOURCE_LOCATOR.fullmatch(locator) is None:
            raise EpubBlocksError(
                f"{location}.skip_source[{index}]: must contain a document and "
                "one-based element path separated by #"
            )

    overrides_value = _mapping(mapping.get("overrides", {}), f"{location}.overrides")
    overrides: dict[str, _OverrideSpec] = {}
    for block_id, override_value in overrides_value.items():
        if not block_id:
            raise EpubBlocksError(
                f"{location}.overrides: member names must be non-empty"
            )
        override_location = f"{location}.overrides[{block_id!r}]"
        override = _mapping(override_value, override_location)
        _check_members(override, frozenset({"parts", "separator"}), override_location)
        parts_value = override.get("parts")
        if not isinstance(parts_value, list) or not parts_value:
            raise EpubBlocksError(
                f"{override_location}.parts: must be a non-empty array"
            )
        parts = tuple(
            _fragment(part, f"{override_location}.parts[{index}]")
            for index, part in enumerate(cast(list[object], parts_value), 1)
        )
        separator = override.get("separator", "")
        if not isinstance(separator, str):
            raise EpubBlocksError(f"{override_location}.separator: must be a string")
        overrides[block_id] = _OverrideSpec(parts, separator)

    _validate_override_reuse(overrides, f"{location}.overrides")

    compiled_sha256: str | None = None
    if require_compiled_hash or "compiled_sha256" in mapping:
        compiled_sha256 = _sha256(
            mapping.get("compiled_sha256"), f"{location}.compiled_sha256"
        )
    return _MappingSpec(
        groups,
        join_separator,
        type_rules,
        skip_source,
        overrides,
        compiled_sha256,
    )


def _validate_override_reuse(
    overrides: Mapping[str, _OverrideSpec], location: str
) -> None:
    uses: dict[tuple[str, str], list[tuple[str, int, Fragment]]] = defaultdict(list)
    for block_id, override in overrides.items():
        for part_index, part in enumerate(override.parts, 1):
            uses[(part.document_path, part.element_path)].append(
                (block_id, part_index, part)
            )

    for (document_path, element_path), entries in uses.items():
        if len(entries) < 2:
            continue
        locator = f"{document_path}#{element_path}"
        if any(part.start is None or part.end is None for _, _, part in entries):
            raise EpubBlocksError(
                f"{location}: source fragment {locator!r} is reused; every reuse "
                "must use non-overlapping slices"
            )

        sliced = sorted(
            (
                cast(int, part.start),
                cast(int, part.end),
                block_id,
                part_index,
            )
            for block_id, part_index, part in entries
        )
        _, previous_end, previous_id, previous_part = sliced[0]
        for start, end, block_id, part_index in sliced[1:]:
            if start < previous_end:
                raise EpubBlocksError(
                    f"{location}: source fragment {locator!r} has overlapping "
                    f"slices in {previous_id!r} part {previous_part} and "
                    f"{block_id!r} part {part_index}"
                )
            previous_end = end
            previous_id, previous_part = block_id, part_index


def _parse_recipe(
    recipe: Mapping[str, object], location: str, *, require_compiled_hash: bool
) -> _RecipeSpec:
    _check_members(
        recipe,
        frozenset(
            {
                "recipe_version",
                "metadata",
                "epub",
                "references",
                "normalization",
                "omit_epub_types",
                "source_blocks",
                "mapping",
            }
        ),
        location,
    )
    if recipe.get("recipe_version") != "1":
        raise EpubBlocksError(f"{location}.recipe_version: unsupported version")
    if "metadata" in recipe:
        _mapping(recipe["metadata"], f"{location}.metadata")

    epub = _mapping(recipe.get("epub"), f"{location}.epub")
    _check_members(epub, frozenset({"identifier", "sha256"}), f"{location}.epub")
    identifier = _nonempty_string(epub.get("identifier"), f"{location}.epub.identifier")
    source_sha256 = _sha256(epub.get("sha256"), f"{location}.epub.sha256")
    references = _reference_spec(recipe.get("references"), f"{location}.references")
    normalization = _normalization(
        recipe.get("normalization", {}), f"{location}.normalization"
    )
    omitted_types = frozenset(
        _string_array(recipe.get("omit_epub_types", []), f"{location}.omit_epub_types")
    )
    source_blocks = _source_blocks_spec(
        recipe.get("source_blocks"), f"{location}.source_blocks"
    )
    mapping = _mapping_spec(
        recipe.get("mapping"),
        f"{location}.mapping",
        require_compiled_hash=require_compiled_hash,
    )
    return _RecipeSpec(
        identifier,
        source_sha256,
        references,
        normalization,
        omitted_types,
        source_blocks,
        mapping,
    )


def _reference_path(spec: _ReferenceSpec, base_dir: StrPath | None) -> Path:
    path = Path(spec.path)
    if path.is_absolute():
        return path
    if base_dir is None:
        raise EpubBlocksError(
            "recipe.references.path: relative paths require a recipe base directory"
        )
    return Path(base_dir) / path


def _read_references(
    spec: _ReferenceSpec, base_dir: StrPath | None
) -> list[BlockReference]:
    path = _reference_path(spec, base_dir)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != spec.sha256:
        raise EpubBlocksError(f"{path}: reference-index SHA-256 does not match recipe")
    references: list[BlockReference] = []
    seen: set[str] = set()
    try:
        with io.StringIO(data.decode("utf-8"), newline="") as source:
            reader = csv.reader(source, delimiter="\t", strict=True)
            header = next(reader, None)
            if header is None or not header:
                raise EpubBlocksError(f"{path}: reference index has no header")
            if len(header) != 2:
                raise EpubBlocksError(
                    f"{path}: reference index must have exactly two columns"
                )
            if len(set(header)) != len(header):
                raise EpubBlocksError(f"{path}: reference index has duplicate columns")
            missing = [
                column for column in spec.columns.values() if column not in header
            ]
            if missing:
                names = ", ".join(repr(name) for name in missing)
                raise EpubBlocksError(
                    f"{path}: reference index missing column(s): {names}"
                )
            positions = {
                name: header.index(column) for name, column in spec.columns.items()
            }
            for line_number, row in enumerate(reader, 2):
                if len(row) != 2:
                    raise EpubBlocksError(
                        f"{path}:{line_number}: expected 2 fields, found {len(row)}"
                    )
                block_id = row[positions["id"]]
                block_type = row[positions["type"]]
                if not block_id:
                    raise EpubBlocksError(
                        f"{path}:{line_number}: empty canonical reference"
                    )
                if block_id in seen:
                    raise EpubBlocksError(
                        f"{path}:{line_number}: duplicate canonical reference "
                        f"{block_id!r}"
                    )
                if not block_type:
                    raise EpubBlocksError(f"{path}:{line_number}: empty block type")
                references.append(BlockReference(block_id, block_type))
                seen.add(block_id)
    except UnicodeDecodeError as error:
        raise EpubBlocksError(f"{path}: reference index is not UTF-8") from error
    except csv.Error as error:
        raise EpubBlocksError(
            f"{path}: invalid reference-index TSV: {error}"
        ) from error
    if not references:
        raise EpubBlocksError(f"{path}: reference index has no records")
    return references


_ROMAN_PATTERN = re.compile(
    r"M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
)


def _roman_to_int(value: str, location: str) -> int:
    canonical = value.upper()
    if not canonical or _ROMAN_PATTERN.fullmatch(canonical) is None:
        raise EpubBlocksError(f"{location}: invalid or noncanonical Roman numeral")
    numbers = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for character in reversed(canonical):
        number = numbers[character]
        if number < previous:
            total -= number
        else:
            total += number
            previous = number
    return total


def _capture_key(value: str, kind: str, location: str) -> str:
    if not value:
        raise EpubBlocksError(f"{location}: group capture must not be empty")
    if kind == "string":
        return value
    if kind == "decimal":
        if re.fullmatch(r"[0-9]+", value) is None:
            raise EpubBlocksError(f"{location}: group capture is not decimal")
        return str(int(value))
    if kind == "roman":
        return str(_roman_to_int(value, location))
    raise AssertionError(f"validated capture kind is unknown: {kind!r}")


def _group_references(
    records: Sequence[BlockReference], spec: _GroupSpec
) -> dict[str, list[BlockReference]]:
    if spec.reference_pattern is None:
        return {"": list(records)}
    expression = re.compile(spec.reference_pattern, re.IGNORECASE)
    grouped: dict[str, list[BlockReference]] = defaultdict(list)
    for record in records:
        match = expression.search(record.block_id)
        if not match:
            raise EpubBlocksError(
                f"canonical reference does not match group pattern: {record.block_id!r}"
            )
        captured = match.group(1)
        if captured is None:
            raise EpubBlocksError(
                "canonical-reference group capture did not participate for "
                f"{record.block_id!r}"
            )
        if not captured:
            raise EpubBlocksError(
                f"canonical-reference group capture is empty for {record.block_id!r}"
            )
        if captured in spec.reference_map:
            group = spec.reference_map[captured]
        else:
            group = _capture_key(
                captured,
                spec.reference_capture_kind,
                f"canonical reference {record.block_id!r}",
            )
        grouped[group].append(record)
    return dict(grouped)


def _group_sources(
    records: Sequence[TextBlock],
    spec: _GroupSpec,
    reference_group_order: Sequence[str],
) -> dict[str, list[TextBlock]]:
    if spec.source_pattern is None and spec.source_marker is None:
        return {"": list(records)}

    if spec.source_marker is not None:
        marker = spec.source_marker
        flags = re.IGNORECASE if marker.case_insensitive else 0
        expression = re.compile(marker.pattern, flags)
        order = {group: index for index, group in enumerate(reference_group_order)}
        grouped: dict[str, list[TextBlock]] = defaultdict(list)
        current: str | None = None
        seen: set[str] = set()
        last_index = -1
        for record in records:
            match = expression.match(record.text)
            if match is not None:
                captured = match.group(1)
                if captured is None or not captured:
                    raise EpubBlocksError(
                        f"source-marker capture is empty for {record.source_locator!r}"
                    )
                if captured in spec.source_map:
                    group = spec.source_map[captured]
                else:
                    group = _capture_key(
                        captured,
                        marker.capture_kind,
                        f"source marker {record.source_locator!r}",
                    )
                    if spec.source_offset:
                        try:
                            numeric_group = int(group) + spec.source_offset
                        except ValueError as error:
                            raise EpubBlocksError(
                                "source_offset requires a numeric source-marker capture"
                            ) from error
                        if numeric_group < 1:
                            raise EpubBlocksError(
                                f"source marker maps to invalid group {numeric_group}: "
                                f"{record.source_locator!r}"
                            )
                        group = str(numeric_group)
                if group in seen:
                    raise EpubBlocksError(
                        f"source marker repeats group {group!r}: "
                        f"{record.source_locator!r}"
                    )
                if group not in order:
                    raise EpubBlocksError(
                        f"source marker establishes unknown group {group!r}: "
                        f"{record.source_locator!r}"
                    )
                group_index = order[group]
                if group_index <= last_index:
                    raise EpubBlocksError(
                        f"source marker moves backward to group {group!r}: "
                        f"{record.source_locator!r}"
                    )
                current = group
                seen.add(group)
                last_index = group_index
            if current is None:
                raise EpubBlocksError(
                    "source block occurs before the first source marker: "
                    f"{record.source_locator!r}"
                )
            grouped[current].append(record)
        return dict(grouped)

    if spec.source_pattern is None:
        raise AssertionError("validated source grouping mechanism is missing")
    expression = re.compile(spec.source_pattern, re.IGNORECASE)
    grouped: dict[str, list[TextBlock]] = defaultdict(list)
    for record in records:
        match = expression.search(record.source_locator)
        if not match:
            raise EpubBlocksError(
                f"source block does not match group pattern: {record.source_locator!r}"
            )
        captured = match.group(1)
        if captured is None:
            raise EpubBlocksError(
                "source group capture did not participate for "
                f"{record.source_locator!r}"
            )
        if spec.source_map:
            if captured not in spec.source_map:
                raise EpubBlocksError(f"source group {captured!r} has no mapping")
            group = spec.source_map[captured]
        else:
            try:
                numeric_group = int(captured) + spec.source_offset
            except ValueError as error:
                if spec.source_offset:
                    raise EpubBlocksError(
                        "source group must be a decimal integer for "
                        f"{record.source_locator!r}"
                    ) from error
                group = captured
            else:
                if numeric_group < 1:
                    raise EpubBlocksError(
                        f"source block maps to invalid group {numeric_group}: "
                        f"{record.source_locator!r}"
                    )
                group = str(numeric_group)
        grouped[group].append(record)
    return dict(grouped)


def _fragment_from_block(block: TextBlock) -> Fragment:
    return Fragment(block.document_path, block.element_path)


def _fragment_locator(fragment: Fragment) -> str:
    return f"{fragment.document_path}#{fragment.element_path}"


def _apply_prefix_rule(
    block: TextBlock,
    rule: _RemovePrefix,
    *,
    location: str,
) -> Fragment:
    flags = re.IGNORECASE if rule.case_insensitive else 0
    match = re.match(rule.pattern, block.text, flags)
    if match is None or match.end() == 0:
        raise EpubBlocksError(
            f"{location}: remove_prefix did not match a non-empty source prefix"
        )
    if match.end() == len(block.text):
        raise EpubBlocksError(f"{location}: remove_prefix would remove all text")
    return Fragment(
        block.document_path,
        block.element_path,
        start=match.end(),
        end=len(block.text),
    )


def _compile_blocks(
    references: Sequence[BlockReference],
    candidates: Sequence[TextBlock],
    spec: _RecipeSpec,
) -> tuple[tuple[CompiledBlock, ...], tuple[str, ...], tuple[str, ...]]:
    overrides = spec.mapping.overrides
    known_reference_ids = {reference.block_id for reference in references}
    unknown_overrides = sorted(set(overrides) - known_reference_ids)
    if unknown_overrides:
        names = ", ".join(repr(name) for name in unknown_overrides)
        raise EpubBlocksError(f"mapping overrides unknown reference(s): {names}")

    reserved = {
        _fragment_locator(part)
        for override in overrides.values()
        for part in override.parts
    }
    skipped = set(spec.mapping.skip_source)
    collisions = sorted(skipped & reserved)
    if collisions:
        names = ", ".join(repr(locator) for locator in collisions)
        raise EpubBlocksError(
            "mapping source locators are both skipped and reserved: " + names
        )
    candidate_locator_counts = Counter(block.locator for block in candidates)
    missing_skips = sorted(
        locator for locator in skipped if candidate_locator_counts[locator] == 0
    )
    if missing_skips:
        names = ", ".join(repr(locator) for locator in missing_skips)
        raise EpubBlocksError(
            "mapping skip_source locator(s) are not selected source candidates: "
            + names
        )
    ambiguous_skips = sorted(
        locator for locator in skipped if candidate_locator_counts[locator] > 1
    )
    if ambiguous_skips:
        names = ", ".join(repr(locator) for locator in ambiguous_skips)
        raise EpubBlocksError(
            "mapping skip_source locator(s) do not identify exactly one selected "
            f"source candidate: {names}"
        )
    ambiguous_reserved = sorted(
        locator for locator in reserved if candidate_locator_counts[locator] > 1
    )
    if ambiguous_reserved:
        names = ", ".join(repr(locator) for locator in ambiguous_reserved)
        raise EpubBlocksError(
            "mapping override locator(s) identify more than one selected source "
            f"candidate: {names}"
        )

    reference_groups = _group_references(references, spec.mapping.groups)
    grouped_candidates = _group_sources(
        candidates,
        spec.mapping.groups,
        tuple(reference_groups),
    )
    source_groups = {
        group: available
        for group, blocks in grouped_candidates.items()
        if (
            available := [
                block for block in blocks if block.locator not in skipped | reserved
            ]
        )
    }
    extra_source_groups = sorted(set(source_groups) - set(reference_groups))
    if extra_source_groups:
        raise EpubBlocksError(
            "reference and source group sets differ: unexpected source group(s) "
            + ", ".join(repr(group) for group in extra_source_groups)
        )
    missing_source_groups = [
        group
        for group, group_references in reference_groups.items()
        if group not in source_groups
        and any(reference.block_id not in overrides for reference in group_references)
    ]
    if missing_source_groups:
        raise EpubBlocksError(
            "reference and source group sets differ: missing source group(s) "
            + ", ".join(repr(group) for group in missing_source_groups)
        )

    compiled_by_id: dict[str, CompiledBlock] = {}
    for group, group_references in reference_groups.items():
        source = source_groups.get(group, [])
        cursor = 0
        for reference in group_references:
            override = overrides.get(reference.block_id)
            if override is not None:
                compiled_by_id[reference.block_id] = CompiledBlock(
                    reference.block_id,
                    reference.block_type,
                    override.parts,
                    override.separator,
                    (),
                )
                continue

            rule = spec.mapping.type_rules.get(reference.block_type)
            consume = rule.consume if rule is not None else 1
            available = len(source) - cursor
            if available < consume:
                raise EpubBlocksError(
                    f"group {group!r}: reference {reference.block_id!r} needs "
                    f"{consume} source blocks, found {available}"
                )
            used = source[cursor : cursor + consume]
            emit = (
                rule.emit
                if rule is not None and rule.emit is not None
                else tuple(range(1, consume + 1))
            )
            emitted = [used[index - 1] for index in emit]
            parts = tuple(_fragment_from_block(block) for block in emitted)
            if rule is not None and rule.remove_prefix is not None:
                if len(emitted) != 1:
                    raise AssertionError(
                        "remove_prefix emit count validated during parsing"
                    )
                parts = (
                    _apply_prefix_rule(
                        emitted[0],
                        rule.remove_prefix,
                        location=(f"group {group!r}: reference {reference.block_id!r}"),
                    ),
                )
            separator = (
                rule.separator
                if rule is not None and rule.separator is not None
                else spec.mapping.join_separator
                if len(parts) > 1
                else ""
            )
            compiled_by_id[reference.block_id] = CompiledBlock(
                reference.block_id,
                reference.block_type,
                parts,
                separator,
                tuple(block.locator for block in used),
            )
            cursor += consume
        if cursor != len(source):
            remaining = ", ".join(
                block.source_locator for block in source[cursor : cursor + 3]
            )
            raise EpubBlocksError(
                f"group {group!r}: {len(source) - cursor} unmapped source "
                f"block(s), beginning with {remaining}"
            )
    return (
        tuple(compiled_by_id[reference.block_id] for reference in references),
        tuple(sorted(skipped)),
        tuple(sorted(reserved)),
    )


def _fragment_digest_value(fragment: Fragment, location: str) -> dict[str, object]:
    document = cast(object, fragment.document_path)
    if not isinstance(document, str) or not document:
        raise EpubBlocksError(f"{location}.document: must be a non-empty string")
    element_path = cast(object, fragment.element_path)
    if not isinstance(element_path, str):
        raise EpubBlocksError(f"{location}.element_path: must be a string")
    _validate_element_path(element_path, f"{location}.element_path", allow_empty=True)
    omit_value = cast(object, fragment.omit_paths)
    if not isinstance(omit_value, tuple):
        raise EpubBlocksError(f"{location}.omit: must contain non-empty paths")
    omit_items = cast(tuple[object, ...], omit_value)
    if not all(isinstance(path, str) and path for path in omit_items):
        raise EpubBlocksError(f"{location}.omit: must contain non-empty paths")
    omit_paths = cast(tuple[str, ...], omit_items)
    _validate_omit_paths(omit_paths, f"{location}.omit")
    has_start = fragment.start is not None
    has_end = fragment.end is not None
    if has_start != has_end:
        raise EpubBlocksError(f"{location}.slice: start and end must both be set")
    if has_start:
        if type(fragment.start) is not int or type(fragment.end) is not int:
            raise EpubBlocksError(f"{location}.slice: start and end must be integers")
        if fragment.start < 0 or fragment.end <= fragment.start:
            raise EpubBlocksError(f"{location}.slice: must satisfy 0 <= start < end")
    value: dict[str, object] = {
        "document": document,
        "element_path": element_path,
    }
    if omit_paths:
        value["omit"] = sorted(omit_paths)
    if fragment.start is not None and fragment.end is not None:
        value["slice"] = {"start": fragment.start, "end": fragment.end}
    return value


def _locator_tuple(
    value: object, location: str, *, allow_body: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise EpubBlocksError(f"{location}: must be a tuple of source locators")
    items = cast(tuple[object, ...], value)
    pattern = _FRAGMENT_LOCATOR if allow_body else _SOURCE_LOCATOR
    if not all(
        isinstance(locator, str) and pattern.fullmatch(locator) for locator in items
    ):
        raise EpubBlocksError(f"{location}: contains an invalid source locator")
    locators = cast(tuple[str, ...], items)
    if len(set(locators)) != len(locators):
        raise EpubBlocksError(f"{location}: source locators must be unique")
    return locators


def compiled_recipe_digest(compiled: CompiledRecipe) -> str:
    """Return the canonical SHA-256 digest of compiled extraction state."""

    normalization = compiled.normalization
    collapse_whitespace = cast(object, normalization.collapse_whitespace)
    strip = cast(object, normalization.strip)
    unicode_normalization = cast(object, normalization.unicode_normalization)
    if not isinstance(collapse_whitespace, bool):
        raise EpubBlocksError("compiled normalization collapse_whitespace is invalid")
    if not isinstance(strip, bool):
        raise EpubBlocksError("compiled normalization strip is invalid")
    if (
        not isinstance(unicode_normalization, str)
        or unicode_normalization not in _UNICODE_NORMALIZATIONS
    ):
        raise EpubBlocksError("compiled Unicode normalization is invalid")
    omitted_value = cast(object, compiled.omit_epub_types)
    if not isinstance(omitted_value, frozenset):
        raise EpubBlocksError(
            "compiled omit_epub_types values must be non-empty strings"
        )
    omitted_items = cast(frozenset[object], omitted_value)
    if not all(isinstance(value, str) and value for value in omitted_items):
        raise EpubBlocksError(
            "compiled omit_epub_types values must be non-empty strings"
        )
    omitted_types = cast(frozenset[str], omitted_items)
    skipped = _locator_tuple(compiled.skipped_locators, "compiled skipped_locators")
    reserved = _locator_tuple(
        compiled.reserved_locators,
        "compiled reserved_locators",
        allow_body=True,
    )
    if set(skipped) & set(reserved):
        raise EpubBlocksError(
            "compiled skipped_locators and reserved_locators must be disjoint"
        )
    if not compiled.blocks:
        raise EpubBlocksError("compiled recipe must contain at least one block")

    blocks: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    all_consumed: set[str] = set()
    for block_index, block in enumerate(compiled.blocks, 1):
        location = f"compiled block {block_index}"
        block_id = cast(object, block.block_id)
        block_type = cast(object, block.block_type)
        separator = cast(object, block.separator)
        if not isinstance(block_id, str) or not block_id:
            raise EpubBlocksError(f"{location}.id: must be a non-empty string")
        if block_id in seen_ids:
            raise EpubBlocksError(f"{location}.id: duplicate {block_id!r}")
        seen_ids.add(block_id)
        if not isinstance(block_type, str) or not block_type:
            raise EpubBlocksError(f"{location}.type: must be a non-empty string")
        if not block.parts:
            raise EpubBlocksError(f"{location}.parts: must not be empty")
        if not isinstance(separator, str):
            raise EpubBlocksError(f"{location}.separator: must be a string")
        consumed = _locator_tuple(
            block.consumed_locators, f"{location}.consumed_locators"
        )
        duplicates = sorted(all_consumed & set(consumed))
        if duplicates:
            raise EpubBlocksError(
                f"{location}.consumed_locators: already consumed "
                + ", ".join(repr(locator) for locator in duplicates)
            )
        all_consumed.update(consumed)
        if set(consumed) & (set(skipped) | set(reserved)):
            raise EpubBlocksError(
                f"{location}.consumed_locators: overlaps skipped or reserved source"
            )
        value: dict[str, object] = {
            "id": block_id,
            "type": block_type,
            "parts": [
                _fragment_digest_value(part, f"{location}.parts[{part_index}]")
                for part_index, part in enumerate(block.parts, 1)
            ],
            "consumed_locators": list(consumed),
        }
        if separator:
            value["separator"] = separator
        blocks.append(value)

    value = {
        "normalization": {
            "collapse_whitespace": collapse_whitespace,
            "strip": strip,
            "unicode_normalization": unicode_normalization,
        },
        "omit_epub_types": sorted(omitted_types),
        "skipped_locators": sorted(skipped),
        "reserved_locators": sorted(reserved),
        "blocks": blocks,
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extract_joined_parts(
    epub: EpubArchive,
    parts: Sequence[Fragment],
    separator: str,
    normalization: NormalizationOptions,
    omitted_types: frozenset[str],
    document_cache: dict[str, XmlElement],
    *,
    location: str,
) -> str:
    extracted: list[str] = []
    for part in parts:
        text = extract_fragment(
            epub,
            part,
            cache=document_cache,
            normalization=normalization,
            omit_epub_types=omitted_types,
        )
        if part.start is None and part.end is None:
            text = normalize_text(text, normalization)
        extracted.append(text)
    text = normalize_text(separator.join(extracted), normalization)
    if not text:
        raise EpubBlocksError(f"{location}: extraction produced no text")
    return text


def _prepare_recipe(
    epub_path: StrPath,
    recipe: Mapping[str, object],
    base_dir: StrPath | None,
    recipe_location: str,
    *,
    verify_digest: bool,
) -> tuple[Path, _RecipeSpec, list[BlockReference]]:
    path = Path(epub_path)
    spec = _parse_recipe(recipe, recipe_location, require_compiled_hash=verify_digest)
    references = _read_references(spec.references, base_dir)
    reference_types = {reference.block_type for reference in references}
    unknown_type_rules = sorted(set(spec.mapping.type_rules) - reference_types)
    if unknown_type_rules:
        names = ", ".join(repr(name) for name in unknown_type_rules)
        raise EpubBlocksError(
            f"{recipe_location}.mapping.type_rules: unknown reference type(s): {names}"
        )
    return path, spec, references


def _compile_from_epub(
    path: Path,
    spec: _RecipeSpec,
    references: Sequence[BlockReference],
    epub: EpubArchive,
    package: EpubPackage,
    document_cache: dict[str, XmlElement],
    *,
    verify_digest: bool,
) -> CompiledRecipe:
    if spec.identifier not in package.identifiers:
        raise EpubBlocksError(
            f"{path}: expected package identifier {spec.identifier!r} not found"
        )
    source = spec.source_blocks
    candidates = _extract_blocks_from_epub(
        epub,
        package,
        path,
        include_documents=source.include_documents,
        exclude_documents=source.exclude_documents,
        exclude_classes=source.exclude_classes,
        include_locators=source.include_locators,
        exclude_locators=source.exclude_locators,
        include_non_linear=source.include_non_linear,
        omit_epub_types=spec.omitted_types,
        normalization=spec.normalization,
        document_cache=document_cache,
    )
    for block_id, override in spec.mapping.overrides.items():
        _extract_joined_parts(
            epub,
            override.parts,
            override.separator,
            spec.normalization,
            spec.omitted_types,
            document_cache,
            location=f"mapping override {block_id!r}",
        )
    blocks, skipped, reserved = _compile_blocks(references, candidates, spec)
    compiled = CompiledRecipe(
        spec.identifier,
        spec.sha256,
        spec.normalization,
        spec.omitted_types,
        blocks,
        skipped,
        reserved,
    )
    actual_digest = compiled_recipe_digest(compiled)
    if verify_digest and spec.mapping.compiled_sha256 != actual_digest:
        raise EpubBlocksError(
            "mapping compiled SHA-256 does not match: "
            f"expected {spec.mapping.compiled_sha256!r}, found {actual_digest!r}"
        )
    return compiled


def compile_recipe(
    epub_path: StrPath,
    recipe: Mapping[str, object],
    *,
    base_dir: StrPath | None = None,
    recipe_location: str = "recipe",
    verify_digest: bool = True,
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> CompiledRecipe:
    """Compile a strictly validated version 1 recipe into an extraction plan."""

    path, spec, references = _prepare_recipe(
        epub_path,
        recipe,
        base_dir,
        recipe_location,
        verify_digest=verify_digest,
    )
    with path.open("rb") as source:
        if _stream_sha256(source) != spec.sha256:
            raise EpubBlocksError(f"{path}: source SHA-256 does not match recipe")
        source.seek(0)
        try:
            zip_file = ZipFile(source)
        except BadZipFile as error:
            raise EpubBlocksError(f"{path}: not a valid ZIP container") from error
        with zip_file:
            epub = EpubArchive(zip_file, limits)
            package = read_epub_package(epub)
            return _compile_from_epub(
                path,
                spec,
                references,
                epub,
                package,
                {},
                verify_digest=verify_digest,
            )


def compile_recipe_file(
    epub_path: StrPath,
    recipe_path: StrPath,
    *,
    verify_digest: bool = True,
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> CompiledRecipe:
    """Load and compile a JSON version 1 recipe."""

    recipe_file = Path(recipe_path)
    return compile_recipe(
        epub_path,
        load_recipe(recipe_file),
        base_dir=recipe_file.parent,
        recipe_location=str(recipe_file),
        verify_digest=verify_digest,
        limits=limits,
    )


def _extract_compiled_from_epub(
    epub: EpubArchive,
    compiled: CompiledRecipe,
    document_cache: dict[str, XmlElement],
    *,
    recipe_location: str,
) -> list[ExtractedBlock]:
    result: list[ExtractedBlock] = []
    for block_index, block in enumerate(compiled.blocks, 1):
        location = f"{recipe_location}: compiled block {block_index}"
        text = _extract_joined_parts(
            epub,
            block.parts,
            block.separator,
            compiled.normalization,
            compiled.omit_epub_types,
            document_cache,
            location=location,
        )
        result.append(ExtractedBlock(block.block_id, block.block_type, text))
    return result


def extract_recipe(
    epub_path: StrPath,
    recipe: Mapping[str, object],
    *,
    base_dir: StrPath | None = None,
    recipe_location: str = "recipe",
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> list[ExtractedBlock]:
    """Compile and apply a strictly validated version 1 extraction recipe."""

    path, spec, references = _prepare_recipe(
        epub_path,
        recipe,
        base_dir,
        recipe_location,
        verify_digest=True,
    )
    with path.open("rb") as source:
        if _stream_sha256(source) != spec.sha256:
            raise EpubBlocksError(f"{path}: source SHA-256 does not match recipe")
        source.seek(0)
        try:
            zip_file = ZipFile(source)
        except BadZipFile as error:
            raise EpubBlocksError(f"{path}: not a valid ZIP container") from error
        with zip_file:
            epub = EpubArchive(zip_file, limits)
            package = read_epub_package(epub)
            cache: dict[str, XmlElement] = {}
            compiled = _compile_from_epub(
                path,
                spec,
                references,
                epub,
                package,
                cache,
                verify_digest=True,
            )
            return _extract_compiled_from_epub(
                epub,
                compiled,
                cache,
                recipe_location=recipe_location,
            )


def extract_recipe_file(
    epub_path: StrPath,
    recipe_path: StrPath,
    *,
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> list[ExtractedBlock]:
    """Load, compile, and apply a JSON version 1 extraction recipe."""

    recipe_file = Path(recipe_path)
    return extract_recipe(
        epub_path,
        load_recipe(recipe_file),
        base_dir=recipe_file.parent,
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
