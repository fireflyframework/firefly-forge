# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A container-only recovery may publish only the explicitly verified stable tag."""

import base64
import copy
import hashlib
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/container_release_binding.py"
WORKFLOW = ROOT / ".github/workflows/container-recovery.yml"
COMMIT, TAG_OBJECT = "a" * 40, "b" * 40


class PublicAPI:
    def __init__(self):
        self.body = b'[project]\nname="firefly-forge"\nversion="0.2.49"\n'
        self.data = {
            "repos/acme/forge": {"private": False, "full_name": "acme/forge"},
            "repos/acme/forge/releases/tags/v0.2.49": {
                "id": 123,
                "tag_name": "v0.2.49",
                "draft": False,
                "prerelease": False,
                "published_at": "2026-10-31T01:26:39Z",
            },
            "repos/acme/forge/git/ref/tags/v0.2.49": {"object": {"type": "tag", "sha": TAG_OBJECT}},
            "repos/acme/forge/git/tags/" + TAG_OBJECT: {"sha": TAG_OBJECT, "object": {"type": "commit", "sha": COMMIT}},
            "repos/acme/forge/contents/pyproject.toml?ref=" + COMMIT: {
                "type": "file",
                "path": "pyproject.toml",
                "encoding": "base64",
                "size": len(self.body),
                "content": base64.b64encode(self.body).decode(),
                "sha": hashlib.sha1(b"blob " + str(len(self.body)).encode() + b"\0" + self.body).hexdigest(),
            },
        }
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        return copy.deepcopy(self.data[path])


def helpers():
    assert SCRIPT.is_file(), "The container publication binding guard is missing"
    return runpy.run_path(str(SCRIPT))


