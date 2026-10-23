# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An unqualified executable cannot turn safe-looking metadata into authority."""

from dataclasses import replace

import pytest
from source_edit_qualification_fixtures import register_fixture_profile

from firefly_forge import source_edit_qualification as module
from firefly_forge.provider_executable_adoption import executable_identity


@pytest.fixture
def qualified(tmp_path, monkeypatch):
    executable = tmp_path / "public-fixture"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    profile = register_fixture_profile(monkeypatch, executable)
    return profile, executable_identity(str(executable), tmp_path / "workspaces")


def test_empty_registry_admits_neither_unknown_build_nor_pinned_unproven_build(monkeypatch):
    monkeypatch.setattr(module, "_profiles", lambda: ())
    assert module._profiles() == ()
    for checksum, size in (
        ("f" * 64, 100),
        ("8813367285524ac798dd96d1b071aea30d58708c7226fd236cd275cf1cfdee31", 249952688),
    ):
        with pytest.raises(ValueError, match="qualified"):
            module.qualified_profile({"sha256": checksum, "size": size}, "gpt-6-astra")


def test_fixture_profile_seals_observed_capabilities_and_runtime(qualified):
    profile, identity = qualified
    assert module.qualified_profile(identity, "fixture-model") == profile
    seal = module.profile_seal(profile)
    assert set(seal) == {
        "registry_version",
        "profile_id",
        "profile_digest",
        "catalog_digest",
        "proof_sha256",
        "loader_profile",
    }
    assert "closed" not in seal and len(seal["profile_digest"]) == 64


@pytest.mark.parametrize("missing", ["collaboration_tools_denied", "tool_search_denied"])
def test_profile_requires_proof_of_model_driven_collaboration_and_discovery_denials(qualified, monkeypatch, missing):
    profile, identity = qualified
    changed = replace(
        profile,
        proof=replace(
            profile.proof, outcome_checks=tuple(value for value in profile.proof.outcome_checks if value != missing)
        ),
    )
    changed = replace(changed, proof=replace(changed.proof, contract_digest=module.profile_contract_digest(changed)))
    monkeypatch.setattr(module, "_profiles", lambda: (changed,))
    with pytest.raises(ValueError, match="qualified"):
        module.qualified_profile(identity, "fixture-model")


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 999),
        ("system", "other"),
        ("machine", "other"),
        ("policy_digest", "f" * 64),
        ("model_catalog_sha256", "f" * 64),
        ("loader_profile", "different-loader"),
        ("loader_hashes", ()),
        ("runtime_versions", ()),
        ("catalog", ()),
        ("executable_size", 0),
    ],
)
def test_profile_contract_or_loader_drift_refuses(qualified, monkeypatch, field, value):
    profile, identity = qualified
    changed = replace(profile, **{field: value})
    changed = replace(changed, proof=replace(changed.proof, contract_digest=module.profile_contract_digest(changed)))
    monkeypatch.setattr(module, "_profiles", lambda: (changed,))
    with pytest.raises(ValueError, match="qualified"):
        module.qualified_profile(identity, "fixture-model")


@pytest.mark.parametrize(
    "field,value",
    [
        ("exit_code", 65),
        ("exit_code", False),
        ("requests", 0),
        ("requests", True),
        ("requests", 513),
        ("outcome_checks", ()),
        ("reader_calls", ()),
        ("group_absent", False),
        ("streams_drained", False),
        ("filesystem_policy_version", 999),
        ("filesystem_checks", ()),
        ("real_model_calls", 1),
        ("artifact_sha256", ""),
    ],
)
def test_missing_or_incomplete_native_proof_refuses(qualified, monkeypatch, field, value):
    profile, identity = qualified
    changed = replace(profile, proof=replace(profile.proof, **{field: value}))
    monkeypatch.setattr(module, "_profiles", lambda: (changed,))
    with pytest.raises(ValueError, match="qualified"):
        module.qualified_profile(identity, "fixture-model")


def test_binary_or_model_change_cannot_reuse_profile(qualified):
    _profile, identity = qualified
    for changed, model in ((identity | {"sha256": "f" * 64}, "fixture-model"), (identity, "other-model")):
        with pytest.raises(ValueError, match="qualified"):
            module.qualified_profile(changed, model)


def test_catalog_schema_cannot_borrow_another_proof(qualified, monkeypatch):
    profile, identity = qualified
    name, kind, _checksum = profile.catalog[0]
    changed = replace(profile, catalog=((name, kind, "f" * 64), *profile.catalog[1:]))
    monkeypatch.setattr(module, "_profiles", lambda: (changed,))
    with pytest.raises(ValueError, match="qualified"):
        module.qualified_profile(identity, "fixture-model")


def test_declared_config_cannot_enable_known_execution_controls(qualified, monkeypatch):
    profile, identity = qualified
    for changed in (
        replace(profile, configuration_features=(("code_mode", True), *profile.configuration_features[1:])),
        replace(profile, configuration_tools=(("update_plan", True), ("experimental_request_user_input", False))),
    ):
        changed = replace(
            changed, proof=replace(changed.proof, contract_digest=module.profile_contract_digest(changed))
        )
        monkeypatch.setattr(module, "_profiles", lambda changed=changed: (changed,))
        with pytest.raises(ValueError, match="qualified"):
            module.qualified_profile(identity, "fixture-model")
