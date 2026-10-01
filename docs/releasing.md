# Release checklist

1. Ensure the working tree is clean and CI passes on every supported Python
   version.
2. Move relevant entries in `CHANGELOG.md` from **Unreleased** to a heading for
   the release version and date.
3. Set the intended version in `pyproject.toml`, refresh `uv.lock`, and confirm
   that the lock file is current with `uv lock --check`. Update the version and
   release date in `CITATION.cff` to match the changelog, and replace development-only
   installation instructions in the README and relevant feature guides.
4. Run the complete check sequence from `CONTRIBUTING.md` in a clean checkout.
5. Do not reuse or manually upload files already present in a local `dist/`
   directory. A release must be built from the tagged commit by the release
   workflow.
6. Before pushing the tag, confirm that the GitHub `pypi` environment and the
   PyPI Trusted Publisher both authorize `.github/workflows/release.yml`.
   Keep immutable GitHub releases enabled. Do not store a long-lived PyPI
   token in the repository.
7. Commit the release, create an annotated `vVERSION` tag (signed when signing
   is configured), and push the commit and tag. The tag must exactly equal `v`
   followed by the version in `pyproject.toml`.
8. Pushing the tag starts the release workflow. **Do not publish a GitHub
   release manually first.** The workflow reruns formatting, lint, strict
   Pyright, branch-coverage, and public-type checks on every supported Python
   version; recreates an empty
   `dist/`; and builds exactly one source archive and one wheel. It applies
   strict Twine validation, checks the artifact names and required
   documentation, schema, and typing marker, then installs and smoke-tests the
   wheel. It creates or resumes a **draft** GitHub release for the existing
   tag, attaches those checked artifacts, and downloads them again to verify
   the exact filenames and bytes. Only then does it publish the GitHub release,
   making its assets immutable. PyPI publication runs afterwards, using the
   same build artifacts and the existing Trusted Publisher configuration.
9. After the workflow succeeds, install the published wheel into a new Python
   3.13+ environment and verify `epub-blocks --version` and one documented
   extraction command against the published artifact.

## Recovering a partial release

Prefer **Re-run failed jobs** on the original workflow run. The checked build
artifacts are retained for seven days; retry within that window to reuse
exactly the same files, rather than rebuilding archives with new timestamps.

- A failed draft upload can be retried: only assets on a draft may be replaced.
  Unexpected extra assets cause verification to fail rather than publishing
  them or deleting them automatically.
- If GitHub publication succeeded but the response was lost, a retry verifies
  the published assets against the build and performs no release mutations.
  Any filename or byte mismatch fails closed; never disable immutability or
  recreate a published tag to work around it.
- If PyPI publication failed, retry just the failed job. Already-uploaded files
  are skipped to recover a partial upload; the remaining files come from the
  same checked artifact that was frozen on GitHub. A GitHub failure blocks
  PyPI publication entirely.
- If the artifacts have expired, stop and plan recovery from the existing
  immutable release. Do not blindly rerun a build or upload a local `dist/`.

This follows GitHub's recommended
[draft → attach assets → publish sequence](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases).
The workflow handles both registries itself; it does not depend on a second
workflow being triggered when its GitHub token publishes the release.
