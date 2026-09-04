# Changelog

All notable changes to this project will be documented here. The project uses
[Semantic Versioning](https://semver.org/).

## Unreleased

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
