# Marked-up text

This is the marked-up text contract as implemented in epub-blocks 0.8.0.
It extends recipe version `"1"`; it does not introduce a separate recipe format.

Version 0.6.0 additionally supports
`attribute_prefixes` in shared selectors. For example, a page milestone rule
can match `{"tag": "span", "empty": true, "attribute_prefixes": {"id": "page_"}}`
and use `"label_attribute": "id"`. Only page-prefixed IDs match, and labels
retain their complete source value unless the recipe explicitly opts into
`label_strip_prefix` (added in 0.8.0).

The output remains headerless `id`, `type`, `text` TSV. With `text.markup`
enabled, the third column contains a marked-up intermediate. A separate
consumer can derive plain text and stand-off annotations together. This
package does not produce that stand-off output or define its schema.

## One extraction policy, three serializations

```json
"text": {
  "markup": {
    "format": "xml",
    "between_blocks": "next",
    "trailing": "error",
    "rules": [
      {"match": {"tag": "em"}, "kind": "span", "name": "em"},
      {
        "match": {"epub_types": ["pagebreak"]},
        "kind": "milestone",
        "name": "page",
        "label_attribute": "title"
      },
      {
        "match": {"tag": "div", "classes_all": ["scene"], "empty": true},
        "kind": "milestone",
        "name": "scene-break"
      }
    ],
    "delimiters": {
      "em": ["⧼", "⧽"],
      "page": ["⟦", "⟧"],
      "scene-break": ["⟬", "⟭"]
    }
  }
}
```

Change only `format` to `"delimiters"` to select the other serialization.
As with any deliberate recipe change, recompute and review the compiled-plan
digest. The format choice does not change block IDs, types, normalized text,
or span/milestone positions. Both formats can use the same rules and delimiter
table; the table is optional for XML but required for every used name in
delimiter mode. Defined but unused names are rejected.

