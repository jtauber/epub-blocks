from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from string import Formatter
from typing import BinaryIO, cast
from zipfile import BadZipFile, ZipFile

from ._content import (
    RichText,
    boolean,
    content_value,
    element_rules,
    join_rich_text,
    milestones,
    parse_text_options,
    plain_text,
    render_rich_text,
    validate_content,
)
from .errors import EpubBlocksError
from .extract import _extract_blocks_from_epub  # pyright: ignore[reportPrivateUsage]
from .models import (
    DEFAULT_CONTENT,
    CompiledBlock,
    CompiledRecipe,
    ContentOptions,
    ElementRule,
    EpubPackage,
    ExtractedBlock,
    Fragment,
    NormalizationOptions,
    SpineDocument,
    TextBlock,
    UnicodeNormalization,
)
from .package import matches, read_epub_package, select_spine_documents
from .safety import DEFAULT_SAFETY_LIMITS, EpubArchive, SafetyLimits
from .xhtml import extract_fragment, extract_rich_fragment, normalize_text
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
_ROLES = frozenset({"block", "line-start", "line", "fixed"})


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
    element_rules: tuple[ElementRule, ...]
    strict_coverage: bool


@dataclass(frozen=True, slots=True)
class _GroupSpec:
    source_pattern: str | None
    source_marker: _SourceMarker | None
    capture_kind: str
    source_offset: int
    source_map: Mapping[str, str]
    capture_width: int | None
    transitions: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _SourceMarker:
    pattern: str
    case_insensitive: bool


@dataclass(frozen=True, slots=True)
class _MatchSpec:
    tag: str | None
    classes: frozenset[str] | None
    classes_any: frozenset[str]
    classes_all: frozenset[str]
    locators: tuple[str, ...]
    text_pattern: str | None
    case_insensitive: bool


@dataclass(frozen=True, slots=True)
class _EmissionSpec:
    block_type: str
    role: str
    block_id: str | None
    allow_empty: bool = False


@dataclass(frozen=True, slots=True)
class _BlockRule:
    match: _MatchSpec
    emission: _EmissionSpec
    consume: int
    emit: tuple[int, ...] | None
    separator: str | None
    remove_prefix: _RemovePrefix | None


@dataclass(frozen=True, slots=True)
class _RemovePrefix:
    pattern: str
    case_insensitive: bool


@dataclass(frozen=True, slots=True)
class _ProducedBlockSpec:
    emission: _EmissionSpec
    parts: tuple[Fragment, ...]
    separator: str


@dataclass(frozen=True, slots=True)
class _ReplacementSpec:
    anchor: str
    outputs: tuple[_ProducedBlockSpec, ...]


@dataclass(frozen=True, slots=True)
class _InsertionSpec:
    after: str
    outputs: tuple[_ProducedBlockSpec, ...]


@dataclass(frozen=True, slots=True)
class _IdentifierSpec:
    block_template: str
    block_start: int
    line_template: str | None
    line_start: int


@dataclass(frozen=True, slots=True)
class _OutputSpec:
    groups: _GroupSpec
    identifiers: _IdentifierSpec
    default: _EmissionSpec
    rules: tuple[_BlockRule, ...]
    skip_source: tuple[str, ...]
    replacements: tuple[_ReplacementSpec, ...]
    insertions: tuple[_InsertionSpec, ...]
    compiled_sha256: str | None


@dataclass(frozen=True, slots=True)
class _RecipeSpec:
    identifier: str
    sha256: str
    normalization: NormalizationOptions
    omitted_types: frozenset[str]
    source_blocks: _SourceBlocksSpec
    output: _OutputSpec
    content: ContentOptions = DEFAULT_CONTENT


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
                "element_rules",
                "strict_coverage",
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
        element_rules(source.get("element_rules", []), location + ".element_rules"),
        boolean(source.get("strict_coverage", False), location + ".strict_coverage"),
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
                "source_pattern",
                "source_marker",
                "capture_kind",
                "source_offset",
                "source_map",
                "capture_width",
                "transitions",
            }
        ),
        location,
    )
    has_source_pattern = "source_pattern" in groups
    has_source_marker = "source_marker" in groups
    has_transitions = "transitions" in groups
    if sum((has_source_pattern, has_source_marker, has_transitions)) > 1:
        raise EpubBlocksError(
            f"{location}: source_pattern, source_marker, and transitions are "
            "mutually exclusive"
        )
    source_pattern = (
        _group_pattern(groups["source_pattern"], f"{location}.source_pattern")
        if has_source_pattern
        else None
    )
    capture_kind = groups.get("capture_kind", "string")
    if not isinstance(capture_kind, str) or capture_kind not in {
        "string",
        "decimal",
        "roman",
    }:
        raise EpubBlocksError(
            f"{location}.capture_kind: must be 'string', 'decimal', or 'roman'"
        )
    if "capture_kind" in groups and not (has_source_pattern or has_source_marker):
        raise EpubBlocksError(
            f"{location}.capture_kind: requires source_pattern or source_marker"
        )

    source_marker: _SourceMarker | None = None
    if has_source_marker:
        marker_location = f"{location}.source_marker"
        marker = _mapping(groups["source_marker"], marker_location)
        _check_members(
            marker,
            frozenset({"pattern", "case_insensitive"}),
            marker_location,
        )
        marker_pattern = _group_pattern(
            marker.get("pattern"), f"{marker_location}.pattern"
        )
        if not marker_pattern.startswith("^"):
            raise EpubBlocksError(f"{marker_location}.pattern: must be anchored with ^")
        case_insensitive = marker.get("case_insensitive", False)
        if not isinstance(case_insensitive, bool):
            raise EpubBlocksError(
                f"{marker_location}.case_insensitive: must be a boolean"
            )
        source_marker = _SourceMarker(marker_pattern, case_insensitive)

    source_offset = _integer(
        groups.get("source_offset", 0), f"{location}.source_offset"
    )
    source_map = _string_map(groups.get("source_map", {}), f"{location}.source_map")
    if "source_map" in groups and not source_map:
        raise EpubBlocksError(f"{location}.source_map: must not be empty")
    if ("source_map" in groups or "source_offset" in groups) and not (
        has_source_pattern or has_source_marker
    ):
        raise EpubBlocksError(
            f"{location}: source_map and source_offset require a source pattern"
        )
    if source_map and source_offset:
        raise EpubBlocksError(
            f"{location}: source_offset cannot be combined with source_map"
        )
    capture_width: int | None = None
    if "capture_width" in groups:
        if not (has_source_pattern or has_source_marker):
            raise EpubBlocksError(
                f"{location}.capture_width: requires a source pattern"
            )
        capture_width = _integer(
            groups["capture_width"], f"{location}.capture_width", minimum=1
        )
        if source_map:
            raise EpubBlocksError(
                f"{location}: capture_width cannot be combined with source_map"
            )
    transitions = _string_map(groups.get("transitions", {}), f"{location}.transitions")
    if has_transitions and not transitions:
        raise EpubBlocksError(f"{location}.transitions: must not be empty")
    for locator in transitions:
        _validate_source_locator(locator, f"{location}.transitions[{locator!r}]")
    return _GroupSpec(
        source_pattern,
        source_marker,
        capture_kind,
        source_offset,
        source_map,
        capture_width,
        transitions,
    )


