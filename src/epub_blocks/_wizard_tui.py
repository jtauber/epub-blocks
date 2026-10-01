"""Textual adapter for recipe authoring; never imported by the extractor."""

from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import ClassVar, cast

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from .authoring import (
    KINDS,
    SESSION_MAX_BYTES,
    AuthoringState,
    Inventory,
    pattern_key,
    preview_recipe,
    save_json,
)
from .errors import EpubBlocksError
from .models import ExtractedBlock


def display(value: str) -> str:
    """Do not interpret source strings as terminal controls or Rich markup."""
    return "".join(c if c.isprintable() or c == "\n" else repr(c)[1:-1] for c in value)


class SectionCodeInput(Input):
    """Commit input to its owning document before asynchronous event delivery.

    Switching the document only displays another stored code; it must not turn
    a delayed edit or a programmatic reload into an edit of that other document.
    """

    def __init__(self, codes: dict[str, str], document: str | None) -> None:
        self.codes = codes
        self.document = document
        self._reloading_code = False
        super().__init__(
            value=codes[document] if document is not None else "",
            disabled=document is None,
            id="group-code",
        )

    def show_document(self, document: str | None) -> None:
        self.document = document
        self.disabled = document is None
        self._reloading_code = True
        try:
            self.value = self.codes[document] if document is not None else ""
        finally:
            self._reloading_code = False

    def post_message(self, message: Message) -> bool:
        if isinstance(message, Input.Changed):
            if (
                self.document is None
                or self._reloading_code
                or self.codes[self.document] == message.value
            ):
                return False
            self.codes[self.document] = message.value
        return super().post_message(message)


