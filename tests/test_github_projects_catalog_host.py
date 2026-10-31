# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reviewed prompt rows retain their exact content; new tools append explicit resources."""

import hashlib
import json
from pathlib import Path

from firefly_forge.prompts import prompt_catalog


def test_projects_appends_eight_tools_after_the_exact_parent_prompt_rows():
    root = Path(__file__).parents[1] / "src/firefly_forge/resources/prompts/v1"
    manifest = json.loads((root / "manifest.json").read_text())
    original = json.dumps(manifest["templates"][:341], sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(original).hexdigest() == "828db0243ba2b53cbad932c9c46b9ced69eacd0e774d4335578e69dbe47993e8"
    names = {
        "create_github_projects",
        "get_github_projects_creation",
        "get_github_projects_connection",
        "discover_github_projects",
        "configure_github_projects",
        "sync_github_projects",
        "get_github_projects_request",
        "observe_github_projects",
    }
    assert {entry["id"] for entry in manifest["templates"][341:]} == {"mcp/" + name for name in names}
    assert len(manifest["templates"]) == len(prompt_catalog().registry) == 349
    for row in manifest["templates"]:
        assert hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() == row["sha256"]
