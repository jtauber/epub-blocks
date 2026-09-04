# epub-blocks

`epub-blocks` is a small, dependency-free Python library for reproducibly
extracting ordered, structured text blocks from EPUB 2 and EPUB 3 containers.
A declarative recipe maps an edition's XHTML structure to canonical block
references without requiring edition-specific Python extraction code.

It requires Python 3.13 or later. The project is at an early 0.x stage, so its
public API and recipe format may evolve before 1.0.

## Installation

```bash
python -m pip install epub-blocks
```

The runtime package has no third-party dependencies.

## Scope

The package handles the reusable EPUB extraction and structural-mapping layer:

- locating and safely parsing the EPUB package document and XHTML spine;
- selecting source documents and blocks with case-insensitive globs;
- extracting text with stable EPUB-local locators;
- normalizing text and removing page-break or note-reference elements;
- assigning an ordered canonical reference and type to each result;
- handling structural joins, omitted source blocks, explicit splits, and sparse
  edition-specific exceptions declaratively; and
- verifying the EPUB, canonical reference index, and compiled extraction plan
  with independent SHA-256 hashes.

The package does not compare an edition against canonical text. In particular,
recipes contain no expected text, text hashes, or character counts. They do not
choose between variant readings or apply editorial corrections.

## Discovering source blocks

Use the lower-level API to inspect an EPUB before writing a recipe:

```python
from epub_blocks import extract_blocks

for block in extract_blocks(
    "book.epub",
    include_documents=["*chapter*.xhtml"],
    exclude_classes=["image-caption"],
):
    print(block.source_locator, block.tag, block.text)
```

A source locator such as `s008:text/chapter-01.xhtml#1.3.2` combines the
one-based spine position, archive document path, and one-based element path
within the XHTML `body`. Recipe overrides use the EPUB-local portion,
`text/chapter-01.xhtml#1.3.2`.

## Recipes

Recipe version 1 is the package's initial serialized recipe format:

```json
{
  "recipe_version": "1",
  "metadata": {
    "name": "Example edition"
  },
  "epub": {
    "identifier": "9780000000000",
    "sha256": "f4f9c2d902a41b80732b2dce7ad01a57f615859c21417a5019e3cd8e4d271282"
  },
  "references": {
    "path": "references.tsv",
    "sha256": "5c0c7f91488d7a31fe599ad950162523292e8865cb946d3698ce2a477227d08c"
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
  "mapping": {
    "strategy": "ordered",
    "groups": {
      "reference_pattern": "^(\\d+)\\.",
      "reference_capture_kind": "decimal",
      "source_marker": {
        "pattern": "^CHAPTER\\s+([IVXLCDM]+)\\b",
        "capture_kind": "roman",
        "case_insensitive": true
      }
    },
    "type_rules": {
      "heading": {
        "remove_prefix": {
          "pattern": "^CHAPTER\\s+[IVXLCDM]+\\s+",
          "case_insensitive": true
        }
      }
    },
    "skip_source": [],
    "overrides": {},
    "compiled_sha256": "28354978484f35525c616d138bdeb8f801039d665235ac88bd02f1a25791c410"
  }
}
```

The referenced TSV is deliberately structural. It has exactly two columns and
one row per desired result:

```text
id	type
01.000	heading
01.001	paragraph
```

It supplies stable identifiers and types, not fingerprints of any text. Its
path is resolved relative to the recipe file.

Within each group, ordinary references consume and emit one source block in
order. Type rules can consume more blocks, emit selected one-based positions,
join emitted parts, or remove a required regular-expression prefix. For
example, `{"consume": 2, "emit": [2]}` consumes a separate chapter label and
title but emits only the title. Multiple overrides with slices of the same
source fragment express a split.

Compilation expands these rules into an immutable in-memory plan. The compiled
digest records output identifiers and types, emitted fragments, every consumed
source locator, and skipped and override-reserved locators. It therefore detects
changes to cursor advancement as well as output selection.

See the
[complete recipe specification](https://github.com/jtauber/epub-blocks/blob/main/docs/recipe-format.md)
and the packaged
[JSON Schema](https://github.com/jtauber/epub-blocks/blob/main/src/epub_blocks/schemas/recipe-v1.schema.json).

## Command line

```bash
epub-blocks book.epub recipe.json records.tsv
```

The output is headerless TSV with `id`, `type`, and extracted `text` columns.
Standard CSV quoting with a tab delimiter preserves tabs and line breaks.

## Python API

`extract_recipe_file` performs the same verified operation:

```python
from epub_blocks import extract_recipe_file

records = extract_recipe_file("book.epub", "recipe.json")
for record in records:
    print(record.block_id, record.block_type, record.text)
```

`compile_recipe_file` returns the explicit in-memory plan without making it a
second persisted format. `Fragment` and `extract_fragments` provide a
target-independent low-level primitive for consumers that need selected XHTML
subtrees directly.

The supported import surface is the names exported by `epub_blocks`.
Unexported helpers in submodules are implementation details.

## Safety and trust model

EPUB files are untrusted ZIP and XML input. Default extraction APIs bound
archive membership, individual and cumulative reads, compression ratios, XML
document size, element count, and nesting depth. Duplicate or unsafe archive
paths and encrypted members are rejected. External XHTML DTDs are never read;
internal subsets, entity declarations, other doctypes, and unknown named
character references are rejected. Known HTML named character references are
resolved locally. UTF-16 XML receives the same declaration checks as UTF-8.

Recipes are trusted local configuration because they contain regular
expressions and local file paths. `epub-blocks` never executes EPUB scripts or
fetches recipes, reference indexes, DTDs, or other resources from the network.

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

Coverage includes branch measurement and enforces a 90% minimum. Tests build
small synthetic EPUBs; no EPUB files are committed to this repository.

See
[CONTRIBUTING.md](https://github.com/jtauber/epub-blocks/blob/main/CONTRIBUTING.md)
and
[CHANGELOG.md](https://github.com/jtauber/epub-blocks/blob/main/CHANGELOG.md)
for the development and release policies.

## License

`epub-blocks` is available under the
[MIT License](https://github.com/jtauber/epub-blocks/blob/main/LICENSE).
