# Changelog

All notable changes to this project will be documented here. The project uses
[Semantic Versioning](https://semver.org/).

## 0.7.0 - 2026-09-09

Recipe format remains `"1"`. New extraction features are opt-in; existing
valid 0.6.0 recipes keep their outputs and compiled digests when the new
options are absent.

- Add explicit guarded `xml_repairs` for malformed UTF-8 content documents.
  Original-byte offsets and expected strings are verified before XML parsing;
  repairs are pinned in the compiled policy and never rewrite the EPUB.

- Add opt-in span `preserve_whitespace` for preformatted regions, with logical
  code-point offsets and TSV-safe XML/control escapes in delimiter output.
- Add opt-in `text.markup.attachment_order: "source"` so detached metadata
  follows its source fragment through explicit output reordering.

- Accept XHTML doctypes with an empty (or XML-whitespace-only) internal subset.
  Continue stripping, never resolving, external identifiers; reject all actual
  DTD/entity declarations, non-prolog doctypes, and duplicate doctypes.
- Add opt-in `consume_while` for variable-length joining of consecutive source
  candidates. Preserve source order, markup, and consumed-locator provenance;
  stop at document/group and specially handled source boundaries. Reject
  conflicting fixed-count, selective-emission, and prefix-removal options.
  Recipe format remains `"1"`; existing recipes keep their compiled digests.

## 0.6.0 - 2026-09-07

Recipe format remains `"1"`. New extraction features are opt-in; valid 0.5.0
recipes retain their outputs and compiled digests without new options.

- Add opt-in `continue_matching: true` on leading milestone rules. Ordered
  leading markers can compose with a final image/replacing milestone or span
  on the same source element; ordinary rules retain first-match behavior.
  Attach coincident markers as one source bundle, preserve slicing and source
  reuse protections, and reject duplicate effect names per matched element.
  Keep schema, typed validation, and compiled policy hashing aligned; absent
  or false options preserve existing digests.
- Allow recipes to omit `epub.identifier` only for packages with no nonempty
  identifiers. SHA-256 remains mandatory, supplied identifiers must match,
  and packages with identifiers still require an identifier pin. Apply the
  same policy during candidate inspection, compilation, and extraction.
  Expose `CompiledRecipe.epub_identifier` as `str | None`; retain existing
  compiled digests and recipe format `"1"`.
- Add case-sensitive literal `attribute_prefixes` to shared structural
  selectors, including contextual predicates, source rules, markup, and
  selective whitespace boundaries.
- Add opt-in `label_text` milestone labels for numbers or other labels stored
  as element text. Retained descendant text and configured boundaries form the
  label; whitespace collapses independently of prose normalization. Labels
  remain zero-width, preserve leading zeroes, and work with both serializers.
- Reject empty text labels, non-boolean options, span usage, and simultaneous
  text/attribute label sources; retain nested-milestone protection.
- Add `position: "before"` milestones that retain matched content, including
  nested spans/events, and attach only within their source subtree.
- Preserve leading bundles on textless wrappers containing retained detached
  child markers. Attach wrapper and child events once in source order under
  the configured next/trailing policy, while still discarding labels from
  genuinely empty or fully omitted wrappers.
- Add `label_counter: "ordered-list"` for decimal list ordinals derived from
  original HTML structure, including starts, value resets, reversed and nested
  lists. Filtering/omissions do not renumber items or leak their labels.
- Validate and hash leading/counter policy consistently across recipes,
  public models, and schema; existing default-policy digests remain unchanged.
- Keep exact attribute matching and opaque attribute-derived labels unchanged;
  existing recipes retain their compiled digests when prefixes are absent.
- Require explicit wrapper selection for leading text-derived labels when only
  descendant fragments would otherwise be retained. Reject ambiguous detached
  labels instead of restoring omitted, skipped, or sliced-away source text.
- Apply candidate filters before markup validation and validate detached leading
  bundles only when attached to retained content. Unused wrappers cannot fail
  extraction because of irrelevant labels or duplicate effects; applied effects
  and all original ordinals in a used list still receive full validation.
- Discard unused leading bundles before enforcing detached-marker output-order
  constraints. Reordered output without retained detached events remains valid;
  retained prefixes and child markers still require source-ordered fragments.
- Wrap corrupt DEFLATE member failures as contextual extraction errors across
  reader APIs and the CLI, preserving their cause, read budget, and any existing
  output file.
- Add regression and failure-path tests for retention, label ownership, prefix
  removal, XML encodings, and atomic output; raise the coverage floor to 99%.

## 0.5.0 - 2026-09-06

- Add `extract_recipe_candidates` for source inspection with the same
  structural selection and text policies used by recipe compilation.
- Add ordered, source-selective whitespace boundary rules with independent
  before/after insertion, preserving text inside matched elements.
- Keep whitespace insertion independent of optional retained spacing markup;
  normalize and slice the resulting character stream consistently in both
  XML and delimiter output.
- Keep recipe format `"1"` and existing compiled digests unchanged when new
  boundary rules are absent.
- Expand regression coverage for archive safety, source ambiguity, compiled
  plan validation, and markup edge cases; raise the coverage floor to 98%.
- Make schema whitespace validation agree with the runtime in both Python
  and JavaScript, including Unicode whitespace and the exclusion of U+FEFF.
- Trigger releases from version tags, attach and verify distributions while
  the GitHub release is still a draft, and publish to PyPI only after the
  immutable GitHub release succeeds; safely resume partial publication.

## 0.4.0 - 2026-09-05

- Add ordered structural source-element rules, explicit empty-block retention,
  and opt-in checks for unclaimed source text.
- Add configurable nested-block boundaries when extracting whole compound blocks.
- Preserve spans and page/structural milestones in marked-up TSV, with XML
  and configurable delimiter serializers sharing the same source rules.
- Discover milestones outside nonempty blocks and attach them explicitly to
  neighbouring output fragments without advancing reference counters.
- Escape literal text and labels; preserve balanced markup through slices and
  prefix removal; reject ambiguous Unicode normalization across retained markup.
- Write plain TSV with literal quotes and backslashes, not CSV-style quoting.
  Reject embedded TAB, CR, or LF in fields without replacing existing output.
- Keep recipe format `"1"` and preserve no-feature 0.3.0 extracted fields and
  compiled digests; serialized TSV hashes can change with the quoting removal.
- Keep plain-text/stand-off conversion outside this package.
- Honour explicit omissions and slices when resolving detached milestones;
  do not reattach discarded events elsewhere.
- Keep out-of-scope inserted notes out of main-text milestone attachment and
  ordering checks; reject reversed slices when detached events need anchors.
- Match selectors against original source structure after fragment omissions,
  and ignore semantically omitted subtrees when checking milestone overlap.
- Require actual booleans for compiled element-rule `keep_empty` values.
- Do not introduce nested-block separators for structurally or semantically
  omitted subtrees; preserve boundaries around empty retained blocks.
- Pin trailing milestones to the last in-scope fragment, before any joined
  auxiliary material and its separator.
- Preserve hashes in EPUB-internal document paths when resolving detached
  milestone locators.

## 0.3.0 - 2026-09-04

- Make recipes self-contained generators of output identifiers and types rather
  than mappings to an externally stored reference table.
- Add source-defined groups, identifier templates and counters, source-block
  rules, nested line identifiers, and source-anchored replacements and
  insertions.
- Remove the runtime `references` input and the public `BlockReference` model.
- Keep `recipe_version` at `"1"`: the project remains pre-1.0 with no known
  external recipe users, so existing recipes should be regenerated.
- Require complete source-group maps and consistent omission sets for reused
  slices; reject insertions whose consumed anchor is discarded.
- Preserve verse numbering across fixed-ID insertions, align structural
  schema/runtime validation, and report identifier-format overflows as recipe
  errors.

## 0.2.0 - 2026-09-04

- Replace the provisional recipe contract shipped in 0.1.0. Because that
  contract had no external users, the corrected initial contract deliberately
  retains `recipe_version: "1"`; 0.1.0 recipes must be regenerated rather than
  treated as a separately versioned format.
- Define version 1 of the native, rule-driven recipe format, which compiles to
  an internal extraction plan.
- Map selected source blocks to a hash-pinned, two-column canonical reference
  index using ordered groups, source text markers, general type rules, and
  sparse skips and overrides.
- Support structural consume/emit rules, joins, explicit splits, and compiled
  regular-expression prefix removal without comparing source text to an
  expected reading.
- Pin the EPUB, reference index, and compiled extraction state independently
  with SHA-256.
- Include the recipe JSON Schema in both source and wheel distributions.
- Preserve XHTML `<br>` elements as text boundaries during block and recipe
  extraction.
- Safely accept external XHTML doctypes without loading their DTDs, and resolve
  known HTML named character references from a local standard-library table.
- Apply declaration checks equivalently to UTF-8 and UTF-16 XML input, with
  linear-time scanning across comments, CDATA, and processing instructions.

## 0.1.0 - 2026-09-04

- Initial public release of EPUB inspection, block discovery, extraction APIs,
  TSV output, safety limits, and validation.
