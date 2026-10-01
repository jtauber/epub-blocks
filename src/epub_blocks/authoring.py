"""Experimental, dependency-free helpers for interactive recipe authoring.

Discovery suggests structure, never editorial intent. Generated recipes use the
normal version-1 compiler; no wizard state is needed to execute them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from .errors import EpubBlocksError
from .extract import extract_blocks
from .models import EpubPackage, ExtractedBlock, TextBlock
from .package import inspect_epub
from .recipe import (
    compile_recipe,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_candidates,
)

KINDS = (
    "paragraph",
    "heading",
    "quotation",
    "preformatted",
    "scene-break",
    "line-start",
    "line",
    "skip",
)
MARKUP = ("delimiters", "xml", "plain")
SESSION_MAX_BYTES = 8 * 1024 * 1024


def pattern_key(block: TextBlock) -> str:
    """Identify a candidate's tag and exact class set without text matching."""
    return json.dumps([block.tag, sorted(block.classes)], ensure_ascii=False)


def literal_glob(value: str) -> str:
    """Quote path characters interpreted by the recipe's glob matcher."""
    return "".join({"[": "[[]", "*": "[*]", "?": "[?]"}.get(c, c) for c in value)


def file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@dataclass(frozen=True)
class Inventory:
    """Validated source identity and candidate blocks in spine order."""

    path: Path
    sha256: str
    package: EpubPackage
    blocks: tuple[TextBlock, ...]

    @classmethod
    def read(cls, path: Path) -> Inventory:
        path = path.resolve()
        digest = file_digest(path)
        package = inspect_epub(path)
        if not package.spine:
            raise EpubBlocksError("Wizard needs at least one XHTML spine document")
        paths = [document.path.casefold() for document in package.spine]
        if len(paths) != len(set(paths)):
            raise EpubBlocksError("Wizard needs unique, case-distinct spine paths")
        blocks = extract_blocks(
            path, include_non_linear=True, omit_epub_types=frozenset()
        )
        if file_digest(path) != digest:
            raise EpubBlocksError("EPUB changed during inspection; start again")
        return cls(path, digest, package, tuple(blocks))


@dataclass
class AuthoringState:
    """User decisions; unknown patterns remain explicitly unresolved."""

    selected: list[str]
    groups: dict[str, str]
    decisions: dict[str, str] = field(default_factory=dict[str, str])
    overrides: dict[str, dict[str, str]] = field(
        default_factory=dict[str, dict[str, str]]
    )
    markup: str = "delimiters"
    omit_noterefs: bool = False
    omit_pagebreaks: bool = False

    @classmethod
    def initial(cls, inventory: Inventory) -> AuthoringState:
        return cls(
            selected=[
                d.path
                for d in inventory.package.spine
                if d.linear and "nav" not in d.properties
            ],
            groups={d.path: f"d{d.position:02d}" for d in inventory.package.spine},
        )

    def choice(self, block: TextBlock) -> str | None:
        key = pattern_key(block)
        return self.overrides.get(block.document_path, {}).get(
            key, self.decisions.get(key)
        )

    def selected_blocks(self, inventory: Inventory) -> list[TextBlock]:
        return [b for b in inventory.blocks if b.document_path in self.selected]

    def validate(self, inventory: Inventory) -> None:
        paths = {d.path for d in inventory.package.spine}
        patterns = {pattern_key(b) for b in inventory.blocks}
        if (
            len(set(self.selected)) != len(self.selected)
            or not set(self.selected) <= paths
        ):
            raise EpubBlocksError("Unknown or duplicate document selection")
        if set(self.groups) != paths or any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", v) is None
            for v in self.groups.values()
        ):
            raise EpubBlocksError(
                "Section codes must use letters, numbers, dots, underscores or hyphens"
            )
        if self.markup not in MARKUP:
            raise EpubBlocksError("Unknown markup format")
        if (
            type(self.omit_noterefs) is not bool
            or type(self.omit_pagebreaks) is not bool
        ):
            raise EpubBlocksError("Omission settings must be booleans")
        if not set(self.overrides) <= paths:
            raise EpubBlocksError("Unknown document override")
        for choices in [self.decisions, *self.overrides.values()]:
            if not set(choices) <= patterns or any(
                v not in KINDS for v in choices.values()
            ):
                raise EpubBlocksError("Unknown pattern or block classification")

    def to_json(self, inventory: Inventory) -> dict[str, object]:
        self.validate(inventory)
        return {
            "wizard_version": 1,
            "epub_sha256": inventory.sha256,
            "selected": self.selected,
            "groups": self.groups,
            "decisions": self.decisions,
            "overrides": self.overrides,
            "markup": self.markup,
            "omit_noterefs": self.omit_noterefs,
            "omit_pagebreaks": self.omit_pagebreaks,
        }

    @classmethod
    def from_json(cls, raw: bytes, inventory: Inventory) -> AuthoringState:
        try:
            if len(raw) > SESSION_MAX_BYTES:
                raise ValueError("wizard session exceeds 8 MiB")
            value = cast(object, json.loads(raw))
            if not isinstance(value, dict):
                raise TypeError("expected an object")
            obj = cast(dict[str, object], value)
            expected = set(cls.initial(inventory).to_json(inventory))
            if (
                set(obj) != expected
                or type(obj["wizard_version"]) is not int
                or obj["wizard_version"] != 1
            ):
                raise ValueError("unsupported wizard state")
            if obj["epub_sha256"] != inventory.sha256:
                raise ValueError("state belongs to a different EPUB")
            selected = obj["selected"]
            if not isinstance(selected, list) or not all(
                isinstance(p, str) for p in cast(list[object], selected)
            ):
                raise ValueError("invalid document selection")

            def string_map(value: object) -> dict[str, str]:
                if not isinstance(value, dict):
                    raise TypeError("expected a string map")
                mapping = cast(dict[object, object], value)
                if not all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in mapping.items()
                ):
                    raise ValueError("expected a string map")
                return cast(dict[str, str], mapping)

            overrides = obj["overrides"]
            if not isinstance(overrides, dict):
                raise TypeError("invalid overrides")
            per_document: dict[str, dict[str, str]] = {}
            for key, item in cast(dict[object, object], overrides).items():
                if not isinstance(key, str):
                    raise TypeError("invalid override path")
                per_document[key] = string_map(item)
            if not isinstance(obj["markup"], str):
                raise TypeError("invalid markup format")
            state = cls(
                cast(list[str], selected),
                string_map(obj["groups"]),
                string_map(obj["decisions"]),
                per_document,
                obj["markup"],
                cast(bool, obj["omit_noterefs"]),
                cast(bool, obj["omit_pagebreaks"]),
            )
            state.validate(inventory)
            return state
        except (
            TypeError,
            ValueError,
            UnicodeError,
            RecursionError,
            EpubBlocksError,
        ) as error:
            raise EpubBlocksError(f"Cannot resume wizard: {error}") from error


