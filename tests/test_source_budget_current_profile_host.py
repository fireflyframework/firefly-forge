# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit controlled profile vectors; no native qualification or execution."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from source_edit_qualification_fixtures import register_fixture_profile

from firefly_forge import source_budget_allowances as budgets
from firefly_forge import source_edit_qualification as q
from firefly_forge.source_edit_filesystem_policy import build_named_permissions, source_private_roots
from firefly_forge.source_edit_model_catalog import model_catalog_path
from firefly_forge.source_edit_observed_reconciliation import _configuration_digest


def transition_case(tmp_path, monkeypatch, *, historical=False, current_first=True, control=None):
    executable = tmp_path / "controlled-native"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    current = register_fixture_profile(monkeypatch, executable)
    previous = replace(current, version=6, loader_profile="codex-exec-restricted-source/v7")
    if control is not None:
        previous = replace(previous, **control)
    previous = replace(previous, proof=replace(previous.proof, contract_digest=q.profile_contract_digest(previous)))
    monkeypatch.setattr(q, "_profiles", lambda: (current, previous) if current_first else (previous, current))
    config = SimpleNamespace(
        directory=tmp_path / "state", load_snapshot=lambda revision: {"FORGE_CODEX_MODEL": current.model}
    )
    r = SimpleNamespace(
        store=SimpleNamespace(configuration=config), engine=SimpleNamespace(workspaces=tmp_path / "workspaces")
    )
    run = {"id": "controlled-source-run", "config_revision": "frozen-revision"}
    (r.engine.workspaces / run["id"]).mkdir(parents=True)
    permissions = build_named_permissions(r.engine.workspaces / run["id"], source_private_roots(config.directory))
    previous_path = str(tmp_path / "prior/resources/native/codex-0.163.0/source-only-model.json")
    selected = previous if historical else current
    prior_seal = q.historical_profile_seal(previous) if historical else q.profile_seal(current)
    common = {
        "calls_limit": 35,
        "per_tool_limit": 10,
        "tools": ["read_file"],
        "source_tree": "a" * 40,
        "original13_sha256": "b" * 64,
        "commands": ["python -m controlled_checks"],
    }
    old = {
        **deepcopy(common),
        "qualification": prior_seal,
        "configuration_proof": {
            "profile": prior_seal,
            "configuration_digest": _configuration_digest(selected, permissions, previous_path),
            "managed_controls": "unchanged",
        },
    }
    current_seal = q.profile_seal(current)
    scope = {
        **deepcopy(common),
        "executable": {"sha256": current.executable_sha256, "size": current.executable_size},
        "qualification": current_seal,
        "configuration_proof": {
            "profile": current_seal,
            "configuration_digest": _configuration_digest(current, permissions, str(model_catalog_path(current.model))),
            "managed_controls": "unchanged",
        },
    }
    old["executable"] = deepcopy(scope["executable"])
    review = {"parameters": {"read_file_lifetime_limit": 20, "activation_ceiling": 5}, "policy": {"digest": "c" * 64}}
    return r, run, old, scope, previous_path, review, current, previous


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("current_first", [False, True])
def test_validated_current_or_strict_older_seal_preserves_scope_and_counters(
    tmp_path, monkeypatch, historical, current_first
):
    case = transition_case(tmp_path, monkeypatch, historical=historical, current_first=current_first)
    r, run, old, scope, path, review, current, previous = case
    before = deepcopy((old, scope, review, run))
    strict = q.historical_profile_seal
    seen = []

    def older_only(profile):
        assert profile.version < current.version
        seen.append(profile)
        return strict(profile)

    monkeypatch.setattr(q, "historical_profile_seal", older_only)
    result, provenance = budgets.qualified_scope_transition(r, run, old, scope, path, review)
    assert result["budget_allowance"]["read_file_lifetime_limit"] == 20
    assert result["budget_allowance"]["activation_ceiling"] == 5
    assert result["budget_allowance"]["original_calls_limit"] == 35
    assert result["budget_allowance"]["original_per_tool_limit"] == 10
    assert (old, scope, review, run) == before
    assert provenance["native_controls_unchanged"] is True
    assert provenance["previous_profile"] == old["qualification"]
    assert provenance["current_profile"] == scope["qualification"]
    assert seen == ([previous] if historical else [])
    with pytest.raises(ValueError):
        q.require_profile(previous)


@pytest.mark.parametrize(
    "field", ["profile_id", "profile_digest", "proof_sha256", "registry_version", "loader_profile"]
)
def test_unknown_or_mutated_parent_seal_never_selects_by_name_or_version(tmp_path, monkeypatch, field):
    r, run, old, scope, path, review, _, _ = transition_case(tmp_path, monkeypatch)
    old["qualification"] = old["qualification"] | {field: "not-the-exact-seal"}
    old["configuration_proof"]["profile"] = old["qualification"]
    with pytest.raises(ValueError, match="no exact package-owned historical profile"):
        budgets.qualified_scope_transition(r, run, old, scope, path, review)


def test_unqualified_current_profile_cannot_become_parent_authority(tmp_path, monkeypatch):
    r, run, old, scope, path, review, current, _ = transition_case(tmp_path, monkeypatch)
    invalid = replace(current, proof=replace(current.proof, artifact_sha256="not-a-proof"))
    invalid = replace(invalid, proof=replace(invalid.proof, contract_digest=q.profile_contract_digest(invalid)))
    monkeypatch.setattr(q, "_profiles", lambda: (invalid,))
    with pytest.raises(ValueError):
        budgets.qualified_scope_transition(r, run, old, scope, path, review)


@pytest.mark.parametrize("change", ["commands", "calls_limit", "per_tool_limit", "configuration", "over_budget"])
def test_same_current_profile_does_not_relax_original_scope_config_or_numeric_limit(tmp_path, monkeypatch, change):
    r, run, old, scope, path, review, _, _ = transition_case(tmp_path, monkeypatch)
    if change == "configuration":
        scope["configuration_proof"]["managed_controls"] = "changed"
    elif change == "over_budget":
        review["parameters"]["read_file_lifetime_limit"] = 21
    else:
        scope[change] = ["different command"] if change == "commands" else 36
    with pytest.raises(ValueError):
        budgets.qualified_scope_transition(r, run, old, scope, path, review)


def test_historical_native_control_drift_is_still_refused(tmp_path, monkeypatch):
    r, run, old, scope, path, review, _, _ = transition_case(
        tmp_path, monkeypatch, historical=True, current_first=False, control={"native_provider": "changed-provider"}
    )
    with pytest.raises(ValueError, match="cannot widen native provider or isolation controls"):
        budgets.qualified_scope_transition(r, run, old, scope, path, review)
