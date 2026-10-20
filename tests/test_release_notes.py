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
                '# Notes\n\n[Read [nested] guide](guides/recovery.md#resume "Recovery")\n'
                "[Spaced](<guides/recovery (advanced).md#resume>)\n"
                "[Query](guides/recovery.md?plain=1#resume)\n"
                "[Project](../README.md)\n"
                "[Anchor](#notes) [External](https://example.com/a_(b)#x)\n"
                "[Mail](mailto:ops@example.com) [Network](//example.com/docs) [Root](/rooted)\n"
                "`[Literal](missing.md)`\n\n```md\n[Example](missing.md)\n```\n"
            )
            expected = (
                "# Notes\n\n[Read [nested] guide](https://github.com/acme/project/blob/v1.2.3/docs/"
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
            self.assertEqual(output.read_text(), expected)

    def test_cli_corrects_every_real_v023_documentation_target(self):
        expected = json.loads((ROOT / "tests/fixtures/release-note-links-v023.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            source = root / "docs/release-notes.md"
            source.write_text("\n".join(f"[Guide]({target})" for target in expected) + "\n")
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
            self.assertEqual(output.read_text(), "\n".join(f"[Guide]({target})" for target in expected.values()) + "\n")


if __name__ == "__main__":
    unittest.main()
