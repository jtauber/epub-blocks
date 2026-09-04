# Recipe format

This document specifies `epub-blocks` recipe version 1. A recipe maps the
ordered source blocks of one pinned EPUB to an ordered, structural canonical
reference index. The result is a sequence of `id`, `type`, and `text` records.

The reference index contains no text, text hashes, or text lengths. Mapping is
therefore structural and deterministic; it never chooses a source span by
comparing it with an expected reading.

## Top-level object

A recipe is a UTF-8 JSON object with these members:

| Member | Required | Meaning |
| --- | --- | --- |
| `recipe_version` | yes | Must be the string `"1"`. |
| `metadata` | no | Opaque object ignored by `epub-blocks`. |
| `epub` | yes | Pins the source publication. |
| `references` | yes | Pins the ordered canonical reference index. |
| `normalization` | no | Controls text normalization. |
| `omit_epub_types` | no | Removes descendants with listed EPUB semantic types. |
| `source_blocks` | yes | Selects candidate source blocks. |
| `mapping` | yes | Maps candidates to references. |

Unknown members and duplicate JSON member names are errors. `NaN`, `Infinity`,
and `-Infinity` are not accepted JSON values.

## EPUB pin

```json
"epub": {
  "identifier": "9780000000000",
  "sha256": "f4f9c2d902a41b80732b2dce7ad01a57f615859c21417a5019e3cd8e4d271282"
}
```

Both members are required. `identifier` must equal one of the package
metadata's nonempty `dc:identifier` values. `sha256` is 64 lowercase hexadecimal
characters and must match the complete EPUB file.

## Canonical reference index

```json
"references": {
  "path": "references.tsv",
  "sha256": "5c0c7f91488d7a31fe599ad950162523292e8865cb946d3698ce2a477227d08c",
  "columns": {
    "id": "id",
    "type": "type"
  }
}
```

The path is absolute or relative to the JSON recipe. Programmatic calls using a
relative path must supply `base_dir`. The hash pins the complete TSV bytes.

The UTF-8 TSV must have exactly two uniquely named columns. By default they are
`id` and `type`; `columns` may assign different names. Every data row must have
exactly two fields, both nonempty. IDs must be unique. Row order is output
order. An empty index is invalid.

This file defines only a reference sequence and type sequence. A file with
fingerprint or text columns is not a version 1 reference index.

## Normalization

```json
"normalization": {
  "collapse_whitespace": true,
  "strip": true,
  "unicode_normalization": "NFC"
}
```

All members are optional. Defaults are shown above. Unicode normalization may
be `NFC`, `NFD`, `NFKC`, `NFKD`, or `none`.

Operations occur in this order:

1. Configured descendants are removed from a copy of the selected XHTML
   subtree, first by explicit fragment `omit` paths and then by EPUB semantic
   type.
2. The remaining XHTML is flattened, with `<br>` represented by a newline.
3. Runs matched by Python `\s+` collapse to one ASCII space when enabled.
4. Leading and trailing whitespace is removed when enabled.
5. Unicode normalization is applied unless set to `none`.

Each emitted fragment is normalized before joining. The joined result is then
normalized once more. Fragment slice offsets and compiled prefix-removal slices
refer to Unicode code points in normalized fragment text.

## EPUB semantic omissions

`omit_epub_types` is an optional array of unique, nonempty EPUB type tokens.
Every descendant whose whitespace-separated `epub:type` tokens intersect the
set is removed while preserving its tail text. The default is an empty array.
A typical recipe explicitly uses `['noteref', 'pagebreak']`.

## Candidate source blocks

`source_blocks` recognizes these optional members:

- `include_documents`: case-insensitive archive-path globs; empty means all
  eligible XHTML spine documents.
- `exclude_documents`: case-insensitive archive-path globs applied after
  inclusion.
- `exclude_classes`: exact CSS class tokens compared case-insensitively.
- `include_locators`: case-insensitive globs for `document#element-path`.
- `exclude_locators`: case-insensitive locator globs applied last.
- `include_non_linear`: include `linear="no"` spine items; default `false`.