A third format, `"literal"`, uses the same rules and offsets but emits source
text and configured tokens without escaping. It supports ambiguous legacy
notation and does not promise reversible decoding. See
[literal notation](#literal-notation-and-exact-whitespace-080) before choosing it.

For a source paragraph containing emphasis and an inline page marker:

```text
XML:        A <em>quiet</em> word.<page label="iv"/>
Delimiters: A ⧼quiet⧽ word.⟦iv⟧
```

Delimiter tokens are recipe configuration, not a fixed list of styles hard-coded
into the extractor. A consumer must know the recipe's name/kind/delimiter mapping.

## Spacing and markup

Selective `text.block_boundaries.rules` can insert real whitespace while
`text.markup.rules` independently records the source spacing element. For
example, a boundary rule matching `span.space` with `"before": " "`, combined
with an empty-element milestone rule, can represent the invented source
`left<span class="space"/>right` as:

```text
XML:        left <space/>right
Delimiters: left ⟬⟭right
Plain text: left right
```

The space before the marker is an actual U+0020 in both serializations. The
marker records source structure; removing the marker must not remove that
textual space. Both representations therefore share offsets that already
include the inserted whitespace. This differs from older schemes where a
visible spacing glyph must later be replaced by whitespace. Delimiter syntax
is unchanged: even an unlabeled milestone has an opening/closing pair, not
a new single-token escape convention.

Use `"empty": true` on a milestone rule intended only for empty spacing
elements. By default a milestone replaces its matched contents. If a spacing element
can contain text that should survive, use a span rule instead (or an
empty-only milestone followed by a differently named span rule). A
before-only boundary rule with span markup might give
`left <spaced>right</spaced>end`, whose plain text is `left rightend`.

Inserted whitespace lies outside the matched element's marker/span, inside
any enclosing markup. Whitespace normalization may merge it with neighbouring
source whitespace, retaining the first contributing run as usual. Slices
and prefix removal count this normalized text and use the existing rules for
clipping spans and assigning a milestone at a slice boundary.

Boundaries apply only inside a selected fragment. A detached milestone
attached to the start/end of another fragment does not acquire artificial
whitespace from a rule on its own root; record boundaries already separate
paragraphs. There is no CSS interpreter or inferred spacing: recipe authors
choose the structural selectors, sides, and markup deliberately.

## Preserving preformatted whitespace (0.7.0)

A span rule may set `"preserve_whitespace": true` to retain whitespace inside
that element and its descendants while ordinary prose still uses the recipe's
normalization policy. Select the entire preformatted element as a source block
or include it inside a larger selected block; rules do not inherit from ancestors
outside a selected fragment.

```json
{"match": {"tag": "pre"}, "kind": "span", "name": "pre", "preserve_whitespace": true}
```

Leading/trailing spaces, tabs and newlines within the span survive, including
whitespace-only spans and whitespace at slice/join boundaries. Unicode
normalization still applies. Candidate text and slice offsets refer to decoded
logical code points, not the length of serialized escapes. XML parsing still
performs its normal source line-ending normalization before extraction.

When any rule enables this option, XML serializes tabs and newlines as `&#9;`
and `&#10;` (carriage returns as `&#13;`). Delimiter mode uses `\t`, `\n` and
`\r`; literal backslashes remain `\\`. These escapes apply to text and labels
throughout that marked output. Decoders must distinguish `\n` (one newline)
from `\\n` (a literal backslash followed by `n`). Delimiter tokens must not
contain `n`, `r`, `t`, or tab/newline/carriage-return characters when this option
is enabled, to avoid collisions with escaped delimiter characters.

Each TSV record remains one physical line with exactly three literal tab-separated
fields. No CSV-style quoting is introduced. The default is `false`; absent and
explicitly false options retain the previous normalization and digest behavior.
This option is valid only on spans, not milestones.

## Attaching milestones before reordering (0.7.0)

`text.markup.attachment_order` defaults to `"output"`, retaining the existing
guard requiring source-ordered fragments when detached milestones are attached.
Set it to `"source"` when a recipe deliberately relocates text, such as moving a
footnote from the end of a chapter to its callout. Detached events are resolved
against source-ordered fragments and then follow their owning fragments into
output order. `between_blocks` and `trailing` still determine attachment policy.

A trailing event follows the last fragment in source order, even if that
fragment becomes an earlier part of an output join. Disjoint slices of one
source element are supported; repeated or overlapping fragments remain errors.
Overlapping parent/descendant source subtrees are rejected as ambiguous. This
option neither changes textual readings nor infers a reading order for the book.

When detached markers need owners, same-element reuse is checked across all
in-scope output fragments, including insertions and ordinary emissions, in
both attachment orders. Reuse requires non-overlapping slices with identical
omissions. Notes from unselected documents are not marker owners; this check
does not prohibit their reuse or reuse when there are no detached markers.

## Markup rules

Rules are ordered and first-match-wins by default, using the structural selectors described
in the [recipe specification](recipe-format.md#structural-element-selectors).
They apply to selected roots as well as descendants. An unhandled inline
element is transparent: its text and children remain, without invented spaces.
Thus a drop cap or wrapper splitting a word does not split the extracted word.

Each rule requires:

- `match`: a nonempty structural selector;
- `kind`: `"span"` or `"milestone"`;
- `name`: an ASCII XML name matching `[A-Za-z_][A-Za-z0-9_.-]*`.

`span` wraps the retained contents, with normal nesting. `milestone` represents
the selected source element as a zero-width event, replacing that element's
text/subtree rather than transcribing it by default (`position: "replace"`).
The `position: "before"` alternative retains those contents, as
described below. A milestone may have
`label_attribute`, the exact source attribute name from which to obtain a
nonempty label. Namespaced attributes use expanded XML names, such as
`{http://www.idpf.org/2007/ops}type`. Labels are opaque strings, not integers,
and are escaped without whitespace or Unicode normalization. The
`label_text` alternative below deliberately has its own whitespace policy.

A name cannot be used for both kinds. `label_attribute` is not allowed for
spans. A missing/empty required label is an error. Matching a milestone around
another retained milestone is an error rather than silently losing the nested
event (unless it is a content-preserving leading milestone). If one element has several relevant styles, give its combined condition
a rule/name before the more general rules; first-match-wins does not apply
multiple span wrappers to the same source element.
An opt-in leading rule can also continue to another effect on the same element;
see [composed markup](#composing-effects-on-one-element-060).

Do not also list a retained semantic type in `omit_epub_types`. For example,
page-retaining recipes must remove `"pagebreak"` from their omission list.
Conflicting retention/omission is rejected. Explicit structural skips and
source filters still define what is intentionally outside the extraction.
Descendants of a semantically omitted subtree do not participate in the
nested-milestone check, because they are not retained events.

### Text-derived labels (0.6.0)

Some sources store printed page or verse numbers in an element's contents
instead of an attribute. To retain that content as a zero-width label:

```json
{
  "match": {"tag": "p", "classes_any": ["printed-number"]},
  "kind": "milestone",
  "name": "printed-line-number",
  "label_text": true
}
```

For `<p class="printed-number"> 005 </p>`, this emits
`<printed-line-number label="005"/>`. When detached, it follows the same
explicit attachment policies as an attribute-derived milestone; it does not
consume a block or line number or contribute `005` to the reading. A number
inside a selected verse wrapper stays on that line. This does not infer a
citation reference from the printed number; output rules still generate IDs.

Contract:

- `label_text` is a boolean, default `false`, and is only valid on milestones
  (even an explicit `false` is invalid on a span rule).
- `true` and `label_attribute` are mutually exclusive. `false` with an
  attribute label is allowed and has the same meaning as omitting the option.
- The label uses retained text from the matched element and its descendants,
  excluding its outer tail. Source skips, semantic omissions, fragment
  omissions, original selector context, and nested whitespace boundaries
  apply. Nested span markup is flattened, not serialized into the label.
- Leading/trailing whitespace is removed and every run of Python whitespace
  becomes one ordinary space, independently of the recipe's prose
  normalization settings. Leading zeroes, case, punctuation, and Unicode
  code points are otherwise unchanged. Labels are not parsed as numbers or
  Unicode-normalized. Existing attribute-derived labels remain opaque and
  are **not** subjected to this whitespace policy.
- An empty retained label is an error. With the default `position: "replace"`, a retained nested milestone still
  raises an overlap error; it is not silently folded into or lost from the
  label. Use a span or separate source structure when both events must survive.
- XML and Unicode serializers escape the same resulting label using their
  existing rules. Prose offsets and slices count neither labels nor markup.
- A leading-only text-label bundle on a wrapper requires that the wrapper be
  inside an explicitly selected source block or fragment. Selecting only its
  descendants is rejected: the extractor cannot infer the wrapper's retained
  label text from separately omitted, skipped, or sliced descendant fragments.
  Select the wrapper itself with a `source_blocks.element_rules` rule using
  `"action": "block"`, then apply any fragment omissions to that wrapper. This
  requirement does not affect detached **replacing** text labels, attribute
  labels, or original ordered-list counters. An entirely unused wrapper is
  ignored.

The public `MarkupRule` model has the corresponding `label_text: bool = False`
field. The schema, runtime parser, and compiled-policy hash use the same
contract. Only `true` is stored in the canonical policy, so recipes that omit
this option or set it to `false` retain their previous compiled digests.

### Leading labels and ordered lists (0.6.0)

`position: "before"` emits a milestone immediately before the matched
element's retained contents, instead of replacing them. Text, spans, and
nested milestones survive. The event remains zero-width: it does not add
words to the reading, consume a citation number, or change text offsets.
This option also works with existing attribute/text labels or no label.

For HTML-generated list numbers:

```json
{
  "match": {"tag": "li"},
  "kind": "milestone",
  "name": "list-number",
  "position": "before",
  "label_counter": "ordered-list"
}
```

With `"list-number": ["⟬", "⟭"]` in the delimiter table, the invented
source `<ol><li value="5"><p>First</p></li><li><p>Next</p></li></ol>` yields:

```text
XML:        <list-number label="5"/>First
            <list-number label="6"/>Next
Delimiters: ⟬5⟭First
            ⟬6⟭Next
```

Counter contract:

- A match must be an original `li` directly inside an `ol`. Use locators or
  other selectors to distinguish ordered from unordered items in mixed books.
- Numbers follow original direct-child list order: default 1, overridden by
  `ol start`; `li value` resets that item's number and following progression.
  A present `reversed` attribute decrements; its default start is the original
  direct-item count. Nested lists have independent state. These are the
  [HTML ordinal rules](https://html.spec.whatwg.org/multipage/grouping-content.html#ordinal-value).
- Filtering, skipping, slicing, or omitting an earlier item never renumbers
  later ones. Resets on omitted items still count. Source context is retained
  even when extracting a copied fragment or a single nested paragraph.
- Only decimal numbering is supported: absent `type` or `type="1"` on the
  list and its direct items. Other explicit types are errors. CSS counters,
  `list-style-type`, and visual punctuation are not interpreted; authors must
  check the source styling before choosing this rule. The emitted label is
  the ordinal, not a claim to reproduce the rendered marker glyphs.
- Start/value attributes must be whole ASCII integers, with optional sign
  and surrounding HTML ASCII whitespace. Zero and negatives are supported.
  Output is canonical decimal (`+005` becomes `5`). Malformed/overlarge values
  fail rather than applying browser error recovery. All direct items in a
  used list are validated, including omitted siblings.

Selection and attachment contract (also applies to other leading labels):

- Selecting the whole list/item includes each leading event once in its
  normal subtree position. Boundaries still control spacing between items.
- A leading rule does not itself turn a wrapper into a text block. With
  nested-paragraph selection an attribute or counter event attaches once,
  before the first retained descendant fragment; later paragraphs get no
  duplicate label. Leading text-derived labels instead require explicit
  wrapper selection, as described above.
  Nested prefixes retain outer-before-inner source order at a shared offset.
- A wrapper whose entire subtree is excluded contributes no leading event
  to the next unrelated item. Structural skips remove the wrapper/subtree;
  ordinary candidate filters still apply to the selected candidate elements.
  A retained detached child marker also counts as retained subtree content:
  its textless wrappers' leading bundles accompany it under the same
  `between_blocks`/`trailing` policy, in outer-before-inner order. For example,
  a page-labelled wrapper containing only a retained image keeps both markers
  on the following caption. A genuinely empty or entirely omitted wrapper
  still contributes nothing. Source-ordered fragment requirements still apply.
  Detached leading bundles are validated only when retained: invalid labels
  or duplicate effects in unused wrappers do not abort extraction. Candidate
  filters likewise run before markup validation. Once a list counter is used,
  its original list's numbering must still be valid, including omitted items.
  Unused leading bundles are also discarded before checking output order;
  they cannot forbid reordering when no retained detached event needs attachment.
- An explicitly selected fragment owns its markers: omitted or sliced-away
  events are not restored as attachments. Slices use the usual zero-width
  boundary rules. A replacing ancestor cannot silently discard a retained
  leading descendant; the existing overlap error still applies.

`position` is milestone-only, with values `"replace"` (default) and `"before"`.
`label_counter` is milestone-only and currently accepts only `"ordered-list"`;
it requires `position: "before"` and is mutually exclusive with
`label_attribute` or true `label_text`. A false `label_text` is allowed.
The public `MarkupRule` adds `position: str = "replace"` and
`label_counter: str | None = None`. Runtime, typed-state validation, JSON
Schema, and compiled hashes share this contract. Default position and absent
counter fields do not change existing recipe digests. Recipe version stays `"1"`.

### Composing effects on one element (0.6.0)

A source element can carry more than one kind of information: for example,
an inline image may also have a page ID, or an italic paragraph may start a
page. Add `continue_matching: true` to a **leading milestone** rule to emit
its event and continue searching later rules for that same element:

```json
"rules": [
  {
    "match": {"attribute_prefixes": {"id": "page_"}},
    "kind": "milestone",
    "name": "page",
    "label_attribute": "id",
    "position": "before",
    "continue_matching": true
  },
  {
    "match": {"tag": "img"},
    "kind": "milestone",
    "name": "image",
    "label_attribute": "src"
  },
  {
    "match": {"classes_all": ["italic"]},
    "kind": "span",
    "name": "em"
  }
]
```

The source `<img id="page_5" src="letter.jpg"/>` now emits
`<page label="page_5"/><image label="letter.jpg"/>`. The source
`<p id="page_6" class="italic">A word.</p>` emits
`<page label="page_6"/><em>A word.</em>`. In delimiter mode, with pairs
`page: ["⟦", "⟧"]`, `image: ["⟮", "⟯"]`, and `em: ["⧼", "⧽"]`, the same
outputs are `⟦page_5⟧⟮letter.jpg⟯` and `⟦page_6⟧⧼A word.⧽`.

The contract is deliberately explicit:

- `continue_matching` is a strict boolean, default `false`. Omission or
  explicit `false` preserves existing behavior and compiled digests. A true
  value is part of the compiled content policy even if no later rule matches.
- `true` requires `kind: "milestone"` and `position: "before"`. A span or
  replacing milestone cannot continue; it would introduce ambiguous wrapping
  or replacement behavior. Use an existing combined style name for multiple
  styles on one element, rather than stacking span rules.
- After a continuing match, nonmatching rules are skipped. The next matching
  rule is applied and ends the search unless it also explicitly continues.
  Thus several leading effects can precede at most one ordinary effect. If
  no further rule matches, the leading markers precede the retained contents.
- Prefix milestones are emitted in matching-rule order, before any span on
  that element. Child markup retains source order and nesting. An image's
  replacing effect still omits the image subtree, not its sibling tail.
- Each composed effect must have a distinct name on the matched element;
  repeating a name is a runtime error. Reusing a name in non-overlapping
  selectors or on different source elements remains allowed. Every applied
  label must be valid; later label errors are not hidden by an earlier match.
- Attribute labels remain opaque. Text-derived labels read the element's
  retained source contents, not labels emitted by other effects. Ordered-list
  labels retain the original source counter rules. A leading-only bundle with
  any text-derived label requires explicit wrapper selection; descendant-only
  selection cannot supply its retained-content projection.
- Effects on one element form one bundle for detached placement and duplicate
  source-use checks. A detached page-plus-image bundle follows normal
  `between_blocks`/`trailing` policies; a leading-only bundle belongs to its
  retained subtree. When that subtree consists of detached child markers,
  the leading bundle accompanies those markers under their attachment policy.
  It is attached once, not once per effect. This does not make unselected
  ancestor spans inherited.
- Slicing keeps coincident zero-width markers together under the existing
  shared-boundary rules. Omitting their element removes the bundle. Reusing
  the element's markers through another fragment still raises an error.
- Source skips, semantic-omission conflicts, and protection against replacing
  a nested milestone remain in force. A leading effect and a replacing effect
  on the **same** element do not constitute nested source milestones.

The example's leading page rule suits content-bearing paragraphs and inline
images. For separate empty page anchors that should attach to a following
block, put a more specific **replacing** page rule first; a leading marker on
an empty, unselected wrapper has no retained descendant to attach to.

The public `MarkupRule` gains `continue_matching: bool = False`. JSON Schema,
recipe parsing, compiled-model validation, candidate inspection, extraction,
and all serializers use the same policy. Recipe format remains `"1"`.

## XML fragment grammar and escaping

The third column is an XML **fragment**, not a standalone XML document:

- literal text escapes `&`, `<`, and `>`;
- spans are `<name>contents</name>`;
- unlabeled milestones are `<name/>`;
- labeled milestones are `<name label="escaped value"/>`;
- attribute values also escape both quotation marks;
- carriage returns in text and tabs/newlines/carriage returns in labels use
  character references so XML parsing does not normalize their values.

Wrap a field in a synthetic root to parse it. There are no namespaces,
declarations, external entities, or executable content in the generated markup.
Literal text resembling tags remains text after parsing. The TSV has no quoting
or escaping layer: split each record on literal tabs before parsing the XML
fragment. Attribute quotation marks appear once, exactly as shown above, not
doubled or wrapped in extra quotes. Characters forbidden by XML 1.0 (for example, a NUL
introduced by a join separator) cause an error rather than invalid XML output.

## Delimiter grammar and escaping

For legacy literal notation, see the opt-in mode below. The existing
`delimiters` format keeps its strict grammar and escaping unchanged.

Each name has a two-item `[opening, closing]` array. Both tokens must be
nonempty and different. Across all names, tokens must be distinct and
prefix-free: no complete token may be the prefix of another. Backslash is
reserved for escaping and cannot occur in a delimiter token.

- A span is `opening + escaped contents + closing` and may nest.
- A milestone is `opening + escaped label + closing`, or just the pair if
  unlabeled. Its label is data, not nested markup.
- In literal text and labels, prefix every backslash and every character
  appearing in any configured delimiter token with a backslash.
- An escape consumes the backslash and exactly the following Unicode code
  point. A trailing backslash is not a complete encoded field.

For the example table above, a literal `⧼` becomes `\⧼` and a literal
backslash becomes `\\`. This character-level escape rule also works with
multi-character delimiter tokens. Delimiter tokens themselves are emitted
verbatim, not escaped. Escaping takes place after normalization and slicing.

The TSV writer rejects raw TAB, CR, or LF characters in any field, including
delimiter tokens and opaque labels. It does not silently normalize them.
Choose a TSV-safe delimiter table; for labels containing such characters, XML
serialization represents them with character references. The extraction API
can still return strings that are not representable as plain TSV fields.

## Literal notation and exact whitespace (0.8.0)

`text.markup.format: "literal"` emits configured tokens, source text and
labels verbatim. Unlike `delimiters`, it does **not** insert backslash escapes.
Each name still needs a two-string entry in `delimiters`, but the strings may
be identical or share prefixes, and one may be empty. At least one token in
each pair must be nonempty. Tokens cannot contain TAB, CR or LF.

For example, `"paragraph": ["¶", "¶"]` wraps a span in the same token on
both sides; `"line-break": ["∥", ""]` emits an unlabeled milestone as a
single token. No new block types, references, or reading text are invented.

This format deliberately has **no general decoding or round-trip guarantee**:
literal source text can collide with configured tokens. Use it only when
matching an established literal convention. It is not HTML and must be
escaped for display on the web. TSV output still rejects raw TAB, CR and LF;
literal mode does not quote or conceal them. It also does not use the
preformatted `\\n`/`\\t` escape layer, even if a span preserves whitespace.

Two independent, opt-in markup settings apply before slicing and rendering:

- `strip_outer_whitespace: true` removes whitespace outside the marked-up
  fragment. Span and milestone nodes stop trimming, including empty markers;
  padding inside an outer span is retained. This is distinct from ordinary
  `normalization.strip`, which trims the decoded reading across markup.
- `remove_source_newlines: true` removes source LF characters from text nodes.
  It does not collapse spaces, remove non-breaking spaces, modify labels or
  remove line-break milestones. XML parsing has already normalized source
  CR/CRLF line endings. Explicit newline removal also applies inside preserved
  spans; it should not be enabled for preformatted text needing those LFs.

Both default to `false` and work in XML and escaped-delimiter formats too.
They run after ordinary normalization. To preserve raw spacing while trimming
only outside markup, set `normalization.collapse_whitespace` and
`normalization.strip` to `false`, then enable `strip_outer_whitespace`.
Candidate readings, slice offsets, joined output and serializers use the same
cleaned tree. Existing policies with absent/false options retain their digests.

If cleanup changes the reading, normalization is repeated until the cleaned
reading is stable, before any matching or slicing. In particular, removing an
LF between a letter and a combining accent must not leave candidate text in
a different Unicode form from the final output. If the new adjacency would
compose across a retained markup boundary, candidate inspection and compilation
reject it using the usual boundary safeguard. Opaque labels remain unchanged.

A milestone with an attribute label may use `label_strip_prefix`, a nonempty
literal prefix. For example, `label_attribute: "id"` with
`label_strip_prefix: "page"` changes label `page31` to `31`. A missing prefix
or empty suffix is an error. This is exact prefix removal, not numeric parsing
or global replacement; `page003` retains `003`.

Finally, `between_blocks: "ignore"` and `trailing: "ignore"` explicitly
discard otherwise detached milestones in their respective positions. Inline
milestones inside selected chunks remain. Existing source-safety, label and
attachment validation still apply; this is not a rule for dropping text.
Subtree-owned leading events still follow retained descendants.

## Milestones between blocks

### Default attachment behavior

This section describes the default **replacing** milestones. Leading events
use the subtree-bound attachment rules above.

Page-only paragraphs, empty scene separators, and milestone elements in
otherwise unselected wrappers are discovered in the selected documents, not
just inside nonempty text blocks. Discovery honours structural skips,
semantic omissions, candidate filters, and the selected source scope.

`between_blocks` defaults to `"error"`. Setting it to `"next"` attaches
detached milestones to the next emitted fragment from the selected source
documents, before its text. No row or
counter is allocated. If a consuming rule joins several fragments, a milestone
between them remains before the following fragment, after the join separator;
it is not moved to the beginning of the combined output block.

`trailing` defaults to `"error"`. Setting it to `"previous"` attaches any
remaining milestones immediately after the last fragment from the selected
source documents. If its output block also joins out-of-scope note fragments,
the markers precede the join separator and note text. Neither policy
silently discards an event. Consecutive milestones retain source order, even
at the same text position. Attachment can cross selected source-document or
group boundaries. Inserted fragments from non-spine, excluded, or unselected
non-linear documents are not anchors for these events; inline milestones
within those inserted fragments are still retained normally. When there are
detached milestones, fragments from the selected source documents must be in
source order, including increasing slice starts within the same element.
Reordered output is rejected because the neighbouring destination is ambiguous.

The compiled plan records each detached event's source fragment and attachment
position. An explicit fragment owns the disposition of events in its subtree:
retained events are not attached again, and events discarded by its omissions
or slice are not relocated to another block. Reusing a retained milestone more
than once is rejected.

Before-part and after-part attachments are distinct in the compiled plan, so
moving a marker across a join separator changes the plan digest. Existing
block-end attachments keep their previous representation and digest.

## Normalization and structural transformations

Structural matching and reference generation use normalized **plain text**,
not the serialized marker strings. Spans and milestone labels cannot affect
heading recognition, counters, prefix matching, or slice lengths.

For each fragment:

1. Select the source subtree, preserving original source structure and locators
   for all selector matching, including contextual predicates.
2. Apply explicit fragment omissions and structural/semantic skips.
3. Build text with selected spans and milestones. Insert configured nested
   boundaries for retained elements only, using ordered selective rules or
   the tag fallback. Unmatched inline wrappers and omitted subtrees insert
   none. Empty retained elements can still introduce configured boundaries.
4. Collapse/trim whitespace across text runs, ignoring markers. The single
   surviving space stays with the run that contributed its first character.
5. Normalize Unicode in text, not labels or marker tokens.
6. Apply a slice or compiled prefix removal in normalized plain-text code points.
7. Join fragments and normalize the joined text, then serialize and escape.

Slicing clips and reopens enclosing spans to keep each output field balanced.
Milestones outside the selected slice are omitted with that text selection.
A milestone exactly on a shared slice boundary belongs to the slice starting
there, not both slices; at the natural end of a fragment it belongs to the
slice that reaches that end. Prefix removal follows these same slice rules.

Unicode normalization that would compose/reorder characters **across a retained
markup boundary** is rejected: assigning the resulting character to one side
would require an annotation decision. For example, `e<em>&#x301;</em>` cannot be
silently NFC-composed while retaining that exact boundary. Adjust the source
rule or explicitly select `unicode_normalization: "none"`. Ordinary NFC within
spans and across transparent wrappers is supported. All serializers share
the same check.

Empty slices and a prefix consuming all text remain errors. Intentional empty
structural output rows instead require both `keep_empty` in source selection
and `allow_empty` in the output rule. Such rows are an alternative to attaching
a zero-width milestone, not a requirement to count separators as paragraphs.

## Compatibility and scope

0.8.0 adds opt-in literal serialization, markup-aware outer trimming, source
LF removal, exact attribute-label prefix removal and explicit ignoring of
detached milestones. Without these options, valid 0.7.0 recipes retain their
fields and compiled digests. Explicit `strip_outer_whitespace: false` and
`remove_source_newlines: false` are equivalent to omission. Literal mode does
not use the escaping or decoding contract of `delimiters`, including the
preformatted control-character escapes introduced in 0.7.0.

0.7.0 adds opt-in preserved-whitespace spans and source-order milestone
attachment. Without those options, valid 0.6.0 recipes retain their fields
and compiled digests. Explicit `preserve_whitespace: false` and
`attachment_order: "output"` have the same effective policy as omission.
Enabling preserved whitespace also enables TSV-safe control-character escapes
in the chosen markup serialization, as described above.

0.6.0 adds prefix selectors, text-derived and leading labels, ordered-list
counters, and composed effects. These options are opt-in; valid 0.5.0 recipes
without them retain their fields and compiled digests. Default or false new
options are hash-neutral. Ambiguous text-label projections and conflicting
markup are rejected rather than silently changing the selected content.

0.5.0 adds selective before/after boundary rules; with no new rules, 0.4.0
recipes keep their fields and compiled digests. An explicit empty rules array
has the same effective policy as omission. Existing markup kinds, delimiter
grammar, and milestone attachment behavior are unchanged.

Without the new options, extracted fields and compiled digests remain compatible
with 0.3.0. TSV bytes deliberately change wherever the former writer used
CSV-style quoting. All new content policies, empty-output permissions, and event attachments
are pinned in the compiled plan. Changing serialization format changes the plan
digest even though references and underlying text remain the same.

The lower-level `extract_blocks` and `extract_fragments` APIs retain their
plain-text inspection contract. Recipe extraction applies the new content
policies. The immutable compiled plan exposes typed `ContentOptions`, selectors,
rules, and attachment fragments; its state is not a stand-off annotation export.