def _validate_source_locator(value: str, location: str) -> None:
    if _SOURCE_LOCATOR.fullmatch(value) is None:
        raise EpubBlocksError(
            f"{location}: must contain a document and one-based element path "
            "separated by #"
        )


def _emission_spec(value: Mapping[str, object], location: str) -> _EmissionSpec:
    block_type = _nonempty_string(value.get("type"), f"{location}.type")
    role = value.get("role", "block")
    if not isinstance(role, str) or role not in _ROLES:
        raise EpubBlocksError(
            f"{location}.role: must be 'block', 'line-start', 'line', or 'fixed'"
        )
    block_id = (
        _nonempty_string(value["id"], f"{location}.id") if "id" in value else None
    )
    if role == "fixed" and block_id is None:
        raise EpubBlocksError(f"{location}.id: required when role is 'fixed'")
    if role != "fixed" and block_id is not None:
        raise EpubBlocksError(f"{location}.id: only allowed when role is 'fixed'")
    if block_id is not None:
        _validate_template(
            block_id,
            f"{location}.id",
            allowed_fields=frozenset({"group"}),
            required_fields=frozenset(),
        )
    return _EmissionSpec(
        block_type,
        role,
        block_id,
        boolean(value.get("allow_empty", False), location + ".allow_empty"),
    )


def _match_spec(value: object, location: str) -> _MatchSpec:
    spec = _mapping(value, location)
    _check_members(
        spec,
        frozenset(
            {
                "tag",
                "classes",
                "classes_any",
                "classes_all",
                "locators",
                "text_pattern",
                "case_insensitive",
            }
        ),
        location,
    )
    tag = _nonempty_string(spec["tag"], f"{location}.tag") if "tag" in spec else None
    classes = (
        frozenset(_string_array(spec["classes"], f"{location}.classes"))
        if "classes" in spec
        else None
    )
    classes_any = frozenset(
        _string_array(spec.get("classes_any", []), f"{location}.classes_any")
    )
    classes_all = frozenset(
        _string_array(spec.get("classes_all", []), f"{location}.classes_all")
    )
    locators = _string_array(spec.get("locators", []), f"{location}.locators")
    text_pattern = (
        _nonempty_string(spec["text_pattern"], f"{location}.text_pattern")
        if "text_pattern" in spec
        else None
    )
    if text_pattern is not None:
        try:
            re.compile(text_pattern)
        except re.error as error:
            raise EpubBlocksError(
                f"{location}.text_pattern: invalid regular expression: {error}"
            ) from error
    case_insensitive = spec.get("case_insensitive", False)
    if not isinstance(case_insensitive, bool):
        raise EpubBlocksError(f"{location}.case_insensitive: must be a boolean")
    if not any(
        (
            tag is not None,
            classes is not None,
            classes_any,
            classes_all,
            locators,
            text_pattern is not None,
        )
    ):
        raise EpubBlocksError(f"{location}: must contain at least one criterion")
    return _MatchSpec(
        tag,
        classes,
        classes_any,
        classes_all,
        locators,
        text_pattern,
        case_insensitive,
    )


def _block_rule(value: object, location: str) -> _BlockRule:
    rule = _mapping(value, location)
    _check_members(
        rule,
        frozenset(
            {
                "match",
                "type",
                "role",
                "id",
                "consume",
                "emit",
                "separator",
                "remove_prefix",
                "allow_empty",
            }
        ),
        location,
    )
    emission = _emission_spec(rule, location)
    match = _match_spec(rule.get("match"), f"{location}.match")
    consume = _integer(rule.get("consume", 1), f"{location}.consume", minimum=1)
    emit = _integer_array(rule["emit"], f"{location}.emit") if "emit" in rule else None
    if emit is not None and max(emit) > consume:
        raise EpubBlocksError(
            f"{location}.emit: values must not exceed consume ({consume})"
        )
    separator_value: str | None = None
    if "separator" in rule:
        raw_separator = rule["separator"]
        if not isinstance(raw_separator, str):
            raise EpubBlocksError(f"{location}.separator: must be a string")
        separator_value = raw_separator
    remove_prefix = (
        _remove_prefix(rule["remove_prefix"], f"{location}.remove_prefix")
        if "remove_prefix" in rule
        else None
    )
    emitted_count = len(emit) if emit is not None else consume
    if remove_prefix is not None and emitted_count != 1:
        raise EpubBlocksError(
            f"{location}.remove_prefix: requires exactly one emitted part"
        )
    return _BlockRule(
        match,
        emission,
        consume,
        emit,
        separator_value,
        remove_prefix,
    )