Document and locator selectors are conjunctive: an included locator cannot
bring back a document excluded by the document selectors.

Candidate elements are `p`, `h1` through `h6`, and `pre`. A `blockquote` or
`li` is a candidate only when it contains no candidate primary descendant.
Candidates retain EPUB spine and XHTML document order. Empty normalized blocks
are discarded.

## Mapping

```json
"mapping": {
  "strategy": "ordered",
  "groups": {},
  "join_separator": " ",
  "type_rules": {},
  "skip_source": [],
  "overrides": {},
  "compiled_sha256": "28354978484f35525c616d138bdeb8f801039d665235ac88bd02f1a25791c410"
}
```

`strategy` must be `ordered`. `compiled_sha256` is required during normal
compilation and extraction. The remaining members have the defaults shown.

After grouping and removing skipped or override-reserved candidates, the
compiler walks references and source candidates together. Unless a type rule
or override says otherwise, each reference consumes and emits the next source
candidate. Every reference must be produced and every available source
candidate must be consumed.

### Groups by locator

Groups keep independent cursors, which prevents a structural exception in one
chapter or section from shifting everything after it.

```json
"groups": {
  "reference_pattern": "^(\\d+)\\.",
  "reference_capture_kind": "decimal",
  "source_pattern": "chapter-(\\d+)\\.xhtml#",
  "source_offset": 0
}
```

`reference_pattern` and `source_pattern` are case-insensitive regular
expressions with exactly one capture. The former searches the canonical ID;
the latter searches `source_locator`. `reference_capture_kind` is `string` by
default or `decimal`; decimal capture removes leading zeroes.

`source_offset` adds an integer to numeric source captures. A result below one
is invalid. `reference_map` and `source_map` map captured strings to explicit
group keys. Maps require patterns, must be nonempty, and `source_map` cannot be
combined with `source_offset`.

When `groups` is empty, all references and candidates belong to one group.

### Groups by heading marker

When EPUB filenames do not provide the canonical chapter number, a heading can
establish the current group:

```json
"groups": {
  "reference_pattern": "^(\\d+)\\.",
  "reference_capture_kind": "decimal",
  "source_marker": {
    "pattern": "^CHAPTER\\s+([IVXLCDM]+)\\b",
    "capture_kind": "roman",
    "case_insensitive": true
  }
}
```

`source_marker` is mutually exclusive with `source_pattern`. Its anchored
`pattern` has exactly one capture and is matched against normalized block text.
A match assigns that block and following blocks to the captured group until the
next marker.

`capture_kind` is `string`, `decimal`, or `roman`. Decimal values normalize to
ordinary decimal without leading zeroes. Roman values are case-insensitive
during conversion but must use canonical subtractive notation in the range
supported by standard Roman numerals. `case_insensitive` controls matching of
the full marker pattern and defaults to `false`.

A candidate before the first marker is an error. Marker groups must exist in
the reference index, may not repeat, and must advance in reference-group order.
Grouping happens before skipped and override-reserved candidates are removed,
so a marker can still establish a group when it is not emitted normally.

### Type rules

A type rule applies to every non-overridden reference of that type:

```json
"type_rules": {
  "heading": {
    "consume": 2,
    "emit": [2],
    "separator": " "
  }
}
```

Every type-rule key must occur as a type in the pinned reference index. An
unknown or misspelled type is an error rather than an ignored rule.

- `consume` is the positive number of consecutive candidates by which the
  group cursor advances; default `1`.
- `emit` is a nonempty array of unique one-based positions among those
  candidates; default is every consumed position in order. Values cannot
  exceed `consume`. Their array order is output order.
- `separator` joins emitted fragments. It defaults to `mapping.join_separator`
  when several parts are emitted and to the empty string for one part.

Thus `consume: 2, emit: [2]` accounts for a separate chapter-label block while
emitting only the following title. Omitting `emit` joins both blocks.

A one-part rule can remove a structural prefix:

