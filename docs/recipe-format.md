# Recipe format

This document specifies `epub-blocks` recipe version 1 as implemented by
epub-blocks 0.4.0. A recipe is a self-contained structural program that turns
one pinned EPUB into an ordered sequence of `id`, `type`, and `text` blocks.

The recipe generates its output identifiers and types. It does not refer to a
pre-existing output table, expected reading, text hash, or character count.
Such material can be used outside epub-blocks to test or review a recipe, but
it is not a runtime input.

## Top-level object

| Member | Required | Meaning |
| --- | --- | --- |
| `recipe_version` | yes | The string `"1"`. |
| `metadata` | no | Opaque object ignored by epub-blocks. |
| `epub` | yes | Pins the source publication. |
| `normalization` | no | Controls text normalization. |
| `omit_epub_types` | no | Removes descendants with listed EPUB semantic types. |
| `source_blocks` | yes | Selects candidate source blocks. |
| `text` | no | Nested-block boundaries and optional XML/delimiter markup. |
| `output` | yes | Defines groups, identifiers, types, and exceptions. |

Unknown members, duplicate JSON members, `NaN`, and infinities are errors.
Optional members receive their defaults only when absent; explicit `null`
values are invalid outside opaque `metadata`.

## EPUB pin

```json
"epub": {
  "identifier": "9780000000000",
  "sha256": "f4f9c2d902a41b80732b2dce7ad01a57f615859c21417a5019e3cd8e4d271282"
}
```

Both values are required. `identifier` must equal one of the nonempty
`dc:identifier` values in the package metadata. `sha256` is the lowercase
SHA-256 of the complete EPUB file.

## Normalization

```json
"normalization": {
  "collapse_whitespace": true,
  "strip": true,
  "unicode_normalization": "NFC"
}
```

The defaults are shown. Unicode normalization can be `NFC`, `NFD`, `NFKC`,
`NFKD`, or `none`.

