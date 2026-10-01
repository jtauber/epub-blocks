# Interactive recipe authoring (experimental)

The optional Textual terminal wizard helps you build a **draft**, ordinary
version-1 recipe. It discovers source structure deterministically and asks you
to make the editorial decisions. It uses no LLM, network service, existing base
text, or external reference table. Its output runs with the normal extractor;
Textual and the saved wizard session are not required to execute the recipe.

The wizard is new in 0.9.0. Install the optional interface and launch it with:

```bash
python -m pip install "epub-blocks[wizard]"
epub-blocks-wizard /path/to/book.epub /path/to/draft.recipe.json
```

The core extraction package still has no third-party runtime dependencies.
`epub-blocks-wizard --help` works without Textual; launching it without the extra
prints an installation hint. From a development checkout, use
`uv sync --extra wizard` and prefix the commands below with `uv run`.

Use a terminal at least 80 columns wide and 30 rows high; 120 × 45 is more
comfortable. Tab / Shift-Tab move between controls, arrows move within lists,
Space toggles document selection, and Enter activates buttons. Mouse selection
also works. Use the tab bar to revisit earlier steps.

## 1. Choose scope

The Scope tab shows XHTML documents in spine order with candidate-block counts.
Linear documents start selected, except documents marked as navigation. This
does **not** mean they are all main text: explicitly review covers, contents,
dedications, appendices and back matter. Non-linear documents can be selected.

Highlight a document to see its first five text candidates and source locators.
Default candidates are paragraphs, headings, preformatted blocks, and the
extractor's eligible list items and blockquotes. Images and empty scene-break
elements are not inferred. Selecting no documents is allowed while editing,
but cannot produce a recipe.

## 2. Define references

The document picker contains only files selected in step 1, in spine order, and
updates when that selection changes. If the current file is unchecked, the
picker moves to the first remaining file. With no selected files the reference
controls are disabled. Previously entered codes are retained if you reselect a file.

Use **Previous / Next** or **Alt+Left / Alt+Right** to work through the selected
files without reopening the dropdown. **File 3 of 12** shows your position in
that selection. Navigation stops at the first and last file, and puts focus in
the section-code field for the next edit. These shortcuts apply only in step 2;
ordinary Left / Right still move the text cursor. The dropdown remains available
for jumping directly to a file (including typing to search).

Each document initially receives a provisional code based on its spine position:
`d01`, `d02`, etc. These are **not inferred chapter numbers**. Replace them with
your intended section codes, for example `pr`, `01`, `02`, `ap`.

Use letters, numbers, dots, underscores or hyphens, starting with a letter or
number. Reusing a code across files continues that section's counter. Emission
always follows spine order, regardless of selection order.

For this first version:

- ordinary blocks get `{group}.{number:03d}`, starting at 1;
- headings consume ordinary block numbers too, rather than receiving `.000`;
- `line-start` begins a verse block with a nested line counter;
- `line` continues it, producing IDs such as `01.003.01`, `01.003.02`;
- a normal block or a different group ends a verse sequence;
- skipped candidates do not consume numbers.

If one XHTML file contains several sections, or you need title-prefix removal,
fixed heading IDs, different widths, or heading-driven group transitions, refine
the exported recipe using the [recipe reference](recipe-format.md). The wizard
does not invent a citation system on your behalf.

## 3. Classify patterns

The Patterns tab groups candidates by tag and **exact, case-sensitive class
set**. It shows counts, decisions, and up to six examples (the first three and
last three for larger groups), including source locators.

Nothing is classified initially. **Suggest by tag** fills unresolved general
patterns with `heading` for `h1`–`h6`, `preformatted` for `pre`, and `paragraph`
otherwise. These are suggestions: captions, quotations, notes and verse often
use indistinguishable tags. Inspect and override them.

Select a pattern, choose its type/role, and press **Apply choice**. Choices can
apply to all selected documents or one document. Document-specific choices
take precedence. Clearing an override restores the general choice; clearing a
general choice leaves that pattern unresolved unless a document overrides it.
`skip` excludes matching candidates. It does not remove inline text from other
candidates.

The same pattern sometimes means different things within one document. This
prototype cannot distinguish those occurrences interactively: refine the
exported recipe's ordered rules or exact exceptions. A `line` without an
appropriate preceding `line-start` fails validation instead of guessing.

## 4. Choose markup

