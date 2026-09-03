# epub-blocks

`epub-blocks` is a small, dependency-free Python library for recipe-driven
extraction of ordered text blocks from EPUB 2 and EPUB 3 containers. It
also exposes locator-bearing source blocks so that consuming projects can
prepare and review recipes reproducibly.

It requires Python 3.13 or later.

This project is at an early 0.x stage. Its public API and recipe format may
evolve before 1.0.

## Installation

Install the 0.1.0 release directly from GitHub:

```bash
python -m pip install "git+https://github.com/jtauber/epub-blocks.git@v0.1.0"
```

For development, install from a local checkout:

```bash
python -m pip install .
```

The runtime package has no third-party dependencies.

## Scope

The package handles the reusable EPUB layer:

- locating and parsing the EPUB package document;
- reading identifiers and the XHTML spine;
- excluding auxiliary `linear="no"` spine items by default;
- selecting spine documents with case-insensitive glob patterns;
- extracting paragraph, heading, quotation, list-item, and preformatted text
  blocks with stable source locators;
- excluding document paths, element locators, CSS classes, page-breaks, and
  note references; and
- selecting, omitting, slicing, joining, and normalizing XHTML fragments;
- applying explicit extraction recipes to produce `id`, `type`, and `text`
  records; and
- writing those records as headerless TSV files.

It deliberately leaves the interpretation and downstream use of extracted
records to consuming projects.

## Example

```python
from pathlib import Path

from epub_blocks import extract_blocks

blocks = extract_blocks(
    Path("book.epub"),
    include_documents=["*chapter*.xhtml"],
    exclude_classes=["image-caption"],
)

for block in blocks:
    print(block.source_locator, block.tag, block.text)
```

`source_locator` has the form
`s008:text/chapter-01.xhtml#1.3.2`. Element-path components are one-based child
positions within the XHTML `body`.

## Extraction recipes

An extraction recipe pins the EPUB identity and explicitly maps output blocks
to XHTML fragments:

```json
{
  "recipe_version": "1",
  "epub": {
    "identifier": "9780000000000",
    "sha256": "f4f9c2d902a41b80732b2dce7ad01a57f615859c21417a5019e3cd8e4d271282"
  },
  "normalization": {
    "collapse_whitespace": true,
    "strip": true,
    "unicode_normalization": "NFC"
  },
  "omit_epub_types": ["noteref", "pagebreak"],
  "blocks": [
    {
      "id": "01.001",
      "type": "{p}",
      "parts": [
        {
          "document": "text/chapter-01.xhtml",
          "element_path": "1.3.2"
        }
      ]
    }
  ]
}
```

The command-line interface writes a three-column, headerless TSV containing
the identifier, type, and text of each extracted record:

```bash
epub-blocks book.epub recipe.json records.tsv
```

Identifiers and types are opaque strings chosen by the recipe. See the
[complete recipe format](https://github.com/jtauber/epub-blocks/blob/main/docs/recipe-format.md)
for the full field reference, locator and slicing semantics, normalization
order, validation rules, and worked examples. A strict
[JSON Schema](https://github.com/jtauber/epub-blocks/blob/main/schemas/recipe-v1.schema.json)
is also available for editor and pipeline integration.

## Safety and trust model

EPUB files are untrusted ZIP and XML input. The default extraction APIs bound
archive membership, individual and cumulative reads, compression ratios, XML
document size, element count, and nesting depth. Duplicate or unsafe archive
paths, encrypted members, external or internal DTDs, and entity declarations
are rejected. The declaration-only HTML5 `<!DOCTYPE html>` is permitted.
Custom positive limits can be supplied with `SafetyLimits` when a legitimate
book is larger than a default.

These checks reduce resource-exhaustion and ambiguity risks; they are not a
sandbox for arbitrary code. The package does not execute EPUB scripts or fetch
network resources.

## Public API

The supported import surface is the names exported by `epub_blocks`: the
result and option data classes, `SafetyLimits`, `inspect_epub`,
`extract_blocks`, the recipe-loading and extraction functions, `write_tsv`,
and `EpubBlocksError`. Helpers in submodules are implementation details and
may change during the pre-1.0 period.

## Development

```bash
uv sync
uv run coverage run -m unittest discover -s tests
uv run coverage report
uv run ruff check .
uv run pyright
uv run python -m build
uv run twine check dist/*
uv run pyright --verifytypes epub_blocks --ignoreexternal
```

Coverage includes branch measurement and enforces a 90% minimum. Run
`uv run coverage html` after the test command for a browsable report in
`htmlcov/`.

No EPUB files are committed to this repository. Tests construct small synthetic
EPUB containers in temporary directories.

See [CONTRIBUTING.md](https://github.com/jtauber/epub-blocks/blob/main/CONTRIBUTING.md)
for the complete check sequence and change policy, and
[CHANGELOG.md](https://github.com/jtauber/epub-blocks/blob/main/CHANGELOG.md)
for release notes. Bugs and proposals are tracked in the
[issue tracker](https://github.com/jtauber/epub-blocks/issues).

## License

`epub-blocks` is available under the [MIT License](LICENSE).
