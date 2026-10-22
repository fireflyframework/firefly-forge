# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host contracts for hash-only diagnostics around the unchanged Git fence."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from firefly_forge.git_workspace import metadata_digest
from scripts.git_metadata_diagnostic import SELECTED_TEST, MetadataDiagnostic


class MetadataDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        (self.workspace / ".git").mkdir(parents=True)
        self.config = self.workspace / ".git/config"
        self.config.write_text("[core]\nrepositoryformatversion = 0\n")
        self.config.chmod(0o644)
        self.output = self.root / "diagnostic"
        self.observer = MetadataDiagnostic(metadata_digest, self.output, "gw0")
        self.observer.nodeid = SELECTED_TEST

    def records(self):
        return [json.loads(path.read_text()) for path in sorted(self.output.rglob("*.json"))]

    def test_records_baseline_before_original_digest_without_logging_contents(self):
        expected = metadata_digest(self.workspace)
        seen = []

        def observed_original(workspace):
            seen.extend(self.records())
            return metadata_digest(workspace)

        self.observer.original = observed_original
        self.assertEqual(self.observer(self.workspace), expected)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["phase"], "before_digest")
        records = self.records()
        self.assertEqual(records[-1]["digest"], expected)
        self.assertFalse(records[-1]["digest_differs_from_baseline"])
        self.assertEqual(records[-1]["changes_from_baseline"], [])
        self.assertNotIn("repositoryformatversion", json.dumps(records))

    def test_content_and_mode_drift_identify_exact_protected_file(self):
        self.observer(self.workspace)
        self.config.write_text("[core]\nrepositoryformatversion = 1\n")
        self.observer(self.workspace)
        content = self.records()[-1]
        self.assertTrue(content["digest_differs_from_baseline"])
        self.assertEqual([row["path"] for row in content["changes_from_baseline"]], ["config"])
        self.assertNotEqual(
            content["changes_from_baseline"][0]["before"]["sha256"],
            content["changes_from_baseline"][0]["after"]["sha256"],
        )
        self.config.chmod(0o600)
        self.observer(self.workspace)
        mode = self.records()[-1]
        self.assertEqual(mode["after"]["files"]["config"]["mode"], 0o600)

    def test_original_exception_and_call_count_survive_inventory_and_output_failure(self):
        failure = ValueError("original fence failure")
        calls = []

        def refused(workspace):
            calls.append(workspace)
            raise failure

        self.output.write_text("diagnostic destination unavailable")
        self.observer.original = refused
        warnings = io.StringIO()
        with contextlib.redirect_stderr(warnings), self.assertRaises(ValueError) as raised:
            self.observer(self.workspace)
        self.assertIs(raised.exception, failure)
        self.assertEqual(calls, [self.workspace])
        self.assertIn("receipt could not be written", warnings.getvalue())
        self.observer.original = metadata_digest
        with contextlib.redirect_stderr(warnings):
            self.assertEqual(self.observer(self.workspace), metadata_digest(self.workspace))

    def test_symlink_is_never_followed_and_original_refusal_remains(self):
        target = self.root / "private"
        target.write_text("private-value-not-for-artifacts")
        self.config.unlink()
        self.config.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "cannot redirect"):
            self.observer(self.workspace)
        records = self.records()
        row = records[0]["before"]["files"]["config"]
        self.assertNotIn("sha256", row)
        self.assertEqual(records[-1]["original_error_type"], "ValueError")
        self.assertNotIn("private-value", json.dumps(records))

    def test_metadata_root_symlink_is_refused_before_inventory_reads(self):
        retained = self.root / "retained-metadata"
        (self.workspace / ".git").rename(retained)
        (self.workspace / ".git").symlink_to(retained, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "owned directory"):
            self.observer(self.workspace)
        before = self.records()[0]["before"]
        self.assertEqual(before["files"], {})
        self.assertEqual(before["error_type"], "ValueError")

    def test_protected_parent_symlink_is_never_traversed(self):
        external = self.root / "external-objects"
        (external / "info").mkdir(parents=True)
        (external / "info/private").write_text("outside-observer-custody")
        (self.workspace / ".git/objects").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "cannot redirect"):
            self.observer(self.workspace)
        before = self.records()[0]["before"]
        self.assertEqual(before["files"], {})
        self.assertEqual(before["error_type"], "ValueError")

    def test_unselected_test_calls_original_once_without_diagnostics(self):
        self.observer.nodeid = "tests/test_unrelated.py::test_other"
        expected = metadata_digest(self.workspace)
        self.assertEqual(self.observer(self.workspace), expected)
        self.assertFalse(self.output.exists())

    def test_worker_outputs_do_not_collide(self):
        other = MetadataDiagnostic(metadata_digest, self.output, "gw1")
        other.nodeid = SELECTED_TEST
        self.observer(self.workspace)
        other(self.workspace)
        self.assertEqual(len(self.records()), 4)
        self.assertTrue((self.output / "gw0/metadata-000001-0-before_digest.json").is_file())
        self.assertTrue((self.output / "gw1/metadata-000001-0-before_digest.json").is_file())

    def test_oversize_inventory_does_not_replace_original_digest_error(self):
        self.config.write_bytes(b"x" * 1_000_001)
        with self.assertRaisesRegex(ValueError, "inspection bound"):
            self.observer(self.workspace)
        records = self.records()
        self.assertEqual(records[0]["before"]["error_type"], "ValueError")
        self.assertEqual(records[-1]["original_error_type"], "ValueError")

    def test_real_engine_fence_still_refuses_a_changed_digest(self):
        from firefly_forge import engine as engine_module

        engine = object.__new__(engine_module.Engine)
        baseline = self.observer(self.workspace)
        engine.git_metadata = {self.workspace.resolve(): baseline}
        original = engine_module.metadata_digest
        engine_module.metadata_digest = self.observer
        try:
            engine._assert_git_metadata(self.workspace)
            self.config.write_text("[core]\nrepositoryformatversion = 1\n")
            with self.assertRaisesRegex(engine_module.ReconciliationRequired, "Git metadata changed"):
                engine._assert_git_metadata(self.workspace)
        finally:
            engine_module.metadata_digest = original
        self.assertTrue(self.records()[-1]["digest_differs_from_baseline"])
        self.assertEqual(self.records()[-1]["caller"]["function"], "_assert_git_metadata")


if __name__ == "__main__":
    unittest.main()
