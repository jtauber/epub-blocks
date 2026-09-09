"""Internal structural policies and loss-aware inline markup rendering."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import cast

from .errors import EpubBlocksError
from .models import (
    BoundaryRule,
    ContentOptions,
    ElementRule,
    ElementSelector,
    MarkupOptions,
    MarkupRule,
    NormalizationOptions,
)
from .package import matches
from .xml import XmlElement, local_name

EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_INVALID_XML_CHARACTER = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]"
)


def object_value(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EpubBlocksError(f"{location}: must be an object")
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise EpubBlocksError(f"{location}: object keys must be strings")
    return cast(Mapping[str, object], mapping)


def members(value: Mapping[str, object], allowed: set[str], location: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise EpubBlocksError(f"{location}: unknown member(s): {sorted(unknown)}")


def string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise EpubBlocksError(f"{location}: must be a non-empty string")
    return value


def boolean(value: object, location: str) -> bool:
    if not isinstance(value, bool):
        raise EpubBlocksError(f"{location}: must be a boolean")
    return value


def array(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise EpubBlocksError(f"{location}: must be an array")
    return cast(list[object], value)


def strings(value: object, location: str) -> tuple[str, ...]:
    result = tuple(string(item, location) for item in array(value, location))
    if len(result) != len(set(result)):
        raise EpubBlocksError(f"{location}: values must be unique")
    return result


def selector(
    value: object, location: str, *, contextual: bool = True
) -> ElementSelector:
    spec = object_value(value, location)
    allowed = {
        "tag",
        "classes",
        "classes_any",
        "classes_all",
        "locators",
        "epub_types",
        "attributes",
        "attribute_prefixes",
        "empty",
    }
    if contextual:
        allowed |= {"previous_sibling", "has_child"}
    members(spec, allowed, location)
    attrs = object_value(spec.get("attributes", {}), location + ".attributes")
    attributes: list[tuple[str, str]] = []
    for key, item in attrs.items():
        string(key, location + ".attributes")
        if not isinstance(item, str):
            raise EpubBlocksError(f"{location}.attributes: values must be strings")
        attributes.append((key, item))
    prefixes = object_value(
        spec.get("attribute_prefixes", {}), location + ".attribute_prefixes"
    )
    attribute_prefixes = tuple(
        sorted(
            (
                string(key, location + ".attribute_prefixes"),
                string(item, location + ".attribute_prefixes"),
            )
            for key, item in prefixes.items()
        )
    )
    result = ElementSelector(
        string(spec["tag"], location + ".tag") if "tag" in spec else None,
        strings(spec["classes"], location + ".classes") if "classes" in spec else None,
        strings(spec.get("classes_any", []), location + ".classes_any"),
        strings(spec.get("classes_all", []), location + ".classes_all"),
        strings(spec.get("locators", []), location + ".locators"),
        strings(spec.get("epub_types", []), location + ".epub_types"),
        tuple(sorted(attributes)),
        boolean(spec["empty"], location + ".empty") if "empty" in spec else None,
        selector(
            spec["previous_sibling"], location + ".previous_sibling", contextual=False
        )
        if "previous_sibling" in spec
        else None,
        selector(spec["has_child"], location + ".has_child", contextual=False)
        if "has_child" in spec
        else None,
        attribute_prefixes=attribute_prefixes,
    )
    if result == ElementSelector():
        raise EpubBlocksError(f"{location}: must contain at least one criterion")
    return result


def element_rules(value: object, location: str) -> tuple[ElementRule, ...]:
    result: list[ElementRule] = []
    for i, item in enumerate(array(value, location), 1):
        at = f"{location}[{i}]"
        rule = object_value(item, at)
        members(rule, {"match", "action", "keep_empty"}, at)
        action = string(rule.get("action"), at + ".action")
        if action not in {"block", "descend", "skip"}:
            raise EpubBlocksError(f"{at}.action: expected block, descend, or skip")
        keep = boolean(rule.get("keep_empty", False), at + ".keep_empty")
        if "keep_empty" in rule and action != "block":
            raise EpubBlocksError(f"{at}: keep_empty requires action block")
        result.append(
            ElementRule(selector(rule.get("match"), at + ".match"), action, keep)
        )
    return tuple(result)


def markup_options(value: object, location: str) -> MarkupOptions:
    spec = object_value(value, location)
    members(
        spec,
        {
            "format",
            "rules",
            "delimiters",
            "between_blocks",
            "trailing",
            "attachment_order",
            "strip_outer_whitespace",
            "remove_source_newlines",
        },
        location,
    )
    format_name = string(spec.get("format", "xml"), location + ".format")
    if format_name not in {"xml", "delimiters", "literal"}:
        raise EpubBlocksError(
            f"{location}.format: expected xml, delimiters, or literal"
        )
    between = string(spec.get("between_blocks", "error"), location + ".between_blocks")
    trailing = string(spec.get("trailing", "error"), location + ".trailing")
    if between not in {"next", "error", "ignore"} or trailing not in {
        "previous",
        "error",
        "ignore",
    }:
        raise EpubBlocksError(f"{location}: invalid milestone attachment policy")
    order = string(
        spec.get("attachment_order", "output"), location + ".attachment_order"
    )
    if order not in {"output", "source"}:
        raise EpubBlocksError(f"{location}.attachment_order: expected output or source")
    rules: list[MarkupRule] = []
    kinds: dict[str, str] = {}
    for i, item in enumerate(array(spec.get("rules", []), location + ".rules"), 1):
        at = f"{location}.rules[{i}]"
        rule = object_value(item, at)
        members(
            rule,
            {
                "match",
                "kind",
                "name",
                "label_attribute",
                "label_text",
                "position",
                "label_counter",
                "continue_matching",
                "preserve_whitespace",
                "label_strip_prefix",
            },
            at,
        )
        kind = string(rule.get("kind"), at + ".kind")
        name = string(rule.get("name"), at + ".name")
        if kind not in {"span", "milestone"} or _NAME.fullmatch(name) is None:
            raise EpubBlocksError(f"{at}: invalid markup kind or XML name")
        if name in kinds and kinds[name] != kind:
            raise EpubBlocksError(f"{at}: a markup name cannot have different kinds")
        kinds[name] = kind
        label = (
            string(rule["label_attribute"], at + ".label_attribute")
            if "label_attribute" in rule
            else None
        )
        if kind == "span" and label is not None:
            raise EpubBlocksError(f"{at}: label_attribute is only valid for milestones")
        label_prefix = (
            string(rule["label_strip_prefix"], at + ".label_strip_prefix")
            if "label_strip_prefix" in rule
            else None
        )
        if label_prefix is not None and label is None:
            raise EpubBlocksError(f"{at}: label_strip_prefix requires label_attribute")
        label_text = boolean(rule.get("label_text", False), at + ".label_text")
        if kind == "span" and "label_text" in rule:
            raise EpubBlocksError(f"{at}: label_text is only valid for milestones")
        if label_text and label is not None:
            raise EpubBlocksError(f"{at}: label_text and label_attribute are exclusive")
        position = string(rule.get("position", "replace"), at + ".position")
        if position not in {"replace", "before"}:
            raise EpubBlocksError(f"{at}: position must be replace or before")
        counter = (
            string(rule["label_counter"], at + ".label_counter")
            if "label_counter" in rule
            else None
        )
        if kind == "span" and ("position" in rule or counter is not None):
            raise EpubBlocksError(
                f"{at}: position and label_counter are only valid for milestones"
            )
        if counter is not None:
            if counter != "ordered-list" or position != "before":
                raise EpubBlocksError(
                    f"{at}: label_counter requires ordered-list and position: before"
                )
            if label is not None or label_text:
                raise EpubBlocksError(f"{at}: milestone label sources are exclusive")
        continuing = boolean(
            rule.get("continue_matching", False), at + ".continue_matching"
        )
        if continuing and (kind != "milestone" or position != "before"):
            raise EpubBlocksError(
                f"{at}: continue_matching requires a milestone with position: before"
            )
        preserve = boolean(
            rule.get("preserve_whitespace", False), at + ".preserve_whitespace"
        )
        if "preserve_whitespace" in rule and kind != "span":
            raise EpubBlocksError(f"{at}: preserve_whitespace is only valid for spans")
        rules.append(
            MarkupRule(
                selector(rule.get("match"), at + ".match"),
                kind,
                name,
                label,
                label_text,
                position,
                counter,
                continuing,
                preserve,
                label_prefix,
            )
        )
    delimiters = object_value(spec.get("delimiters", {}), location + ".delimiters")
    pairs: list[tuple[str, tuple[str, str]]] = []
    tokens: list[str] = []
    for name, item in sorted(delimiters.items()):
        if name not in kinds:
            raise EpubBlocksError(f"{location}.delimiters: unused name {name!r}")
        if format_name == "literal":
            literal_pair = array(item, location + ".delimiters")
            if (
                len(literal_pair) != 2
                or not all(isinstance(token, str) for token in literal_pair)
                or not any(literal_pair)
            ):
                raise EpubBlocksError(
                    f"{location}.delimiters: literal mode needs two strings, at least one nonempty"
                )
            pair = cast(tuple[str, str], tuple(literal_pair))
            if any(char in "\t\r\n" for token in pair for char in token):
                raise EpubBlocksError(
                    f"{location}.delimiters: literal tokens must not contain TAB, CR or LF"
                )
            pairs.append((name, pair))
            continue
        pair = strings(item, location + ".delimiters")
        if len(pair) != 2 or any("\\" in token for token in pair):
            raise EpubBlocksError(
                f"{location}.delimiters: need two distinct nonempty tokens without backslashes"
            )
        for token in pair:
            if any(
                token.startswith(other) or other.startswith(token) for other in tokens
            ):
                raise EpubBlocksError(
                    f"{location}.delimiters: tokens must be distinct and prefix-free"
                )
            tokens.append(token)
        pairs.append((name, (pair[0], pair[1])))
    if any(rule.preserve_whitespace for rule in rules) and any(
        char in "nrt\t\r\n" for token in tokens for char in token
    ):
        raise EpubBlocksError(
            f"{location}.delimiters: preserved-whitespace escapes reserve n, r, t; tokens must not contain TAB, CR or LF"
        )
    if format_name in {"delimiters", "literal"} and set(delimiters) != set(kinds):
        raise EpubBlocksError(f"{location}.delimiters: every markup name needs a pair")
    return MarkupOptions(
        format_name,
        tuple(rules),
        tuple(pairs),
        between,
        trailing,
        order,
        boolean(
            spec.get("strip_outer_whitespace", False),
            location + ".strip_outer_whitespace",
        ),
        boolean(
            spec.get("remove_source_newlines", False),
            location + ".remove_source_newlines",
        ),
    )


def whitespace(value: object, location: str) -> str:
    # The schema's boundaryWhitespace explicitly enumerates this character set;
    # ECMA-262 \s differs from Python's definition. Tests keep them in agreement.
    if not isinstance(value, str) or not value or not value.isspace():
        raise EpubBlocksError(f"{location}: must be nonempty whitespace")
    return value


def boundary_rules(value: object, location: str) -> tuple[BoundaryRule, ...]:
    result: list[BoundaryRule] = []
    for index, item in enumerate(array(value, location), 1):
        at = f"{location}[{index}]"
        rule = object_value(item, at)
        members(rule, {"match", "before", "after"}, at)
        if "before" not in rule and "after" not in rule:
            raise EpubBlocksError(f"{at}: requires before and/or after whitespace")
        result.append(
            BoundaryRule(
                selector(rule.get("match"), at + ".match"),
                whitespace(rule["before"], at + ".before") if "before" in rule else "",
                whitespace(rule["after"], at + ".after") if "after" in rule else "",
            )
        )
    return tuple(result)


def parse_text_options(value: object, location: str) -> ContentOptions:
    spec = object_value(value, location)
    members(spec, {"block_boundaries", "markup"}, location)
    boundary = object_value(
        spec.get("block_boundaries", {}), location + ".block_boundaries"
    )
    members(boundary, {"tags", "separator", "rules"}, location + ".block_boundaries")
    tags = strings(boundary.get("tags", []), location + ".block_boundaries.tags")
    separator_value = whitespace(
        boundary.get("separator", " "), location + ".block_boundaries.separator"
    )
    markup = (
        markup_options(spec["markup"], location + ".markup")
        if "markup" in spec
        else None
    )
    return ContentOptions(
        boundary_tags=tuple(sorted(tag.casefold() for tag in tags)),
        boundary_separator=separator_value if tags else " ",
        markup=markup,
        boundary_rules=boundary_rules(
            boundary.get("rules", []), location + ".block_boundaries.rules"
        ),
    )


def selector_value(spec: ElementSelector) -> dict[str, object]:
    result: dict[str, object] = {}
    if spec.tag is not None:
        result["tag"] = spec.tag
    if spec.classes is not None:
        result["classes"] = list(spec.classes)
    for key in ("classes_any", "classes_all", "locators", "epub_types"):
        values = getattr(spec, key)
        if values:
            result[key] = list(values)
    if spec.attributes:
        result["attributes"] = dict(spec.attributes)
    if spec.attribute_prefixes:
        result["attribute_prefixes"] = dict(spec.attribute_prefixes)
    if spec.empty is not None:
        result["empty"] = spec.empty
    if spec.previous_sibling is not None:
        result["previous_sibling"] = selector_value(spec.previous_sibling)
    if spec.has_child is not None:
        result["has_child"] = selector_value(spec.has_child)
    return result


def content_value(content: ContentOptions) -> dict[str, object]:
    """Canonical effective policy; the empty policy keeps old digests unchanged."""
    result: dict[str, object] = {}
    if content.element_rules:
        result["element_rules"] = [
            {
                "match": selector_value(rule.match),
                "action": rule.action,
                **({"keep_empty": True} if rule.keep_empty else {}),
            }
            for rule in content.element_rules
        ]
    if content.strict_coverage:
        result["strict_coverage"] = True
    if content.boundary_tags or content.boundary_rules:
        boundaries: dict[str, object] = {}
        if content.boundary_tags:
            boundaries.update(
                tags=list(content.boundary_tags), separator=content.boundary_separator
            )
        if content.boundary_rules:
            boundaries["rules"] = [
                {
                    "match": selector_value(rule.match),
                    **({"before": rule.before} if rule.before else {}),
                    **({"after": rule.after} if rule.after else {}),
                }
                for rule in content.boundary_rules
            ]
        result["block_boundaries"] = boundaries
    if content.markup is not None:
        markup = content.markup
        result["markup"] = {
            "format": markup.format,
            "rules": [
                {
                    "match": selector_value(rule.match),
                    "kind": rule.kind,
                    "name": rule.name,
                    **(
                        {"label_attribute": rule.label_attribute}
                        if rule.label_attribute is not None
                        else {}
                    ),
                    **({"label_text": True} if rule.label_text else {}),
                    **(
                        {"label_strip_prefix": rule.label_strip_prefix}
                        if rule.label_strip_prefix is not None
                        else {}
                    ),
                    **(
                        {"position": rule.position}
                        if rule.position != "replace"
                        else {}
                    ),
                    **(
                        {"label_counter": rule.label_counter}
                        if rule.label_counter is not None
                        else {}
                    ),
                    **({"continue_matching": True} if rule.continue_matching else {}),
                    **(
                        {"preserve_whitespace": True}
                        if rule.preserve_whitespace
                        else {}
                    ),
                }
                for rule in markup.rules
            ],
            "delimiters": {name: list(pair) for name, pair in markup.delimiters},
            "between_blocks": markup.between_blocks,
            "trailing": markup.trailing,
            **(
                {"strip_outer_whitespace": True}
                if markup.strip_outer_whitespace
                else {}
            ),
            **(
                {"remove_source_newlines": True}
                if markup.remove_source_newlines
                else {}
            ),
            **(
                {"attachment_order": markup.attachment_order}
                if markup.attachment_order != "output"
                else {}
            ),
        }
    return result


def validate_content(value: object) -> ContentOptions:
    """Validate public compiled-state values as strictly as recipe values."""
    if not isinstance(value, ContentOptions):
        raise EpubBlocksError("compiled content: must be ContentOptions")
    boolean(value.strict_coverage, "compiled content.strict_coverage")
    try:
        for index, rule in enumerate(value.element_rules, 1):
            boolean(
                rule.keep_empty, f"compiled content.element_rules[{index}].keep_empty"
            )
        if value.markup is not None:
            boolean(
                value.markup.strip_outer_whitespace,
                "compiled content.markup.strip_outer_whitespace",
            )
            boolean(
                value.markup.remove_source_newlines,
                "compiled content.markup.remove_source_newlines",
            )
            for index, mark in enumerate(value.markup.rules, 1):
                boolean(
                    mark.label_text,
                    f"compiled content.markup.rules[{index}].label_text",
                )
                boolean(
                    mark.continue_matching,
                    f"compiled content.markup.rules[{index}].continue_matching",
                )
                boolean(
                    mark.preserve_whitespace,
                    f"compiled content.markup.rules[{index}].preserve_whitespace",
                )
        serialized = content_value(value)
        rules = element_rules(
            serialized.pop("element_rules", []), "compiled content.element_rules"
        )
        strict = boolean(
            serialized.pop("strict_coverage", False), "compiled content.strict_coverage"
        )
        parsed = replace(
            parse_text_options(serialized, "compiled content.text"),
            element_rules=rules,
            strict_coverage=strict,
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise EpubBlocksError(f"compiled content: invalid policy: {error}") from error
    if parsed != value:
        raise EpubBlocksError("compiled content: policy is not in canonical form")
    return parsed


def child_locator(locator: str, index: int) -> str:
    return f"{locator}{'' if locator.endswith('#') else '.'}{index}"


def matches_element(
    element: XmlElement,
    spec: ElementSelector,
    locator: str,
    previous: XmlElement | None = None,
) -> bool:
    classes = set(element.get("class", "").split())
    if (
        spec.tag is not None
        and local_name(element.tag).casefold() != spec.tag.casefold()
    ):
        return False
    if spec.classes is not None and classes != set(spec.classes):
        return False
    if spec.classes_any and not classes.intersection(spec.classes_any):
        return False
    if not set(spec.classes_all) <= classes:
        return False
    if spec.locators and not matches(locator, spec.locators):
        return False
    if spec.epub_types and not set(spec.epub_types) <= set(
        element.get(EPUB_TYPE, "").split()
    ):
        return False
    if any(element.get(key) != value for key, value in spec.attributes):
        return False
    if any(
        not element.get(key, "").startswith(prefix)
        for key, prefix in spec.attribute_prefixes
    ):
        return False
    if (
        spec.empty is not None
        and (not "".join(element.itertext()).strip()) != spec.empty
    ):
        return False
    if spec.previous_sibling is not None:
        if previous is None:
            return False
        prefix, index = (
            locator.rsplit(".", 1)
            if "." in locator.split("#")[-1]
            else locator.rsplit("#", 1)
        )
        separator_value = "." if "." in locator.split("#")[-1] else "#"
        previous_locator = f"{prefix}{separator_value}{int(index) - 1}"
        if not matches_element(previous, spec.previous_sibling, previous_locator):
            return False
    return spec.has_child is None or any(
        matches_element(child, spec.has_child, child_locator(locator, index))
        for index, child in enumerate(element, 1)
    )


def source_rule(
    element: XmlElement,
    content: ContentOptions,
    locator: str,
    previous: XmlElement | None = None,
) -> ElementRule | None:
    return next(
        (
            rule
            for rule in content.element_rules
            if matches_element(element, rule.match, locator, previous)
        ),
        None,
    )


def markup_rules(
    element: XmlElement,
    content: ContentOptions,
    locator: str,
    previous: XmlElement | None = None,
    *,
    validate: bool = True,
) -> tuple[MarkupRule, ...]:
    """First match wins unless a leading milestone explicitly continues."""
    if content.markup is None:
        return ()
    result: list[MarkupRule] = []
    names: set[str] = set()
    for rule in content.markup.rules:
        if not matches_element(element, rule.match, locator, previous):
            continue
        if validate and rule.name in names:
            raise EpubBlocksError(
                f"{locator}: composed markup repeats name {rule.name!r}"
            )
        names.add(rule.name)
        result.append(rule)
        if not rule.continue_matching:
            break
    return tuple(result)


def boundary_spacing(
    element: XmlElement,
    content: ContentOptions,
    locator: str,
    previous: XmlElement | None,
) -> tuple[str, str]:
    for rule in content.boundary_rules:
        if matches_element(element, rule.match, locator, previous):
            return rule.before, rule.after
    if local_name(element.tag).casefold() in content.boundary_tags:
        return content.boundary_separator, content.boundary_separator
    return "", ""


@dataclass(frozen=True)
class RichText:
    """Internal tree, not a public stand-off output."""

    kind: str = "root"
    name: str = ""
    label: str | None = None
    children: tuple[str | RichText, ...] = ()
    locator: str = ""
    preserve_whitespace: bool = False


def plain_text(tree: RichText) -> str:
    return "".join(
        child if isinstance(child, str) else plain_text(child)
        for child in tree.children
    )


def milestones(tree: RichText) -> tuple[str, ...]:
    if tree.kind == "milestone":
        return (tree.locator,)
    return tuple(
        locator
        for child in tree.children
        if isinstance(child, RichText)
        for locator in milestones(child)
    )


class SourceContext:
    """Original document structure, independent of selection and omissions."""

    def __init__(self, body: XmlElement) -> None:
        self.parents: dict[XmlElement, XmlElement] = {
            child: parent for parent in body.iter() for child in parent
        }
        self.ordinals: dict[XmlElement, dict[XmlElement, str]] = {}

    def ordered_list_label(self, item: XmlElement, locator: str) -> str:
        parent = self.parents.get(item)
        if (
            local_name(item.tag) != "li"
            or parent is None
            or local_name(parent.tag) != "ol"
        ):
            raise EpubBlocksError(
                f"{locator}: ordered-list label requires a direct li child of ol"
            )
        if parent not in self.ordinals:
            items = [child for child in parent if local_name(child.tag) == "li"]
            if parent.get("type", "1") != "1" or any(
                child.get("type", "1") != "1" for child in items
            ):
                raise EpubBlocksError(
                    f"{locator}: ordered-list labels support decimal type 1 only"
                )

            def integer(element: XmlElement, attribute: str, default: int) -> int:
                value = element.get(attribute)
                if value is None:
                    return default
                value = value.strip(" \t\r\n\f")
                if re.fullmatch(r"[+-]?[0-9]+", value) is None:
                    raise EpubBlocksError(
                        f"{locator}: invalid ordered-list {attribute}: {value!r}"
                    )
                try:
                    return int(value)
                except ValueError as error:
                    raise EpubBlocksError(
                        f"{locator}: ordered-list {attribute} is too large"
                    ) from error

            reversed_list = "reversed" in parent.attrib
            current = integer(parent, "start", len(items) if reversed_list else 1)
            numbers: dict[XmlElement, str] = {}
            for child in items:
                current = integer(child, "value", current)
                try:
                    numbers[child] = str(current)
                except ValueError as error:
                    raise EpubBlocksError(
                        f"{locator}: ordered-list ordinal is too large"
                    ) from error
                current += -1 if reversed_list else 1
            self.ordinals[parent] = numbers
        return self.ordinals[parent][item]


def build_rich_text(
    element: XmlElement,
    locator: str,
    content: ContentOptions,
    omitted_types: frozenset[str],
    previous: XmlElement | None = None,
    origins: Mapping[int, tuple[str, XmlElement, XmlElement | None]] | None = None,
    source: SourceContext | None = None,
    *,
    milestone_only: bool = False,
) -> RichText:
    def source_context(
        current: XmlElement, at: str, prev: XmlElement | None
    ) -> tuple[str, XmlElement, XmlElement | None]:
        if origins is not None:
            return origins.get(id(current), (at, current, prev))
        return at, current, prev

    def visit(
        current: XmlElement, at: str, prev: XmlElement | None, root: bool
    ) -> list[str | RichText] | None:
        at, original, prev = source_context(current, at, prev)
        selected = source_rule(original, content, at, prev)
        if selected is not None and selected.action == "skip":
            return None
        rules = markup_rules(original, content, at, prev)
        if set(current.get(EPUB_TYPE, "").split()) & omitted_types:
            if rules:
                raise EpubBlocksError(
                    f"{at}: retained markup is also semantically omitted"
                )
            if not root:
                return None
        prefix: list[str | RichText] = []
        for rule in rules:
            if rule.kind != "milestone":
                continue

            def nested_milestone(parent: XmlElement, parent_locator: str) -> bool:
                previous_child: XmlElement | None = None
                for index, child in enumerate(parent, 1):
                    child_at, original_child, original_previous = source_context(
                        child, child_locator(parent_locator, index), previous_child
                    )
                    source = source_rule(
                        original_child, content, child_at, original_previous
                    )
                    if source is None or source.action != "skip":
                        marks = markup_rules(
                            original_child, content, child_at, original_previous
                        )
                        if set(child.get(EPUB_TYPE, "").split()) & omitted_types:
                            if marks:
                                raise EpubBlocksError(
                                    f"{child_at}: retained markup is also semantically omitted"
                                )
                            previous_child = child
                            continue
                        if any(mark.kind == "milestone" for mark in marks):
                            return True
                        if nested_milestone(child, child_at):
                            return True
                    previous_child = child
                return False

            if rule.position == "replace" and nested_milestone(current, at):
                raise EpubBlocksError(
                    f"{at}: overlapping milestone rules would discard a nested milestone"
                )
            label = current.get(rule.label_attribute) if rule.label_attribute else None
            if rule.label_attribute and (label is None or not label):
                raise EpubBlocksError(
                    f"{at}: milestone label attribute {rule.label_attribute!r} is missing or empty"
                )
            if rule.label_strip_prefix is not None:
                if (
                    label is None
                    or not label.startswith(rule.label_strip_prefix)
                    or label == rule.label_strip_prefix
                ):
                    raise EpubBlocksError(
                        f"{at}: milestone label must start with label_strip_prefix and retain a nonempty suffix"
                    )
                label = label[len(rule.label_strip_prefix) :]
            if rule.label_text:
                # Source skips, omissions, original context, and nested boundaries
                # apply to label content, but markup does not become label syntax.
                source_text = plain_text(
                    build_rich_text(
                        current,
                        at,
                        replace(content, markup=None),
                        omitted_types,
                        prev,
                        origins,
                        source,
                    )
                )
                label = " ".join(source_text.split())
                if not label:
                    raise EpubBlocksError(f"{at}: milestone label text is empty")
            if rule.label_counter is not None:
                if source is None:
                    raise EpubBlocksError(
                        f"{at}: ordered-list label requires original document context"
                    )
                label = source.ordered_list_label(original, at)
            prefix.append(RichText("milestone", rule.name, label, locator=at))
            if rule.position == "replace":
                return prefix
        if root and milestone_only:
            if not prefix:
                raise EpubBlocksError(f"{at}: attached fragment must be a milestone")
            return prefix
        pieces: list[str | RichText] = []
        if local_name(current.tag) == "br":
            pieces.append("\n")
        if current.text:
            pieces.append(current.text)
        prev_child: XmlElement | None = None
        for index, child in enumerate(current, 1):
            retained = visit(child, child_locator(at, index), prev_child, False)
            # None means omitted, unlike an empty but retained structural block.
            if retained is not None:
                child_at, original_child, original_previous = source_context(
                    child, child_locator(at, index), prev_child
                )
                before, after = boundary_spacing(
                    original_child, content, child_at, original_previous
                )
                if before:
                    pieces.append(before)
                pieces.extend(retained)
                if after:
                    pieces.append(after)
            if child.tail:
                pieces.append(child.tail)
            prev_child = child
        merged: list[str | RichText] = []
        for item in pieces:
            if isinstance(item, str) and merged and isinstance(merged[-1], str):
                merged[-1] += item
            else:
                merged.append(item)
        if rules and rules[-1].kind == "span":
            return prefix + [
                RichText(
                    "span",
                    rules[-1].name,
                    children=tuple(merged),
                    locator=at,
                    preserve_whitespace=rules[-1].preserve_whitespace,
                )
            ]
        return prefix + merged

    return RichText(children=tuple(visit(element, locator, previous, True) or ()))


def normalize_rich_text(
    tree: RichText, options: NormalizationOptions, markup: MarkupOptions | None = None
) -> RichText:
    def coalesce(node: RichText) -> RichText:
        children: list[str | RichText] = []
        for item in node.children:
            if isinstance(item, str) and children and isinstance(children[-1], str):
                children[-1] += item
            else:
                children.append(item if isinstance(item, str) else coalesce(item))
        return replace(node, children=tuple(children))

    tree = coalesce(tree)
    leaves: list[str] = []
    protected: list[bool] = []

    def collect(node: RichText, preserve: bool = False) -> None:
        preserve = preserve or node.preserve_whitespace
        for child in node.children:
            if isinstance(child, str):
                leaves.append(child)
                protected.append(preserve)
            else:
                collect(child, preserve)

    collect(tree)
    raw = "".join(leaves)
    start = len(raw) - len(raw.lstrip()) if options.strip else 0
    end = len(raw.rstrip()) if options.strip else len(raw)
    if any(protected):
        # A preserved region, including its leading/trailing whitespace, is
        # part of the reading. Only unprotected outer whitespace may be trimmed.
        offset = 0
        for leaf, preserve in zip(leaves, protected, strict=True):
            if preserve and leaf:
                start = min(start, offset)
                end = max(end, offset + len(leaf))
            offset += len(leaf)
    position = 0
    last_space = False
    whitespace_normalized: list[str] = []
    for leaf, preserve in zip(leaves, protected, strict=True):
        chars: list[str] = []
        for char in leaf:
            keep = start <= position < end
            position += 1
            if not keep:
                continue
            if options.collapse_whitespace and not preserve and char.isspace():
                if not last_space:
                    chars.append(" ")
                last_space = True
            else:
                chars.append(char)
                last_space = False
        whitespace_normalized.append("".join(chars))
    normalized = whitespace_normalized
    if options.unicode_normalization != "none":
        normalized = [
            unicodedata.normalize(options.unicode_normalization, value)
            for value in normalized
        ]
        whole = unicodedata.normalize(
            options.unicode_normalization, "".join(whitespace_normalized)
        )
        if "".join(normalized) != whole:
            raise EpubBlocksError(
                "Unicode normalization crosses a retained markup boundary; adjust the rule or use unicode_normalization: none"
            )
    values = iter(normalized)

    def rebuild(node: RichText) -> RichText:
        return replace(
            node,
            children=tuple(
                next(values) if isinstance(child, str) else rebuild(child)
                for child in node.children
            ),
        )

    result = rebuild(tree)
    if markup is not None and (
        markup.strip_outer_whitespace or markup.remove_source_newlines
    ):
        result = clean_markup_whitespace(result, markup)
    return result


def clean_markup_whitespace(tree: RichText, markup: MarkupOptions) -> RichText:
    """Apply opt-in cleanup before slicing; markers stop outer trimming."""

    def clean(node: RichText) -> RichText:
        return replace(
            node,
            children=tuple(
                (child.replace("\n", "") if markup.remove_source_newlines else child)
                if isinstance(child, str)
                else clean(child)
                for child in node.children
            ),
        )

    result = clean(tree)
    if not markup.strip_outer_whitespace:
        return result

    def trim(node: RichText, reverse: bool) -> tuple[RichText, bool]:
        if node.kind != "root":
            return node, False
        children = list(reversed(node.children)) if reverse else list(node.children)
        trimming = True
        for index, child in enumerate(children):
            if not trimming:
                break
            if isinstance(child, str):
                child = child.rstrip() if reverse else child.lstrip()
                children[index] = child
                trimming = not child
            else:
                children[index], trimming = trim(child, reverse)
        if reverse:
            children.reverse()
        return replace(node, children=tuple(children)), trimming

    result, _ = trim(result, False)
    result, _ = trim(result, True)
    return result


def slice_rich_text(tree: RichText, start: int, end: int) -> RichText:
    length = len(plain_text(tree))
    if not 0 <= start < end <= length:
        raise EpubBlocksError(
            f"slice [{start}, {end}) is outside a {length}-code-point fragment"
        )
    position = 0

    def clip(node: RichText) -> RichText:
        nonlocal position
        children: list[str | RichText] = []
        for child in node.children:
            if isinstance(child, str):
                left, right = max(0, start - position), min(len(child), end - position)
                if left < right:
                    children.append(child[left:right])
                position += len(child)
            elif child.kind == "milestone":
                if start <= position < end or position == end == length:
                    children.append(child)
            else:
                clipped = clip(child)
                if clipped.children:
                    children.append(clipped)
        return replace(node, children=tuple(children))

    return clip(tree)


def join_rich_text(
    trees: Sequence[RichText],
    separator: str,
    options: NormalizationOptions,
    markup: MarkupOptions | None = None,
) -> RichText:
    children: list[str | RichText] = []
    for index, tree in enumerate(trees):
        if index:
            children.append(separator)
        children.extend(tree.children)
    return normalize_rich_text(RichText(children=tuple(children)), options, markup)


def render_rich_text(tree: RichText, markup: MarkupOptions | None) -> str:
    if markup is None:
        return plain_text(tree)
    pairs = dict(markup.delimiters)
    reserved = {"\\"} | {
        char for pair in pairs.values() for token in pair for char in token
    }
    controls = any(rule.preserve_whitespace for rule in markup.rules)

    def escape(value: str, *, attribute: bool = False) -> str:
        if markup.format == "literal":
            return value
        if markup.format == "xml":
            if _INVALID_XML_CHARACTER.search(value):
                raise EpubBlocksError(
                    "markup contains a character not allowed in XML 1.0"
                )
            # XML parsers normalize literal CRs in text and all literal
            # whitespace in attributes. Character references preserve them.
            escaped = html.escape(value, quote=attribute).replace("\r", "&#13;")
            if attribute or controls:
                escaped = escaped.replace("\t", "&#9;").replace("\n", "&#10;")
            return escaped
        escapes = {"\t": "\\t", "\n": "\\n", "\r": "\\r"} if controls else {}
        return "".join(
            escapes.get(char, "\\" + char if char in reserved else char)
            for char in value
        )

    def render(node: RichText) -> str:
        text = "".join(
            escape(child) if isinstance(child, str) else render(child)
            for child in node.children
        )
        if node.kind == "root":
            return text
        if markup.format == "xml":
            if node.kind == "milestone":
                label = (
                    f' label="{escape(node.label, attribute=True)}"'
                    if node.label is not None
                    else ""
                )
                return f"<{node.name}{label}/>"
            return f"<{node.name}>{text}</{node.name}>"
        opening, closing = pairs[node.name]
        if node.kind == "milestone":
            text = escape(node.label or "")
        return opening + text + closing

    return render(tree)
