# Marked-up text

This is the marked-up text contract for epub-blocks 0.4.0.
It extends recipe version `"1"`; it does not introduce a separate recipe format.

The output remains headerless `id`, `type`, `text` TSV. With `text.markup`
enabled, the third column contains a marked-up intermediate. A separate
consumer can derive plain text and stand-off annotations together. This
package does not produce that stand-off output or define its schema.

## One extraction policy, two serializations

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

For a source paragraph containing emphasis and an inline page marker:

```text
XML:        A <em>quiet</em> word.<page label="iv"/>
Delimiters: A ⧼quiet⧽ word.⟦iv⟧
```

Delimiter tokens are recipe configuration, not a fixed list of styles hard-coded
into the extractor. A consumer must know the recipe's name/kind/delimiter mapping.

## Markup rules

Rules are ordered, first-match-wins, and use the structural selectors described
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
text/subtree rather than transcribing it. A milestone may have
`label_attribute`, the exact source attribute name from which to obtain a
nonempty label. Namespaced attributes use expanded XML names, such as
`{http://www.idpf.org/2007/ops}type`. Labels are opaque strings, not integers,
and are escaped without whitespace or Unicode normalization.

A name cannot be used for both kinds. `label_attribute` is not allowed for
spans. A missing/empty required label is an error. Matching a milestone around
another retained milestone is an error rather than silently losing the nested
event. If one element has several relevant styles, give its combined condition
a rule/name before the more general rules; first-match-wins does not apply
multiple span wrappers to the same source element.

Do not also list a retained semantic type in `omit_epub_types`. For example,
page-retaining recipes must remove `"pagebreak"` from their omission list.
Conflicting retention/omission is rejected. Explicit structural skips and
source filters still define what is intentionally outside the extraction.
Descendants of a semantically omitted subtree do not participate in the
nested-milestone check, because they are not retained events.

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

## Milestones between blocks

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
   block boundaries for retained elements only; transparent inline wrappers
   and omitted subtrees insert none. Empty retained blocks can still introduce
   their configured boundaries.
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
spans and across transparent wrappers is supported. The two serializers share
the same check.

Empty slices and a prefix consuming all text remain errors. Intentional empty
structural output rows instead require both `keep_empty` in source selection
and `allow_empty` in the output rule. Such rows are an alternative to attaching
a zero-width milestone, not a requirement to count separators as paragraphs.

## Compatibility and scope

Without the new options, extracted fields and compiled digests remain compatible
with 0.3.0. TSV bytes deliberately change wherever the former writer used
CSV-style quoting. All new content policies, empty-output permissions, and event attachments
are pinned in the compiled plan. XML/delimiter serialization changes the plan
digest even though references and underlying text remain the same.

The lower-level `extract_blocks` and `extract_fragments` APIs retain their
plain-text inspection contract. Recipe extraction applies the new content
policies. The immutable compiled plan exposes typed `ContentOptions`, selectors,
rules, and attachment fragments; its state is not a stand-off annotation export.