def build_recipe(inventory: Inventory, state: AuthoringState) -> dict[str, object]:
    """Create an unpinned recipe only after every in-scope pattern is classified."""
    state.validate(inventory)
    blocks = state.selected_blocks(inventory)
    if not state.selected or not blocks:
        raise EpubBlocksError(
            "Select at least one document containing candidate blocks"
        )
    pin: dict[str, object] = {"sha256": inventory.sha256}
    if inventory.package.identifiers:
        pin["identifier"] = min(inventory.package.identifiers)
    rules: list[dict[str, object]] = []
    # Specific document decisions precede general rules, like ordinary recipes.
    for document, choices in [*state.overrides.items(), (None, state.decisions)]:
        for key, kind in sorted(choices.items()):
            if kind == "skip":
                continue
            tag, classes = cast(tuple[str, list[str]], json.loads(key))
            match: dict[str, object] = {"tag": tag, "classes": classes}
            if document is not None:
                match["locators"] = [literal_glob(document) + "#*"]
            rules.append(
                {
                    "match": match,
                    "type": "line" if kind.startswith("line") else kind,
                    "role": kind if kind.startswith("line") else "block",
                }
            )
    output: dict[str, object] = {
        "groups": {
            "source_pattern": r"^s[0-9]+:(.+)#[0-9.]+$",
            "source_map": {
                d.path: state.groups[d.path]
                for d in inventory.package.spine
                if d.path in state.selected
            },
        },
        "identifiers": {
            "block": {"template": "{group}.{number:03d}", "start": 1},
            "line": {"template": "{group}.{block:03d}.{number:02d}", "start": 1},
        },
        "default": {"type": "paragraph", "role": "block"},
        "rules": rules,
        "skip_source": [],
    }
    omitted: list[str] = []
    if state.omit_noterefs:
        omitted.append("noteref")
    if state.omit_pagebreaks:
        omitted.append("pagebreak")
    recipe: dict[str, object] = {
        "recipe_version": "1",
        "metadata": {"status": "wizard-draft"},
        "epub": pin,
        "normalization": {
            "collapse_whitespace": True,
            "strip": True,
            "unicode_normalization": "NFC",
        },
        "omit_epub_types": omitted,
        "source_blocks": {
            "include_documents": [
                literal_glob(d.path)
                for d in inventory.package.spine
                if d.path in state.selected
            ],
            "include_non_linear": any(
                not d.linear and d.path in state.selected
                for d in inventory.package.spine
            ),
            "strict_coverage": True,
            # Source skips precede inline markup matching. The semantic omission
            # alone would conflict with the preset's broad emphasis/pre rules.
            "element_rules": [
                {"match": {"epub_types": [kind]}, "action": "skip"} for kind in omitted
            ],
        },
        "output": output,
    }
    retained = extract_recipe_candidates(inventory.path, recipe)
    has_pre = any(b.tag == "pre" and state.choice(b) != "skip" for b in retained)
    if has_pre and state.markup == "plain":
        raise EpubBlocksError(
            "Use marked output to preserve preformatted whitespace; plain TSV cannot contain literal newlines or tabs"
        )
    if state.markup != "plain":
        markup_rules: list[dict[str, object]] = [
            {"match": {"tag": tag}, "kind": "span", "name": name}
            for tag, name in [
                ("em", "em"),
                ("i", "em"),
                ("strong", "strong"),
                ("b", "strong"),
            ]
        ]
        delimiters = {"em": ["⧼", "⧽"], "strong": ["⟪", "⟫"]}
        if has_pre:
            markup_rules.append(
                {
                    "match": {"tag": "pre"},
                    "kind": "span",
                    "name": "pre",
                    "preserve_whitespace": True,
                }
            )
            delimiters["pre"] = ["⟦", "⟧"]
        recipe["text"] = {
            "markup": {
                "format": state.markup,
                "rules": markup_rules,
                "delimiters": delimiters,
            }
        }
    # Omissions can remove whole candidates, including candidates marked skip.
    # Classify and skip only candidates that survive the recipe's text policy.
    candidates = extract_recipe_candidates(inventory.path, recipe)
    unresolved = {pattern_key(b) for b in candidates if state.choice(b) is None}
    if unresolved:
        raise EpubBlocksError(
            f"Classify {len(unresolved)} unresolved pattern(s) before previewing"
        )
    output["skip_source"] = [b.locator for b in candidates if state.choice(b) == "skip"]
    return recipe


