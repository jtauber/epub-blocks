"""Optional terminal recipe wizard entry point (Textual is loaded lazily)."""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence
from pathlib import Path

from .authoring import SESSION_MAX_BYTES, AuthoringState, Inventory
from .errors import EpubBlocksError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Interactively author a draft EPUB extraction recipe."
    )
    parser.add_argument("epub", type=Path)
    parser.add_argument(
        "recipe",
        type=Path,
        help="new recipe destination (never overwritten on startup)",
    )
    parser.add_argument(
        "--resume", action="store_true", help="resume RECIPE.wizard.json decisions"
    )
    parser.add_argument(
        "--session",
        type=Path,
        help="custom progress file (use with --resume to export to a new destination)",
    )
    args = parser.parse_args(argv)
    try:
        importlib.import_module("textual")

        from ._wizard_tui import RecipeWizard
    except ModuleNotFoundError as error:
        if error.name != "textual":
            raise
        print(
            'Install the optional interface with: pip install "epub-blocks[wizard]"',
            file=sys.stderr,
        )
        return 2
    try:
        destination: Path = args.recipe.absolute()
        session_path: Path = (
            args.session.absolute()
            if args.session is not None
            else destination.with_name(destination.name + ".wizard.json")
        )
        if session_path.is_symlink() or session_path.resolve() == destination.resolve():
            raise EpubBlocksError("Session must be a distinct, non-symlink path")
        if destination.exists() or destination.is_symlink():
            raise EpubBlocksError(
                "Recipe destination already exists; choose a new filename"
            )
        if args.epub.resolve() in {destination.resolve(), session_path.resolve()}:
            raise EpubBlocksError("Output paths must not replace the EPUB")
        if session_path.exists() and not args.resume:
            raise EpubBlocksError(
                "Saved decisions exist; use --resume or choose a new destination"
            )
        inventory = Inventory.read(args.epub)
        raw = None
        if args.resume:
            with session_path.open("rb") as stream:
                raw = stream.read(SESSION_MAX_BYTES + 1)
        state = (
            AuthoringState.initial(inventory)
            if raw is None
            else AuthoringState.from_json(raw, inventory)
        )
        app = RecipeWizard(inventory, state, destination, session_path, raw)
        app.run()
        return app.return_code or 0
    except (EpubBlocksError, OSError) as error:
        print(f"epub-blocks-wizard: error: {error}", file=sys.stderr)
        return 2
