"""Synthetic authoring and headless terminal tests; no copyrighted sources."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from rich.text import Text
from test_epub_blocks import AUXILIARY, PACKAGE, make_epub
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Select,
    SelectionList,
    Static,
    TabbedContent,
    TextArea,
)
from textual.widgets.data_table import RowKey
from textual.widgets.select import InvalidSelectValueError
from textual.worker import WorkerCancelled

from epub_blocks import EpubBlocksError, ExtractedBlock, extract_recipe, write_tsv
from epub_blocks._wizard_tui import (  # pyright: ignore[reportPrivateUsage]
    RecipeWizard,
    display,
)
from epub_blocks.authoring import (
    SESSION_MAX_BYTES,
    AuthoringState,
    Inventory,
    build_recipe,
    literal_glob,
    pattern_key,
    preview_recipe,
    save_json,
)
from epub_blocks.package import matches
from epub_blocks.wizard import main


def document(body: str) -> str:
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        + body
        + "</body></html>"
    )


def fixture(
    directory: Path, body: str | None = None, *, package: str = PACKAGE
) -> Inventory:
    return Inventory.read(
        make_epub(
            directory,
            documents={
                "text/chapter.xhtml": document(
                    body
                    or '<h1>Chapter One</h1><p>A <em>small</em> story.</p><p class="verse-first">First line</p><p class="verse">Second line</p><p class="caption">A picture</p>'
                ),
                "text/aux.xhtml": AUXILIARY,
            },
            package=package,
        )
    )


def classified(inventory: Inventory) -> AuthoringState:
    state = AuthoringState.initial(inventory)
    for b in inventory.blocks:
        state.decisions[pattern_key(b)] = (
            "heading"
            if b.tag == "h1"
            else "preformatted"
            if b.tag == "pre"
            else "line-start"
            if "verse-first" in b.classes
            else "line"
            if "verse" in b.classes
            else "skip"
            if "caption" in b.classes
            else "paragraph"
        )
    return state


class AuthoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.inventory = fixture(self.directory)
        self.state = classified(self.inventory)

    def test_plain_recipe_numbering_and_pins(self) -> None:
        self.state.markup = "plain"
        self.state.groups["text/chapter.xhtml"] = "01"
        recipe, records = preview_recipe(self.inventory, self.state)
        self.assertEqual(
            records,
            [
                ExtractedBlock("01.001", "heading", "Chapter One"),
                ExtractedBlock("01.002", "paragraph", "A small story."),
                ExtractedBlock("01.003.01", "line", "First line"),
                ExtractedBlock("01.003.02", "line", "Second line"),
            ],
        )
        self.assertEqual(extract_recipe(self.inventory.path, recipe), records)
        self.assertEqual(recipe["recipe_version"], "1")
        output = cast(dict[str, object], recipe["output"])
        self.assertEqual(len(str(output["compiled_sha256"])), 64)
        self.assertNotIn("First line", json.dumps(recipe))
        destination = self.directory / "out.tsv"
        write_tsv(destination, records)
        self.assertEqual(
            destination.read_text().splitlines()[1], "01.002\tparagraph\tA small story."
        )

    def test_markup_modes(self) -> None:
        for mode, expected in [
            ("delimiters", "A ⧼small⧽ story."),
            ("xml", "A <em>small</em> story."),
        ]:
            with self.subTest(mode=mode):
                self.state.markup = mode
                self.assertEqual(
                    preview_recipe(self.inventory, self.state)[1][1].text, expected
                )

    def test_document_overrides_shared_counters_and_skips(self) -> None:
        self.state.selected.append("text/aux.xhtml")
        self.state.groups = dict.fromkeys(self.state.groups, "01")
        key = pattern_key(self.inventory.blocks[-1])
        self.state.overrides["text/aux.xhtml"] = {key: "quotation"}
        self.assertEqual(
            preview_recipe(self.inventory, self.state)[1][-1],
            ExtractedBlock("01.004", "quotation", "Auxiliary material."),
        )
        self.state.overrides["text/aux.xhtml"][key] = "skip"
        self.assertEqual(len(preview_recipe(self.inventory, self.state)[1]), 4)

    def test_omissions_remove_candidates_without_unused_skip(self) -> None:
        inventory = fixture(
            self.directory,
            '<p>Text <a epub:type="noteref">1</a><span epub:type="pagebreak">9</span></p><p epub:type="noteref" class="caption">Note call</p><p epub:type="pagebreak" class="unresolved">10</p>',
        )
        state = classified(inventory)
        state.decisions.pop('["p", ["unresolved"]]')
        state.omit_noterefs = state.omit_pagebreaks = True
        recipe, rows = preview_recipe(inventory, state)
        self.assertEqual([b.text for b in rows], ["Text"])
        self.assertEqual(cast(dict[str, object], recipe["output"])["skip_source"], [])

    def test_preformatted_whitespace(self) -> None:
        inventory = fixture(self.directory, "<pre>  first\n\tsecond</pre>")
        state = classified(inventory)
        for mode, expected in [
            ("delimiters", "⟦  first\\n\\tsecond⟧"),
            ("xml", "<pre>  first&#10;&#9;second</pre>"),
        ]:
            state.markup = mode
            self.assertEqual(preview_recipe(inventory, state)[1][0].text, expected)
        state.markup = "plain"
        with self.assertRaisesRegex(EpubBlocksError, "preformatted"):
            preview_recipe(inventory, state)

    def test_omissions_take_precedence_over_markup_presets(self) -> None:
        inventory = fixture(
            self.directory,
            """
            <p>Text<em epub:type="noteref">1</em> after
            <strong epub:type="pagebreak">9</strong> with <i>emphasis</i>.</p>
            <pre epub:type="pagebreak">  10\n  </pre>
            <div epub:type="noteref"><pre>omitted ancestor</pre></div>
            <p epub:type="noteref pagebreak">both semantics</p>
        """,
        )
        for mode, emphasized in [
            ("plain", "emphasis"),
            ("xml", "<em>emphasis</em>"),
            ("delimiters", "⧼emphasis⧽"),
        ]:
            with self.subTest(mode=mode):
                state = classified(inventory)
                state.markup = mode
                state.omit_noterefs = state.omit_pagebreaks = True
                state.decisions.pop('["pre", []]')
                recipe, rows = preview_recipe(inventory, state)
                self.assertEqual(
                    [b.text for b in rows], [f"Text after with {emphasized}."]
                )
                self.assertEqual(extract_recipe(inventory.path, recipe), rows)

    def test_omissions_do_not_remove_other_semantics_or_retained_pre(self) -> None:
        inventory = fixture(
            self.directory,
            '<p>Text <em epub:type="noteref">1</em><b epub:type="pagebreak">9</b></p><pre>  keep\n this</pre>',
        )
        for omit_notes, omit_pages, expected in [
            (True, False, "Text ⟪9⟫"),
            (False, True, "Text ⧼1⧽"),
        ]:
            with self.subTest(notes=omit_notes, pages=omit_pages):
                state = classified(inventory)
                state.omit_noterefs, state.omit_pagebreaks = omit_notes, omit_pages
                rows = preview_recipe(inventory, state)[1]
                self.assertEqual(rows[0].text, expected)
                self.assertEqual(rows[1].text, "⟦  keep\\n this⟧")
                state.markup = "plain"
                with self.assertRaisesRegex(EpubBlocksError, "preformatted"):
                    preview_recipe(inventory, state)

    def test_incomplete_empty_or_invalid_output(self) -> None:
        with self.assertRaisesRegex(EpubBlocksError, "unresolved"):
            build_recipe(self.inventory, AuthoringState.initial(self.inventory))
        self.state.selected.clear()
        with self.assertRaisesRegex(EpubBlocksError, "Select at least"):
            build_recipe(self.inventory, self.state)
        self.state = classified(self.inventory)
        self.state.decisions = dict.fromkeys(self.state.decisions, "skip")
        with self.assertRaises(EpubBlocksError):
            preview_recipe(self.inventory, self.state)
        self.state = classified(self.inventory)
        for rows, message in [
            ([], "no output"),
            ([ExtractedBlock("01", "paragraph", "bad\tfield")], "literal TSV"),
        ]:
            with (
                patch("epub_blocks.authoring.extract_recipe", return_value=rows),
                self.assertRaisesRegex(EpubBlocksError, message),
            ):
                preview_recipe(self.inventory, self.state)
        self.state.decisions['["p", ["verse-first"]]'] = "line"
        with self.assertRaises(EpubBlocksError):
            preview_recipe(self.inventory, self.state)

    def test_strict_coverage_refuses_unclaimed_text(self) -> None:
        inventory = fixture(
            self.directory, "<p>Okay</p><div>Not a default candidate</div>"
        )
        with self.assertRaisesRegex(EpubBlocksError, "unclaimed"):
            preview_recipe(inventory, classified(inventory))

    def test_source_change_is_detected(self) -> None:
        make_epub(self.directory)
        with self.assertRaisesRegex(EpubBlocksError, "SHA-256"):
            preview_recipe(self.inventory, self.state)
        with (
            patch("epub_blocks.authoring.file_digest", side_effect=["a", "b"]),
            self.assertRaisesRegex(EpubBlocksError, "changed during inspection"),
        ):
            Inventory.read(self.inventory.path)

    def test_inventory_rejects_empty_or_ambiguous_spine(self) -> None:
        package = self.inventory.package
        for spine, message in [
            ((), "at least one"),
            ((package.spine[0], package.spine[0]), "unique"),
        ]:
            with (
                patch(
                    "epub_blocks.authoring.inspect_epub",
                    return_value=replace(package, spine=spine),
                ),
                self.assertRaisesRegex(EpubBlocksError, message),
            ):
                Inventory.read(self.inventory.path)
        no_ids = fixture(
            self.directory,
            package=PACKAGE.replace(
                '<dc:identifier id="pub-id">sample-edition</dc:identifier>', ""
            ),
        )
        self.assertNotIn(
            "identifier",
            cast(dict[str, object], build_recipe(no_ids, classified(no_ids))["epub"]),
        )

    def test_literal_globs_and_controls(self) -> None:
        path = "path/[book]*?.xhtml"
        self.assertTrue(matches(path, [literal_glob(path)]))
        self.assertFalse(matches("path/bad.xhtml", [literal_glob(path)]))
        self.assertEqual(display("[red]text\x1b\t\n"), "[red]text\\x1b\\t\n")

    def test_session_roundtrip_and_validation(self) -> None:
        self.state.overrides["text/aux.xhtml"] = {'["p", []]': "quotation"}
        obj = self.state.to_json(self.inventory)
        self.assertEqual(
            AuthoringState.from_json(json.dumps(obj).encode(), self.inventory),
            self.state,
        )
        bad_values: list[object] = [[], {"wizard_version": 1}]
        mutations: list[tuple[str, object]] = [
            ("wizard_version", True),
            ("epub_sha256", "wrong"),
            ("selected", None),
            ("selected", [42]),
            ("selected", ["missing"]),
            ("selected", self.state.selected * 2),
            ("groups", []),
            ("groups", {"text/chapter.xhtml": 42}),
            ("groups", {}),
            ("groups", dict.fromkeys(self.state.groups, "bad code")),
            ("overrides", []),
            ("overrides", {"unknown": {}}),
            ("overrides", {"text/aux.xhtml": {"unknown": "paragraph"}}),
            ("markup", 42),
            ("markup", "unknown"),
            ("omit_noterefs", 1),
            ("omit_pagebreaks", None),
            ("decisions", {"unknown": "paragraph"}),
            ("decisions", {'["p", []]': "bogus"}),
        ]
        for name, value in mutations:
            candidate = deepcopy(obj)
            candidate[name] = value
            bad_values.append(candidate)
        for value in bad_values:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(EpubBlocksError, "Cannot resume"),
            ):
                AuthoringState.from_json(json.dumps(value).encode(), self.inventory)
        for raw in [b"bad JSON", b"\xff"]:
            with self.assertRaises(EpubBlocksError):
                AuthoringState.from_json(raw, self.inventory)
        # Non-string JSON keys cannot arise through json.loads; test the guard.
        invalid = deepcopy(obj)
        invalid["overrides"] = {42: {}}
        with (
            patch("epub_blocks.authoring.json.loads", return_value=invalid),
            self.assertRaisesRegex(EpubBlocksError, "override path"),
        ):
            AuthoringState.from_json(b"{}", self.inventory)

    def test_atomic_private_writes_and_external_edits(self) -> None:
        path = self.directory / "nested" / "recipe.json"
        raw = save_json(path, {"first": "é"})
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            save_json(path, {})
        path.chmod(0o640)
        updated = save_json(path, {"second": True}, expected=raw)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        with self.assertRaisesRegex(EpubBlocksError, "changed outside"):
            save_json(path, {}, expected=raw)
        self.assertEqual(path.read_bytes(), updated)
        link = self.directory / "link.json"
        link.symlink_to(path)
        with self.assertRaisesRegex(EpubBlocksError, "symbolic link"):
            save_json(link, {})
        for target in ["tempfile.NamedTemporaryFile", "os.fsync"]:
            with (
                patch(
                    "epub_blocks.authoring." + target, side_effect=OSError("disk error")
                ),
                self.assertRaises(OSError),
            ):
                save_json(path, {}, expected=updated)
            self.assertEqual(path.read_bytes(), updated)
        self.assertEqual(list(path.parent.glob(".*")), [])

    def test_concurrent_writers_and_processes_cannot_both_commit(self) -> None:
        path = self.directory / "shared.json"
        original = save_json(path, {"writer": "original"})
        started, release = threading.Event(), threading.Event()
        fsync = os.fsync

        def hold_save(fd: int) -> None:
            fsync(fd)
            started.set()
            if not release.wait(timeout=10):
                raise AssertionError("test did not release its writer")

        child = """