def _validate_template(
    template: str,
    location: str,
    *,
    allowed_fields: frozenset[str],
    required_fields: frozenset[str],
) -> None:
    fields: set[str] = set()
    try:
        parsed = tuple(Formatter().parse(template))
    except ValueError as error:
        raise EpubBlocksError(f"{location}: invalid template: {error}") from error
    for _literal, field, format_spec, conversion in parsed:
        if field is None:
            continue
        if field not in allowed_fields:
            raise EpubBlocksError(f"{location}: unsupported field {field!r}")
        if conversion is not None:
            raise EpubBlocksError(f"{location}: conversions are not supported")
        if format_spec is not None and ("{" in format_spec or "}" in format_spec):
            raise EpubBlocksError(f"{location}: nested fields are not supported")
        fields.add(field)
    missing = sorted(required_fields - fields)
    if missing:
        names = ", ".join(repr(name) for name in missing)
        raise EpubBlocksError(f"{location}: missing required field(s): {names}")


def _identifiers_spec(value: object, location: str) -> _IdentifierSpec:
    identifiers = _mapping(value, location)
    _check_members(identifiers, frozenset({"block", "line"}), location)
    block = _mapping(identifiers.get("block"), f"{location}.block")
    _check_members(block, frozenset({"template", "start"}), f"{location}.block")
    block_template = _nonempty_string(
        block.get("template"), f"{location}.block.template"
    )
    _validate_template(
        block_template,
        f"{location}.block.template",
        allowed_fields=frozenset({"group", "number"}),
        required_fields=frozenset({"number"}),
    )
    block_start = _integer(block.get("start", 1), f"{location}.block.start", minimum=0)
    line_template: str | None = None
    line_start = 1
    if "line" in identifiers:
        line = _mapping(identifiers["line"], f"{location}.line")
        _check_members(line, frozenset({"template", "start"}), f"{location}.line")
        line_template = _nonempty_string(
            line.get("template"), f"{location}.line.template"
        )
        _validate_template(
            line_template,
            f"{location}.line.template",
            allowed_fields=frozenset({"group", "block", "number"}),
            required_fields=frozenset({"block", "number"}),
        )
        line_start = _integer(line.get("start", 1), f"{location}.line.start", minimum=0)
    return _IdentifierSpec(block_template, block_start, line_template, line_start)


def _produced_block(value: object, location: str) -> _ProducedBlockSpec:
    output = _mapping(value, location)
    _check_members(
        output,
        frozenset({"type", "role", "id", "parts", "separator", "allow_empty"}),
        location,
    )
    emission = _emission_spec(output, location)
    parts_value = output.get("parts")
    if not isinstance(parts_value, list) or not parts_value:
        raise EpubBlocksError(f"{location}.parts: must be a non-empty array")
    parts = tuple(
        _fragment(part, f"{location}.parts[{index}]")
        for index, part in enumerate(cast(list[object], parts_value), 1)
    )
    separator = output.get("separator", "")
    if not isinstance(separator, str):
        raise EpubBlocksError(f"{location}.separator: must be a string")
    return _ProducedBlockSpec(emission, parts, separator)


def _produced_blocks(value: object, location: str) -> tuple[_ProducedBlockSpec, ...]:
    if not isinstance(value, list) or not value:
        raise EpubBlocksError(f"{location}: must be a non-empty array")
    return tuple(
        _produced_block(item, f"{location}[{index}]")
        for index, item in enumerate(cast(list[object], value), 1)
    )