class RecipeWizard(App[None]):
    TITLE = "epub-blocks · Recipe wizard"
    CSS = """
    Screen { min-width: 60; }
    TabbedContent { height: 1fr; }
    TabbedContent > ContentSwitcher { height: 1fr; }
    TabPane { height: 1fr; padding: 1; overflow-y: auto; }
    .help { height: auto; margin-bottom: 1; }
    .split { height: 1fr; }
    #documents { width: 1fr; height: 1fr; }
    #document-preview { width: 1fr; margin-left: 1; }
    #patterns { height: 1fr; min-height: 5; }
    #examples { height: 1fr; min-height: 5; }
    #records { height: 1fr; min-height: 5; }
    #record-text { height: 7; }
    Select, Input { margin-bottom: 1; }
    .actions { height: auto; }
    .actions Button { margin-right: 1; min-width: 12; }
    #status { height: auto; max-height: 5; padding: 0 1; }
    #recipe-json { height: 1fr; }
    #reference-navigation { height: 3; margin-bottom: 1; }
    #reference-navigation Button { width: 14; min-width: 14; }
    #reference-position { width: 1fr; height: 3; content-align: center middle; }
    """
    BINDINGS: ClassVar = [
        ("ctrl+s", "save_progress", "Save progress"),
        ("ctrl+q", "quit", "Quit (save first)"),
        Binding("alt+left", "previous_reference", "Previous file", priority=True),
        Binding("alt+right", "next_reference", "Next file", priority=True),
    ]

    def __init__(
        self,
        inventory: Inventory,
        state: AuthoringState,
        destination: Path,
        session_path: Path,
        session_bytes: bytes | None = None,
    ) -> None:
        super().__init__()
        self.inventory = inventory
        self.state = state
        self.destination = destination
        self.session_path = session_path
        self.session_bytes = session_bytes
        self.recipe_bytes: bytes | None = None
        self.preview: tuple[dict[str, object], list[ExtractedBlock]] | None = None
        self.preview_state: dict[str, object] | None = None
        self.current_pattern: str | None = None
        self.validation_task: (
            asyncio.Task[tuple[dict[str, object], list[ExtractedBlock]]] | None
        ) = None

    def compose(self) -> ComposeResult:
        reference_documents = self.reference_documents()
        yield Header()
        with TabbedContent():
            with TabPane("1 Scope", id="scope"):
                yield Static(
                    "Select source documents. Space toggles a document; arrows inspect it. Non-linear and navigation documents start unchecked.",
                    classes="help",
                )
                with Horizontal(classes="split"):
                    counts = Counter(b.document_path for b in self.inventory.blocks)
                    yield SelectionList[str](
                        *[
                            (
                                Text(
                                    display(
                                        f"{d.position:02d}  {d.path} ({counts[d.path]} blocks)"
                                    )
                                ),
                                d.path,
                                d.path in self.state.selected,
                            )
                            for d in self.inventory.package.spine
                        ],
                        id="documents",
                    )
                    yield TextArea(read_only=True, id="document-preview")
            with TabPane("2 References", id="references"), VerticalScroll():
                yield Static(
                    "Only documents selected in step 1 appear here. Each document starts a section. Codes begin as provisional d01, d02, etc., not inferred chapter numbers. Reuse a code to continue its counter across files. Headings share the block counter; verse uses a nested line counter. Multiple sections within one file need manual recipe editing.",
                    classes="help",
                )
                with Horizontal(id="reference-navigation"):
                    yield Button(
                        "← Previous",
                        id="previous-reference",
                        disabled=True,
                        tooltip="Previous selected file (Alt+Left)",
                    )
                    yield Static("", id="reference-position", markup=False)
                    yield Button(
                        "Next →",
                        id="next-reference",
                        disabled=True,
                        tooltip="Next selected file (Alt+Right)",
                    )
                yield Select[str](
                    [(Text(display(path)), path) for path in reference_documents],
                    value=next(iter(reference_documents), Select.NULL),
                    disabled=not reference_documents,
                    prompt="Choose a document selected in step 1",
                    id="group-document",
                )
                yield Label("Section code (edits apply immediately)")
                yield SectionCodeInput(
                    self.state.groups,
                    next(iter(reference_documents), None),
                )
            with TabPane("3 Patterns", id="classification"):
                yield Static(
                    "Review repeated tag/class patterns. A document-specific choice overrides the general choice. Line continuation requires a preceding line-start. Skip omits matching candidates, not whole documents.",
                    classes="help",
                )
                yield DataTable[Text](id="patterns", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("Suggest by tag", id="suggest")
                yield Select[str](
                    [("All selected documents", "*")]
                    + [
                        (Text(display(d.path)), d.path)
                        for d in self.inventory.package.spine
                    ],
                    value="*",
                    allow_blank=False,
                    id="rule-scope",
                )
                with Horizontal(classes="actions"):
                    yield Select[str](
                        [(k, k) for k in KINDS]
                        + [("Leave unresolved / clear override", "unresolved")],
                        value="paragraph",
                        allow_blank=False,
                        id="kind",
                    )
                    yield Button("Apply choice", id="apply")
                yield TextArea(read_only=True, id="examples")
            with TabPane("4 Markup", id="markup"), VerticalScroll():
                yield Static(
                    "Preserves em/i and strong/b, and whitespace inside pre elements. CSS-only styles, small caps, page-marker retention and empty scene breaks require manual recipe refinements. Ordinary prose uses NFC and collapsed whitespace. Plain mode is unavailable for retained pre blocks.",
                    classes="help",
                )
                yield Select[str](
                    [
                        ("Unicode delimiters (preferred)", "delimiters"),
                        ("XML fragments", "xml"),
                        ("Plain text", "plain"),
                    ],
                    value=self.state.markup,
                    allow_blank=False,
                    id="markup-format",
                )
                yield Checkbox(
                    "Omit elements explicitly marked epub:type=noteref",
                    value=self.state.omit_noterefs,
                    id="omit-noterefs",
                )
                yield Checkbox(
                    "Omit elements explicitly marked epub:type=pagebreak",
                    value=self.state.omit_pagebreaks,
                    id="omit-pagebreaks",
                )
            with TabPane("5 Preview", id="preview-tab"):
                yield Static(
                    "Compile and inspect before saving. Strict coverage checks selected documents, not omitted documents or image-only/empty structures. This is a draft, not a certification of editorial correctness.",
                    classes="help",
                )
                yield Button(
                    "Validate / refresh preview", id="validate", variant="primary"
                )
                yield DataTable[Text](id="records", cursor_type="row")
                yield TextArea(read_only=True, id="record-text")
            with TabPane("Recipe JSON", id="json-tab"):
                yield TextArea(read_only=True, id="recipe-json")
        yield Static(
            "Choose scope, review patterns, then validate. Nothing is saved automatically.",
            id="status",
            markup=False,
        )
        with Horizontal(classes="actions"):
            yield Button("Save progress", id="progress")
            yield Button("Save recipe", id="save", variant="success", disabled=True)
            yield Button("Quit", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#patterns", DataTable).add_columns("Pattern", "Count", "Choice")
        self.query_one("#records", DataTable).add_columns(
            "Reference", "Type", "Text (select for full block)"
        )
        self.refresh_patterns()
        self.group_document_changed()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action in {"previous_reference", "next_reference"}:
            if not self.screen_stack:
                return False
            tabs = next(iter(self.query(TabbedContent)), None)
            return tabs is not None and tabs.active == "references"
        return True

    @on(TabbedContent.TabActivated)
    def tab_changed(self) -> None:
        self.refresh_bindings()

    def status(self, message: str) -> None:
        self.query_one("#status", Static).update(display(message))

    def invalidate(self) -> None:
        self.preview = None
        self.preview_state = None
        self.query_one("#save", Button).disabled = True
        self.query_one("#records", DataTable).clear()
        self.query_one("#record-text", TextArea).load_text("")
        self.query_one("#recipe-json", TextArea).load_text("")
        self.status(
            "Decisions changed. Validate again before saving the recipe; save progress to resume later."
        )

    def refresh_patterns(self) -> None:
        table = cast(DataTable[Text], self.query_one("#patterns", DataTable))
        table.clear()
        grouped: dict[str, list[str]] = {}
        for block in self.state.selected_blocks(self.inventory):
            grouped.setdefault(pattern_key(block), []).append(
                self.state.choice(block) or "unresolved"
            )
        for key, kinds in grouped.items():
            choice = kinds[0] if len(set(kinds)) == 1 else "mixed / per document"
            table.add_row(
                Text(display(key)), Text(str(len(kinds))), Text(choice), key=key
            )
        if self.current_pattern not in grouped:
            self.current_pattern = next(iter(grouped), None)
        if self.current_pattern is not None:
            table.move_cursor(row=list(grouped).index(self.current_pattern))
        self.show_examples()

    def show_examples(self) -> None:
        scope = cast(Select[str], self.query_one("#rule-scope", Select)).value
        matches = [
            b
            for b in self.state.selected_blocks(self.inventory)
            if pattern_key(b) == self.current_pattern
            and (scope == "*" or b.document_path == scope)
        ]
        chosen = matches[:3] + matches[-3:] if len(matches) > 6 else matches
        text = f"{len(matches)} matching blocks; showing up to six examples.\n\n"
        text += "\n\n".join(
            f"{b.source_locator}\n[{self.state.choice(b) or 'unresolved'}]\n{b.text}"
            for b in chosen
        )
        self.query_one("#examples", TextArea).load_text(display(text))

    @on(SelectionList.SelectedChanged, "#documents")
    def scope_changed(self) -> None:
        selected = cast(
            SelectionList[str], self.query_one("#documents", SelectionList)
        ).selected
        if set(selected) != set(self.state.selected):
            self.state.selected = [
                d.path for d in self.inventory.package.spine if d.path in selected
            ]
            self.invalidate()
            self.refresh_reference_documents()
            self.refresh_patterns()

    def reference_documents(self) -> list[str]:
        """Only selected documents, always in source spine order."""
        return [
            d.path
            for d in self.inventory.package.spine
            if d.path in self.state.selected
        ]

    def refresh_reference_documents(self) -> None:
        documents = self.reference_documents()
        selector = cast(Select[str], self.query_one("#group-document", Select))
        previous = selector.selection
        selector.set_options([(Text(display(path)), path) for path in documents])
        selector.value = (
            previous if previous in documents else next(iter(documents), Select.NULL)
        )
        selector.disabled = not documents
        # Update the input owner now; queued selection events must not restore
        # a document removed from scope or an intermediate blank selection.
        self.group_document_changed()

    @on(SelectionList.SelectionHighlighted, "#documents")
    def document_highlighted(
        self, event: SelectionList.SelectionHighlighted[str]
    ) -> None:
        blocks = [
            b for b in self.inventory.blocks if b.document_path == event.selection.value
        ]
        text = "\n\n".join(
            f"{b.tag} {sorted(b.classes)}  #{b.element_path}\n{b.text}"
            for b in blocks[:5]
        )
        self.query_one("#document-preview", TextArea).load_text(
            display(
                text
                or "No default text candidates. Image-only or unusual structures need manual review."
            )
        )

    @on(Select.Changed, "#group-document")
    def group_document_changed(self) -> None:
        selector = cast(Select[str], self.query_one("#group-document", Select))
        self.query_one("#group-code", SectionCodeInput).show_document(
            selector.selection
        )
        documents = self.reference_documents()
        position = (
            documents.index(selector.selection)
            if selector.selection in documents
            else None
        )
        self.query_one("#reference-position", Static).update(
            f"File {position + 1} of {len(documents)}"
            if position is not None
            else f"Choose a file ({len(documents)} available)"
            if documents
            else "No files selected"
        )
        self.query_one("#previous-reference", Button).disabled = (
            position is None or position == 0
        )
        self.query_one("#next-reference", Button).disabled = (
            position is None or position == len(documents) - 1
        )

    @on(Button.Pressed, "#previous-reference")
    def action_previous_reference(self) -> None:
        self.move_reference(-1)

    @on(Button.Pressed, "#next-reference")
    def action_next_reference(self) -> None:
        self.move_reference(1)

    def move_reference(self, offset: int) -> None:
        documents = self.reference_documents()
        selector = cast(Select[str], self.query_one("#group-document", Select))
        if selector.selection not in documents:
            return
        index = documents.index(selector.selection) + offset
        if 0 <= index < len(documents):
            selector.expanded = False
            selector.value = documents[index]
            # Change owners synchronously so rapid edits cannot reach the old file.
            self.group_document_changed()
            self.query_one("#group-code", SectionCodeInput).focus()

    @on(Input.Changed, "#group-code")
    def group_code_changed(self) -> None:
        self.invalidate()

    @on(DataTable.RowHighlighted, "#patterns")
    def pattern_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.current_pattern = event.row_key.value
        self.show_examples()

    @on(Select.Changed, "#rule-scope")
    def rule_scope_changed(self) -> None:
        self.show_examples()

    @on(Button.Pressed, "#apply")
    def apply_choice(self) -> None:
        scope, kind = (
            cast(Select[str], self.query_one("#rule-scope", Select)).value,
            cast(Select[str], self.query_one("#kind", Select)).value,
        )
        if (
            self.current_pattern is None
            or not isinstance(scope, str)
            or not isinstance(kind, str)
        ):
            return
        choices = (
            self.state.decisions
            if scope == "*"
            else self.state.overrides.setdefault(scope, {})
        )
        if kind == "unresolved":
            choices.pop(self.current_pattern, None)
        else:
            choices[self.current_pattern] = kind
        self.invalidate()
        self.refresh_patterns()

    @on(Button.Pressed, "#suggest")
    def suggest(self) -> None:
        for b in self.state.selected_blocks(self.inventory):
            self.state.decisions.setdefault(
                pattern_key(b),
                "heading"
                if b.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
                else "preformatted"
                if b.tag == "pre"
                else "paragraph",
            )
        self.invalidate()
        self.refresh_patterns()
        self.status(
            "Applied tag-based suggestions to unresolved general patterns. Inspect verse, notes, captions and preformatted text; these are not inferred."
        )

    @on(Select.Changed, "#markup-format")
    def markup_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value != self.state.markup:
            self.state.markup = event.value
            self.invalidate()

    @on(Checkbox.Changed)
    def omission_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "omit-noterefs":
            self.state.omit_noterefs = event.value
        else:
            self.state.omit_pagebreaks = event.value
        self.invalidate()

    @on(Button.Pressed, "#validate")
    @work(exclusive=False)
    async def validate_preview(self) -> None:
        if self.validation_task is not None and not self.validation_task.done():
            self.status(
                "Validation is already running. Wait for it to finish before refreshing."
            )
            return
        self.invalidate()
        self.status(
            "Validating source identity, coverage, references and extracted output…"
        )
        try:
            state = deepcopy(self.state)
            snapshot = deepcopy(state.to_json(self.inventory))
            button = self.query_one("#validate", Button)
            button.disabled = True

            def finished(
                task: asyncio.Task[tuple[dict[str, object], list[ExtractedBlock]]],
            ) -> None:
                button.disabled = False
                # A cancelled UI waiter may never collect a thread's exception.
                # Observe it here too, without suppressing errors for live waiters.
                if not task.cancelled():
                    task.exception()

            # Shield the actual job from worker cancellation: a cancelled waiter
            # does not stop its thread. Keep tracking it so retries cannot overlap.
            self.validation_task = asyncio.create_task(
                asyncio.to_thread(preview_recipe, self.inventory, state)
            )
            self.validation_task.add_done_callback(finished)
            result = await asyncio.shield(self.validation_task)
            if self.state.to_json(self.inventory) != snapshot:
                self.status("Decisions changed while validating. Refresh the preview.")
                return
            self.preview, self.preview_state = result, snapshot
            table = cast(DataTable[Text], self.query_one("#records", DataTable))
            for i, block in enumerate(result[1][:200]):
                table.add_row(
                    Text(display(block.block_id)),
                    Text(block.block_type),
                    Text(display(block.text[:160])),
                    key=str(i),
                )
            import json

            self.query_one("#recipe-json", TextArea).load_text(
                json.dumps(result[0], ensure_ascii=False, indent=2)
            )
            self.query_one("#save", Button).disabled = False
            self.status(
                f"Validated {len(result[1])} blocks. Preview shows the first 200. Review before saving a draft recipe."
            )
        except (EpubBlocksError, OSError) as error:
            self.status(f"Validation needs attention: {error}")

    @on(DataTable.RowHighlighted, "#records")
    def record_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self.preview is not None and event.row_key.value is not None:
            self.query_one("#record-text", TextArea).load_text(
                display(self.preview[1][int(event.row_key.value)].text)
            )

    @on(Button.Pressed, "#progress")
    def action_save_progress(self) -> None:
        try:
            self.session_bytes = save_json(
                self.session_path,
                self.state.to_json(self.inventory),
                expected=self.session_bytes,
                max_bytes=SESSION_MAX_BYTES,
            )
            self.status(
                f"Progress saved: {self.session_path}. Resume with --resume. No EPUB prose is stored in the session."
            )
        except (EpubBlocksError, OSError) as error:
            self.status(f"Could not save progress: {error}")

    @on(Button.Pressed, "#save")
    def save_recipe(self) -> None:
        try:
            if self.preview is None or self.preview_state != self.state.to_json(
                self.inventory
            ):
                raise EpubBlocksError("Validate the current decisions before saving")
            self.recipe_bytes = save_json(
                self.destination, self.preview[0], expected=self.recipe_bytes
            )
            self.status(
                f"Draft recipe saved: {self.destination}. Run epub-blocks with this recipe to generate a TSV."
            )
        except (EpubBlocksError, OSError) as error:
            self.status(f"Could not save recipe: {error}")

    @on(Button.Pressed, "#quit")
    def quit_pressed(self) -> None:
        self.exit()
