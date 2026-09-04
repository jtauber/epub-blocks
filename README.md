# epub-blocks

`epub-blocks` is a dependency-free Python library for reproducibly extracting
ordered, structured text blocks from EPUB 2 and EPUB 3 files. A declarative
recipe selects an edition’s XHTML, assigns block types, and generates stable
identifiers without edition-specific Python code or a pre-existing output
table.

It requires Python 3.13 or later. The project is pre-1.0, so its public API and
recipe format may still make breaking changes.

## Installation

```bash
python -m pip install epub-blocks
```

The runtime package has no third-party dependencies.

## What it does

The package:

- safely reads EPUB package metadata and XHTML spine documents;
- selects source documents and blocks with case-insensitive globs;
- normalizes extracted text and omits configured EPUB semantic elements;
- derives groups from source paths, heading markers, or explicit transitions;
- generates flat and nested identifiers with per-group counters;
- assigns output types through ordered source-block rules;
- handles joins, splits, omissions, skipped blocks, and inserted material;
- pins both the EPUB and the compiled extraction plan with SHA-256; and
- writes headerless `id`, `type`, `text` TSV records.

A recipe is sufficient to generate a new base text. It does not need a
separately stored list of expected identifiers or types. Existing outputs may
be useful as test oracles while authoring a recipe, but they are not recipe
inputs and are not read by `epub-blocks`.

The package does not compare editions, select preferred readings, or apply
editorial corrections. Recipes describe source structure, not expected prose.

## Inspecting an EPUB

Use the lower-level API to discover source blocks before writing a recipe:

```python
from epub_blocks import extract_blocks

for block in extract_blocks(
    "book.epub",
    include_documents=["*chapter*.xhtml"],
    exclude_classes=["image-caption"],
):
    print(block.source_locator, block.tag, sorted(block.classes), block.text)
```

A locator such as `s008:text/chapter-01.xhtml#1.3.2` combines the one-based
spine position, EPUB-internal document path, and element path within the XHTML
`body`. Recipes use the EPUB-local portion: `text/chapter-01.xhtml#1.3.2`.

## Recipe example

Recipe version 1 remains the serialized format in epub-blocks 0.3.0:

```json
{
  "recipe_version": "1",
  "metadata": {"name": "Example edition"},
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
  "source_blocks": {
    "include_documents": ["text/chapter-*.xhtml"],
    "exclude_classes": ["image-caption"]
  },
  "output": {
    "groups": {
      "source_marker": {
        "pattern": "^CHAPTER\\s+([IVXLCDM]+)\\b",
        "case_insensitive": true
      },
      "capture_kind": "roman",
      "capture_width": 2
    },
    "identifiers": {
      "block": {
        "template": "{group}.{number:03d}",
        "start": 1
      },
      "line": {
        "template": "{group}.{block:03d}.{number:02d}",
        "start": 1
      }
    },
    "default": {"type": "paragraph", "role": "block"},
    "rules": [
      {
        "match": {"tag": "h1"},
        "type": "heading",
        "role": "fixed",
        "id": "{group}.000",
        "remove_prefix": {
          "pattern": "^CHAPTER\\s+[IVXLCDM]+\\s+",
          "case_insensitive": true
        }
      },
      {
        "match": {"classes": ["verse-first"]},
        "type": "line",
        "role": "line-start"
      },
      {
        "match": {"classes": ["verse"]},
        "type": "line",
        "role": "line"
      }
    ],
    "skip_source": [],
    "replacements": [],
    "insertions": [],
    "compiled_sha256": "28354978484f35525c616d138bdeb8f801039d665235ac88bd02f1a25791c410"
  }
}
```

This recipe generates identifiers such as `01.000`, `01.001`, `01.002.01`,
and `01.002.02`. Rules are matched against source structure; no expected text
or identifier table is consulted.

See the
[complete recipe specification](https://github.com/jtauber/epub-blocks/blob/main/docs/recipe-format.md)
and packaged
[JSON Schema](https://github.com/jtauber/epub-blocks/blob/main/src/epub_blocks/schemas/recipe-v1.schema.json).

## Command line

```bash
epub-blocks book.epub recipe.json records.tsv
```

The output is headerless TSV with `id`, `type`, and extracted `text` columns.
Standard CSV quoting with a tab delimiter preserves tabs and line breaks.

## Python API

```python
from epub_blocks import extract_recipe_file

records = extract_recipe_file("book.epub", "recipe.json")
for record in records:
    print(record.block_id, record.block_type, record.text)
```

`compile_recipe_file` returns the immutable in-memory extraction plan.
`Fragment` and `extract_fragments` provide lower-level access to selected XHTML
subtrees. The supported import surface is the names exported by `epub_blocks`.

## Safety

EPUB files are untrusted ZIP and XML input. Default APIs bound archive size,
individual and cumulative reads, compression ratios, XML size, element count,
and nesting depth. Duplicate or unsafe paths and encrypted members are rejected.
External DTDs are never loaded; internal subsets, entity declarations, other
doctypes, and unknown named character references are rejected.

Recipes are trusted local configuration because they contain regular
expressions. `epub-blocks` never executes EPUB scripts or fetches resources from
the network.

## Development

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run coverage erase
uv run coverage run -m unittest discover -s tests
uv run coverage report
uv run python -m build
uv run twine check dist/*
uv run pyright --verifytypes epub_blocks --ignoreexternal
```

Coverage includes branches and enforces a 90% minimum. Tests build synthetic
EPUBs; no EPUB files are committed. See
[CONTRIBUTING.md](https://github.com/jtauber/epub-blocks/blob/main/CONTRIBUTING.md)
and [CHANGELOG.md](https://github.com/jtauber/epub-blocks/blob/main/CHANGELOG.md).

## License

`epub-blocks` is available under the
[MIT License](https://github.com/jtauber/epub-blocks/blob/main/LICENSE).
