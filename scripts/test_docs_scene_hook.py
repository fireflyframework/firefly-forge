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
    def test_shared_helper_config_and_each_producer_bind_the_exact_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs"
            paths = {
                "studio/src/App.tsx": "fixture source",
                "studio/index.html": "fixture entry",
                "studio/package.json": '{"version":"0.2.3"}',
                "studio/tests/walkthrough-capture.ts": "fixture helper",
                "studio/tests/navigation.ts": "fixture navigation",
                "studio/tests/auth-fixture.ts": "fixture identity",
                "studio/tests/learning-record-fixture.ts": "fixture learning record",
                "studio/tests/docs-walkthrough.spec.ts": "fixture console producer",
                "studio/tests/external-tools.spec.ts": "fixture MCP producer",
                "studio/playwright.docs.config.ts": "fixture capture config",
            }
            for name, content in paths.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            (root / "studio/public").mkdir()
            asset = docs / "assets/screenshots/external-phone-mobile-light.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(bytes(16) + (390).to_bytes(4, "big") + (844).to_bytes(4, "big"))

            def sha(path):
                return hashlib.sha256(path.read_bytes()).hexdigest()

            def fingerprint(value):
                return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

            frontend = {
                name: sha(root / name)
                for name in paths
                if not name.startswith("studio/tests") and name != "studio/playwright.docs.config.ts"
            }
            helpers = {
                name: sha(root / name)
                for name in (
                    "studio/tests/walkthrough-capture.ts",
                    "studio/tests/navigation.ts",
                    "studio/tests/auth-fixture.ts",
                    "studio/tests/learning-record-fixture.ts",
                )
            }
            producers = {name: sha(root / name) for name in paths if name.endswith(".spec.ts")}
            config = {
                "path": "studio/playwright.docs.config.ts",
                "sha256": sha(root / "studio/playwright.docs.config.ts"),
            }
            record = {
                "fixture": True,
                "stage": "external-phone",
                "theme": "light",
                "device": "mobile",
                "path": "assets/screenshots/external-phone-mobile-light.png",
                "viewport": {"width": 390, "height": 844},
                "sha256": sha(asset),
                "studio_version": "0.2.3",
                "frontend_digest": fingerprint(frontend),
                "producer": "studio/tests/external-tools.spec.ts",
                "producer_sha256": producers["studio/tests/external-tools.spec.ts"],
                "capture_helpers_digest": fingerprint(helpers),
                "capture_config_sha256": config["sha256"],
            }
            manifest = {
                "schema_version": 2,
                "fixture_only": True,
                "studio_version": "0.2.3",
                "frontend": frontend,
                "capture_helpers": helpers,
                "capture_producers": producers,
                "capture_config": config,
                "captures": {"external-phone-mobile-light.png": record},
            }
            manifest_path = asset.parent / "walkthrough-manifest.json"

            def save():
                manifest_path.write_text(json.dumps(manifest))

            save()
            hook._load_walkthrough({"docs_dir": str(docs)}, root)
            for name, error in [
                ("studio/tests/walkthrough-capture.ts", "capture helper"),
                ("studio/tests/learning-record-fixture.ts", "capture helper"),
                ("studio/tests/docs-walkthrough.spec.ts", "capture producer"),
                ("studio/playwright.docs.config.ts", "capture config"),
            ]:
                (root / name).write_text("changed input")
                with self.assertRaisesRegex(ValueError, error):
                    hook._load_walkthrough({"docs_dir": str(docs)}, root)
                (root / name).write_text(paths[name])
            for field, wrong, error in [
                ("producer_sha256", "stale", "stale producer"),
                ("producer", "studio/tests/unknown.spec.ts", "unknown producer"),
                ("capture_helpers_digest", "stale", "stale capture helpers"),
                ("capture_config_sha256", "stale", "stale capture config"),
            ]:
                original = record[field]
                record[field] = wrong
                save()
                with self.assertRaisesRegex(ValueError, error):
                    hook._load_walkthrough({"docs_dir": str(docs)}, root)
                record[field] = original
                save()
            for group, name, error in [
                ("capture_helpers", "studio/tests/navigation.ts", "capture helper registry"),
                ("capture_producers", "studio/tests/docs-walkthrough.spec.ts", "capture producer registry"),
            ]:
                original = manifest[group].pop(name)
                record["capture_helpers_digest"] = fingerprint(helpers)
                save()
                try:
                    with self.subTest(missing=group), self.assertRaisesRegex(ValueError, error):
                        hook._load_walkthrough({"docs_dir": str(docs)}, root)
                finally:
                    manifest[group][name] = original
                    record["capture_helpers_digest"] = fingerprint(helpers)
                    save()
            for field in ("fixture", "stage", "device", "theme"):
                original = record.pop(field)
                save()
                try:
                    with self.subTest(missing=field), self.assertRaisesRegex(ValueError, "fixture|stage"):
                        hook._load_walkthrough({"docs_dir": str(docs)}, root)
                finally:
                    record[field] = original
                    save()
            for field, wrong in (
                ("fixture", False),
                ("stage", "external-other"),
                ("device", "desktop"),
                ("theme", "dark"),
            ):
                original = record[field]
                record[field] = wrong
                save()
                try:
                    with self.subTest(wrong=field), self.assertRaisesRegex(ValueError, "fixture|stage"):
                        hook._load_walkthrough({"docs_dir": str(docs)}, root)
                finally:
                    record[field] = original
                    save()
            record["producer"] = "studio/tests/docs-walkthrough.spec.ts"
            record["producer_sha256"] = producers[record["producer"]]
            save()
            with self.assertRaisesRegex(ValueError, "stage.*producer"):
                hook._load_walkthrough({"docs_dir": str(docs)}, root)
            record["producer"] = "studio/tests/external-tools.spec.ts"
            record["producer_sha256"] = producers[record["producer"]]
            save()
            extra = root / "studio/src/New.tsx"
            extra.write_text("new frontend input")
            with self.assertRaisesRegex(ValueError, "frontend inventory"):
                hook._load_walkthrough({"docs_dir": str(docs)}, root)

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
            dark_reference = "![Connect & inspect](assets/screenshots/connect-mobile-dark.png)"
            output = hook.on_page_markdown(dark_reference, SimpleNamespace(url="console-tour/"), {})
            self.assertIn(
                'class="forge-shot__link theme-light" href="../assets/screenshots/connect-mobile-light.png"', output
            )
            self.assertIn(
                'class="forge-shot__link theme-dark" href="../assets/screenshots/connect-mobile-dark.png"', output
            )
        finally:
            hook._walkthrough = previous

    def test_theme_pair_never_invents_a_missing_captured_appearance(self):
        previous = hook._walkthrough
        try:
            hook._walkthrough = {
                "captures": {
                    "connect-mobile-dark.png": {
                        "path": "assets/screenshots/connect-mobile-dark.png",
                        "viewport": {"width": 390, "height": 844},
                    }
                }
            }
            with self.assertRaisesRegex(ValueError, "counterpart"):
                hook.on_page_markdown(
                    "![Connect](assets/screenshots/connect-mobile-dark.png)", SimpleNamespace(url="console-tour/"), {}
                )
            markdown = "![Uncaptured](assets/screenshots/not-a-capture.png)"
            self.assertEqual(hook.on_page_markdown(markdown, SimpleNamespace(url="console-tour/"), {}), markdown)
        finally:
            hook._walkthrough = previous

    def test_rejects_a_changed_capture_or_frontend_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs"
            asset = docs / "assets/screenshots/phone-mobile-light.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(bytes(16) + (390).to_bytes(4, "big") + (844).to_bytes(4, "big"))
            source = root / "studio/src/App.tsx"
            source.parent.mkdir(parents=True)
            source.write_text("fixture source")
            version = root / "studio/package.json"
            version.write_text('{"version":"0.2.1"}')
            entry = root / "studio/index.html"
            entry.write_text("fixture entry")
            (root / "studio/public").mkdir()
            capture = root / "studio/tests/docs-walkthrough.spec.ts"
            capture.parent.mkdir(parents=True)
            capture.write_text("fixture capture test")
            helper = root / "studio/tests/walkthrough-capture.ts"
            helper.write_text("fixture capture helper")
            navigation = root / "studio/tests/navigation.ts"
            navigation.write_text("fixture navigation")
            identity = root / "studio/tests/auth-fixture.ts"
            identity.write_text("fixture identity")
            learning_fixture = root / "studio/tests/learning-record-fixture.ts"
            learning_fixture.write_text("fixture learning record")
            mcp_capture = root / "studio/tests/external-tools.spec.ts"
            mcp_capture.write_text("fixture MCP producer")
            capture_config = root / "studio/playwright.docs.config.ts"
            capture_config.write_text("fixture capture config")

            def sha(path):
                return hashlib.sha256(path.read_bytes()).hexdigest()

            manifest = {
                "schema_version": 2,
                "fixture_only": True,
                "studio_version": "0.2.1",
                "frontend": {
                    "studio/src/App.tsx": sha(source),
                    "studio/package.json": sha(version),
                    "studio/index.html": sha(entry),
                },
                "capture_producers": {
                    "studio/tests/docs-walkthrough.spec.ts": sha(capture),
                    "studio/tests/external-tools.spec.ts": sha(mcp_capture),
                },
                "capture_helpers": {
                    "studio/tests/walkthrough-capture.ts": sha(helper),
                    "studio/tests/navigation.ts": sha(navigation),
                    "studio/tests/auth-fixture.ts": sha(identity),
                    "studio/tests/learning-record-fixture.ts": sha(learning_fixture),
                },
                "capture_config": {"path": "studio/playwright.docs.config.ts", "sha256": sha(capture_config)},
                "captures": {
                    "phone-mobile-light.png": {
                        "fixture": True,
                        "stage": "phone",
                        "device": "mobile",
                        "theme": "light",
                        "path": "assets/screenshots/phone-mobile-light.png",
                        "viewport": {"width": 390, "height": 844},
                        "sha256": sha(asset),
                        "studio_version": "0.2.1",
                        "producer": "studio/tests/docs-walkthrough.spec.ts",
                        "producer_sha256": sha(capture),
                        "capture_config_sha256": sha(capture_config),
                    }
                },
            }
            for key, source_key in (("frontend_digest", "frontend"), ("capture_helpers_digest", "capture_helpers")):
                manifest["captures"]["phone-mobile-light.png"][key] = hashlib.sha256(
                    json.dumps(manifest[source_key], sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            (asset.parent / "walkthrough-manifest.json").write_text(json.dumps(manifest))
            hook._load_walkthrough({"docs_dir": str(docs)}, root)
            manifest["studio_version"] = "0.2.2"
            manifest["captures"]["phone-mobile-light.png"]["studio_version"] = "0.2.1"
            (asset.parent / "walkthrough-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "stale Studio version"):
                hook._load_walkthrough({"docs_dir": str(docs)}, root)
            manifest["studio_version"] = "0.2.1"
            manifest["captures"]["phone-mobile-light.png"]["frontend_digest"] = "outdated-source"
            (asset.parent / "walkthrough-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "stale frontend"):
                hook._load_walkthrough({"docs_dir": str(docs)}, root)
            manifest["captures"]["phone-mobile-light.png"]["frontend_digest"] = hashlib.sha256(
                json.dumps(manifest["frontend"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
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
            with self.assertRaisesRegex(ValueError, "screenshot.*phone-mobile-light.png"):
                hook._load_walkthrough({"docs_dir": str(docs)}, root)


if __name__ == "__main__":
    unittest.main()