The steps below describe plain-text extraction without the new content
policies. With `text.markup`, normalization operates on text before marker
serialization; see the [markup contract](markup.md#normalization-and-structural-transformations).

For each fragment, operations occur in this order:

1. Explicitly omitted descendants are removed.
2. Descendants with configured EPUB semantic types are removed.
3. XHTML is flattened, with `<br>` represented by a newline.
4. Runs matched by Python `\s+` collapse to one ASCII space when enabled.
5. Leading and trailing whitespace is stripped when enabled.
6. Unicode normalization is applied unless set to `none`.

Fragments are normalized before joining; the joined result is normalized once
more. Slice offsets and compiled prefix-removal offsets count Unicode code
points in normalized fragment text.

## EPUB semantic omissions

`omit_epub_types` is an array of unique, nonempty EPUB type tokens. A descendant
whose whitespace-separated `epub:type` values intersect this set is removed
while its tail text is preserved. The default is empty. A typical recipe uses
`["noteref", "pagebreak"]`.

## Candidate source blocks

`source_blocks` supports:

- `include_documents`: case-insensitive EPUB-path globs; empty means all
  eligible XHTML spine documents;
- `exclude_documents`: case-insensitive path globs applied after inclusion;
- `exclude_classes`: exact class tokens compared case-insensitively;
- `include_locators`: case-insensitive `document#element-path` globs;
- `exclude_locators`: locator globs applied after inclusion; and
- `include_non_linear`: include `linear="no"` spine items; default `false`;
- `element_rules`: ordered structural selection overrides; default empty;
- `strict_coverage`: report unclaimed non-whitespace source text; default `false`.

Document and locator selection are conjunctive. Locator inclusion cannot bring
back an excluded document.

Candidate elements are `p`, `h1` through `h6`, and `pre`. A `blockquote` or `li`
is a candidate only when it has no primary candidate descendant. Candidates
remain in spine and document order. Empty normalized blocks are discarded.

These are the defaults; explicit source-element rules can override the
candidate boundary and retain deliberate empty elements as described below.

The stable local locator syntax is `document#element-path`, for example
`text/chapter-01.xhtml#1.3.2`. Element paths are dot-separated, one-based child
positions within the XHTML `body`.

### Structural element selectors

Source-element and markup rules use a structural selector. All supplied
criteria must pass:

| Criterion | Meaning |
| --- | --- |
| `tag` | Case-insensitive local tag name |
| `classes` | Exact set of class tokens, including `[]` for no classes |
| `classes_any`, `classes_all` | Any/all exact, case-sensitive tokens |
| `locators` | Any case-insensitive EPUB-local locator glob |
| `epub_types` | All exact tokens must occur in `epub:type` |
| `attributes` | Map of exact XML attribute names to exact string values |
| `empty` | Whether the subtree's raw text is entirely whitespace |
| `previous_sibling` | A selector for the immediately preceding element sibling |
| `has_child` | A selector matching at least one immediate child element |

The two contextual selectors cannot themselves contain contextual selectors.
They use original source structure, not a previous emitted row; comments are
not elements. Fragment omissions do not renumber source locators or change
the children, sibling context, or raw text inspected by selectors. A selector
must contain at least one effective criterion.
Empty arrays other than `classes`, and an empty attribute map, impose no
restriction. Structural selectors do not accept regular-expression text
matching. Output-rule `match` retains its separate `text_pattern` vocabulary.

### Ordered source-element rules

```json
"element_rules": [
  {"match": {"tag": "li"}, "action": "block"},
  {"match": {"tag": "aside", "classes_all": ["navigation"]}, "action": "skip"},
  {
    "match": {"tag": "div", "classes_all": ["scene"]},
    "action": "block",
    "keep_empty": true
  }
]
```

The first matching rule wins. `block` selects the complete subtree without
also emitting descendants. `descend` visits children instead of selecting the
current element. `skip` excludes the subtree while retaining its tail in a
selected parent. No match retains the existing candidate defaults. Skips also
apply inside selected whole blocks. `keep_empty` is only valid with `block`;
to emit an empty row, its output emission also needs `allow_empty: true`.

Document and candidate class/locator filters still apply. A rule cannot bring
back an excluded document or candidate. With `strict_coverage: true`, text not
covered by selected subtrees or deliberate exclusions is an error. This
includes text before/after nested blocks, body text, and child tails. Errors
identify source locations without dumping prose. Non-textual structure still
needs inspection: a text-coverage check cannot detect an unconfigured empty
scene separator or page marker.

## Text handling

```json
"text": {
  "block_boundaries": {"tags": ["p", "li"], "separator": " "}
}
```

The optional `block_boundaries` inserts its nonempty, whitespace-only separator
at entry and exit of configured nested block tags inside a selected fragment.
It does not surround the selected root. Normalization applies afterwards.
For example, a selected `<li>Open.<p>Another.</p>Tail.</li>` becomes
`Open. Another. Tail.`. Inline wrappers still join `in<em>side</em>` as `inside`.
The default tag list is empty, preserving the old flattening behaviour.

The other optional member is `text.markup`, fully specified in
[Marked-up text](markup.md). It supports XML fragments or configurable
delimiters, spans, attribute-labeled milestones, escaping, and explicit
between-block attachment policies. Both serializers preserve the same
references and underlying normalized text.

## Output

The `output` object has this shape:

```json
"output": {
  "groups": {},
  "identifiers": {
    "block": {"template": "{group}.{number:03d}", "start": 1},
    "line": {
      "template": "{group}.{block:03d}.{number:02d}",
      "start": 1
    }
  },
  "default": {"type": "paragraph", "role": "block"},
  "rules": [],
  "skip_source": [],
  "replacements": [],
  "insertions": [],
  "compiled_sha256": "28354978484f35525c616d138bdeb8f801039d665235ac88bd02f1a25791c410"
}
```

`identifiers`, `default`, and `compiled_sha256` are required during normal
extraction. The other members have the defaults shown.

## Groups

Counters are independent within each group. Exactly one of three grouping
mechanisms may be used. With an empty `groups` object, every candidate belongs
to the single empty-string group.

### Source-path pattern

```json
"groups": {
  "source_pattern": "chapter-(\\d+)\\.xhtml#",
  "capture_kind": "decimal",
  "capture_width": 2
}
```

`source_pattern` is a case-insensitive regular expression with exactly one
capturing group. It searches each source locator, including its `sNNN:` spine
prefix. Every candidate must match.

### Source marker

```json
"groups": {
  "source_marker": {
    "pattern": "^CHAPTER\\s+([IVXLCDM]+)\\b",
    "case_insensitive": true
  },
  "capture_kind": "roman",
  "capture_width": 2
}
```

The anchored marker pattern has exactly one capture and is matched against
normalized candidate text. A match establishes the group for that candidate
and following candidates until another marker matches. A candidate used for
output before the first marker is an error. Marker groups may not repeat.
Grouping is computed before skips and replacements, so a non-emitted marker can
still establish a group.

### Explicit transitions

```json
"groups": {
  "transitions": {
    "text/front.xhtml#1": "front",
    "text/chapter-01.xhtml#1": "chapter-01"
  }
}
```

A transition assigns its group to the named candidate and all following
candidates until the next transition. Every transition locator must select
exactly one candidate. Transitions are useful when filename patterns alone do
not express subsections or when headings do not contain machine-readable
numbers.

### Capture conversion

`source_pattern` and `source_marker` accept these shared members:

- `capture_kind`: `string` (default), `decimal`, or `roman`;
- `source_offset`: integer added to a numeric captured value; default `0`;
- `source_map`: nonempty mapping from raw captures to final group names; and
- `capture_width`: positive zero-padding width for numeric groups.

Decimal conversion removes leading zeroes. Roman conversion is
case-insensitive but requires canonical subtractive notation. A mapped capture
is already a final group name. When `source_map` is present, it must include
every raw capture encountered; a missing key is an error, with no fallback to
capture conversion. A map cannot be combined with `capture_width` or a nonzero
`source_offset`. These capture options require a source pattern or marker and
do not apply to explicit transitions, even when `source_offset` is `0`.

## Identifier generation

The required block counter accepts `{group}` and `{number}` fields, and its
template must contain `{number}`:

```json
"block": {"template": "{group}.{number:03d}", "start": 1}
```

The optional line counter accepts `{group}`, `{block}`, and `{number}` and must
contain both `{block}` and `{number}`:

```json
"line": {
  "template": "{group}.{block:03d}.{number:02d}",
  "start": 1
}
```

Templates use Python’s ordinary format mini-language. Conversions and nested
replacement fields are not supported. Counter starts are nonnegative and
default to `1`.

Every emission has a nonempty `type` and one of four roles:

- `block`: allocate the next block number in the current group;
- `line-start`: allocate a new block number and the first nested line number;
- `line`: allocate the next line under the most recent `line-start` in the
  current group; or
- `fixed`: render the required `id`, which can contain `{group}` but does not
  advance a counter.

An emission may also set `allow_empty: true` (default `false`). This permits
an intentionally empty output but does not itself retain empty source
candidates; use `keep_empty` for those. The flag is available in defaults,
rules, replacement outputs, and insertion outputs. It does not turn an empty
slice or a prefix consuming all text into a valid transformation.

An `id` is only allowed for `fixed`. A line role requires a line template, and
`line` without a preceding `line-start` in the same group is an error. Every
generated ID must be unique.

Fixed IDs are suitable for structural labels such as `{group}.head` and for
sparse exceptions such as an inserted footnote. They do not require an
exhaustive identifier list.

Ordinary `block` or `fixed` emissions, including replacement outputs, end the
active verse sequence in their group. A `fixed` insertion preserves that
sequence, so a footnote between verse lines leaves the next line number
unchanged. Other insertion roles have their normal counter and verse-state
effects.

## Default emission and rules

`default` handles every ordinary candidate that matches no rule:

```json
"default": {"type": "paragraph", "role": "block"}
```

Rules are tested in array order; the first match wins. A rule has an emission
plus a nonempty `match` object:

```json
{
  "match": {
    "tag": "p",
    "classes_all": ["verse"],
    "locators": ["text/chapter-*.xhtml#*"],
    "text_pattern": "^Sing",
    "case_insensitive": true
  },
  "type": "line",
  "role": "line-start"
}
```

All configured match criteria must pass:

- `tag`: case-insensitive exact element name;
- `classes`: exact class-token set;
- `classes_any`: at least one token must occur;
- `classes_all`: every token must occur;
- `locators`: at least one case-insensitive glob must match; and
- `text_pattern`: regular-expression search in normalized text, optionally
  controlled by `case_insensitive`.

`classes`, `classes_any`, and `classes_all` compare class tokens exactly.
`classes: []` matches only elements with no class tokens. Empty `classes_any`,
`classes_all`, and `locators` arrays impose no restriction and do not count as
match criteria. A match must contain at least one effective criterion; a
`case_insensitive` flag alone is not one.

### Structural consume and emit

A rule can combine or discard consecutive candidates:

```json
{
  "match": {"tag": "h1"},
  "type": "heading",
  "role": "fixed",
  "id": "{group}.000",
  "consume": 2,
  "emit": [2],
  "separator": " "
}
```

- `consume` is a positive count, default `1`.
- `emit` is a nonempty array of unique one-based positions within the consumed
  candidates. It defaults to all positions in order.
- `separator` joins emitted fragments. It defaults to a space for multiple
  fragments and to the empty string for one.

The consumed candidates must remain within one group and may not cross a skip,
replacement, or insertion anchor. All consumed locators are recorded in the
compiled plan, including candidates that are not emitted.

A one-fragment rule can remove a required structural prefix:

```json
"remove_prefix": {
  "pattern": "^CHAPTER\\s+[IVXLCDM]+\\s+",
  "case_insensitive": true
}
```

The pattern must begin with `^`, cannot match an empty prefix, must match the
candidate, and must leave nonempty text. Prefix removal compiles to an explicit
fragment slice; retained text keeps its original casing.

## Skipped source blocks

`skip_source` is an array of exact local source locators. Each must identify one
selected candidate. Skipped candidates are accounted for but produce no
output. They still participate in group-marker and transition processing.

## Source-anchored replacements

A replacement substitutes one or more outputs for a selected source block:

```json
"replacements": [
  {
    "anchor": "text/chapter.xhtml#4.2",
    "outputs": [
      {
        "type": "paragraph",
        "role": "block",
        "parts": [
          {
            "document": "text/chapter.xhtml",
            "element_path": "4.2",
            "slice": {"start": 0, "end": 42}
          }
        ]
      },
      {
        "type": "paragraph",
        "role": "block",
        "parts": [
          {
            "document": "text/chapter.xhtml",
            "element_path": "4.2",
            "slice": {"start": 43, "end": 80}
          }
        ]
      }
    ]
  }
]
```

The anchor and every part locator must identify a selected candidate, and the
anchor must occur in at least one output part. Part locators are reserved and
removed from ordinary rule processing. The anchor’s group is used for every
replacement output. Output IDs are generated in array order using the same
roles and counters as ordinary rules.

Each part requires `document`. `element_path` defaults to the XHTML `body`;
otherwise it is a dot-separated one-based child path. `omit` lists relative
descendant paths. Duplicate and ancestor/descendant-overlapping omissions are
invalid. `slice` is a zero-based, half-open code-point range satisfying
`0 <= start < end`.

Several parts in one output express a join. Several outputs can express a
split. If a `document`/`element_path` pair is reused anywhere among
replacements, every use must have a non-overlapping slice and the same set of
`omit` paths. The order of those paths does not matter. This ensures the slice
offsets refer to the same normalized fragment. Adjacent slices are allowed;
whole-fragment reuse is not.

## Source-anchored insertions

An insertion emits material immediately after a selected, ordinarily emitted
or replaced anchor:

```json
"insertions": [
  {
    "after": "text/chapter.xhtml#8.1",
    "outputs": [
      {
        "type": "footnote",
        "role": "fixed",
        "id": "{group}.008-fn",
        "parts": [
          {"document": "text/notes.xhtml", "element_path": "2.1"}
        ]
      }
    ]
  }
]
```

The anchor must identify one selected candidate. Unlike replacement parts,
insertion parts may address other XHTML in the EPUB, including non-linear or
otherwise unselected note documents. Insertions do not reserve their part
locators. Their outputs use the anchor group and the ordinary identifier
allocator, with fixed-ID insertions preserving any active verse sequence.

If an ordinary rule consumes several candidates, an insertion can follow only
the last consumed candidate, and that candidate must occur in `emit`. An
insertion anchored to a discarded candidate is an error. An insertion at a
replacement anchor follows all outputs from that replacement.

## Compilation and digest

Compilation creates an immutable `CompiledRecipe`; there is no serialized
compiled-recipe format. Each `CompiledBlock` records its generated ID and type,
fragments, separator, and any source locators consumed by an ordinary rule. The
compiled recipe separately records sorted skipped and replacement-reserved
locators.

The canonical digest includes all generated IDs and types, fragment selection,
counter effects, separators, consumed locators, skips, reservations,
normalization, and EPUB-type omissions. It therefore detects structural rule
changes even if the resulting prose happens to look similar.

While authoring, compile without digest verification and save the result:

```python
from epub_blocks import compile_recipe, compiled_recipe_digest

compiled = compile_recipe("book.epub", recipe, verify_digest=False)
recipe["output"]["compiled_sha256"] = compiled_recipe_digest(compiled)
```

Normal compilation and extraction require and verify `compiled_sha256`.
Metadata and the EPUB’s external filesystem path are excluded from the digest;
the EPUB SHA-256 separately pins the input file.

## Output APIs

`extract_recipe` and `extract_recipe_file` return ordered `ExtractedBlock`
values. `write_tsv` and the CLI write headerless columns:

1. generated `id`;
2. generated `type`;
3. extracted `text`.

The file is UTF-8, with literal TAB field separators and LF record endings.
There is no CSV quoting or escaping: quotes and backslashes are ordinary field
characters. Read each record by splitting on literal tabs, not with a CSV
reader's default quote handling. Embedded TAB, CR, or LF characters in any
field are rejected; the writer does not normalize them or replace the existing
output file on failure. XML character references can represent these characters
inside markup without introducing raw field/record separators.

```bash
epub-blocks book.epub recipe.json records.tsv
```

## Schema and runtime validation

The distributed JSON Schema is
`epub_blocks/schemas/recipe-v1.schema.json`. It catches structural errors in
editors and pipelines. Runtime validation is authoritative and additionally
checks regular expressions, templates, EPUB contents, group captures, Roman
numerals, locator uniqueness and disposition, fragment ranges, counter state,
generated-ID uniqueness, source and compiled hashes, and all extraction safety
limits.
