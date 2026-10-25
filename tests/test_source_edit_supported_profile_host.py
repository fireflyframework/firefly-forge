# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Only the package-owned measured native boundary profile grants source admission."""

from dataclasses import replace

import pytest

from firefly_forge import source_edit_qualification as q


def registered_profile(loader_profile):
    profiles = [profile for profile in q._profiles() if profile.loader_profile == loader_profile]
    assert len(profiles) == 1
    return profiles[0]


def test_package_owns_measured_profile():
    profiles = q._profiles()
    assert {profile.loader_profile for profile in profiles} == {
        "codex-exec-restricted-source/v5",
        "codex-exec-restricted-source/v6",
        "codex-exec-restricted-source/v7",
        "codex-exec-restricted-source/v8",
        "codex-exec-restricted-source/v9",
        "codex-exec-restricted-source/v10",
        "codex-exec-restricted-source/v11",
        "codex-exec-restricted-source/v12",
        "codex-exec-restricted-source/v13",
        "codex-exec-restricted-source/v14",
        "codex-exec-restricted-source/v15",
        q.LOADER_PROFILE,
    }
    assert len(profiles) == 12
    profile = registered_profile("codex-exec-restricted-source/v5")
    assert profile.profile_id == "codex-0163-darwin-arm64-source-v5"
    assert profile.executable_sha256 == "8813367285524ac798dd96d1b071aea30d58708c7226fd236cd275cf1cfdee31"
    assert profile.executable_size == 249952688
    assert profile.model == "gpt-6-astra" and profile.native_provider == "openai"
    assert profile.proof.artifact_sha256 == "5a29bcea8327bf04ee2aedbac99e15603e3aca8709ae9f40c60da965cf92f078"
    assert profile.proof.requests == 251
    assert profile.proof.outcome_checks == q.NATIVE_OUTCOMES
    assert profile.proof.filesystem_checks == q.FILESYSTEM_CHECKS
    assert profile.proof.real_model_calls == 0
    assert len(profile.catalog) == 8
    assert len(profile.configuration_features) == 38
    assert dict(profile.configuration_features)["network_proxy"] is None
    assert profile.proof.contract_digest == q.profile_contract_digest(profile)


def test_historical_loader_and_policy_keep_their_exact_provenance():
    profile = registered_profile("codex-exec-restricted-source/v5")
    assert len(profile.loader_hashes) == 17
    assert (
        q.historical_profile_seal(profile)["profile_digest"]
        == "17320f82c0f2f9ab4b65c7e3bd8b89741a3716a8896dd5f674f6409ac1490c3c"
    )
    assert profile.loader_hashes != q.loader_hashes()
    assert profile.loader_profile == "codex-exec-restricted-source/v5"
    assert q.LOADER_PROFILE == "codex-exec-restricted-source/v16"
    assert profile.policy_digest == q.policy_digest()
    with pytest.raises(ValueError, match="qualified"):
        q.require_profile(profile)
    with pytest.raises(ValueError, match="qualified"):
        q.source_budget_limits(profile)


def test_historical_v6_budget_and_receipt_remain_immutable():
    profile = registered_profile("codex-exec-restricted-source/v6")
    assert profile.version == 5 and len(profile.loader_hashes) == 20
    assert q.historical_profile_seal(profile)["profile_digest"] == (
        "20e13d34f87314b12fa9a25a0c6411e75e8eb70083a083d743c7fa5487cff76a"
    )
    assert profile.proof.artifact_sha256 == "e0a21355651f9f0cff58a19e162c85a0d6eb1bc2bc0f52d0575e156ce05ed516"
    assert profile.source_budget_limits == (("read_file_lifetime_limit", 20), ("activation_ceiling", 5))
    with pytest.raises(ValueError, match="qualified"):
        q.require_profile(profile)


@pytest.fixture
def measured_runtime(monkeypatch):
    profile = registered_profile(q.LOADER_PROFILE)
    monkeypatch.setattr(q.platform, "system", lambda: profile.system)
    monkeypatch.setattr(q.platform, "machine", lambda: profile.machine)
    monkeypatch.setattr(q, "runtime_versions", lambda: profile.runtime_versions)
    return profile


