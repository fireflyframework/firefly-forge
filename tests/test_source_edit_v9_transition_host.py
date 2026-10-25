# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The protected reap successor requires fresh proof and preserves exact v8 provenance."""

from dataclasses import replace

import pytest

from firefly_forge import source_edit_qualification as qualification

V8_SEAL = {
    "registry_version": 7,
    "profile_id": "codex-0163-darwin-arm64-source-v8",
    "profile_digest": "ab6b1582f5d364e9341e5d25e0f8dd343c3033d0f2ed885e025f7b38ebb64a97",
    "catalog_digest": "c3de69cb83da83e6d8cbf8f5e5797c4ecac5ae654e5cd70e66b6648c08786114",
    "proof_sha256": "3adb21fdcbb1e9f8ae08a1471e1f612a78828d895c523b1d203f2ad995be1460",
    "loader_profile": "codex-exec-restricted-source/v8",
}


def previous_profile():
    return next(profile for profile in qualification._profiles() if profile.loader_profile == V8_SEAL["loader_profile"])


def test_protected_cleanup_has_a_distinct_current_profile():
    assert qualification.REGISTRY_VERSION == 12
    assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v13"


def test_registered_v8_remains_exact_historical_provenance_only():
    profile = previous_profile()
    assert qualification.historical_profile_seal(profile) == V8_SEAL
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(profile)
    with pytest.raises(ValueError, match="qualified"):
        qualification.source_budget_limits(profile)


@pytest.mark.parametrize("field,value", [("profile_id", "caller-copy"), ("version", 8)])
def test_caller_v8_copy_cannot_become_historical_provenance(field, value):
    changed = replace(previous_profile(), **{field: value})
    with pytest.raises(ValueError, match="registered provenance"):
        qualification.historical_profile_seal(changed)


def test_registered_v8_with_changed_contract_is_not_historical_proof(monkeypatch):
    original = previous_profile()
    changed = replace(original, proof=replace(original.proof, contract_digest="f" * 64))
    monkeypatch.setattr(qualification, "_profiles", lambda: (changed,))
    with pytest.raises(ValueError, match="registered provenance"):
        qualification.historical_profile_seal(changed)


def test_current_looking_unmeasured_profile_never_grants_authority():
    caller = replace(
        previous_profile(),
        version=8,
        loader_profile="codex-exec-restricted-source/v9",
        profile_id="unmeasured-cleanup-candidate",
    )
    with pytest.raises(ValueError, match="registered"):
        qualification.require_profile(caller)