import sys
from pathlib import Path
from epub_blocks.authoring import save_json
from epub_blocks.errors import EpubBlocksError
try:
    save_json(Path(sys.argv[1]), {"writer": "child"})
except EpubBlocksError as error:
    print(error)
    sys.exit(2)
"""
        with (
            patch("epub_blocks.authoring.os.fsync", side_effect=hold_save),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            pending = pool.submit(
                save_json, path, {"writer": "first"}, expected=original
            )
            try:
                self.assertTrue(started.wait(timeout=10))
                with self.assertRaisesRegex(EpubBlocksError, "Another save"):
                    save_json(path, {"writer": "second"}, expected=original)
                other = subprocess.run(
                    [sys.executable, "-c", child, str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(other.returncode, 2, other.stderr)
                self.assertIn("Another save", other.stdout)
                self.assertEqual(path.read_bytes(), original)
            finally:
                release.set()
            updated = pending.result(timeout=10)
        self.assertEqual(path.read_bytes(), updated)
        self.assertEqual(list(self.directory.glob(".*")), [])
        with self.assertRaisesRegex(EpubBlocksError, "changed outside"):
            save_json(path, {"writer": "second retry"}, expected=original)

    def test_external_changes_during_write_are_detected(self) -> None:
        path = self.directory / "external.json"
        original = save_json(path, {"value": "original"})
        fsync = os.fsync

        def edit_during_write(fd: int) -> None:
            fsync(fd)
            path.write_bytes(b"external editor's replacement")

        with (
            patch("epub_blocks.authoring.os.fsync", side_effect=edit_during_write),
            self.assertRaisesRegex(EpubBlocksError, "changed outside"),
        ):
            save_json(path, {"value": "wizard"}, expected=original)
        self.assertEqual(path.read_bytes(), b"external editor's replacement")
        self.assertEqual(list(self.directory.glob(".*")), [])

    def test_save_limits_count_utf8_bytes_and_preserve_previous_file(self) -> None:
        path = self.directory / "limited.json"
        value: dict[str, object] = {"text": "é"}
        size = len((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
        raw = save_json(path, value, max_bytes=size)
        self.assertEqual(len(raw), size)
        with self.assertRaisesRegex(EpubBlocksError, "save limit"):
            save_json(path, value, expected=raw, max_bytes=size - 1)
        self.assertEqual(path.read_bytes(), raw)
        new = self.directory / "not-created" / "state.json"
        with self.assertRaisesRegex(EpubBlocksError, "save limit"):
            save_json(new, value, max_bytes=1)
        self.assertFalse(new.parent.exists())
        with self.assertRaisesRegex(EpubBlocksError, "8 MiB"):
            AuthoringState.from_json(b" " * (SESSION_MAX_BYTES + 1), self.inventory)

    def test_deep_session_json_has_normal_domain_error(self) -> None:
        with self.assertRaisesRegex(EpubBlocksError, "Cannot resume"):
            AuthoringState.from_json(
                b"[" * 20_000 + b"0" + b"]" * 20_000, self.inventory
            )


class WizardCliTests(unittest.TestCase):
    def test_help_without_optional_dependency(self) -> None:
        with (
            patch(
                "epub_blocks.wizard.importlib.import_module",
                side_effect=AssertionError("must not import"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main(["--help"])
        self.assertEqual(error.exception.code, 0)

    def test_missing_dependency_message(self) -> None:
        stderr = io.StringIO()
        with (
            patch(
                "epub_blocks.wizard.importlib.import_module",
                side_effect=ModuleNotFoundError(name="textual"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(main(["book.epub", "recipe.json"]), 2)
        self.assertIn("epub-blocks[wizard]", stderr.getvalue())
        with (
            patch(
                "epub_blocks.wizard.importlib.import_module",
                side_effect=ModuleNotFoundError(name="other"),
            ),
            self.assertRaises(ModuleNotFoundError),
        ):
            main(["book.epub", "recipe.json"])

    def test_cli_new_resume_and_collisions(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(RecipeWizard, "run") as run,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            root = Path(tmp)
            inventory = fixture(root)
            destination = root / "draft.json"
            session = root / "draft.json.wizard.json"
            args = [str(inventory.path), str(destination)]
            self.assertEqual(main(args), 0)
            run.assert_called_once()
            save_json(session, classified(inventory).to_json(inventory))
            self.assertEqual(main(args), 2)
            self.assertEqual(main([*args, "--resume"]), 0)
            self.assertEqual(main([*args, "--session", str(session), "--resume"]), 0)
            self.assertEqual(main([*args, "--session", str(destination)]), 2)
            self.assertEqual(main([*args, "--session", str(inventory.path)]), 2)
            self.assertEqual(
                main([*args, "--session", str(root / "missing"), "--resume"]), 2
            )
            with (
                patch(
                    "epub_blocks.wizard.Path.open",
                    return_value=io.BytesIO(b" " * (SESSION_MAX_BYTES + 1)),
                ),
                patch("epub_blocks.wizard.Inventory.read", return_value=inventory),
            ):
                self.assertEqual(main([*args, "--resume"]), 2)
            save_json(destination, {})
            self.assertEqual(main(args), 2)
            self.assertEqual(main([str(inventory.path), str(inventory.path)]), 2)
            destination.unlink()
            destination.symlink_to(root / "absent")
            self.assertEqual(main(args), 2)
            destination.unlink()
            session.unlink()
            session.symlink_to(root / "absent")
            self.assertEqual(main(args), 2)

    def test_large_valid_saved_session_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents: dict[str, str | bytes] = {
                f"text/chapter-{i:03d}.xhtml": document(
                    "".join(
                        f'<p class="paragraph-style-{j:03d}">Paragraph {j} in chapter {i}.</p>'
                        for j in range(50)
                    )
                )
                for i in range(500)
            }
            package = (
                '<package xmlns="http://www.idpf.org/2007/opf"><metadata/><manifest>'
                + "".join(
                    f'<item id="d{i}" href="{path}" media-type="application/xhtml+xml"/>'
                    for i, path in enumerate(documents)
                )
                + "</manifest><spine>"
                + "".join(f'<itemref idref="d{i}"/>' for i in range(500))
                + "</spine></package>"
            )
            inventory = Inventory.read(
                make_epub(root, package=package, documents=documents)
            )
            state = AuthoringState.initial(inventory)
            for block in inventory.blocks:
                state.overrides.setdefault(block.document_path, {})[
                    pattern_key(block)
                ] = "paragraph"
            session = root / "saved.json"
            raw = save_json(
                session, state.to_json(inventory), max_bytes=SESSION_MAX_BYTES
            )
            self.assertGreater(len(raw), 1_000_000)
            self.assertEqual(AuthoringState.from_json(raw, inventory), state)
            with patch.object(RecipeWizard, "run") as run:
                self.assertEqual(
                    main(
                        [
                            str(inventory.path),
                            str(root / "new.json"),
                            "--resume",
                            "--session",
                            str(session),
                        ]
                    ),
                    0,
                )
                self.assertEqual(run.call_count, 1)

    def test_deep_session_cli_returns_two_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory = fixture(root)
            session = root / "deep.json"
            session.write_bytes(b"[" * 20_000 + b"0" + b"]" * 20_000)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                status = main(
                    [
                        str(inventory.path),
                        str(root / "new.json"),
                        "--resume",
                        "--session",
                        str(session),
                    ]
                )
            self.assertEqual(status, 2)
            self.assertIn("Cannot resume", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_actual_textual_failure_propagates_exit_status(self) -> None:
        run = RecipeWizard.run
        observed: list[int | None] = []

        def run_headless(app: RecipeWizard) -> None:
            run(app, headless=True)
            observed.append(app.return_code)

        def broken_mount(_app: RecipeWizard) -> None:
            raise RuntimeError("deliberate UI failure")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(RecipeWizard, "run", run_headless),
            patch.object(RecipeWizard, "on_mount", broken_mount),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            root = Path(tmp)
            inventory = fixture(root)
            self.assertEqual(main([str(inventory.path), str(root / "draft.json")]), 1)
        self.assertEqual(observed, [1])


class WizardUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2

    async def finish_preview(self) -> None:
        # Textual's WorkerManager leaves its Worker generic unparameterized.
        await self.app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.inventory = fixture(self.directory)
        self.state = AuthoringState.initial(self.inventory)
        self.destination = self.directory / "draft.json"
        self.session = self.directory / "draft.json.wizard.json"
        self.app = RecipeWizard(
            self.inventory, self.state, self.destination, self.session
        )

    def select(self, selector: str) -> Select[str]:
        return cast(Select[str], self.app.query_one(selector, Select))

    def status(self) -> str:
        return str(self.app.query_one("#status", Static).content)

    def reference_position(self) -> str:
        return str(self.app.query_one("#reference-position", Static).content)

    async def test_reference_navigation_buttons_shortcuts_and_dropdown(self) -> None:
        names = ["first", "excluded", "middle", "last"]
        package = (
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata/><manifest>'
            + "".join(
                f'<item id="{name}" href="{name}.xhtml" media-type="application/xhtml+xml"/>'
                for name in names
            )
            + "</manifest><spine>"
            + "".join(f'<itemref idref="{name}"/>' for name in names)
            + "</spine></package>"
        )
        self.inventory = Inventory.read(
            make_epub(
                self.directory,
                package=package,
                documents={
                    f"{name}.xhtml": document(f"<p>{name}</p>") for name in names
                },
            )
        )
        self.state = AuthoringState.initial(self.inventory)
        self.state.selected.remove("excluded.xhtml")
        self.app = RecipeWizard(
            self.inventory, self.state, self.destination, self.session
        )
        self.assertFalse(self.app.check_action("next_reference", ()))
        async with self.app.run_test(size=(80, 30)) as pilot:
            tabs = self.app.query_one(TabbedContent)
            references = self.select("#group-document")
            code = self.app.query_one("#group-code", Input)
            previous = self.app.query_one("#previous-reference", Button)
            following = self.app.query_one("#next-reference", Button)
            await pilot.press("alt+right")
            self.assertEqual(references.selection, "first.xhtml")
            tabs.active = "references"
            await pilot.pause()
            code.focus()
            code.value = "01"
            self.assertEqual(self.reference_position(), "File 1 of 3")
            self.assertTrue(previous.disabled)
            self.assertFalse(following.disabled)
            await pilot.press("alt+left")
            self.assertEqual(references.selection, "first.xhtml")
            await pilot.press("alt+right")
            self.assertEqual(references.selection, "middle.xhtml")
            self.assertEqual(self.reference_position(), "File 2 of 3")
            self.assertFalse(previous.disabled)
            self.assertFalse(following.disabled)
            self.assertIs(self.app.focused, code)
            self.assertEqual(code.value, "d03")
            code.cursor_position = len(code.value)
            cursor = code.cursor_position
            await pilot.press("left")
            self.assertEqual(code.cursor_position, cursor - 1)
            self.assertEqual(references.selection, "middle.xhtml")
            code.value = "02"
            self.assertTrue(await pilot.click("#next-reference"))
            self.assertEqual(references.selection, "last.xhtml")
            self.assertEqual(self.reference_position(), "File 3 of 3")
            self.assertFalse(previous.disabled)
            self.assertTrue(following.disabled)
            self.assertIs(self.app.focused, code)
            await pilot.press("alt+right")
            self.assertEqual(references.selection, "last.xhtml")
            self.assertTrue(await pilot.click("#previous-reference"))
            self.assertEqual(references.selection, "middle.xhtml")
            self.assertEqual(code.value, "02")
            await pilot.press("alt+left")
            self.assertEqual(references.selection, "first.xhtml")
            self.assertEqual(code.value, "01")
            # No event-loop pause: edits after moving must belong to the new file.
            code.value = "01a"
            self.app.action_next_reference()
            code.value = "02a"
            await pilot.pause()
            self.assertEqual(self.state.groups["first.xhtml"], "01a")
            self.assertEqual(self.state.groups["middle.xhtml"], "02a")
            self.assertEqual(self.state.groups["excluded.xhtml"], "d02")
            self.app.suggest()
            await self.app.validate_preview().wait()
            preview = self.app.preview
            self.assertIsNotNone(preview)
            references.value = "last.xhtml"
            await pilot.pause()
            self.assertEqual(self.reference_position(), "File 3 of 3")
            references.focus()
            references.expanded = True
            await pilot.pause()
            await pilot.press("alt+left")
            self.assertFalse(references.expanded)
            self.assertEqual(references.selection, "middle.xhtml")
            self.assertIs(self.app.focused, code)
            self.assertIs(self.app.preview, preview)
            self.assertFalse(self.app.query_one("#save", Button).disabled)
            tabs.active = "markup"
            await pilot.pause()
            await pilot.press("alt+left", "alt+right")
            self.assertEqual(references.selection, "middle.xhtml")

    async def test_reference_navigation_with_one_empty_or_blank_selection(self) -> None:
        async with self.app.run_test(size=(80, 30)) as pilot:
            self.app.query_one(TabbedContent).active = "references"
            await pilot.pause()
            references = self.select("#group-document")
            previous = self.app.query_one("#previous-reference", Button)
            following = self.app.query_one("#next-reference", Button)
            self.assertEqual(self.reference_position(), "File 1 of 1")
            self.assertTrue(previous.disabled)
            self.assertTrue(following.disabled)
            await pilot.press("alt+left", "alt+right")
            self.assertEqual(references.selection, "text/chapter.xhtml")
            references.clear()
            await pilot.pause()
            self.assertEqual(self.reference_position(), "Choose a file (1 available)")
            await pilot.press("alt+left", "alt+right")
            self.assertIsNone(references.selection)
            self.assertTrue(previous.disabled)
            self.assertTrue(following.disabled)
            scope = cast(
                SelectionList[str], self.app.query_one("#documents", SelectionList)
            )
            scope.deselect_all()
            await pilot.pause()
            self.assertEqual(self.reference_position(), "No files selected")
            await pilot.press("alt+left", "alt+right")
            self.assertIsNone(references.selection)
            self.assertTrue(previous.disabled)
            self.assertTrue(following.disabled)
            scope.select("text/aux.xhtml")
            await pilot.pause()
            self.assertEqual(self.reference_position(), "File 1 of 1")
            scope.select("text/chapter.xhtml")
            await pilot.pause()
            self.assertEqual(self.reference_position(), "File 2 of 2")
            self.assertFalse(previous.disabled)
            self.assertTrue(following.disabled)
            scope.deselect("text/aux.xhtml")
            await pilot.pause()
            self.assertEqual(self.reference_position(), "File 1 of 1")
            self.assertTrue(previous.disabled)
            self.assertTrue(following.disabled)

    async def test_reference_documents_follow_scope_and_preserve_codes(self) -> None:
        async with self.app.run_test(size=(100, 40)) as pilot:
            references = self.select("#group-document")
            code = self.app.query_one("#group-code", Input)
            scope = cast(
                SelectionList[str], self.app.query_one("#documents", SelectionList)
            )
            self.assertEqual(references.selection, "text/chapter.xhtml")
            self.assertEqual(code.value, "d01")
            with self.assertRaises(InvalidSelectValueError):
                references.value = "text/aux.xhtml"
            code.value = "01"
            scope.select("text/aux.xhtml")
            await pilot.pause()
            self.assertEqual(references.selection, "text/chapter.xhtml")
            self.assertEqual(code.value, "01")
            references.value = "text/aux.xhtml"
            await pilot.pause()
            code.value = "ap"
            # Deselect before the input's changed event is delivered.
            scope.deselect("text/aux.xhtml")
            await pilot.pause()
            self.assertEqual(references.selection, "text/chapter.xhtml")
            self.assertEqual(code.value, "01")
            self.assertEqual(self.state.groups["text/aux.xhtml"], "ap")
            with self.assertRaises(InvalidSelectValueError):
                references.value = "text/aux.xhtml"
            scope.deselect_all()
            await pilot.pause()
            self.assertIsNone(references.selection)
            self.assertTrue(references.disabled)
            self.assertTrue(code.disabled)
            self.assertEqual(code.value, "")
            self.assertEqual(self.state.groups["text/chapter.xhtml"], "01")
            scope.select("text/aux.xhtml")
            await pilot.pause()
            self.assertEqual(references.selection, "text/aux.xhtml")
            self.assertEqual(code.value, "ap")
            self.assertFalse(references.disabled)
            self.assertFalse(code.disabled)
            scope.select("text/chapter.xhtml")
            await pilot.pause()
            self.assertEqual(references.selection, "text/aux.xhtml")
            self.assertEqual(code.value, "ap")
            self.assertEqual(
                self.app.reference_documents(), ["text/chapter.xhtml", "text/aux.xhtml"]
            )

    async def test_references_start_with_resumed_scope_not_first_spine_file(
        self,
    ) -> None:
        self.state.selected = ["text/aux.xhtml"]
        self.state.groups["text/aux.xhtml"] = "ap"
        async with self.app.run_test(size=(100, 40)):
            references = self.select("#group-document")
            self.assertEqual(references.selection, "text/aux.xhtml")
            self.assertEqual(self.app.query_one("#group-code", Input).value, "ap")
            with self.assertRaises(InvalidSelectValueError):
                references.value = "text/chapter.xhtml"

    async def test_empty_resumed_scope_disables_reference_controls(self) -> None:
        self.state.selected.clear()
        async with self.app.run_test(size=(100, 40)) as pilot:
            references = self.select("#group-document")
            code = self.app.query_one("#group-code", Input)
            self.assertIsNone(references.selection)
            self.assertTrue(references.disabled)
            self.assertTrue(code.disabled)
            self.assertEqual(code.value, "")
            scope = cast(
                SelectionList[str], self.app.query_one("#documents", SelectionList)
            )
            scope.select("text/chapter.xhtml")
            await pilot.pause()
            self.assertEqual(references.selection, "text/chapter.xhtml")
            self.assertEqual(code.value, "d01")
            self.assertFalse(references.disabled)
            self.assertFalse(code.disabled)

    async def test_queued_code_edits_keep_their_document_owner(self) -> None:
        self.state.selected.append("text/aux.xhtml")
        async with self.app.run_test(size=(100, 40)) as pilot:
            code = self.app.query_one("#group-code", Input)
            documents = self.select("#group-document")
            code.value = "01"
            documents.value = "text/aux.xhtml"
            # Deliberately do not give either widget's queued event time to run.
            self.assertEqual(self.state.groups["text/chapter.xhtml"], "01")
            self.assertEqual(self.state.groups["text/aux.xhtml"], "d02")
            await pilot.pause()
            self.assertEqual(code.value, "d02")
            code.value = "ap"
            code.value = "ap2"
            documents.value = "text/chapter.xhtml"
            await pilot.pause()
            self.assertEqual(code.value, "01")
            self.assertEqual(self.state.groups["text/aux.xhtml"], "ap2")
            self.app.suggest()
            self.app.validate_preview()
            await self.finish_preview()
            preview = self.app.preview
            self.assertIsNotNone(preview)
            documents.value = "text/aux.xhtml"
            await pilot.pause()
            self.assertIs(self.app.preview, preview)
            self.assertEqual(code.value, "ap2")
            self.assertFalse(self.app.query_one("#save", Button).disabled)

    async def test_validation_never_overlaps_even_after_waiter_cancellation(
        self,
    ) -> None:
        started, release = threading.Event(), threading.Event()
        calls = 0

        def held_preview(
            inventory: Inventory, state: AuthoringState
        ) -> tuple[dict[str, object], list[ExtractedBlock]]:
            nonlocal calls
            calls += 1
            started.set()
            if not release.wait(timeout=10):
                raise AssertionError("test did not release validation")
            return preview_recipe(inventory, state)

        async with self.app.run_test(size=(100, 40)) as pilot:
            self.app.suggest()
            await pilot.pause()
            with patch(
                "epub_blocks._wizard_tui.preview_recipe", side_effect=held_preview
            ):
                worker = self.app.validate_preview()
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 10))
                    self.assertTrue(self.app.query_one("#validate", Button).disabled)
                    retries = [self.app.validate_preview() for _ in range(3)]
                    for retry in retries:
                        await retry.wait()
                    self.assertIn("already running", self.status())
                    worker.cancel()
                    with self.assertRaises(WorkerCancelled):
                        await worker.wait()
                    await self.app.validate_preview().wait()
                    self.assertEqual(calls, 1)
                    self.assertTrue(self.app.query_one("#validate", Button).disabled)
                finally:
                    release.set()
                    if self.app.validation_task is not None:
                        await asyncio.wait_for(self.app.validation_task, 10)
                await pilot.pause()
                self.assertIsNone(self.app.preview)
                self.assertFalse(self.app.query_one("#validate", Button).disabled)
                await self.app.validate_preview().wait()
                self.assertEqual(calls, 2)
                self.assertIsNotNone(self.app.preview)

    async def test_cancelled_validation_failure_is_observed_after_quit(self) -> None:
        started, release = threading.Event(), threading.Event()

        def failing_preview(
            _inventory: Inventory, _state: AuthoringState
        ) -> tuple[dict[str, object], list[ExtractedBlock]]:
            started.set()
            if not release.wait(timeout=10):
                raise AssertionError("test did not release validation")
            raise EpubBlocksError("failure after cancellation")

        with (
            patch(
                "epub_blocks._wizard_tui.preview_recipe", side_effect=failing_preview
            ),
            patch.object(
                asyncio.get_running_loop(), "call_exception_handler"
            ) as handler,
        ):
            try:
                async with self.app.run_test(size=(100, 40)) as pilot:
                    self.app.suggest()
                    await pilot.pause()
                    worker = self.app.validate_preview()
                    self.assertTrue(await asyncio.to_thread(started.wait, 10))
                    self.app.quit_pressed()
                with self.assertRaises(WorkerCancelled):
                    await worker.wait()
            finally:
                release.set()
            task = self.app.validation_task
            assert task is not None
            with self.assertRaisesRegex(EpubBlocksError, "after cancellation"):
                await asyncio.wait_for(task, 10)
            await asyncio.sleep(0)  # Completion callbacks must be safe after unmount.
            handler.assert_not_called()

    async def test_highlight_without_preview_or_row_key_is_ignored(self) -> None:
        async with self.app.run_test(size=(100, 40)):
            table = cast(DataTable[Text], self.app.query_one("#records", DataTable))
            detail = self.app.query_one("#record-text", TextArea)
            detail.load_text("Keep existing detail")
            self.app.record_highlighted(DataTable.RowHighlighted(table, 0, RowKey("0")))
            self.app.preview = ({}, [ExtractedBlock("001", "paragraph", "New detail")])
            self.app.record_highlighted(
                DataTable.RowHighlighted(table, 0, RowKey(None))
            )
            self.assertEqual(detail.text, "Keep existing detail")
            self.app.record_highlighted(DataTable.RowHighlighted(table, 0, RowKey("0")))
            self.assertEqual(detail.text, "New detail")

    async def test_cancelled_background_task_reenables_validation(self) -> None:
        started = asyncio.Event()

        async def pending(*_args: object) -> None:
            started.set()
            await asyncio.Event().wait()

        async with self.app.run_test(size=(100, 40)) as pilot:
            self.app.suggest()
            await pilot.pause()
            with patch(
                "epub_blocks._wizard_tui.asyncio.to_thread", side_effect=pending
            ):
                worker = self.app.validate_preview()
                await asyncio.wait_for(started.wait(), 10)
                assert self.app.validation_task is not None
                self.app.validation_task.cancel()
                with self.assertRaises(WorkerCancelled):
                    await worker.wait()
            await pilot.pause()
            self.assertFalse(self.app.query_one("#validate", Button).disabled)
            await self.app.validate_preview().wait()
            self.assertIsNotNone(self.app.preview)

    async def test_progress_save_uses_resume_limit_without_damaging_previous_save(
        self,
    ) -> None:
        async with self.app.run_test(size=(100, 40)) as pilot:
            self.app.action_save_progress()
            previous = self.session.read_bytes()
            with patch("epub_blocks._wizard_tui.SESSION_MAX_BYTES", len(previous)):
                self.app.action_save_progress()
                self.assertIn("Progress saved", self.status())
                self.app.query_one("#group-code", Input).value = "longer-code"
                await pilot.pause()
                self.app.action_save_progress()
                self.assertIn("save limit", self.status())
                self.assertEqual(self.session.read_bytes(), previous)
                self.assertEqual(self.app.session_bytes, previous)
            self.app.action_save_progress()
            self.assertEqual(
                AuthoringState.from_json(self.session.read_bytes(), self.inventory),
                self.state,
            )

    async def test_full_workflow(self) -> None:
        self.state.selected.append("text/aux.xhtml")
        async with self.app.run_test(size=(120, 45)) as pilot:
            self.assertTrue(self.app.query_one("#save", Button).disabled)
            self.app.query_one(TabbedContent).active = "classification"
            await pilot.pause()
            await pilot.click("#suggest")
            self.assertTrue(self.state.decisions)
            self.app.current_pattern = '["p", ["verse-first"]]'
            self.select("#kind").value = "line-start"
            self.app.apply_choice()
            await pilot.pause()
            self.app.current_pattern = '["p", ["verse"]]'
            self.select("#kind").value = "line"
            self.app.apply_choice()
            await pilot.pause()
            self.assertEqual(self.app.current_pattern, '["p", ["verse"]]')
            self.app.query_one(TabbedContent).active = "references"
            await pilot.pause()
            self.app.query_one("#group-code", Input).value = "01"
            await pilot.pause()
            self.assertEqual(self.state.groups["text/chapter.xhtml"], "01")
            self.select("#group-document").value = "text/aux.xhtml"
            await pilot.pause()
            self.assertEqual(self.app.query_one("#group-code", Input).value, "d02")
            self.assertEqual(self.state.groups["text/chapter.xhtml"], "01")
            self.app.query_one(TabbedContent).active = "preview-tab"
            await pilot.pause()
            await pilot.click("#validate")
            await self.finish_preview()
            await pilot.pause()
            self.assertIn("Validated", self.status())
            self.assertIn("d", self.app.query_one("#recipe-json", TextArea).text)
            self.assertFalse(self.app.query_one("#save", Button).disabled)
            self.assertEqual(
                self.app.query_one("#record-text", TextArea).text, "Chapter One"
            )
            await pilot.click("#save")
            self.assertTrue(self.destination.exists())
            await pilot.press("ctrl+s")
            self.assertTrue(self.session.exists())
            self.assertEqual(
                AuthoringState.from_json(self.session.read_bytes(), self.inventory),
                self.state,
            )
            self.app.query_one("#omit-noterefs", Checkbox).value = True
            await pilot.pause()
            self.assertIsNone(self.app.preview)
            self.assertTrue(self.app.query_one("#save", Button).disabled)
            self.app.save_recipe()
            self.assertIn("Validate the current", self.status())
            await pilot.click("#quit")

    async def test_empty_scope_validation_and_overrides(self) -> None:
        async with self.app.run_test(size=(100, 40)) as pilot:
            self.app.scope_changed()  # An unchanged selection must not invalidate state.
            self.select("#group-document").clear()
            await pilot.pause()
            self.assertTrue(self.app.query_one("#group-code", Input).disabled)
            self.app.validate_preview()
            await self.finish_preview()
            self.assertIn("unresolved", self.status())
            self.app.suggest()
            await pilot.pause()
            self.select("#rule-scope").value = "text/chapter.xhtml"
            self.app.current_pattern = '["h1", []]'
            self.select("#kind").value = "skip"
            self.app.apply_choice()
            await pilot.pause()
            self.assertEqual(
                self.state.overrides["text/chapter.xhtml"]['["h1", []]'], "skip"
            )
            self.select("#kind").value = "unresolved"
            self.app.apply_choice()
            await pilot.pause()
            self.assertEqual(self.state.overrides["text/chapter.xhtml"], {})
            self.select("#markup-format").value = "xml"
            self.app.query_one("#omit-pagebreaks", Checkbox).value = True
            await pilot.pause()
            self.assertEqual(self.state.markup, "xml")
            self.assertTrue(self.state.omit_pagebreaks)
            documents = cast(
                SelectionList[str], self.app.query_one("#documents", SelectionList)
            )
            documents.deselect_all()
            await pilot.pause()
            self.assertEqual(self.state.selected, [])
            self.app.apply_choice()
            self.app.validate_preview()
            await self.finish_preview()
            self.assertIn("Select at least", self.status())

    async def test_external_writes_and_stale_preview(self) -> None:
        async with self.app.run_test(size=(100, 40)) as pilot:
            self.app.suggest()
            await pilot.pause()
            self.app.validate_preview()
            await self.finish_preview()
            self.app.save_recipe()
            self.app.save_recipe()
            self.app.action_save_progress()
            self.app.action_save_progress()
            self.assertIn("saved", self.status())
            for path, method in [
                (self.destination, self.app.save_recipe),
                (self.session, self.app.action_save_progress),
            ]:
                path.write_text("external edit")
                method()
                self.assertIn("changed outside", self.status())
                self.assertEqual(path.read_text(), "external edit")

            async def delayed(
                *_args: object,
            ) -> tuple[dict[str, object], list[ExtractedBlock]]:
                self.state.markup = "plain"
                await asyncio.sleep(0)
                return {}, []

            with patch(
                "epub_blocks._wizard_tui.asyncio.to_thread", side_effect=delayed
            ):
                self.app.validate_preview()
                await self.finish_preview()
            self.assertIn("changed while validating", self.status())
            self.assertIsNone(self.app.preview)

    async def test_small_terminal_keeps_examples_accessible(self) -> None:
        async with self.app.run_test(size=(80, 30)) as pilot:
            self.app.query_one(TabbedContent).active = "classification"
            await pilot.pause()
            self.assertTrue(await pilot.click("#suggest"))
            examples = self.app.query_one("#examples", TextArea)
            examples.focus()
            await pilot.pause()
            self.assertLessEqual(
                examples.region.bottom, self.app.query_one("#progress", Button).region.y
            )
            self.assertIn("Chapter One", examples.text)