def preview_recipe(
    inventory: Inventory, state: AuthoringState
) -> tuple[dict[str, object], list[ExtractedBlock]]:
    """Compile, pin and execute with the same checks as the normal extractor."""
    recipe = build_recipe(inventory, state)
    compiled = compile_recipe(inventory.path, recipe, verify_digest=False)
    cast(dict[str, object], recipe["output"])["compiled_sha256"] = (
        compiled_recipe_digest(compiled)
    )
    records = extract_recipe(inventory.path, recipe)
    if not records:
        raise EpubBlocksError("These decisions produce no output blocks")
    if any(
        any(c in field for c in "\t\r\n")
        for b in records
        for field in (b.block_id, b.block_type, b.text)
    ):
        raise EpubBlocksError(
            "Output contains characters that cannot be written as literal TSV"
        )
    return recipe, records


@contextmanager
def _save_lock(path: Path) -> Generator[None]:
    """Serialize cooperating writers, including other processes, without waiting.

    An atomic directory creation works without platform-specific dependencies.
    Never remove another writer's lock; after an abnormal exit its owner must
    verify no save is running and remove the stale directory explicitly.
    """
    key = hashlib.sha256(path.name.casefold().encode("utf-8")).hexdigest()
    lock = path.parent / f".epub-blocks-save-{key}.lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as error:
        raise EpubBlocksError(
            f"Another save owns {lock}; retry after it finishes. If a previous process crashed, verify no save is running before removing that lock directory."
        ) from error
    try:
        yield
    finally:
        lock.rmdir()


def _check_previous(path: Path, expected: bytes | None) -> None:
    if path.is_symlink():
        raise EpubBlocksError(f"Refusing to replace a symbolic link: {path}")
    if expected is not None and path.read_bytes() != expected:
        raise EpubBlocksError(f"File changed outside this wizard: {path}")


def save_json(
    path: Path,
    value: dict[str, object],
    *,
    expected: bytes | None = None,
    max_bytes: int | None = None,
) -> bytes:
    """Atomically save with a cross-process writer lock and optimistic checks.

    The lock serializes wizard writers. External editors do not honor this lock;
    their changes are checked before writing and again immediately before commit,
    but portable filesystems offer no atomic compare-and-replace for such editors.
    """
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if max_bytes is not None and len(payload) > max_bytes:
        raise EpubBlocksError(
            f"JSON exceeds the {max_bytes}-byte save limit; existing file unchanged"
        )
    path = path.parent.resolve() / path.name
    path.parent.mkdir(parents=True, exist_ok=True)
    with _save_lock(path):
        return _save_payload(path, payload, expected)


def _save_payload(path: Path, payload: bytes, expected: bytes | None) -> bytes:
    _check_previous(path, expected)
    mode = stat.S_IMODE(path.stat().st_mode) if expected is not None else 0o600
    stream = tempfile.NamedTemporaryFile(  # noqa: SIM115 -- entered below, inside cleanup guard
        dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    temporary = Path(stream.name)
    try:
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        _check_previous(path, expected)
        if expected is None:
            os.link(temporary, path)
        else:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return payload
