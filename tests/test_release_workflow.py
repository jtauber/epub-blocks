"""Exercise the actual publishing shell block without credentials or network."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

import yaml  # pyright: ignore[reportMissingModuleSource]

ROOT = Path(__file__).resolve().parents[1]

# The fake CLI is intentionally a local state machine, not a wrapper around gh.
# Even an unexpected command can only fail the test; it cannot reach a service.
FAKE_GH = """
import json
import os
import pathlib
import sys

state_path = pathlib.Path(os.environ['RELEASE_TEST_STATE'])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
assert args[0] == 'release', args
assert args[2] == 'v0.5.0', args
assert args[args.index('--repo') + 1] == 'example/project', args
operation = args[1]
state['calls'].append(operation)
state_path.write_text(json.dumps(state))
if state.get('fail') == operation:
    sys.exit(1)
if operation == 'view':
    assert '--json' in args and args[args.index('--json') + 1] == 'isDraft'
    assert '--jq' in args and args[args.index('--jq') + 1] == '.isDraft'
    if state['draft'] is None:
        sys.exit(1)
    print(state.get('view_output', str(state['draft']).lower()))
elif operation == 'create':
    assert '--draft' in args and '--verify-tag' in args and '--generate-notes' in args
    if state['draft'] is not None:
        sys.exit(1)
    state['draft'] = True
elif operation == 'upload':
    assert state['draft'] is True, 'attempted to mutate a published release'
    assert '--clobber' in args
    for name in args[3:args.index('--repo')]:
        asset = pathlib.Path(name)
        state['assets'][asset.name] = asset.read_text()
elif operation == 'download':
    destination = pathlib.Path(args[args.index('--dir') + 1])
    for name, content in state['assets'].items():
        (destination / name).write_text(content)
elif operation == 'edit':
    assert state['draft'] is True and '--draft=false' in args
    state['draft'] = False
else:
    raise AssertionError(args)
state_path.write_text(json.dumps(state))
"""


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = cast(
            dict[str, Any],
            yaml.load(  # pyright: ignore[reportUnknownMemberType]
                (ROOT / ".github/workflows/release.yml").read_text(),
                Loader=yaml.BaseLoader,
            ),
        )
        self.jobs = self.workflow["jobs"]

    def test_tag_trigger_job_order_and_least_permissions(self) -> None:
        self.assertEqual(self.workflow["on"], {"push": {"tags": ["v*"]}})
        self.assertEqual(
            self.workflow["concurrency"],
            {"group": "release-${{ github.ref }}", "cancel-in-progress": "false"},
        )
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["build"]["needs"], "test")
        github = self.jobs["publish-to-github"]
        pypi = self.jobs["publish-to-pypi"]
        self.assertEqual(github["needs"], "build")
        self.assertEqual(pypi["needs"], "publish-to-github")
        self.assertEqual(github["permissions"], {"contents": "write"})
        self.assertEqual(pypi["permissions"], {"id-token": "write"})
        self.assertEqual(pypi["environment"]["name"], "pypi")
        for job in (github, pypi):
            self.assertNotIn("if", job)  # No always() bypass of failed dependencies.
            self.assertEqual(
                job["steps"][0]["with"],
                {"name": "python-package-distributions", "path": "dist/"},
            )
        self.assertEqual(pypi["steps"][-1]["with"]["skip-existing"], "true")
        for name in ("test", "build"):
            self.assertEqual(
                self.jobs[name]["steps"][0]["with"]["ref"], "${{ github.sha }}"
            )

    def run_publisher(
        self,
        draft: bool | None,
        assets: dict[str, str] | None = None,
        **options: object,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        bash = shutil.which("bash")
        if bash is None or shutil.which("diff") is None:
            self.skipTest("release shell tests require Bash and diff")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dist").mkdir()
            for name, text in self.assets().items():
                (root / "dist" / name).write_text(text)
            fake = root / "gh.py"
            fake.write_text(FAKE_GH)
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {"draft": draft, "assets": assets or {}, "calls": [], **options}
                )
            )
            script = cast(str, self.jobs["publish-to-github"]["steps"][-1]["run"])
            completed = subprocess.run(
                [
                    bash,
                    "--noprofile",
                    "--norc",
                    "-euo",
                    "pipefail",
                    "-c",
                    'gh() { "$RELEASE_TEST_PYTHON" "$RELEASE_TEST_FAKE_GH" "$@"; }\n'
                    + script,
                ],
                cwd=root,
                # Do not pass credentials, GH_TOKEN, or shell startup hooks.
                env={
                    "PATH": os.defpath,
                    "TMPDIR": str(root),
                    "GITHUB_REF_NAME": "v0.5.0",
                    "GITHUB_REPOSITORY": "example/project",
                    "RELEASE_TEST_PYTHON": sys.executable,
                    "RELEASE_TEST_FAKE_GH": str(fake),
                    "RELEASE_TEST_STATE": str(state),
                },
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            return completed, json.loads(state.read_text())

    def assets(self) -> dict[str, str]:
        return {"epub_blocks-0.5.0.tar.gz": "sdist", "epub_blocks-0.5.0.whl": "wheel"}

    def test_new_release_attaches_and_verifies_before_publication(self) -> None:
        result, state = self.run_publisher(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            state["calls"], ["view", "create", "upload", "download", "edit"]
        )
        self.assertFalse(state["draft"])
        self.assertEqual(state["assets"], self.assets())

    def test_partial_draft_upload_can_resume(self) -> None:
        result, state = self.run_publisher(True, {"epub_blocks-0.5.0.whl": "partial"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["calls"], ["view", "upload", "download", "edit"])
        self.assertFalse(state["draft"])
        self.assertEqual(state["assets"], self.assets())

    def test_published_release_retry_only_verifies_without_mutations(self) -> None:
        result, state = self.run_publisher(False, self.assets())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["calls"], ["view", "download"])

    def test_published_asset_mismatch_or_missing_file_fails_closed(self) -> None:
        for assets in (
            {},
            {"epub_blocks-0.5.0.whl": "wheel"},
            {**self.assets(), "epub_blocks-0.5.0.whl": "different"},
            {**self.assets(), "unexpected.txt": "extra"},
        ):
            with self.subTest(assets=assets):
                result, state = self.run_publisher(False, assets)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(state["calls"], ["view", "download"])
                self.assertEqual(state["assets"], assets)

    def test_unexpected_draft_assets_block_publication_without_deleting_them(
        self,
    ) -> None:
        result, state = self.run_publisher(True, {"unexpected.txt": "extra"})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(state["draft"])
        self.assertNotIn("edit", state["calls"])
        self.assertEqual(state["assets"]["unexpected.txt"], "extra")

    def test_network_failures_never_continue_to_publication(self) -> None:
        for operation in ("create", "upload", "download", "edit"):
            with self.subTest(operation=operation):
                result, state = self.run_publisher(None, fail=operation)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(state["calls"][-1], operation)
                self.assertIsNot(state["draft"], False)

    def test_failed_or_malformed_lookup_cannot_mutate_an_existing_release(self) -> None:
        for options in ({"fail": "view"}, {"view_output": "null"}):
            with self.subTest(options=options):
                result, state = self.run_publisher(False, self.assets(), **options)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("upload", state["calls"])
                self.assertNotIn("edit", state["calls"])
                self.assertEqual(state["assets"], self.assets())
