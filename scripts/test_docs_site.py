# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
# https://www.apache.org/licenses/LICENSE-2.0
"""Qualify repository-base links using local pages and deliberate broken targets."""

import tempfile
import unittest
from pathlib import Path

from scripts.check_docs_site import BASE, check_site


class DocsSiteTests(unittest.TestCase):
    def test_nested_page_asset_and_encoded_anchor_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "guide").mkdir()
            (root / "image.png").write_bytes(b"fixture image")
            (root / "index.html").write_text(
                f'<link rel="canonical" href="{BASE}"><a href="guide/#field%3A1">Guide</a>'
            )
            (root / "guide/index.html").write_text(
                f'<link rel="canonical" href="{BASE}guide/"><h1 id="field:1">Guide</h1><img src="../image.png">'
            )
            self.assertEqual(check_site(root), (2, 4))

    def test_rejects_root_escape_missing_assets_and_wrong_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for content, error in [
                (f'<link rel="canonical" href="{BASE}"><a href="/guide/">Wrong root</a>', "repository base path"),
                (f'<link rel="canonical" href="{BASE}"><img src="missing.png">', "missing local target"),
                ('<link rel="canonical" href="https://example.test/">', "canonical URL"),
                (f'<link rel="canonical" href="{BASE}"><a href="#missing">Anchor</a>', "missing local anchor"),
            ]:
                with self.subTest(error=error):
                    (root / "index.html").write_text(content)
                    with self.assertRaisesRegex(ValueError, error):
                        check_site(root)


if __name__ == "__main__":
    unittest.main()