Unicode delimiters are the default: `⧼emphasis⧽`, `⟪strong⟫`, and `⟦preformatted⟧`.
XML fragments and plain text are also available. The basic preset recognizes
`em`/`i` and `strong`/`b`, not CSS-only italics or small caps.

Ordinary prose is NFC-normalized with collapsed whitespace. Retained `pre`
elements preserve whitespace in marked output; newlines and tabs use the
extractor's [TSV-safe escaping](markup.md#preserving-preformatted-whitespace-070).
Plain output is refused when retained `pre` candidates would lose that structure.
There is no CSV quoting layer: generated TSVs remain three literal tab-separated
columns with one record per physical line.

Optional checkboxes omit elements explicitly marked `epub:type="noteref"` and
`epub:type="pagebreak"`. Both start off, leaving these decisions to you. Omissions
can remove entire candidates, which then need no classification and are not
included in `skip_source`. Omission takes precedence over markup preservation,
including for emphasized note calls and preformatted text inside omitted elements.

This preset does **not** preserve page labels as milestones, infer CSS styling,
extract link targets, arrange footnotes, or select empty scene separators.
Such cases need manual recipe refinement, even when validation succeeds.

## 5. Validate, review, save

**Validate / refresh preview** runs the normal candidate extractor, compiler and
extraction engine. It verifies the EPUB hash, strict source coverage in selected
documents, reference generation and output, and adds the compiled-plan hash.
Unresolved retained patterns and invalid verse sequences fail visibly.

The table shows the first 200 output blocks; highlighting one shows its full
text. The Recipe JSON tab contains the complete recipe. Neither preview proves
the chosen scope or interpretation is editorially correct: inspect the full
generated TSV too. Strict coverage cannot flag omitted documents, images, or
unselected empty structures.

Changes invalidate the preview and disable **Save recipe** until revalidation.
Validation runs in a background worker; a result is discarded if decisions
change meanwhile. Only one extraction runs at a time, and the validation button
stays disabled until that job finishes. Cancelling its UI worker does not free
the slot while the underlying extraction is still running. **Save recipe** writes
only the recipe, not a TSV:

```bash
epub-blocks /path/to/book.epub /path/to/draft.recipe.json /path/to/book.tsv
```

The recipe pins the source archive's SHA-256 and a package identifier when one
exists. It contains structural rules and source paths, not copied readings.
Source EPUBs are read-only throughout.

## Save progress and resume

Nothing saves automatically. **Save progress** or Ctrl-S stores decisions in
`draft.recipe.json.wizard.json`. This file contains paths, tag/class patterns,
section codes and options, **not the book's prose**. New files are created with
private permissions. Ctrl-Q or Quit exits; save first if you want to resume.
Saved sessions have an 8 MiB UTF-8 size limit, enforced on both save and resume.
Exceeding the limit leaves any previous progress file untouched.

```bash
epub-blocks-wizard book.epub draft.recipe.json --resume
```

Resume verifies the saved state belongs to exactly the same EPUB. Once a recipe
has been exported, the wizard deliberately refuses to reopen that destination:
it cannot safely merge subsequent hand edits. Resume the saved decisions into
a **new** destination instead:

```bash
epub-blocks-wizard book.epub revised.recipe.json \
  --resume --session draft.recipe.json.wizard.json
```

Recipes and progress files are written atomically. During a session, they may
be updated only if their bytes still match the last read/write. A per-destination
lock serializes wizard writers across processes; a competing save is rejected
and can be retried once the active save finishes. If a process crashes while
saving, its lock directory may remain. The error identifies the exact directory;
remove it only after verifying that no save is still running.

External edits are checked before writing and again just before replacement.
This is an optimistic check, not a lock on other editors: avoid editing a file
while a save is actively running. Existing recipe destinations are never
overwritten at startup, and destination symlinks are rejected. There is no
automatic import of existing handwritten recipes.

## Architecture and limits

`epub_blocks.authoring` contains experimental, UI-independent discovery,
decision validation, recipe generation and safe saving. `_wizard_tui` is the
optional adapter. Neither changes the recipe format or the extraction engine.
Automated tests use synthetic EPUBs and Textual's headless driver.

This first version is intended for ordinary prose and simple, class-marked
verse. Complex books still need a recipe author: nested containers, joins and
splits, multiple sections per file, XML repairs, CSS conventions, exact note
placement, and milestone policies are supported by epub-blocks but not exposed
by this wizard yet.
