# Contributing

`epub-blocks` requires Python 3.13 or later and uses
[uv](https://docs.astral.sh/uv/) for its development environment.

## Set up the project

```bash
git clone https://github.com/jtauber/epub-blocks.git
cd epub-blocks
uv sync
```

## Check a change

Run the same checks used by continuous integration:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run coverage erase
uv run coverage run -m unittest discover -s tests
uv run coverage report
uv run python -m build
uv run twine check --strict dist/*
uv run pyright --verifytypes epub_blocks --ignoreexternal
```

New behavior and bug fixes should include tests. Tests create conforming EPUB
fixtures in temporary directories; do not commit copyrighted EPUB files.

Please open an issue before proposing an incompatible recipe-format or public
API change. Recipe readers intentionally reject unknown fields, so an
incompatible semantic change needs a new recipe version.

See the
[release checklist](https://github.com/jtauber/epub-blocks/blob/main/docs/releasing.md)
for the packaging process.