def test_current_runtime_must_match_measured_profile_before_admission(measured_runtime):
    profile = measured_runtime
    identity = {"sha256": profile.executable_sha256, "size": profile.executable_size}
    assert q.qualified_profile(identity, profile.model) == profile
    assert profile.version == q.REGISTRY_VERSION == 15
    assert profile.profile_id == "codex-0163-darwin-arm64-source-v16"
    from firefly_forge.source_budget_allowances import admission_contract_digest

    assert profile.admission_contract_digest == admission_contract_digest()
    assert len(profile.loader_hashes) == 22
    assert profile.loader_hashes == q.loader_hashes()
    assert profile.runtime_versions == q.runtime_versions()
    assert profile.proof.contract_digest == q.profile_contract_digest(profile)
    assert profile.proof.artifact_sha256 != "e0a21355651f9f0cff58a19e162c85a0d6eb1bc2bc0f52d0575e156ce05ed516"
    assert profile.proof.requests == 251
    assert profile.proof.outcome_checks == q.NATIVE_OUTCOMES
    assert profile.proof.filesystem_checks == q.FILESYSTEM_CHECKS
    assert profile.proof.budget_checks == q.SOURCE_BUDGET_CHECKS
    assert profile.proof.budget_limits == profile.source_budget_limits
    assert q.source_budget_limits(profile) == {"read_file_lifetime_limit": 20, "activation_ceiling": 6}
    assert q.profile_seal(profile)["proof_sha256"] == profile.proof.artifact_sha256


@pytest.mark.parametrize("drift", ["system", "machine", "runtime"])
def test_measured_profile_refuses_foreign_runtime(measured_runtime, monkeypatch, drift):
    profile = measured_runtime
    identity = {"sha256": profile.executable_sha256, "size": profile.executable_size}
    if drift == "system":
        monkeypatch.setattr(q.platform, "system", lambda: "Linux")
    elif drift == "machine":
        monkeypatch.setattr(q.platform, "machine", lambda: "x86_64")
    else:
        monkeypatch.setattr(q, "runtime_versions", lambda: (("python", "3.13.17"),))
    with pytest.raises(ValueError, match="qualified"):
        q.qualified_profile(identity, profile.model)


def test_profile_refuses_runtime_drift_and_unknown_build_model(monkeypatch):
    profile = registered_profile(q.LOADER_PROFILE)
    identity = {"sha256": profile.executable_sha256, "size": profile.executable_size}
    for changed, model in (
        (identity | {"sha256": "f" * 64}, profile.model),
        (identity | {"size": profile.executable_size + 1}, profile.model),
        (identity, "other-model"),
    ):
        with pytest.raises(ValueError, match="qualified"):
            q.qualified_profile(changed, model)
    monkeypatch.setattr(q, "runtime_versions", lambda: (("python", "unqualified"),))
    with pytest.raises(ValueError, match="qualified"):
        q.qualified_profile(identity, profile.model)


def test_valid_looking_caller_profile_is_not_registered():
    profile = registered_profile(q.LOADER_PROFILE)
    caller_supplied = replace(profile, profile_id="unregistered-caller-profile")
    with pytest.raises(ValueError, match="registered"):
        q.require_profile(caller_supplied)


def test_registered_profile_refuses_loader_drift(monkeypatch):
    profile = registered_profile(q.LOADER_PROFILE)
    name, _ = profile.loader_hashes[0]
    changed = ((name, "f" * 64), *profile.loader_hashes[1:])
    monkeypatch.setattr(q, "loader_hashes", lambda: changed)
    with pytest.raises(ValueError, match="qualified"):
        q.require_profile(profile)
    with pytest.raises(ValueError, match="qualified"):
        q.source_budget_limits(profile)


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_sha256", "not-a-proof-digest"),
        ("contract_digest", "f" * 64),
        ("requests", 0),
        ("outcome_checks", ()),
        ("reader_calls", ()),
        ("filesystem_checks", ()),
        ("budget_checks", ()),
        ("budget_limits", (("read_file_lifetime_limit", 21), ("activation_ceiling", 6))),
    ],
)
def test_registered_profile_refuses_proof_drift(monkeypatch, field, value):
    profile = registered_profile(q.LOADER_PROFILE)
    changed = replace(profile, proof=replace(profile.proof, **{field: value}))
    profiles = tuple(changed if candidate == profile else candidate for candidate in q._profiles())
    monkeypatch.setattr(q, "_profiles", lambda: profiles)
    with pytest.raises(ValueError, match="qualified"):
        q.require_profile(changed)


@pytest.mark.parametrize("limits", [(20, 7), (21, 6), (True, 6)])
def test_registered_profile_cannot_expand_measured_budget(monkeypatch, limits):
    profile = registered_profile(q.LOADER_PROFILE)
    changed = replace(
        profile,
        source_budget_limits=(("read_file_lifetime_limit", limits[0]), ("activation_ceiling", limits[1])),
    )
    profiles = tuple(changed if candidate == profile else candidate for candidate in q._profiles())
    monkeypatch.setattr(q, "_profiles", lambda: profiles)
    with pytest.raises(ValueError, match="qualified"):
        q.source_budget_limits(changed)
