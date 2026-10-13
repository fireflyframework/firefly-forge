# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
# https://www.apache.org/licenses/LICENSE-2.0
"""Qualify cache versions and generated asset source paths without application state."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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


class WalkthroughTests(unittest.TestCase):
    def test_inline_theme_pairs_preserve_table_rows_and_link_full_native_capture(self):
        previous = hook._walkthrough
        try:
            hook._walkthrough = {
                "captures": {
                    "connect-mobile-light.png": {
                        "path": "assets/screenshots/connect-mobile-light.png",
                        "viewport": {"width": 390, "height": 844},
                    },
                    "connect-mobile-dark.png": {
                        "path": "assets/screenshots/connect-mobile-dark.png",
                        "viewport": {"width": 390, "height": 844},
                    },
                }
            }
            markdown = "| ![Connect & inspect](assets/screenshots/connect-mobile-light.png) | Next |"
            output = hook.on_page_markdown(markdown, SimpleNamespace(url="console-tour/"), {})
            self.assertNotIn("\n", output)
            self.assertIn('href="../assets/screenshots/connect-mobile-dark.png"', output)
            self.assertIn('width="390" height="844"', output)
            self.assertIn('alt="Connect &amp; inspect"', output)
            self.assertIn("theme-light", output)
            self.assertIn("theme-dark", output)
            self.assertTrue(output.endswith(" | Next |"))
        finally:
            hook._walkthrough = previous

    def test_rejects_a_changed_capture_or_frontend_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs"
            asset = docs / "assets/screenshots/phone.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(bytes(16) + (390).to_bytes(4, "big") + (844).to_bytes(4, "big"))
            source = root / "studio/src/App.tsx"
            source.parent.mkdir(parents=True)
            source.write_text("fixture source")
            version = root / "studio/package.json"
            version.write_text('{"version":"0.2.1"}')
            capture = root / "studio/tests/docs-walkthrough.spec.ts"
            capture.parent.mkdir(parents=True)
            capture.write_text("fixture capture test")

            def sha(path):
                return hashlib.sha256(path.read_bytes()).hexdigest()

            manifest = {
                "schema_version": 1,
                "fixture_only": True,
                "frontend": {"studio/src/App.tsx": sha(source), "studio/package.json": sha(version)},
                "capture_test_sha256": sha(capture),
                "captures": {
                    "phone.png": {
                        "path": "assets/screenshots/phone.png",
                        "viewport": {"width": 390, "height": 844},
                        "sha256": sha(asset),
                    }
                },
            }
            (asset.parent / "walkthrough-manifest.json").write_text(json.dumps(manifest))
            hook._load_walkthrough({"docs_dir": str(docs)}, root)
            manifest["studio_version"] = "0.2.2"
            manifest["captures"]["phone.png"]["studio_version"] = "0.2.1"
            (asset.parent / "walkthrough-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "stale Studio version"):
                hook._load_walkthrough({"docs_dir": str(docs)}, root)
            manifest["studio_version"] = "0.2.1"
            manifest["captures"]["phone.png"]["frontend_digest"] = "outdated-source"
            (asset.parent / "walkthrough-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "stale frontend"):
                hook._load_walkthrough({"docs_dir": str(docs)}, root)
            del manifest["captures"]["phone.png"]["frontend_digest"]
            (asset.parent / "walkthrough-manifest.json").write_text(json.dumps(manifest))
            version.write_text('{"version":"0.2.2"}')
            with self.assertRaisesRegex(ValueError, "frontend.*package.json"):
                hook._load_walkthrough({"docs_dir": str(docs)}, root)
            version.write_text('{"version":"0.2.1"}')
            source.write_text("changed frontend")
            with self.assertRaisesRegex(ValueError, "frontend.*App.tsx"):
                hook._load_walkthrough({"docs_dir": str(docs)}, root)
            source.write_text("fixture source")
            asset.write_bytes(b"changed screenshot")
            with self.assertRaisesRegex(ValueError, "screenshot.*phone.png"):
                hook._load_walkthrough({"docs_dir": str(docs)}, root)


if __name__ == "__main__":
    unittest.main()