def _output_spec(
    value: object, location: str, *, require_compiled_hash: bool
) -> _OutputSpec:
    output = _mapping(value, location)
    _check_members(
        output,
        frozenset(
            {
                "groups",
                "identifiers",
                "default",
                "rules",
                "skip_source",
                "replacements",
                "insertions",
                "compiled_sha256",
            }
        ),
        location,
    )
    groups = _group_spec(output.get("groups", {}), f"{location}.groups")
    identifiers = _identifiers_spec(
        output.get("identifiers"), f"{location}.identifiers"
    )
    default_value = _mapping(output.get("default"), f"{location}.default")
    _check_members(
        default_value,
        frozenset({"type", "role", "id", "allow_empty"}),
        f"{location}.default",
    )
    default = _emission_spec(default_value, f"{location}.default")
    rules_value = output.get("rules", [])
    if not isinstance(rules_value, list):
        raise EpubBlocksError(f"{location}.rules: must be an array")
    rules = tuple(
        _block_rule(item, f"{location}.rules[{index}]")
        for index, item in enumerate(cast(list[object], rules_value), 1)
    )
    skip_source = _string_array(
        output.get("skip_source", []), f"{location}.skip_source"
    )
    for index, locator in enumerate(skip_source, 1):
        _validate_source_locator(locator, f"{location}.skip_source[{index}]")

    replacements_value = output.get("replacements", [])
    if not isinstance(replacements_value, list):
        raise EpubBlocksError(f"{location}.replacements: must be an array")
    replacements: list[_ReplacementSpec] = []
    replacement_anchors: set[str] = set()
    for index, item in enumerate(cast(list[object], replacements_value), 1):
        item_location = f"{location}.replacements[{index}]"
        replacement = _mapping(item, item_location)
        _check_members(replacement, frozenset({"anchor", "outputs"}), item_location)
        anchor = _nonempty_string(replacement.get("anchor"), f"{item_location}.anchor")
        _validate_source_locator(anchor, f"{item_location}.anchor")
        if anchor in replacement_anchors:
            raise EpubBlocksError(f"{item_location}.anchor: duplicate anchor")
        replacement_anchors.add(anchor)
        replacements.append(
            _ReplacementSpec(
                anchor,
                _produced_blocks(
                    replacement.get("outputs"), f"{item_location}.outputs"
                ),
            )
        )
        if anchor not in {
            _fragment_locator(part)
            for produced in replacements[-1].outputs
            for part in produced.parts
        }:
            raise EpubBlocksError(
                f"{item_location}.anchor: must also occur in an output part"
            )

    insertions_value = output.get("insertions", [])
    if not isinstance(insertions_value, list):
        raise EpubBlocksError(f"{location}.insertions: must be an array")
    insertions: list[_InsertionSpec] = []
    insertion_anchors: set[str] = set()
    for index, item in enumerate(cast(list[object], insertions_value), 1):
        item_location = f"{location}.insertions[{index}]"
        insertion = _mapping(item, item_location)
        _check_members(insertion, frozenset({"after", "outputs"}), item_location)
        after = _nonempty_string(insertion.get("after"), f"{item_location}.after")
        _validate_source_locator(after, f"{item_location}.after")
        if after in insertion_anchors:
            raise EpubBlocksError(f"{item_location}.after: duplicate anchor")
        insertion_anchors.add(after)
        insertions.append(
            _InsertionSpec(
                after,
                _produced_blocks(insertion.get("outputs"), f"{item_location}.outputs"),
            )
        )

    emissions = (
        default,
        *(rule.emission for rule in rules),
        *(
            produced.emission
            for replacement in replacements
            for produced in replacement.outputs
        ),
        *(
            produced.emission
            for insertion in insertions
            for produced in insertion.outputs
        ),
    )
    if any(item.role in {"line-start", "line"} for item in emissions) and (
        identifiers.line_template is None
    ):
        raise EpubBlocksError(f"{location}.identifiers.line: required by a line role")

    _validate_fragment_reuse(
        {
            f"replacement {index} output {output_index}": produced.parts
            for index, replacement in enumerate(replacements, 1)
            for output_index, produced in enumerate(replacement.outputs, 1)
        },
        f"{location}.replacements",
    )

    compiled_sha256: str | None = None
    if require_compiled_hash or "compiled_sha256" in output:
        compiled_sha256 = _sha256(
            output.get("compiled_sha256"), f"{location}.compiled_sha256"
        )
    return _OutputSpec(
        groups,
        identifiers,
        default,
        rules,
        skip_source,
        tuple(replacements),
        tuple(insertions),
        compiled_sha256,
    )


