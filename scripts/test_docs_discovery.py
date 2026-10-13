# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Public discovery contracts must retain correct repository paths and safe metadata."""

import importlib
import json
import unittest
from types import SimpleNamespace


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("scripts.docs_discovery_hook")
        self.config = {
            "site_name": "firefly forge",
            "site_url": "https://example.org/firefly-forge/",
            "site_author": "Firefly Software Foundation",
            "extra": {"discovery": {}},
        }
        self.page = SimpleNamespace(
            title="Architecture",
            is_homepage=False,
            canonical_url="https://example.org/firefly-forge/architecture/",
            file=SimpleNamespace(src_uri="architecture.md"),
            meta={
                "description": "Understand factory admission, persistent execution evidence "
                "and the boundaries between agents and tools."
            },
        )

    def test_repository_prefix_and_card_identity_are_preserved(self):
        data = self.module.discovery_data(self.page, self.config)
        self.assertEqual(data["url"], self.page.canonical_url)
        self.assertTrue(data["image"].startswith(self.config["site_url"] + "assets/social/"))
        self.assertEqual(data["title"], "Architecture | firefly forge")
        self.assertEqual(data["schema"]["@type"], "WebPage")
        self.assertEqual(data["schema"]["isPartOf"]["url"], self.config["site_url"])

    def test_metadata_escapes_html_and_script_terminators(self):
        self.page.title = '</script><script>alert("x")</script>'
        data = self.module.discovery_data(self.page, self.config)
        output = self.module.render_meta(data)
        self.assertIn("&lt;/script&gt;", output)
        self.assertNotIn("<script>alert(", output)
        script = output.split('type="application/ld+json">')[1].split("</script>")[0]
        self.assertEqual(json.loads(script)["name"], data["page_title"])

    def test_missing_description_stops_the_build(self):
        self.page.meta = {}
        with self.assertRaisesRegex(ValueError, "description"):
            self.module.discovery_data(self.page, self.config)

    def test_homepage_has_site_identity_and_web_site_reference(self):
        self.page.is_homepage = True
        self.page.canonical_url = self.config["site_url"]
        self.page.meta["title"] = "firefly forge software factory"
        data = self.module.discovery_data(self.page, self.config)
        self.assertEqual(data["title"], self.page.meta["title"])
        self.assertEqual(data["schema"]["isPartOf"]["@type"], "WebSite")

    def test_canonical_url_cannot_escape_the_repository(self):
        for url in [
            "http://example.org/firefly-forge/",
            "https://elsewhere.org/",
            "https://example.org/other/",
            "https://example.org/firefly-forge/?secret=value",
        ]:
            with self.subTest(url=url):
                self.page.canonical_url = url
                with self.assertRaises(ValueError):
                    self.module.discovery_data(self.page, self.config)

    def test_template_404_is_noindex_without_publishing_a_card(self):
        output = self.module.on_post_template("<html><head></head><body></body></html>", "404.html", self.config)
        self.assertIn('content="noindex,follow"', output)
        self.assertNotIn("og:image", output)

    def test_card_inputs_are_bounded_and_descriptions_are_not_fabricated(self):
        self.page.meta["description"] = "a" * 241
        with self.assertRaises(ValueError):
            self.module.discovery_data(self.page, self.config)

    def test_all_generated_metadata_uses_public_canonical_resources(self):
        data = self.module.discovery_data(self.page, self.config)
        output = self.module.render_meta(data)
        for name in [
            "og:url",
            "og:image",
            "og:image:width",
            "og:image:height",
            "og:image:alt",
            "twitter:card",
            "twitter:image:alt",
            "robots",
        ]:
            self.assertIn(name, output)
        self.assertIn("summary_large_image", output)
        self.assertNotIn("google-analytics", output)


if __name__ == "__main__":
    unittest.main()
