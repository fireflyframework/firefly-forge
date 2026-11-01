# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Every new authority delegate joins the existing ordered native fingerprints."""

import hashlib
import json
from pathlib import Path

import pytest
from test_source_edit_v14_transition_host import declared_names

from firefly_forge import agentic_checkpoint_invocation, source_budget_allowances

CURRENT_AUTHORITY_APPEND = (
    "linked_verification_models.py",
    "linked_verifications.py",
    "linked_verification_controllers.py",
    "git_linked_verification.py",
    "scm_linked_verification.py",
    "scm_linked_verification_models.py",
    "scm_linked_verification_records.py",
    "object_storage.py",
    "runner_artifact_publications.py",
    "runner_object_artifacts.py",
    "source_archive.py",
    "source_archive_index.py",
    "source_archive_scan_models.py",
    "source_archive_service.py",
    "source_explorer.py",
    "coding_check_grants.py",
    "coding_check_models.py",
)


MATERIALS = {
    "agentic_checkpoint_invocation.py": {
        "function": "runtime_contract",
        "count": 184,
        "prefix_sha256": "58f55a6c8bfe57a18902eeb126fab7c6f04c55c53149ecc6f98219b851df7f16",
        "core_count": 186,
        "core_prefix_sha256": "802dd2abd5000911d1357126b5420db74d74301ca1a4d5f457c70241229b634f",
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
            *CURRENT_AUTHORITY_APPEND,
        ],
    },
    "source_budget_allowances.py": {
        "function": "admission_contract_digest",
        "count": 203,
        "prefix_sha256": "657657f11e8ed1ac865bdcaddc295c05168bd1df2e4004f6c4edb205a77118e8",
        "core_count": 205,
        "core_prefix_sha256": "b6b85b0b4107d93cee0f174a4618179357bbe3e9d2f0241ee71ace724b1f61a7",
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
            *CURRENT_AUTHORITY_APPEND,
            "workspace_tools.py",
        ],
    },
}


@pytest.mark.parametrize("filename", MATERIALS)
def test_existing_material_prefix_is_preserved_and_projects_append(filename):
    contract = MATERIALS[filename]
    root = Path(agentic_checkpoint_invocation.__file__).parent
    names = list(declared_names(root / filename, contract["function"], contract["core_count"]))
    historical = [
        name
        for name in names
        if name not in {"consumed_terminal_completions.py", "consumed_terminal_reconciliations.py"}
    ]
    assert (
        hashlib.sha256(json.dumps(historical[: contract["count"]], separators=(",", ":")).encode()).hexdigest()
        == contract["prefix_sha256"]
    )
    assert (
        hashlib.sha256(json.dumps(names[: contract["core_count"]], separators=(",", ":")).encode()).hexdigest()
        == contract["core_prefix_sha256"]
    )
    assert names[contract["core_count"] :] == contract["append"]


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
