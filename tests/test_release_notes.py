# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release_notes.py"


class ReleaseNotesTests(unittest.TestCase):
    def test_cli_preserves_prose_while_binding_nested_links_to_the_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs/guides").mkdir(parents=True)
            for name in ("recovery.md", "recovery (advanced).md"):
                (root / "docs/guides" / name).write_text("# Recovery\n")
            (root / "README.md").write_text("# Project\n")
            source = root / "docs/release-notes.md"
            source.write_text(
                '## 1.2.3\n\n[Read [nested] guide](guides/recovery.md#resume "Recovery")\n'
                "[Spaced](<guides/recovery (advanced).md#resume>)\n"
                "[Query](guides/recovery.md?plain=1#resume)\n"
                "[Project](../README.md)\n"
                "[Anchor](#notes) [External](https://example.com/a_(b)#x)\n"
                "[Mail](mailto:ops@example.com) [Network](//example.com/docs) [Root](/rooted)\n"
                "`[Literal](missing.md)`\n\n```md\n[Example](missing.md)\n```\n"
            )
            expected = (
                "## 1.2.3\n\n[Read [nested] guide](https://github.com/acme/project/blob/v1.2.3/docs/"
                'guides/recovery.md#resume "Recovery")\n'
                "[Spaced](<https://github.com/acme/project/blob/v1.2.3/docs/guides/recovery%20%28advanced%29.md#resume>)\n"
                "[Query](https://github.com/acme/project/blob/v1.2.3/docs/guides/recovery.md?plain=1#resume)\n"
                "[Project](https://github.com/acme/project/blob/v1.2.3/README.md)\n"
                "[Anchor](#notes) [External](https://example.com/a_(b)#x)\n"
                "[Mail](mailto:ops@example.com) [Network](//example.com/docs) [Root](/rooted)\n"
                "`[Literal](missing.md)`\n\n```md\n[Example](missing.md)\n```\n"
            )
            output = root / "published.md"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(source),
                    str(output),
                    "--repository",
                    "acme/project",
                    "--tag",
                    "v1.2.3",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                output.read_text(),
                expected
                + "\n[Full release history](https://github.com/acme/project/blob/v1.2.3/docs/release-notes.md)\n",
            )

    def test_cli_corrects_every_real_v023_documentation_target(self):
        expected = json.loads((ROOT / "tests/fixtures/release-note-links-v023.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            source = root / "docs/release-notes.md"
            source.write_text("## 0.2.23\n\n" + "\n".join(f"[Guide]({target})" for target in expected) + "\n")
            for target in expected:
                name = target.split("#", 1)[0]
                self.assertTrue((ROOT / "docs" / name).is_file(), name)
                (root / "docs" / name).write_text("# Retained documentation\n")
            output = root / "published.md"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(source),
                    str(output),
                    "--repository",
                    "fireflyframework/firefly-forge",
                    "--tag",
                    "v0.2.23",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                output.read_text(),
                "## 0.2.23\n\n"
                + "\n".join(f"[Guide]({target})" for target in expected.values())
                + "\n\n[Full release history](https://github.com/fireflyframework/firefly-forge/"
                "blob/v0.2.23/docs/release-notes.md)\n",
            )


class CurrentReleaseSectionTests(unittest.TestCase):
    def render(self, root, text, tag="v1.2.3"):
        source, output = root / "release-notes.md", root / "published.md"
        source.write_text(text)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(source), str(output), "--repository", "acme/project", "--tag", tag],
            cwd=root,
            capture_output=True,
            text=True,
        )
        return result, output

    def test_large_history_is_not_a_release_body_and_fenced_headings_stay_verbatim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = (
                "## 1.2.3 — Current release\n\nComplete current prose.\n\n### Details\n\n"
                "```md\n## 1.2.3\n[Literal](missing.md)\n```\n\n"
                "~~~~text\n## 9.9.9\n~~~\n## 8.8.8\n~~~~\n\nFinal relevant paragraph.\n"
            )
            result, output = self.render(
                root, "# History\n\n## 1.2.4\nNewer.\n\n" + current + "\n## 1.2.2\n" + "Old history.\n" * 20000
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                output.read_text(),
                current + "\n[Full release history](https://github.com/acme/project/blob/v1.2.3/release-notes.md)\n",
            )

    def test_current_section_is_complete_and_old_broken_links_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = "## 1.2.3\n\n" + "Complete current paragraph.\n\n" * 500 + "Last authored sentence.\n"
            result, output = self.render(root, current + "\n## 1.2.2\n[Old deleted guide](missing.md)\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                output.read_text(),
                current + "\n[Full release history](https://github.com/acme/project/blob/v1.2.3/release-notes.md)\n",
            )

    def test_missing_or_duplicate_version_refuses_before_creating_output(self):
        examples = (
            "## 1.2.30\nOther version.\n",
            "```md\n## 1.2.3\n```\n## 1.2.2\nOlder.\n",
            "## 1.2.3\nFirst.\n\n## 1.2.3 — Duplicate\nSecond.\n",
        )
        for text in examples:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                result, output = self.render(Path(directory), text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("exactly one release section", result.stderr)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
