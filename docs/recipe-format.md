# Extraction recipe format

This document specifies version 1 of the `epub-blocks` extraction recipe
format. A recipe is a UTF-8 JSON document that describes how to turn one
specific EPUB file into an ordered sequence of structured text blocks.

The format is specific to `epub-blocks`; it is not an EPUB standard. It is
intended to make a reviewed extraction repeatable by recording:

- which EPUB the recipe expects;
- which XHTML fragments supply each output block;
- which descendant elements should be omitted;
- how fragments should be sliced and joined; and
- how the resulting text should be normalized.

A recipe does not define the meaning of block identifiers or block types.
Those are non-empty, opaque strings chosen by the consuming project.

## Minimal recipe

```json
{
  "recipe_version": "1",
  "blocks": [
    {
      "id": "chapter-01-heading",
      "type": "heading",
      "parts": [
        {
          "document": "text/chapter-01.xhtml",
          "element_path": "1"
        }
      ]
    }
  ]
}
```

This selects the first element child of the `<body>` in
`text/chapter-01.xhtml`. With the default normalization settings, its text is
whitespace-collapsed, stripped, and normalized to Unicode NFC.

## Complete structural reference

The following template shows every defined field. JSON does not support
comments, so the comments here are explanatory and must not appear in an
actual recipe.

```text
{
  "recipe_version": "1",              // required string
  "epub": {                            // optional object
    "identifier": "...",              // optional non-empty string
    "sha256": "..."                   // optional lowercase SHA-256 string
  },
  "normalization": {                   // optional object
    "collapse_whitespace": true,       // optional boolean; default true
    "strip": true,                     // optional boolean; default true
    "unicode_normalization": "NFC"     // optional string; default "NFC"
  },
  "omit_epub_types": [                 // optional string array; default []
    "noteref",
    "pagebreak"
  ],
  "blocks": [                          // required non-empty array
    {
      "id": "...",                    // required non-empty unique string
      "type": "...",                  // required non-empty string
      "separator": " ",                // optional string; default ""
      "parts": [                       // required non-empty array
        {
          "document": "...",          // required non-empty string
          "element_path": "1.2.3",    // optional string; default ""
          "omit": ["2", "3.1"],       // optional string array; default []
          "slice": {                   // optional object
            "start": 0,                // required non-negative integer
            "end": 25                  // required integer greater than start
          }
        }
      ]
    }
  ]
}
```

Unknown and duplicate object members are rejected. This catches spelling
mistakes and prevents different JSON readers from silently assigning different
meanings to a recipe. The format has no ad hoc extension mechanism; new
semantics require a new recipe version.