```json
"remove_prefix": {
  "pattern": "^CHAPTER\\s+[IVXLCDM]+\\s+",
  "case_insensitive": true
}
```

The regular expression must begin with `^`, must not match an empty string, and
must match a nonempty prefix of the normalized emitted block at compilation.
It must leave nonempty text. Prefix removal requires exactly one emitted part.
Matching case-insensitively does not alter the casing of retained text. The
compiler records the result as an explicit numeric fragment slice rather than
placing the regular expression in the compiled plan.

### Skipped source blocks

`skip_source` lists exact EPUB-local locators such as
`text/chapter.xhtml#1.2`. Each must identify exactly one selected candidate.
Skipped candidates are accounted for but never assigned to a reference.

### Overrides, joins, omissions, and splits

Overrides are keyed by canonical reference ID and replace ordinary cursor
mapping for that reference:

```json
"overrides": {
  "01.014": {
    "parts": [
      {
        "document": "text/chapter.xhtml",
        "element_path": "4.2",
        "omit": ["2"],
        "slice": {"start": 0, "end": 42}
      },
      {
        "document": "text/notes.xhtml",
        "element_path": "1.3"
      }
    ],
    "separator": " "
  }
}
```

Each part requires `document`. `element_path` defaults to the XHTML `body`
itself; otherwise it is a dot-separated one-based child path. `omit` contains
relative descendant paths. Duplicate or ancestor/descendant-overlapping omit
paths are invalid. `slice` is a zero-based, half-open normalized code-point
range satisfying `0 <= start < end`.

An override's part locators are reserved and removed from ordinary candidate
mapping when present there. A locator cannot be both skipped and reserved.
Override fragments may also address non-candidate or non-spine XHTML, which is
useful for note bodies. Overrides do not inherit type-rule transforms.

Several parts form an explicit join. Several overrides may use non-overlapping
slices of the same source fragment to express a split. Whenever a
`document`/`element_path` pair occurs more than once anywhere in `overrides`,
every occurrence must have a slice and their half-open ranges must not overlap;
adjacent ranges are allowed. Reusing a whole fragment, or combining a whole
fragment with a slice of it, is an error. This check applies even when the
occurrences specify different descendant omissions.

Because overrides do not advance the group cursor, any replaced ordinary
candidate must be reserved by an override part or explicitly skipped.

## Compilation and digest

Compilation produces an immutable `CompiledRecipe`; there is no serialized
compiled-recipe format. Every `CompiledBlock` records:

- canonical ID and type;
- emitted `Fragment` values and separator; and
- all source locators consumed to advance the cursor, including consumed parts
  that were not emitted.

The compiled recipe separately records sorted skipped and override-reserved
locators. Its canonical digest also includes normalization and EPUB-type
omissions. Changing `consume: 2, emit: [2]` to a superficially similar
one-part selection therefore changes the digest.

Call `compile_recipe(..., verify_digest=False)` while authoring a recipe, then
store `compiled_recipe_digest(result)` in `mapping.compiled_sha256`. Normal
compilation and extraction verify it.

`metadata`, filesystem paths used to locate the recipe and reference-index
file, and the input hashes themselves are excluded from the compiled digest
because they are not compiled extraction behavior. EPUB-internal document and
element paths are included. The EPUB and reference-index hashes independently
pin the external inputs.

## Output

`extract_recipe` and `extract_recipe_file` return ordered `ExtractedBlock`
values. `write_tsv` and the command-line program write headerless TSV columns:

1. canonical `id`;
2. canonical `type`;
3. extracted source `text`.

The CLI form is:

```bash
epub-blocks book.epub recipe.json records.tsv
```

## Schema and runtime validation

The JSON Schema is distributed at
`epub_blocks/schemas/recipe-v1.schema.json`. It catches structural mistakes in
editors and pipelines. Runtime validation is authoritative and additionally
checks the reference TSV, EPUB contents, group captures, Roman numerals,
locator disposition, prefix matches, cursor exhaustion, fragments, and all
three hashes.
