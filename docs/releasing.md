# Release checklist

1. Ensure the working tree is clean and CI passes on every supported Python
   version.
2. Move relevant entries in `CHANGELOG.md` from **Unreleased** to a heading for
   the release version and date.
3. Replace the development version in `pyproject.toml` with the intended
   version, then refresh `uv.lock`.
4. Run the complete check sequence from `CONTRIBUTING.md` in a clean checkout.
5. Inspect the source archive and wheel. Confirm that the source archive
   contains `docs/recipe-format.md` and `schemas/recipe-v1.schema.json`, and
   that the wheel contains `epub_blocks/py.typed`.
6. Commit the release, create an annotated `vVERSION` tag (signed when signing
   is configured), and push the commit and tag.
7. Confirm that the GitHub `pypi` environment and the PyPI Trusted Publisher
   both authorize `.github/workflows/release.yml`. Do not store a long-lived
   PyPI token in the repository.
8. Publish the corresponding GitHub release. The release workflow builds and
   checks the source archive and wheel, attaches them to the GitHub release,
   and publishes them to PyPI with Trusted Publishing.
9. Install the published wheel into a new Python 3.13+ environment and verify
   `epub-blocks --version` and one documented extraction command.