The normative machine-readable shape is also published as
[`schemas/recipe-v1.schema.json`](https://github.com/jtauber/epub-blocks/blob/main/schemas/recipe-v1.schema.json).
Runtime validation additionally enforces relationships that the schema cannot
express concisely, including `slice.end > slice.start`, unique block IDs, and
non-overlapping omission paths.

## Top-level fields

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `recipe_version` | yes | none | Format version. Version 1 requires the JSON string `"1"`; the number `1` is not equivalent. |
| `epub` | no | `{}` | Expected source identity. See [Pinning the source EPUB](#pinning-the-source-epub). |
| `normalization` | no | `{}` | Text-normalization settings. Omitted settings receive their individual defaults. |
| `omit_epub_types` | no | `[]` | EPUB semantic types to remove from every selected fragment. |
| `blocks` | yes | none | Non-empty array of output blocks, in output order. |

The EPUB filename is not stored in the recipe. It is supplied separately to
the command-line interface or Python API. This lets filenames and local paths
vary without weakening source verification.

## Pinning the source EPUB

The optional `epub` object can contain either or both of these checks:

| Field | Meaning |
| --- | --- |
| `identifier` | An identifier that must occur in the EPUB package document. |
| `sha256` | The SHA-256 digest of the complete EPUB file, as lowercase hexadecimal. |

For example:

```json
{
  "epub": {
    "identifier": "urn:isbn:9780000000000",
    "sha256": "f4f9c2d902a41b80732b2dce7ad01a57f615859c21417a5019e3cd8e4d271282"
  }
}
```

Identifier matching is exact and case-sensitive after surrounding whitespace
has been removed from values in the package document. An EPUB may expose more
than one identifier; the expected value need only match one of them.

The SHA-256 is calculated over the raw bytes of the EPUB file, not over its
unpacked contents. Repackaging the same XHTML into a different ZIP file will
therefore change the digest. The comparison is exact and case-sensitive, so
the recipe should use the conventional 64-character lowercase form.

Using both fields is recommended for reviewed, reproducible extraction:

- the identifier records the edition identity asserted by the publisher; and
- the hash pins the precise file whose archive structure and text the locators
  were reviewed against.

If `epub`, `identifier`, or `sha256` is omitted, its corresponding check is not
performed. If either identity field is present, however, it must be non-empty;
`sha256` must be exactly 64 lowercase hexadecimal characters. An empty value
never disables a check. The EPUB must still be a readable EPUB container with
a package document and at least one XHTML spine document.

## Blocks

Each object in `blocks` produces exactly one `ExtractedBlock`. Recipe order is
preserved in the result and in TSV output.

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `id` | yes | none | Non-empty block identifier, unique within the recipe. |
| `type` | yes | none | Non-empty block-type string. |
| `parts` | yes | none | Non-empty array of source fragments, in join order. |
| `separator` | no | `""` | Literal string inserted between adjacent extracted parts. |

Neither `id` nor `type` is interpreted by the library. Values such as
`"01.001"`, `"paragraph"`, or `"{p}"` are all valid. The recipe, rather than
the library, is responsible for maintaining any identifier hierarchy or type
vocabulary.

After all parts have been extracted, they are joined with `separator`. The
joined text is then normalized using the recipe's normalization settings. A
block is rejected if this produces an empty string.

### Joining multiple parts

A logical block can combine fragments from one or more XHTML documents:

```json
{
  "id": "paragraph-17",
  "type": "paragraph",
  "separator": " ",
  "parts": [
    {
      "document": "text/chapter-01.xhtml",
      "element_path": "8"
    },
    {
      "document": "text/chapter-02.xhtml",
      "element_path": "1"
    }
  ]
}
```

The separator is inserted literally before block-level normalization. With
`collapse_whitespace: true`, a separator of one space remains one space even
if either source fragment has surrounding whitespace. With whitespace
collapsing disabled, all source and separator whitespace is retained, subject
to `strip`.

## Parts

A part selects one XHTML subtree and may remove descendants or take a text
slice from it.

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `document` | yes | none | Exact, case-sensitive path of an XHTML member inside the EPUB ZIP archive. |
| `element_path` | no | `""` | One-based element path relative to the XHTML `<body>`. An empty path selects the body itself. |
| `omit` | no | `[]` | Element paths to remove, relative to the selected element. |
| `slice` | no | absent | Half-open code-point range to retain after fragment normalization. |

Parts are processed in the order in which they appear. A document is parsed
once per extraction run and cached, but every selected subtree is copied
before omissions are made. Reusing the same source fragment in multiple parts
does not mutate the EPUB or affect later parts.

### Document paths

`document` names a ZIP archive member directly, for example
`OEBPS/Text/chapter01.xhtml`. It must match the archive path exactly, including
case. It is not a filesystem path, URL, or glob pattern.

Recipes normally use the resolved archive paths reported by EPUB inspection
or block discovery. A recipe may address an XHTML document directly even if
that document is not selected from the spine, but the EPUB package itself must
still have an XHTML spine.

## XHTML element paths

An element path is a dot-separated sequence of one-based child positions,
starting at the XHTML `<body>`.

Given this simplified body:

```xml
<body>
  <h1>Chapter One</h1>
  <section>
    <p>First paragraph.</p>
    <p>Second <em>paragraph</em>.</p>
  </section>
</body>
```

the paths are:

| Path | Selected element |
| --- | --- |
| `""` | `<body>` |
| `"1"` | `<h1>` |
| `"2"` | `<section>` |
| `"2.1"` | first `<p>` |
| `"2.2"` | second `<p>` |
| `"2.2.1"` | `<em>` |

Only XML element children count. Text nodes, tail text, attributes, and source
indentation do not occupy positions. Each component must identify a positive,
in-range child position.

For discussion and diagnostics, a fragment can be written as
`document#element_path`, such as
`OEBPS/Text/chapter01.xhtml#2.2`. The `#` notation is not itself used in a
recipe field; `document` and `element_path` remain separate.

Element paths describe the structure of one precise EPUB file. They are not
expected to remain valid after an edition is reflowed, repackaged, or edited.

## Omitting descendant elements

There are two complementary omission mechanisms:

1. a part's `omit` array removes descendants at exact structural paths; and
2. top-level `omit_epub_types` removes descendants carrying selected EPUB
   semantic types.

Omission happens before text is collected.

### Exact omission paths

Paths in `omit` are relative to the part's selected element, not to the XHTML
body. For example:

```xml
<p>Alpha <span class="editorial">remove this</span> omega.</p>
```

If this `<p>` is the selected element, the following part removes its first
element child:

```json
{
  "document": "text/chapter-01.xhtml",
  "element_path": "7",
  "omit": ["1"]
}
```

The extracted text is `Alpha  omega.` before normalization and
`Alpha omega.` with the default settings.

The selected root cannot omit itself, so an empty omission path is invalid.
Duplicate paths and paths where one names an ancestor of another are rejected:
such entries are redundant and can otherwise become order-dependent. Valid
paths are removed rightmost-first, preserving the meaning of earlier sibling
positions.

Removing an element removes its text and all of its descendants. Its XML tail
text—the text following the closing tag—is preserved and reattached to the
preceding sibling or parent. This is why removing the `<span>` above preserves
` omega.`.

### Omission by EPUB type

`omit_epub_types` applies to every part in the recipe. A descendant is removed
when any whitespace-separated token in its `epub:type` attribute exactly
matches a listed value.

```json
{
  "omit_epub_types": ["noteref", "pagebreak"]
}
```

For example, both of these descendants are removed:

```xml
<a epub:type="noteref">1</a>
<span epub:type="pagebreak chapter-boundary">23</span>
```

Matching is case-sensitive. The second element matches because one of its two
tokens is `pagebreak`. As with exact omissions, tail text is preserved.

This mechanism examines descendants of the selected fragment. It does not
discard the selected root merely because the root itself has a matching
`epub:type`. To omit that element, select its parent and name it in `omit`, or
select a different fragment.

Exact `omit` paths are applied first, followed by `omit_epub_types`.

## Text extraction

After omissions, the selected subtree's text nodes and tail text are
concatenated in document order. Markup is discarded. No characters are
inserted at element boundaries.

For example:

```xml
<p>one<em>two</em>three</p>
```

produces `onetwothree`, not `one two three`. Any required spaces must already
exist in the XHTML text or be introduced between separate parts with a block
`separator`.

XML entities and character references have already been decoded by the XML
parser when text is collected.

## Slices

A `slice` retains one half-open range from a part's normalized text:

```json
{
  "document": "text/chapter-01.xhtml",
  "element_path": "12",
  "slice": {
    "start": 0,
    "end": 10
  }
}
```

The range `[start, end)` includes the character at `start` and excludes the
character at `end`. `start` must be zero or greater, `end` must be greater than
`start`, and `end` must not exceed the normalized fragment length. Empty
slices are not allowed.

Offsets count Python Unicode code points, not UTF-8 bytes, UTF-16 code units,
grapheme clusters, words, or XML source positions. Most ordinary characters
count as one. A combining mark also counts as its own code point unless
Unicode normalization composes it with its base character.

### Slicing happens after fragment normalization

Before a slice is applied, the complete post-omission fragment is normalized
with the recipe's settings. The offsets therefore refer to that normalized
string.

If a source fragment normalizes to:

```text
Alpha beta gamma.
```

then:

- `[0, 10)` produces `Alpha beta`; and
- `[11, 17)` produces `gamma.`.

This ordering makes offsets independent of source indentation and line
wrapping when whitespace collapsing is enabled. It also means that changing
any normalization option can invalidate every slice in a recipe.

Slices are intentionally precise but comparatively fragile. Prefer selecting
whole elements or joining whole parts when the XHTML structure permits it.
When a slice is necessary, pin the EPUB SHA-256 and review offsets again after
any source or normalization change.

## Normalization

The optional `normalization` object controls the transformation of extracted
text:

```json
{
  "normalization": {
    "collapse_whitespace": true,
    "strip": true,
    "unicode_normalization": "NFC"
  }
}
```

| Field | Type | Default | Effect |
| --- | --- | --- | --- |
| `collapse_whitespace` | boolean | `true` | Replace each run of one or more Unicode whitespace characters with one U+0020 SPACE. |
| `strip` | boolean | `true` | Remove Unicode whitespace from the beginning and end of the string. |
| `unicode_normalization` | string | `"NFC"` | Apply the named Python Unicode normalization form. The exact string `"none"` disables this step. |

Normalization operations always run in this order:

1. collapse whitespace, if enabled;
2. strip leading and trailing whitespace, if enabled; and
3. apply Unicode normalization, unless set to `"none"`.

Python supports the standard forms `NFC`, `NFD`, `NFKC`, and `NFKD`. `NFC` is
recommended for preserving textual distinctions while giving canonically
equivalent character sequences a stable representation. Compatibility forms
such as `NFKC` can change distinctions that matter to some projects and should
be selected deliberately.

Normalization is applied at two possible points:

- a sliced part is normalized immediately before its offsets are applied; and
- every complete block is normalized after all its parts have been joined.

An unsliced part is not independently normalized before joining. Its raw
post-omission text participates in the final block-level normalization. A
sliced part is normalized before slicing and the joined block is normalized
again afterward. The standard normalization operations are idempotent for
normal usage, but this distinction matters for offset calculation and for
separators when whitespace collapsing or stripping is disabled.

## Exact processing model

For a version 1 recipe, extraction proceeds as follows:

1. Parse the recipe as a top-level JSON object.
2. Reject duplicate or unknown members at every object level.
3. Require `recipe_version` to be the string `"1"`.
4. Calculate and verify the EPUB SHA-256 when requested.
5. Open the EPUB and locate its package document through
   `META-INF/container.xml`.
6. Read package identifiers and the XHTML spine.
7. Verify the expected package identifier when requested.
8. Process `blocks` in listed order.
9. For each part in a block:
   1. read the exact archive document and find its XHTML `<body>`;
   2. select `element_path` and copy that subtree;
   3. remove explicit `omit` paths;
   4. remove descendants matching `omit_epub_types`;
   5. concatenate all remaining text and tail nodes without a delimiter;
   6. if a slice is present, normalize the fragment, validate the range, and
      retain `[start, end)`.
10. Join the block's part strings using `separator`.
11. Normalize the joined string.
12. Reject an empty result and append the block to the output sequence.

The extraction is local and deterministic: the process does not fetch network
resources, add timestamps, or alter the source EPUB.

## Comprehensive example

This example demonstrates source pinning, global omissions, whole-element
selection, exact descendant omission, joining, and slicing:

```json
{
  "recipe_version": "1",
  "epub": {
    "identifier": "sample-edition",
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
      "type": "heading",
      "parts": [
        {
          "document": "text/chapter-01.xhtml",
          "element_path": "1"
        }
      ]
    },
    {
      "id": "01.002",
      "type": "paragraph",
      "separator": " ",
      "parts": [
        {
          "document": "text/chapter-01.xhtml",
          "element_path": "2",
          "omit": ["1"]
        },
        {
          "document": "text/chapter-01.xhtml",
          "element_path": "3"
        }
      ]
    },
    {
      "id": "01.003a",
      "type": "paragraph-part",
      "parts": [
        {
          "document": "text/chapter-01.xhtml",
          "element_path": "4",
          "slice": {"start": 0, "end": 10}
        }
      ]
    },
    {
      "id": "01.003b",
      "type": "paragraph-part",
      "parts": [
        {
          "document": "text/chapter-01.xhtml",
          "element_path": "4",
          "slice": {"start": 11, "end": 17}
        }
      ]
    }
  ]
}
```

## Validation and failure conditions

Extraction stops with `EpubBlocksError` when a recipe or source cannot be
applied unambiguously. Version 1 validates at least the following conditions:

- the recipe is valid JSON and its top level is an object;
- object members are neither duplicated nor unknown;
- the recipe version is supported;
- `epub` and `normalization`, when present, are objects;
- the requested source hash and identifier match;
- `omit_epub_types` and each `omit` value are arrays of strings;
- `blocks` is a non-empty array of objects;
- every block has a non-empty, unique string `id`;
- every block has a non-empty string `type`;
- every block has a non-empty `parts` array;
- every part is an object with a non-empty string `document`;
- `element_path` is a string and resolves inside the document body;
- every omission path is syntactically valid and resolves inside the selected
  fragment;
- omission paths neither duplicate nor contain one another;
- a part cannot omit its selected root;
- `slice` is an object with integer `start` and `end` fields;
- a slice is non-empty, non-negative, and within the normalized fragment;
- `separator` is a string;
- the EPUB is a valid ZIP container with the required EPUB package structure;
- referenced archive documents exist, contain well-formed XML, and have an
  XHTML body;
- the requested Unicode normalization form is supported; and
- every completed block contains text after normalization.

Recipes should use the documented JSON types even where a language runtime
might coerce another value. In particular, write actual JSON booleans
`true`/`false`, not strings such as `"true"` or `"false"`.

## Output

### Python result

`extract_recipe` and `extract_recipe_file` return a list of immutable
`ExtractedBlock` values. Each value has:

| Attribute | Source |
| --- | --- |
| `block_id` | block `id` |
| `block_type` | block `type` |
| `text` | joined and normalized extraction |

### TSV serialization

`write_tsv` writes three columns in this order:

```text
id<TAB>type<TAB>text<LF>
```

The file is UTF-8, has Unix LF record endings, and has no header. Block order
matches recipe order. Missing parent directories are created. The completed
TSV is written to a temporary file in the destination directory and atomically
replaces the target, so an extraction or serialization failure does not leave
a partial output.

TSV fields use conventional CSV-style quoting with a tab delimiter. A field
containing a tab, newline, or double quote is enclosed in double quotes, and
an embedded double quote is doubled. With the default whitespace collapsing,
extracted text cannot retain tabs or newlines; they can occur when
`collapse_whitespace` is disabled.

## Command-line use

```bash
epub-blocks SOURCE.epub RECIPE.json OUTPUT.tsv
```

For example:

```bash
epub-blocks book.epub recipes/book.json build/book.tsv
```

The output path's parent directories are created as needed. On success, the
command prints the number of blocks written. Expected recipe, EPUB, and file
errors are printed as concise diagnostics on standard error and return status
2; they do not expose a Python traceback.

## Input safety limits

Every high-level inspection and extraction function accepts an optional
`limits=SafetyLimits(...)` argument. Defaults bound archive membership,
per-member and cumulative uncompressed bytes read, compression ratio, XML
bytes, XML element count, and XML nesting depth. The reader also rejects
duplicate or unsafe ZIP paths, encrypted members, external or internal DTDs,
and entity declarations. The declaration-only HTML5 `<!DOCTYPE html>` is
permitted. Increase an individual positive limit only when a known source
requires it; do not disable all limits for untrusted input.

Automatic block discovery excludes spine items marked `linear="no"`. Pass
`include_non_linear=True` to `extract_blocks` when auxiliary documents are
intentionally part of the extraction.

## Python use

Load a JSON recipe from a file and write TSV:

```python
from pathlib import Path

from epub_blocks import extract_recipe_file, write_tsv

blocks = extract_recipe_file(
    Path("book.epub"),
    Path("recipe.json"),
)
write_tsv(Path("records.tsv"), blocks)
```

Apply an already-loaded recipe object without writing TSV:

```python
import json
from pathlib import Path

from epub_blocks import extract_recipe

recipe = json.loads(Path("recipe.json").read_text(encoding="utf-8"))
blocks = extract_recipe(Path("book.epub"), recipe)

for block in blocks:
    print(block.block_id, block.block_type, block.text)
```

## Reproducible recipe authoring

For a recipe that can be reviewed and rerun reliably:

1. Record both the EPUB package identifier and raw-file SHA-256.
2. State all normalization fields explicitly, even when using defaults. This
   makes policy visible to reviewers and protects the recipe from a future
   change of defaults in a later format version.
3. Obtain document and element paths from the precise pinned EPUB, then review
   the extracted text rather than assuming structurally similar editions use
   the same paths.
4. Prefer complete element selections. Use `parts` plus `separator` to join
   structurally separate material, and reserve `slice` for boundaries that
   XHTML structure cannot express.
5. Keep block identifiers unique and treat block order as meaningful.
6. Review every explicit omission. Structural locators can remain valid while
   the contents at those locators change in a different source file.
7. Regenerate and compare the complete output after changing the source,
   recipe, or normalization settings.
8. Preserve the reviewed recipe together with the output or the release
   metadata that identifies it.

## Recipe versioning

Version 1 is identified by the exact JSON string:

```json
{
  "recipe_version": "1"
}
```

Readers reject any other value rather than guessing how to interpret it. A
future incompatible change to locator meaning, normalization order, slice
units, or extraction semantics requires a new recipe version. Adding
project-specific meaning to `id` or `type` does not change this format because
those fields are deliberately opaque.
