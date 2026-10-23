# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Only the package-owned measured native boundary profile grants source admission."""

import platform
from dataclasses import replace

import pytest

from firefly_forge import source_edit_qualification as q


def test_package_owns_measured_profile():
    profiles = q._profiles()
    assert len(profiles) == 1
    profile = profiles[0]
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


def test_registered_loader_and_policy_are_the_measured_bytes():
    profile = q._profiles()[0]
    assert len(profile.loader_hashes) == 17
    assert profile.loader_hashes == q.loader_hashes()
    assert profile.loader_profile == q.LOADER_PROFILE
    assert profile.policy_digest == q.policy_digest()


def test_current_runtime_must_match_measured_profile_before_admission():
    profile = q._profiles()[0]
    identity = {"sha256": profile.executable_sha256, "size": profile.executable_size}
    if (profile.system, profile.machine, profile.runtime_versions) == (
        platform.system(),
        platform.machine(),
        q.runtime_versions(),
    ):
        assert q.qualified_profile(identity, profile.model) == profile
    else:
        with pytest.raises(ValueError, match="qualified"):
            q.qualified_profile(identity, profile.model)


def test_profile_refuses_runtime_drift_and_unknown_build_model(monkeypatch):
    profile = q._profiles()[0]
    identity = {"sha256": profile.executable_sha256, "size": profile.executable_size}
    for changed, model in ((identity | {"sha256": "f" * 64}, profile.model), (identity, "other-model")):
        with pytest.raises(ValueError, match="qualified"):
            q.qualified_profile(changed, model)
    monkeypatch.setattr(q, "runtime_versions", lambda: (("python", "unqualified"),))
    with pytest.raises(ValueError, match="qualified"):
        q.qualified_profile(identity, profile.model)


def test_valid_looking_caller_profile_is_not_registered():
    profile = q._profiles()[0]
    caller_supplied = replace(profile, profile_id="unregistered-caller-profile")
    with pytest.raises(ValueError, match="registered"):
        q.require_profile(caller_supplied)
