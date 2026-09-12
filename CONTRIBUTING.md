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

New behavior and bug fixes should include tests. Statement and branch coverage
must remain at 100%, including defensive guards. Focused internal-invariant
tests complement public-API tests where earlier validation normally makes a
failure path unreachable. Tests create conforming EPUB
fixtures in temporary directories; do not commit copyrighted EPUB files.
Release-workflow tests parse the workflow with the development-only PyYAML
dependency and exercise its publishing commands against a local fake GitHub
CLI; they never contact GitHub or PyPI. They require Bash and `diff` (available
on the CI runners). With Node.js on `PATH`, the whitespace tests also check
the schema's regular expression in JavaScript; that cross-engine test is
skipped locally when Node.js is absent but required in CI.

Please open an issue before proposing an incompatible recipe-format or public
API change. During the current pre-1.0 phase, with no known external recipe
users, an agreed incompatible change may retain `recipe_version: "1"`.
The 0.3.0 redesign follows this policy: existing recipes must be regenerated,
and the changelog records the break. Recipe authors should pin the package
version as well as the source EPUB. Once external users depend on the format,
incompatible changes will require an explicit format-version and migration
decision.

See the
[release checklist](https://github.com/jtauber/epub-blocks/blob/main/docs/releasing.md)
for the packaging process.
