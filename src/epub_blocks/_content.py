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
        spec, {"format", "rules", "delimiters", "between_blocks", "trailing"}, location
    )
    format_name = string(spec.get("format", "xml"), location + ".format")
    if format_name not in {"xml", "delimiters"}:
        raise EpubBlocksError(f"{location}.format: expected xml or delimiters")
    between = string(spec.get("between_blocks", "error"), location + ".between_blocks")
    trailing = string(spec.get("trailing", "error"), location + ".trailing")
    if between not in {"next", "error"} or trailing not in {"previous", "error"}:
        raise EpubBlocksError(f"{location}: invalid milestone attachment policy")
    rules: list[MarkupRule] = []
    kinds: dict[str, str] = {}
    for i, item in enumerate(array(spec.get("rules", []), location + ".rules"), 1):
        at = f"{location}.rules[{i}]"
        rule = object_value(item, at)
        members(rule, {"match", "kind", "name", "label_attribute"}, at)
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
        rules.append(
            MarkupRule(selector(rule.get("match"), at + ".match"), kind, name, label)
        )
    delimiters = object_value(spec.get("delimiters", {}), location + ".delimiters")
    pairs: list[tuple[str, tuple[str, str]]] = []
    tokens: list[str] = []
    for name, item in sorted(delimiters.items()):
        if name not in kinds:
            raise EpubBlocksError(f"{location}.delimiters: unused name {name!r}")
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
    if format_name == "delimiters" and set(delimiters) != set(kinds):
        raise EpubBlocksError(f"{location}.delimiters: every markup name needs a pair")
    return MarkupOptions(format_name, tuple(rules), tuple(pairs), between, trailing)


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
                }
                for rule in markup.rules
            ],
            "delimiters": {name: list(pair) for name, pair in markup.delimiters},
            "between_blocks": markup.between_blocks,
            "trailing": markup.trailing,
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


def markup_rule(
    element: XmlElement,
    content: ContentOptions,
    locator: str,
    previous: XmlElement | None = None,
) -> MarkupRule | None:
    if content.markup is None:
        return None
    return next(
        (
            rule
            for rule in content.markup.rules
            if matches_element(element, rule.match, locator, previous)
        ),
        None,
    )


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


def build_rich_text(
    element: XmlElement,
    locator: str,
    content: ContentOptions,
    omitted_types: frozenset[str],
    previous: XmlElement | None = None,
    origins: Mapping[int, tuple[str, XmlElement, XmlElement | None]] | None = None,
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
        rule = markup_rule(original, content, at, prev)
        if set(current.get(EPUB_TYPE, "").split()) & omitted_types:
            if rule is not None:
                raise EpubBlocksError(
                    f"{at}: retained markup is also semantically omitted"
                )
            if not root:
                return None
        if rule is not None and rule.kind == "milestone":

            def nested_milestone(parent: XmlElement, parent_locator: str) -> bool:
                previous_child: XmlElement | None = None
                for index, child in enumerate(parent, 1):
                    child_at, original_child, original_previous = source_context(
                        child, child_locator(parent_locator, index), previous_child
                    )
                    source = source_rule(
                        original_child, content, child_at, original_previous
                    )
                    mark = markup_rule(
                        original_child, content, child_at, original_previous
                    )
                    if source is None or source.action != "skip":
                        if set(child.get(EPUB_TYPE, "").split()) & omitted_types:
                            if mark is not None:
                                raise EpubBlocksError(
                                    f"{child_at}: retained markup is also semantically omitted"
                                )
                            previous_child = child
                            continue
                        if mark is not None and mark.kind == "milestone":
                            return True
                        if nested_milestone(child, child_at):
                            return True
                    previous_child = child
                return False

            if nested_milestone(current, at):
                raise EpubBlocksError(
                    f"{at}: overlapping milestone rules would discard a nested milestone"
                )
            label = current.get(rule.label_attribute) if rule.label_attribute else None
            if rule.label_attribute and (label is None or not label):
                raise EpubBlocksError(
                    f"{at}: milestone label attribute {rule.label_attribute!r} is missing or empty"
                )
            return [RichText("milestone", rule.name, label, locator=at)]
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
        if rule is not None:
            return [RichText("span", rule.name, children=tuple(merged), locator=at)]
        return merged

    return RichText(children=tuple(visit(element, locator, previous, True) or ()))


def normalize_rich_text(tree: RichText, options: NormalizationOptions) -> RichText:
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

    def collect(node: RichText) -> None:
        for child in node.children:
            if isinstance(child, str):
                leaves.append(child)
            else:
                collect(child)

    collect(tree)
    raw = "".join(leaves)
    start = len(raw) - len(raw.lstrip()) if options.strip else 0
    end = len(raw.rstrip()) if options.strip else len(raw)
    position = 0
    last_space = False
    whitespace_normalized: list[str] = []
    for leaf in leaves:
        chars: list[str] = []
        for char in leaf:
            keep = start <= position < end
            position += 1
            if not keep:
                continue
            if options.collapse_whitespace and char.isspace():
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

    return rebuild(tree)


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
    trees: Sequence[RichText], separator: str, options: NormalizationOptions
) -> RichText:
    children: list[str | RichText] = []
    for index, tree in enumerate(trees):
        if index:
            children.append(separator)
        children.extend(tree.children)
    return normalize_rich_text(RichText(children=tuple(children)), options)


def render_rich_text(tree: RichText, markup: MarkupOptions | None) -> str:
    if markup is None:
        return plain_text(tree)
    pairs = dict(markup.delimiters)
    reserved = {"\\"} | {
        char for pair in pairs.values() for token in pair for char in token
    }

    def escape(value: str, *, attribute: bool = False) -> str:
        if markup.format == "xml":
            if _INVALID_XML_CHARACTER.search(value):
                raise EpubBlocksError(
                    "markup contains a character not allowed in XML 1.0"
                )
            # XML parsers normalize literal CRs in text and all literal
            # whitespace in attributes. Character references preserve them.
            escaped = html.escape(value, quote=attribute).replace("\r", "&#13;")
            if attribute:
                escaped = escaped.replace("\t", "&#9;").replace("\n", "&#10;")
            return escaped
        return "".join("\\" + char if char in reserved else char for char in value)

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
