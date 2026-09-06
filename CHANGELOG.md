# Changelog

All notable changes to this project will be documented here. The project uses
[Semantic Versioning](https://semver.org/).

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
