# Release checklist

1. Ensure the working tree is clean and CI passes on every supported Python
   version.
2. Move relevant entries in `CHANGELOG.md` from **Unreleased** to a heading for
   the release version and date.
3. Set the intended version in `pyproject.toml`, refresh `uv.lock`, and confirm
   that the lock file is current with `uv lock --check`.
4. Run the complete check sequence from `CONTRIBUTING.md` in a clean checkout.
5. Do not reuse or manually upload files already present in a local `dist/`
   directory. A release must be built from the tagged commit by the release
   workflow.
6. Commit the release, create an annotated `vVERSION` tag (signed when signing
   is configured), and push the commit and tag. The tag must exactly equal `v`
   followed by the version in `pyproject.toml`.
7. Confirm that the GitHub `pypi` environment and the PyPI Trusted Publisher
   both authorize `.github/workflows/release.yml`. Do not store a long-lived
   PyPI token in the repository.
8. Publish a GitHub release for that exact tag. Before publishing to PyPI, the
   release workflow reruns formatting, lint, strict Pyright, branch-coverage,
   and public-type checks on every supported Python version; recreates an empty
   `dist/`; and builds exactly one source archive and one wheel. It applies
   strict Twine validation, checks the artifact names and required
   documentation, schema, and typing marker, then installs and smoke-tests the
   wheel. Only those checked artifacts are published to PyPI and attached to
   the GitHub release.
9. After the workflow succeeds, install the published wheel into a new Python
   3.13+ environment and verify `epub-blocks --version` and one documented
   extraction command against the published artifact.
