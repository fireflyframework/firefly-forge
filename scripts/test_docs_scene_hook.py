# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
# https://www.apache.org/licenses/LICENSE-2.0
"""Qualify cache versions and generated asset source paths without application state."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts import docs_scene_hook as hook


class SceneHookTests(unittest.TestCase):
    def test_versions_follow_css_content_on_home_and_nested_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stylesheets").mkdir()
            css = root / "stylesheets/forge.css"
            config = {"extra_css": ["stylesheets/forge.css"], "extra_javascript": [], "docs_dir": directory}
            css.write_text("body { color: black; }")
            hook.on_config(config)
            expected = hashlib.sha256(css.read_bytes()).hexdigest()[:16]
            html = '<link href="../stylesheets/forge.css"><script src="../assets/docs-scene/scene.js"></script>'
            before = hook.on_post_page(html, None, None)
            self.assertIn("../stylesheets/forge.css?v=" + expected, before)
            self.assertRegex(before, r"scene.js\?v=[a-f0-9]{16}")
            css.write_text("body { color: white; }")
            hook.on_config({**config, "extra_css": ["stylesheets/forge.css"], "extra_javascript": []})
            self.assertNotEqual(before, hook.on_post_page(html, None, None))

    def test_generated_snapshot_is_an_exact_copy_with_separate_rights(self):
        self.assertEqual(
            next(hook.OUTPUT.glob("forge-operational-model.*.json")).read_bytes(),
            (hook.ROOT / "assets/factory/forge-operational-model.json").read_bytes(),
        )
        notice = (hook.OUTPUT / "THIRD_PARTY_NOTICES.txt").read_text()
        self.assertIn("MIT License", notice)
        self.assertIn("not Apache-2.0", notice)
        config = {"site_dir": str(hook.ROOT / "build/docs-site"), "use_directory_urls": True}
        for file in hook.on_files([], config):
            self.assertTrue(Path(file.abs_src_path).is_file())
            self.assertTrue(file.dest_uri.startswith("assets/docs-scene/"))


if __name__ == "__main__":
    unittest.main()