class ContainerReleaseBindingTests(unittest.TestCase):
    def test_annotated_and_lightweight_public_release_bind_the_exact_commit_and_version(self):
        verify = helpers()["verify_release"]
        for kind in ("annotated", "lightweight"):
            with self.subTest(kind=kind):
                api = PublicAPI()
                if kind == "lightweight":
                    api.data["repos/acme/forge/git/ref/tags/v0.2.49"]["object"] = {"type": "commit", "sha": COMMIT}
                value = verify("acme/forge", "v0.2.49", COMMIT, api)
                self.assertEqual(value["commit"], COMMIT)
                self.assertEqual(value["tag"], "v0.2.49")
                self.assertEqual(value["version"], "0.2.49")
                self.assertEqual(value["release_id"], "123")
                self.assertIn("repos/acme/forge/contents/pyproject.toml?ref=" + COMMIT, api.calls)

    def test_private_draft_prerelease_unpublished_wrong_tag_or_moved_commit_refuse(self):
        verify = helpers()["verify_release"]
        changes = (
            ("repos/acme/forge", "private", True),
            ("repos/acme/forge", "full_name", "another/repository"),
            ("repos/acme/forge/releases/tags/v0.2.49", "draft", True),
            ("repos/acme/forge/releases/tags/v0.2.49", "draft", "false"),
            ("repos/acme/forge/releases/tags/v0.2.49", "prerelease", True),
            ("repos/acme/forge/releases/tags/v0.2.49", "published_at", None),
            ("repos/acme/forge/releases/tags/v0.2.49", "tag_name", "v0.2.48"),
            ("repos/acme/forge/git/tags/" + TAG_OBJECT, "object", {"type": "commit", "sha": "c" * 40}),
        )
        for path, field, value in changes:
            with self.subTest(field=field, value=value):
                api = PublicAPI()
                api.data[path][field] = value
                with self.assertRaises(ValueError):
                    verify("acme/forge", "v0.2.49", COMMIT, api)

    def test_bad_inputs_refuse_before_any_API_call(self):
        verify = helpers()["verify_release"]
        for repository, tag, commit in (
            ("acme/forge", "main", COMMIT),
            ("acme/forge", "v0.2.49;echo bad", COMMIT),
            ("acme/forge", "v0.2.50rc1", COMMIT),
            ("../forge", "v0.2.49", COMMIT),
            ("acme/forge", "v0.2.49", "main"),
        ):
            api = PublicAPI()
            with self.subTest(tag=tag, commit=commit), self.assertRaises(ValueError):
                verify(repository, tag, commit, api)
            self.assertEqual(api.calls, [])

    def test_tagged_metadata_wrong_version_kind_or_blob_digest_refuse(self):
        verify = helpers()["verify_release"]
        path = "repos/acme/forge/contents/pyproject.toml?ref=" + COMMIT
        for field, value in (
            ("content", base64.b64encode(b'[project]\nversion="0.2.50"').decode()),
            ("sha", "c" * 40),
            ("type", "symlink"),
            ("path", "other.toml"),
        ):
            api = PublicAPI()
            api.data[path][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify("acme/forge", "v0.2.49", COMMIT, api)

    def test_valid_git_blob_with_foreign_project_or_version_still_refuses(self):
        verify = helpers()["verify_release"]
        path = "repos/acme/forge/contents/pyproject.toml?ref=" + COMMIT
        for body in (
            b'[project]\nname="firefly-forge"\nversion="0.2.50"\n',
            b'[project]\nname="another-project"\nversion="0.2.49"\n',
        ):
            api = PublicAPI()
            api.data[path].update(
                content=base64.b64encode(body).decode(),
                size=len(body),
                sha=hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest(),
            )
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "project version"):
                verify("acme/forge", "v0.2.49", COMMIT, api)

    def test_second_validation_refuses_release_or_tag_identity_replacement(self):
        verify = helpers()["verify_release"]
        api = PublicAPI()
        binding = verify("acme/forge", "v0.2.49", COMMIT, api)
        api.data["repos/acme/forge/releases/tags/v0.2.49"]["id"] += 1
        with self.assertRaises(ValueError):
            verify("acme/forge", "v0.2.49", COMMIT, api, previous=binding)
        api.data["repos/acme/forge/releases/tags/v0.2.49"]["id"] -= 1
        api.data["repos/acme/forge/git/ref/tags/v0.2.49"]["object"] = {"type": "commit", "sha": COMMIT}
        with self.assertRaises(ValueError):
            verify("acme/forge", "v0.2.49", COMMIT, api, previous=binding)

    def test_checkout_requires_exact_HEAD_and_regular_matching_project_file(self):
        verify, checkout = helpers()["verify_release"], helpers()["verify_checkout"]
        api = PublicAPI()
        binding = verify("acme/forge", "v0.2.49", COMMIT, api)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_bytes(api.body)
            with patch("subprocess.check_output", side_effect=[(COMMIT + "\n").encode(), b""]):
                checkout(root, binding)
            with (
                patch("subprocess.check_output", side_effect=[(COMMIT + "\n").encode(), b" M Dockerfile\n"]),
                self.assertRaisesRegex(ValueError, "clean"),
            ):
                checkout(root, binding)
            with (
                patch("subprocess.check_output", return_value=("c" * 40 + "\n").encode()),
                self.assertRaises(ValueError),
            ):
                checkout(root, binding)
            (root / "pyproject.toml").write_text('[project]\nname="firefly-forge"\nversion="0.2.50"\n')
            with (
                patch("subprocess.check_output", side_effect=[(COMMIT + "\n").encode(), b""]),
                self.assertRaisesRegex(ValueError, "project version"),
            ):
                checkout(root, binding)

    def test_symlink_checkout_metadata_and_tag_cycles_refuse(self):
        verify, checkout = helpers()["verify_release"], helpers()["verify_checkout"]
        api = PublicAPI()
        binding = verify("acme/forge", "v0.2.49", COMMIT, api)
        api.data["repos/acme/forge/git/tags/" + TAG_OBJECT]["object"] = {"type": "tag", "sha": TAG_OBJECT}
        with self.assertRaisesRegex(ValueError, "tag chain"):
            verify("acme/forge", "v0.2.49", COMMIT, api)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "payload.toml").write_bytes(api.body)
            (root / "pyproject.toml").symlink_to("payload.toml")
            with patch("subprocess.check_output") as git, self.assertRaisesRegex(ValueError, "ordinary file"):
                checkout(root, binding)
            git.assert_not_called()

    def test_workflow_is_manual_only_and_reuses_pinned_container_steps_after_validation(self):
        self.assertTrue(WORKFLOW.is_file(), "The bounded container-only workflow is missing")
        workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        existing = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(set(workflow["jobs"]), {"container"})
        job = workflow["jobs"]["container"]
        self.assertIn("github.event.repository.default_branch", job["if"])
        self.assertEqual(job["permissions"], {"contents": "read", "packages": "write"})
        steps = job["steps"]
        uses = [step["uses"] for step in steps if "uses" in step]
        old_uses = [step["uses"] for step in existing["jobs"]["container"]["steps"] if "uses" in step]
        self.assertEqual(uses, [old_uses[0], *old_uses])
        checkouts = [step for step in steps if step.get("uses") == old_uses[0]]
        self.assertEqual(checkouts[0]["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(checkouts[0]["with"]["persist-credentials"], "false")
        self.assertEqual(checkouts[1]["with"]["ref"], "${{ steps.binding.outputs.commit }}")
        self.assertEqual(checkouts[1]["with"]["persist-credentials"], "false")
        initial = next(step for step in steps if step.get("id") == "binding")
        self.assertLess(steps.index(initial), steps.index(checkouts[1]))
        preserved = next(step for step in steps if step.get("run", "").startswith("cp scripts/"))
        self.assertIn("$RUNNER_TEMP/forge-container-release-binding.py", preserved["run"])
        self.assertLess(steps.index(preserved), steps.index(checkouts[1]))
        image = next(step for step in steps if step.get("id") == "image")
        self.assertEqual(image["with"]["provenance"], "mode=max")
        self.assertEqual(image["with"]["sbom"], "true")
        self.assertIn("${{ steps.confirmed.outputs.commit }}", image["with"]["labels"])
        self.assertNotIn("github.sha", image["with"]["labels"])
        self.assertIn("${{ steps.confirmed.outputs.tag }}", image["with"]["tags"])
        confirmed = next(i for i, step in enumerate(steps) if step.get("id") == "confirmed")
        self.assertLess(confirmed, steps.index(image))
        self.assertIn('--checkout "$GITHUB_WORKSPACE"', steps[confirmed]["run"])
        self.assertIn('--previous-tag-object "$TAG_OBJECT"', steps[confirmed]["run"])
        self.assertIn('--previous-release-id "$RELEASE_ID"', steps[confirmed]["run"])
        for step in steps:
            self.assertNotIn("${{ inputs.", step.get("run", ""))
        self.assertNotIn("gh release create", WORKFLOW.read_text())
