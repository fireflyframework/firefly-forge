# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Every new authority delegate joins the existing ordered native fingerprints."""

import ast
import hashlib
import json
from pathlib import Path

import pytest

from firefly_forge import agentic_checkpoint_invocation, source_budget_allowances

MATERIALS = {
    "agentic_checkpoint_invocation.py": {
        "function": "runtime_contract",
        "count": 184,
        "prefix_sha256": "58f55a6c8bfe57a18902eeb126fab7c6f04c55c53149ecc6f98219b851df7f16",
        "append": [
            "github_projects.py",
            "github_projects_controllers.py",
            "github_projects_effects.py",
            "github_projects_graphql.py",
            "github_projects_models.py",
            "github_projects_oauth.py",
            "github_projects_persistence.py",
            "github_projects_projection.py",
            "github_projects_references.py",
            "github_projects_requests.py",
            "github_projects_worker.py",
            "connection_oauth.py",
            "connection_oauth_models.py",
            "connection_oauth_persistence.py",
            "connection_oauth_controllers.py",
            "oauth_provider_policies.py",
            "oauth_resources.py",
            "oauth_authority.py",
            "oauth_credential_fingerprints.py",
        ],
    },
    "source_budget_allowances.py": {
        "function": "admission_contract_digest",
        "count": 203,
        "prefix_sha256": "657657f11e8ed1ac865bdcaddc295c05168bd1df2e4004f6c4edb205a77118e8",
        "append": [
            "github_projects.py",
            "github_projects_controllers.py",
            "github_projects_effects.py",
            "github_projects_graphql.py",
            "github_projects_models.py",
            "github_projects_oauth.py",
            "github_projects_persistence.py",
            "github_projects_projection.py",
            "github_projects_references.py",
            "github_projects_requests.py",
            "github_projects_worker.py",
            "connection_oauth_models.py",
            "connection_oauth_persistence.py",
            "connection_oauth_controllers.py",
            "oauth_provider_policies.py",
            "oauth_resources.py",
            "oauth_authority.py",
        ],
    },
}


@pytest.mark.parametrize("filename", MATERIALS)
def test_existing_material_prefix_is_preserved_and_projects_append(filename):
    contract = MATERIALS[filename]
    root = Path(agentic_checkpoint_invocation.__file__).parent
    tree = ast.parse((root / filename).read_text())
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == contract["function"]
    )
    tuples = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Tuple)
        and len(node.elts) > 10
        and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value.endswith(".py")
            for item in node.elts
        )
    ]
    assert len(tuples) == 1
    names = [node.value for node in tuples[0].elts]
    assert (
        hashlib.sha256(json.dumps(names[: contract["count"]], separators=(",", ":")).encode()).hexdigest()
        == contract["prefix_sha256"]
    )
    assert names[contract["count"] :] == contract["append"]


@pytest.mark.parametrize("name", MATERIALS["agentic_checkpoint_invocation.py"]["append"])
def test_authority_delegate_mutation_changes_both_contracts(monkeypatch, name):
    functions = [agentic_checkpoint_invocation.runtime_contract, source_budget_allowances.admission_contract_digest]
    before = [function() for function in functions]
    read = Path.read_bytes

    def changed(path):
        value = read(path)
        return value + b"\n# controlled authority mutation\n" if path.name == name else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert all(function() != expected for function, expected in zip(functions, before, strict=True))
