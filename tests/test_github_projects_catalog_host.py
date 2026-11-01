# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reviewed prompt rows retain their exact content; new tools append explicit resources."""

import hashlib
import json
from pathlib import Path

from firefly_forge.prompts import prompt_catalog


def test_projects_appends_eight_tools_after_the_exact_parent_prompt_rows():
    root = Path(__file__).parents[1] / "src/firefly_forge/resources/prompts/v1"
    manifest = json.loads((root / "manifest.json").read_text())
    core_additions = {
        ("collaboration/communication-request", "1.0.3"),
        ("collaboration/communication-review-system", "1.0.3"),
        ("collaboration/communication-review-system", "1.0.4"),
        ("collaboration/communication-review-system", "1.0.5"),
        ("collaboration/communication-review-system", "1.0.6"),
        ("collaboration/communication-system", "1.0.6"),
        ("collaboration/communication-system", "1.0.7"),
        ("collaboration/communication-system", "1.0.8"),
        ("collaboration/communication-system", "1.0.9"),
        ("collaboration/publication-review-system", "1.0.1"),
        ("collaboration/publication-system", "1.0.1"),
        ("mcp/get_consumed_terminal_candidate", "1.0.0"),
        ("mcp/get_consumed_terminal_reconciliation_candidate", "1.0.0"),
        ("mcp/get_consumed_terminal_reconciliation_status", "1.0.0"),
        ("mcp/get_consumed_terminal_status", "1.0.0"),
        ("mcp/reconcile_consumed_terminal", "1.0.0"),
        ("mcp/request_consumed_terminal", "1.0.0"),
    }
    parent = manifest["templates"][:358]
    assert {(entry["id"], entry["version"]) for entry in parent} >= core_additions
    original_rows = [entry for entry in parent if (entry["id"], entry["version"]) not in core_additions]
    assert len(original_rows) == 341
    original = json.dumps(original_rows, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(original).hexdigest() == "828db0243ba2b53cbad932c9c46b9ced69eacd0e774d4335578e69dbe47993e8"
    core = json.dumps(parent, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(core).hexdigest() == "122e70efc9b95c6c394ba915183eaf71fd252140f37abc15fea313d5efba5553"
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
    assert {(entry["id"], entry["version"]) for entry in manifest["templates"][358:366]} == {
        ("mcp/" + name, "1.0.0") for name in names
    }
    prior = json.dumps(manifest["templates"][:366], sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(prior).hexdigest() == "8c907f8be997e8dae0d34e9a051dafefb8b420ca451f77c9d068051af5e2c634"
    assert {(entry["id"], entry["version"]) for entry in manifest["templates"][366:]} == {
        ("collaboration/communication-system", "1.0.10"),
        ("collaboration/communication-review-system", "1.0.7"),
        ("collaboration/communication-repair-request", "1.0.2"),
        ("collaboration/communication-delivery-references", "1.0.0"),
    }
    assert len(manifest["templates"]) == len(prompt_catalog().registry) == 370
    for row in manifest["templates"]:
        assert hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() == row["sha256"]