def _validate_fragment_reuse(
    outputs: Mapping[str, tuple[Fragment, ...]], location: str
) -> None:
    uses: dict[tuple[str, str], list[tuple[str, int, Fragment]]] = defaultdict(list)
    for label, parts in outputs.items():
        for part_index, part in enumerate(parts, 1):
            uses[(part.document_path, part.element_path)].append(
                (label, part_index, part)
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
        if len({frozenset(part.omit_paths) for _, _, part in entries}) != 1:
            raise EpubBlocksError(
                f"{location}: source fragment {locator!r} is reused with different "
                "omissions; slice offsets must use the same omitted paths"
            )

        sliced = sorted(
            (
                cast(int, part.start),
                cast(int, part.end),
                label,
                part_index,
            )
            for label, part_index, part in entries
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
                "normalization",
                "omit_epub_types",
                "source_blocks",
                "output",
                "text",
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
    normalization = _normalization(
        recipe.get("normalization", {}), f"{location}.normalization"
    )
    omitted_types = frozenset(
        _string_array(recipe.get("omit_epub_types", []), f"{location}.omit_epub_types")
    )
    source_blocks = _source_blocks_spec(
        recipe.get("source_blocks"), f"{location}.source_blocks"
    )
    output = _output_spec(
        recipe.get("output"),
        f"{location}.output",
        require_compiled_hash=require_compiled_hash,
    )
    content = replace(
        parse_text_options(recipe.get("text", {}), location + ".text"),
        element_rules=source_blocks.element_rules,
        strict_coverage=source_blocks.strict_coverage,
    )
    if content.markup is not None:
        for rule in content.markup.rules:
            if set(rule.match.epub_types) & omitted_types:
                raise EpubBlocksError(
                    f"{location}: retained markup is also semantically omitted"
                )
    return _RecipeSpec(
        identifier,
        source_sha256,
        normalization,
        omitted_types,
        source_blocks,
        output,
        content,
    )


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


def _captured_group(value: str, spec: _GroupSpec, location: str) -> str:
    if spec.source_map:
        if value not in spec.source_map:
            raise EpubBlocksError(
                f"{location}: source group capture {value!r} has no mapping"
            )
        return spec.source_map[value]
    group = _capture_key(value, spec.capture_kind, location)
    if spec.source_offset:
        try:
            number = int(group) + spec.source_offset
        except ValueError as error:
            raise EpubBlocksError(
                f"{location}: source_offset requires a numeric capture"
            ) from error
        if number < 1:
            raise EpubBlocksError(f"{location}: maps to invalid group {number}")
        group = str(number)
    if spec.capture_width is not None:
        try:
            number = int(group)
        except ValueError as error:
            raise EpubBlocksError(
                f"{location}: capture_width requires a numeric group"
            ) from error
        group = f"{number:0{spec.capture_width}d}"
    return group


def _source_groups(
    records: Sequence[TextBlock], spec: _GroupSpec
) -> tuple[str | None, ...]:
    if (
        spec.source_pattern is None
        and spec.source_marker is None
        and not spec.transitions
    ):
        return tuple("" for _record in records)

    candidate_locators = Counter(record.locator for record in records)
    missing_transitions = sorted(
        locator for locator in spec.transitions if candidate_locators[locator] == 0
    )
    if missing_transitions:
        names = ", ".join(repr(locator) for locator in missing_transitions)
        raise EpubBlocksError(
            f"output.groups.transitions: locator(s) are not selected source "
            f"candidates: {names}"
        )
    ambiguous_transitions = sorted(
        locator for locator in spec.transitions if candidate_locators[locator] > 1
    )
    if ambiguous_transitions:
        names = ", ".join(repr(locator) for locator in ambiguous_transitions)
        raise EpubBlocksError(
            f"output.groups.transitions: locator(s) are ambiguous: {names}"
        )

    if spec.source_pattern is not None:
        expression = re.compile(spec.source_pattern, re.IGNORECASE)
        result: list[str] = []
        for record in records:
            match = expression.search(record.source_locator)
            if match is None:
                raise EpubBlocksError(
                    "source block does not match output group pattern: "
                    f"{record.source_locator!r}"
                )
            captured = match.group(1)
            if captured is None:
                raise EpubBlocksError(
                    "source group capture did not participate for "
                    f"{record.source_locator!r}"
                )
            result.append(_captured_group(captured, spec, record.source_locator))
        return tuple(result)

    marker_expression: re.Pattern[str] | None = None
    if spec.source_marker is not None:
        flags = re.IGNORECASE if spec.source_marker.case_insensitive else 0
        marker_expression = re.compile(spec.source_marker.pattern, flags)

    result_optional: list[str | None] = []
    current: str | None = None
    seen_markers: set[str] = set()
    for record in records:
        transition = spec.transitions.get(record.locator)
        if transition is not None:
            current = transition
        if marker_expression is not None:
            match = marker_expression.match(record.text)
            if match is not None:
                captured = match.group(1)
                if captured is None:
                    raise EpubBlocksError(
                        f"source-marker capture did not participate for "
                        f"{record.source_locator!r}"
                    )
                current = _captured_group(captured, spec, record.source_locator)
                if current in seen_markers:
                    raise EpubBlocksError(
                        f"source marker repeats group {current!r}: "
                        f"{record.source_locator!r}"
                    )
                seen_markers.add(current)
        result_optional.append(current)
    return tuple(result_optional)


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


def _rule_matches(block: TextBlock, spec: _MatchSpec) -> bool:
    if spec.tag is not None and block.tag.casefold() != spec.tag.casefold():
        return False
    if spec.classes is not None and block.classes != spec.classes:
        return False
    if spec.classes_any and block.classes.isdisjoint(spec.classes_any):
        return False
    if spec.classes_all and not spec.classes_all.issubset(block.classes):
        return False
    if spec.locators and not matches(block.locator, spec.locators):
        return False
    if spec.text_pattern is not None:
        flags = re.IGNORECASE if spec.case_insensitive else 0
        if re.search(spec.text_pattern, block.text, flags) is None:
            return False
    return True


class _IdentifierAllocator:
    def __init__(self, spec: _IdentifierSpec) -> None:
        self.spec = spec
        self.next_blocks: dict[str, int] = defaultdict(lambda: spec.block_start)
        self.line_blocks: dict[str, int] = {}
        self.next_lines: dict[str, int] = {}
        self.seen: set[str] = set()

    @staticmethod
    def _render(template: str, values: Mapping[str, object], location: str) -> str:
        try:
            result = template.format_map(values)
        except (KeyError, ValueError, OverflowError) as error:
            raise EpubBlocksError(
                f"{location}: identifier template could not be rendered: {error}"
            ) from error
        if not result:
            raise EpubBlocksError(f"{location}: identifier template rendered empty")
        return result

    def allocate(
        self,
        emission: _EmissionSpec,
        group: str,
        location: str,
        *,
        preserve_line_state: bool = False,
    ) -> str:
        if emission.role == "fixed":
            if emission.block_id is None:
                raise AssertionError("fixed emission validated with an identifier")
            block_id = self._render(emission.block_id, {"group": group}, location)
            if not preserve_line_state:
                self.line_blocks.pop(group, None)
                self.next_lines.pop(group, None)
        elif emission.role == "block":
            number = self.next_blocks[group]
            self.next_blocks[group] += 1
            block_id = self._render(
                self.spec.block_template,
                {"group": group, "number": number},
                location,
            )
            self.line_blocks.pop(group, None)
            self.next_lines.pop(group, None)
        elif emission.role == "line-start":
            if self.spec.line_template is None:
                raise AssertionError("line template required during parsing")
            block = self.next_blocks[group]
            self.next_blocks[group] += 1
            number = self.spec.line_start
            self.line_blocks[group] = block
            self.next_lines[group] = number + 1
            block_id = self._render(
                self.spec.line_template,
                {"group": group, "block": block, "number": number},
                location,
            )
        else:
            if self.spec.line_template is None:
                raise AssertionError("line template required during parsing")
            if group not in self.line_blocks:
                raise EpubBlocksError(
                    f"{location}: line role has no preceding line-start in group "
                    f"{group!r}"
                )
            block = self.line_blocks[group]
            number = self.next_lines[group]
            self.next_lines[group] += 1
            block_id = self._render(
                self.spec.line_template,
                {"group": group, "block": block, "number": number},
                location,
            )
        if block_id in self.seen:
            raise EpubBlocksError(
                f"{location}: duplicate generated identifier {block_id!r}"
            )
        self.seen.add(block_id)
        return block_id


def _compiled_produced_block(
    produced: _ProducedBlockSpec,
    allocator: _IdentifierAllocator,
    group: str,
    location: str,
    *,
    consumed_locators: tuple[str, ...] = (),
    preserve_line_state: bool = False,
) -> CompiledBlock:
    block_id = allocator.allocate(
        produced.emission, group, location, preserve_line_state=preserve_line_state
    )
    return CompiledBlock(
        block_id,
        produced.emission.block_type,
        produced.parts,
        produced.separator,
        consumed_locators,
        produced.emission.allow_empty,
    )


def _compile_blocks(
    candidates: Sequence[TextBlock],
    spec: _RecipeSpec,
) -> tuple[tuple[CompiledBlock, ...], tuple[str, ...], tuple[str, ...]]:
    output = spec.output
    replacements = {
        replacement.anchor: replacement for replacement in output.replacements
    }
    insertions = {insertion.after: insertion for insertion in output.insertions}
    reserved = {
        _fragment_locator(part)
        for replacement in output.replacements
        for produced in replacement.outputs
        for part in produced.parts
    }
    skipped = set(output.skip_source)
    collisions = sorted(skipped & reserved)
    if collisions:
        names = ", ".join(repr(locator) for locator in collisions)
        raise EpubBlocksError(
            "output source locators are both skipped and reserved: " + names
        )
    candidate_locator_counts = Counter(block.locator for block in candidates)
    missing_skips = sorted(
        locator for locator in skipped if candidate_locator_counts[locator] == 0
    )
    if missing_skips:
        names = ", ".join(repr(locator) for locator in missing_skips)
        raise EpubBlocksError(
            "output.skip_source locator(s) are not selected source candidates: " + names
        )
    ambiguous_skips = sorted(
        locator for locator in skipped if candidate_locator_counts[locator] > 1
    )
    if ambiguous_skips:
        names = ", ".join(repr(locator) for locator in ambiguous_skips)
        raise EpubBlocksError(
            "output.skip_source locator(s) do not identify exactly one selected "
            f"source candidate: {names}"
        )
    anchors = set(replacements) | set(insertions)
    missing_anchors = sorted(
        locator for locator in anchors if candidate_locator_counts[locator] == 0
    )
    if missing_anchors:
        names = ", ".join(repr(locator) for locator in missing_anchors)
        raise EpubBlocksError(
            f"output anchor locator(s) are not selected source candidates: {names}"
        )
    ambiguous_anchors = sorted(
        locator for locator in anchors if candidate_locator_counts[locator] > 1
    )
    if ambiguous_anchors:
        names = ", ".join(repr(locator) for locator in ambiguous_anchors)
        raise EpubBlocksError(f"output anchor locator(s) are ambiguous: {names}")
    anchor_skip_collisions = sorted(anchors & skipped)
    if anchor_skip_collisions:
        names = ", ".join(repr(locator) for locator in anchor_skip_collisions)
        raise EpubBlocksError(f"output anchor locator(s) are also skipped: {names}")
    missing_reserved = sorted(
        locator for locator in reserved if candidate_locator_counts[locator] == 0
    )
    if missing_reserved:
        names = ", ".join(repr(locator) for locator in missing_reserved)
        raise EpubBlocksError(
            f"output replacement locator(s) are not selected source candidates: {names}"
        )
    ambiguous_reserved = sorted(
        locator for locator in reserved if candidate_locator_counts[locator] > 1
    )
    if ambiguous_reserved:
        names = ", ".join(repr(locator) for locator in ambiguous_reserved)
        raise EpubBlocksError(
            "output replacement locator(s) identify more than one selected source "
            f"candidate: {names}"
        )
    groups = _source_groups(candidates, output.groups)
    allocator = _IdentifierAllocator(output.identifiers)
    compiled: list[CompiledBlock] = []
    handled_insertions: set[str] = set()

    def require_group(index: int, location: str) -> str:
        group = groups[index]
        if group is None:
            raise EpubBlocksError(
                f"{location}: source block occurs before the first group marker or "
                "transition"
            )
        return group

    def add_insertions(locator: str, group: str) -> None:
        insertion = insertions.get(locator)
        if insertion is None:
            return
        for output_index, produced in enumerate(insertion.outputs, 1):
            compiled.append(
                _compiled_produced_block(
                    produced,
                    allocator,
                    group,
                    f"insertion after {locator!r}, output {output_index}",
                    preserve_line_state=True,
                )
            )
        handled_insertions.add(locator)

    cursor = 0
    while cursor < len(candidates):
        block = candidates[cursor]
        locator = block.locator
        if locator in skipped:
            cursor += 1
            continue
        replacement = replacements.get(locator)
        if replacement is not None:
            group = require_group(cursor, f"replacement at {locator!r}")
            for output_index, produced in enumerate(replacement.outputs, 1):
                compiled.append(
                    _compiled_produced_block(
                        produced,
                        allocator,
                        group,
                        f"replacement at {locator!r}, output {output_index}",
                    )
                )
            add_insertions(locator, group)
            cursor += 1
            continue
        if locator in reserved:
            cursor += 1
            continue

        rule = next(
            (
                candidate
                for candidate in output.rules
                if _rule_matches(block, candidate.match)
            ),
            None,
        )
        emission = rule.emission if rule is not None else output.default
        consume = rule.consume if rule is not None else 1
        if cursor + consume > len(candidates):
            raise EpubBlocksError(
                f"source rule at {block.source_locator!r} needs {consume} blocks, "
                f"found {len(candidates) - cursor}"
            )
        used = candidates[cursor : cursor + consume]
        used_groups = groups[cursor : cursor + consume]
        unavailable = [
            item.locator
            for item in used
            if item.locator in skipped
            or item.locator in reserved
            or item.locator in replacements
        ]
        if unavailable:
            names = ", ".join(repr(item) for item in unavailable)
            raise EpubBlocksError(
                f"source rule at {block.source_locator!r} consumes specially handled "
                f"source block(s): {names}"
            )
        group = require_group(cursor, f"source rule at {block.source_locator!r}")
        if any(candidate_group != group for candidate_group in used_groups):
            raise EpubBlocksError(
                f"source rule at {block.source_locator!r} crosses an output group"
            )
        internal_insertions = [
            item.locator for item in used[:-1] if item.locator in insertions
        ]
        if internal_insertions:
            names = ", ".join(repr(item) for item in internal_insertions)
            raise EpubBlocksError(
                f"source rule at {block.source_locator!r} crosses insertion "
                f"anchor(s): {names}"
            )
        emit = (
            rule.emit
            if rule is not None and rule.emit is not None
            else tuple(range(1, consume + 1))
        )
        emitted = [used[index - 1] for index in emit]
        if not any(item.text for item in emitted) and not emission.allow_empty:
            raise EpubBlocksError(f"{locator}: empty output requires allow_empty")
        if used[-1].locator in insertions and used[-1] not in emitted:
            raise EpubBlocksError(
                f"source rule at {block.source_locator!r} discards insertion "
                f"anchor {used[-1].locator!r}; the anchor must be emitted"
            )
        parts = tuple(_fragment_from_block(item) for item in emitted)
        if rule is not None and rule.remove_prefix is not None:
            parts = (
                _apply_prefix_rule(
                    emitted[0],
                    rule.remove_prefix,
                    location=f"source rule at {block.source_locator!r}",
                ),
            )
        separator = (
            rule.separator
            if rule is not None and rule.separator is not None
            else " "
            if len(parts) > 1
            else ""
        )
        block_id = allocator.allocate(
            emission,
            group,
            f"source rule at {block.source_locator!r}",
        )
        compiled.append(
            CompiledBlock(
                block_id,
                emission.block_type,
                parts,
                separator,
                tuple(item.locator for item in used),
                emission.allow_empty,
            )
        )
        add_insertions(used[-1].locator, group)
        cursor += consume

    missing_insertions = sorted(set(insertions) - handled_insertions)
    if missing_insertions:
        names = ", ".join(repr(locator) for locator in missing_insertions)
        raise EpubBlocksError(f"output insertion anchor(s) were not emitted: {names}")
    if not compiled:
        raise EpubBlocksError("recipe generated no output blocks")
    return tuple(compiled), tuple(sorted(skipped)), tuple(sorted(reserved))


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
        if not isinstance(cast(object, block.allow_empty), bool):
            raise EpubBlocksError(f"{location}.allow_empty: must be a boolean")
        if block.allow_empty:
            value["allow_empty"] = True
        for field, position_key, attachments, maximum in (
            ("milestones", "before_part", block.milestones, len(block.parts)),
            (
                "milestones_after",
                "after_part",
                block.milestones_after,
                len(block.parts) - 1,
            ),
        ):
            if not attachments:
                continue
            entries: list[dict[str, object]] = []
            for position, part in attachments:
                if type(position) is not int or not 0 <= position <= maximum:
                    raise EpubBlocksError(
                        f"{location}.{field}: invalid milestone attachment"
                    )
                entries.append(
                    {
                        position_key: position,
                        "source": _fragment_digest_value(part, location + ".milestone"),
                    }
                )
            value[field] = entries
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
    policy = content_value(validate_content(compiled.content))
    if policy:
        value["content"] = policy
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
    content: ContentOptions = DEFAULT_CONTENT,
    allow_empty: bool = False,
    attachments: tuple[tuple[int, Fragment], ...] = (),
    attachments_after: tuple[tuple[int, Fragment], ...] = (),
) -> str:
    if content != ContentOptions():

        def events_at(
            events: tuple[tuple[int, Fragment], ...], position: int
        ) -> tuple[str | RichText, ...]:
            return tuple(
                child
                for index, event in events
                if index == position
                for child in extract_rich_fragment(
                    epub,
                    event,
                    cache=document_cache,
                    normalization=normalization,
                    omit_epub_types=omitted_types,
                    content=content,
                ).children
            )

        trees: list[RichText] = []
        for index, part in enumerate(parts):
            tree = extract_rich_fragment(
                epub,
                part,
                cache=document_cache,
                normalization=normalization,
                omit_epub_types=omitted_types,
                content=content,
            )
            trees.append(
                replace(
                    tree,
                    children=(
                        events_at(attachments, index)
                        + tree.children
                        + events_at(attachments_after, index)
                    ),
                )
            )
        joined = join_rich_text(trees, separator, normalization)
        joined = replace(
            joined, children=joined.children + events_at(attachments, len(parts))
        )
        if not plain_text(joined) and not allow_empty:
            raise EpubBlocksError(f"{location}: extraction produced no text")
        return render_rich_text(joined, content.markup)
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
    if not text and not allow_empty:
        raise EpubBlocksError(f"{location}: extraction produced no text")
    return text


def _prepare_recipe(
    epub_path: StrPath,
    recipe: Mapping[str, object],
    recipe_location: str,
    *,
    verify_digest: bool,
) -> tuple[Path, _RecipeSpec]:
    path = Path(epub_path)
    spec = _parse_recipe(recipe, recipe_location, require_compiled_hash=verify_digest)
    return path, spec


def _attach_milestones(
    blocks: tuple[CompiledBlock, ...],
    detached: Sequence[Fragment],
    documents: Sequence[SpineDocument],
    content: ContentOptions,
) -> tuple[CompiledBlock, ...]:
    if not detached:
        return blocks
    markup = content.markup
    if markup is None:
        raise EpubBlocksError("detached milestones require markup configuration")
    positions = {doc.path: doc.position for doc in documents}

    def key(part: Fragment) -> tuple[int, tuple[int, ...], int]:
        return (
            positions[part.document_path],
            tuple(int(item) for item in part.element_path.split(".") if item),
            part.start or 0,
        )

    anchors = [
        (key(part), block_index, part_index)
        for block_index, block in enumerate(blocks)
        for part_index, part in enumerate(block.parts)
        if part.document_path in positions
    ]
    if not anchors:
        raise EpubBlocksError(
            "detached milestones require output fragments from selected source documents"
        )
    if [item[0] for item in anchors] != sorted(item[0] for item in anchors):
        raise EpubBlocksError(
            "detached milestones require source-ordered output fragments"
        )
    attachments: dict[int, list[tuple[int, Fragment]]] = defaultdict(list)
    attachments_after: dict[int, list[tuple[int, Fragment]]] = defaultdict(list)
    cursor = 0
    for event in detached:
        while cursor < len(anchors) and anchors[cursor][0] < key(event):
            cursor += 1
        if cursor < len(anchors):
            if markup.between_blocks != "next":
                raise EpubBlocksError(
                    f"{_fragment_locator(event)}: detached milestone needs between_blocks: next"
                )
            _, block_index, part_index = anchors[cursor]
            attachments[block_index].append((part_index, event))
        else:
            if markup.trailing != "previous":
                raise EpubBlocksError(
                    f"{_fragment_locator(event)}: trailing milestone needs trailing: previous"
                )
            _, block_index, part_index = anchors[-1]
            if part_index == len(blocks[block_index].parts) - 1:
                # Preserve the existing block-end representation and digest.
                attachments[block_index].append((len(blocks[block_index].parts), event))
            else:
                attachments_after[block_index].append((part_index, event))
    return tuple(
        replace(
            block,
            milestones=tuple(attachments[index]),
            milestones_after=tuple(attachments_after[index]),
        )
        for index, block in enumerate(blocks)
    )


def _compile_from_epub(
    path: Path,
    spec: _RecipeSpec,
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
    detached: list[Fragment] = []
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
        content=spec.content,
        detached_milestones=detached,
    )
    for category, special_outputs in (
        ("replacement", spec.output.replacements),
        ("insertion", spec.output.insertions),
    ):
        for item_index, item in enumerate(special_outputs, 1):
            for output_index, produced in enumerate(item.outputs, 1):
                _extract_joined_parts(
                    epub,
                    produced.parts,
                    produced.separator,
                    spec.normalization,
                    spec.omitted_types,
                    document_cache,
                    location=(f"output {category} {item_index}, output {output_index}"),
                    content=spec.content,
                    allow_empty=produced.emission.allow_empty,
                )
    blocks, skipped, reserved = _compile_blocks(candidates, spec)
    if spec.content != ContentOptions():
        retained_events: set[str] = set()
        selected_roots: dict[str, set[str]] = defaultdict(set)
        for block in blocks:
            for part in block.parts:
                selected_roots[part.document_path].add(part.element_path)
                tree = extract_rich_fragment(
                    epub,
                    part,
                    cache=document_cache,
                    normalization=spec.normalization,
                    omit_epub_types=spec.omitted_types,
                    content=spec.content,
                )
                for locator in milestones(tree):
                    if locator in retained_events:
                        raise EpubBlocksError(
                            f"{locator}: milestone is emitted more than once"
                        )
                    retained_events.add(locator)

        def claimed(event: Fragment) -> bool:
            # A selected fragment owns its subtree, including markers explicitly
            # discarded by its omissions or slice. Do not relocate those markers.
            roots = selected_roots[event.document_path]
            path = event.element_path
            while path:
                if path in roots:
                    return True
                path = path.rpartition(".")[0]
            return "" in roots

        detached = [event for event in detached if not claimed(event)]
    documents = select_spine_documents(
        package,
        source.include_documents,
        source.exclude_documents,
        include_non_linear=source.include_non_linear,
    )
    blocks = _attach_milestones(blocks, detached, documents, spec.content)
    compiled = CompiledRecipe(
        spec.identifier,
        spec.sha256,
        spec.normalization,
        spec.omitted_types,
        blocks,
        skipped,
        reserved,
        spec.content,
    )
    actual_digest = compiled_recipe_digest(compiled)
    if verify_digest and spec.output.compiled_sha256 != actual_digest:
        raise EpubBlocksError(
            "output compiled SHA-256 does not match: "
            f"expected {spec.output.compiled_sha256!r}, found {actual_digest!r}"
        )
    return compiled


def compile_recipe(
    epub_path: StrPath,
    recipe: Mapping[str, object],
    *,
    recipe_location: str = "recipe",
    verify_digest: bool = True,
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> CompiledRecipe:
    """Compile a strictly validated version 1 recipe into an extraction plan."""

    path, spec = _prepare_recipe(
        epub_path,
        recipe,
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
            content=compiled.content,
            allow_empty=block.allow_empty,
            attachments=block.milestones,
            attachments_after=block.milestones_after,
        )
        result.append(ExtractedBlock(block.block_id, block.block_type, text))
    return result


def extract_recipe(
    epub_path: StrPath,
    recipe: Mapping[str, object],
    *,
    recipe_location: str = "recipe",
    limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
) -> list[ExtractedBlock]:
    """Compile and apply a strictly validated version 1 extraction recipe."""

    path, spec = _prepare_recipe(
        epub_path,
        recipe,
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
        recipe_location=str(recipe_file),
        limits=limits,
    )


def write_tsv(path: StrPath, blocks: Iterable[ExtractedBlock]) -> None:
    """Atomically write literal tab-separated fields, without quoting or escaping.

    Tabs, carriage returns, and line feeds within fields are rejected.
    """

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
            for index, block in enumerate(blocks, 1):
                fields = (block.block_id, block.block_type, block.text)
                for name, value in zip(("id", "type", "text"), fields, strict=True):
                    if any(char in value for char in ("\t", "\r", "\n")):
                        raise EpubBlocksError(
                            f"block {index}, {name}: plain TSV fields cannot contain "
                            "TAB, CR, or LF; normalize or encode them before writing"
                        )
                output.write("\t".join(fields) + "\n")
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(output_path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
