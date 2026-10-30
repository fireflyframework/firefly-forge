# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Colored forge identities retain Firefly's chevron accent across themes."""
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOT_EDGE = "M56-148H100L74-122H56Z"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("variant", ["forge-lockup", "forge-lockup-small", "forge-wordmark", "forge-mark"])
def test_forged_edge_matches_parent_chevron_in_each_colored_identity(theme, variant):
    parent = ElementTree.parse(ROOT / "assets/brand/firefly-icon.svg")
    assert any(node.get("fill") == "#ffb34a" for node in parent.iter())
    artwork = ElementTree.parse(ROOT / f"assets/brand/{variant}-{theme}.svg")
    edge = [node for node in artwork.iter() if node.get("d") == HOT_EDGE]
    assert len(edge) == 1
    assert edge[0].get("fill") == "#ffb34a"


def test_all_published_brand_copies_match_their_integrity_inventory():
    inventory = json.loads((ROOT / "assets/brand/inventory.json").read_text())
    for item in inventory["files"]:
        path = ROOT / item["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"], item["path"]
        if item.get("source") and (ROOT / item["source"]).is_file() and path.suffix == ".svg":
            assert path.read_bytes() == (ROOT / item["source"]).read_bytes(), item["path"]
