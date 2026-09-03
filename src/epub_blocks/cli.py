from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .errors import EpubBlocksError
from .recipe import extract_recipe_file, write_tsv


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply a recipe to an EPUB and write structured text as TSV."
    )
    parser.add_argument("epub", type=Path)
    parser.add_argument("recipe", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    args = parser.parse_args()

    try:
        blocks = extract_recipe_file(args.epub, args.recipe)
        write_tsv(args.output, blocks)
    except (EpubBlocksError, OSError) as error:
        print(f"epub-blocks: error: {error}", file=sys.stderr)
        return 2
    print(f"{len(blocks)} blocks -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
