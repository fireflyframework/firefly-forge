# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""V10 is a new admission closure, never inherited authority from five historical profiles."""

import ast
import hashlib
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as qualification
from firefly_forge.journal import digest
from firefly_forge.source_budget_allowances import admission_contract_digest

HISTORICAL = (
    (4, "codex-exec-restricted-source/v5", "17320f82c0f2f9ab4b65c7e3bd8b89741a3716a8896dd5f674f6409ac1490c3c"),
    (5, "codex-exec-restricted-source/v6", "20e13d34f87314b12fa9a25a0c6411e75e8eb70083a083d743c7fa5487cff76a"),
    (6, "codex-exec-restricted-source/v7", "28f152b48aa9ff37c940ae300fd47f4af5ecc0053860b1980b79fa39a20886b6"),
    (7, "codex-exec-restricted-source/v8", "ab6b1582f5d364e9341e5d25e0f8dd343c3033d0f2ed885e025f7b38ebb64a97"),
    (8, "codex-exec-restricted-source/v9", "4c7d2eba0af54ab48ef33fbe2018e797fcb5cb7adc41a9846722c22dd944f3c8"),
)


def names():
    root = Path(qualification.__file__).parent
    tree = ast.parse((root / "source_budget_allowances.py").read_text())
    fn = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "admission_contract_digest"
    )
    return root, next(
        ast.literal_eval(node) for node in ast.walk(fn) if isinstance(node, ast.Tuple) and len(node.elts) >= 32
    )


def test_v10_profile_remains_exact_historical_provenance_after_v11_transition():
    profiles = qualification._profiles()
    assert len(profiles) in {7, 8}
    profile = profiles[5]
    assert (profile.version, profile.loader_profile) == (9, "codex-exec-restricted-source/v10")
    assert (
        qualification.historical_profile_seal(profile)["profile_digest"]
        == "da8231dc4b6bddb14cde5c619416be63c27ea45b316e02c6a24a84e2fab5777b"
    )
    with pytest.raises(ValueError):
        qualification.require_profile(profile)


def test_five_historical_literals_keep_their_exact_seals_without_current_authority():
    profiles = qualification._profiles()
    for profile, (version, loader, fingerprint) in zip(profiles[:5], HISTORICAL, strict=True):
        assert (profile.version, profile.loader_profile) == (version, loader)
        assert digest(qualification.profile_payload(profile)) == fingerprint
        assert qualification.historical_profile_seal(profile)["profile_digest"] == fingerprint
        with pytest.raises(ValueError):
            qualification.require_profile(profile)


def test_allocator_is_explicitly_the_thirty_third_native_admission_dependency():
    root, declared = names()
    assert len(declared[:33]) == len(set(declared[:33])) == 33
    assert declared[32] == "sandbox_command_slots.py"
    assert admission_contract_digest() == digest(
        {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in declared}
    )


def test_allocator_byte_change_invalidates_the_native_admission_contract(monkeypatch):
    before = admission_contract_digest()
    original = Path.read_bytes

    def changed(path):
        value = original(path)
        return value + b"\n# changed allocator\n" if path.name == "sandbox_command_slots.py" else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert admission_contract_digest() != before


def measured_candidate(monkeypatch):
    from dataclasses import fields, replace

    assert "admission_contract_digest" in {field.name for field in fields(qualification.CapabilityProfile)}
    historical = qualification._profiles()
    previous = historical[4]
    candidate = replace(
        previous,
        version=qualification.REGISTRY_VERSION,
        profile_id="controlled-v10-profile",
        loader_profile=qualification.LOADER_PROFILE,
        loader_hashes=qualification.loader_hashes(),
        policy_digest=qualification.policy_digest(),
        runtime_versions=qualification.runtime_versions(),
        admission_contract_digest=admission_contract_digest(),
    )
    candidate = replace(
        candidate,
        proof=replace(
            candidate.proof, artifact_sha256="a" * 64, contract_digest=qualification.profile_contract_digest(candidate)
        ),
    )
    monkeypatch.setattr(qualification, "_profiles", lambda: (*historical, candidate))
    return candidate


def test_a_qualified_profile_must_refuse_a_later_allocator_module_change(monkeypatch):
    candidate = measured_candidate(monkeypatch)
    qualification.require_profile(candidate)
    original = Path.read_bytes

    def changed(path):
        value = original(path)
        return value + b"\n# changed allocator\n" if path.name == "sandbox_command_slots.py" else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    with pytest.raises(ValueError):
        qualification.require_profile(candidate)


def test_a_profile_without_the_exact_measured_admission_closure_is_refused(monkeypatch):
    from dataclasses import replace

    candidate = measured_candidate(monkeypatch)
    candidate = replace(candidate, admission_contract_digest="0" * 64)
    candidate = replace(
        candidate, proof=replace(candidate.proof, contract_digest=qualification.profile_contract_digest(candidate))
    )
    registered = qualification._profiles()[:5]
    monkeypatch.setattr(qualification, "_profiles", lambda: (*registered, candidate))
    with pytest.raises(ValueError):
        qualification.require_profile(candidate)


def test_explicit_controlled_profile_fixture_binds_the_current_admission_digest(tmp_path, monkeypatch):
    from source_edit_qualification_fixtures import register_fixture_profile

    executable = tmp_path / "controlled-native"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    profile = register_fixture_profile(monkeypatch, executable)
    qualification.require_profile(profile)
    assert profile.admission_contract_digest == admission_contract_digest()


@pytest.mark.parametrize("value", [None, True, 0, {}, [], "", "a" * 63])
def test_a_profile_with_an_invalid_admission_digest_shape_is_refused(monkeypatch, value):
    from dataclasses import replace

    candidate = measured_candidate(monkeypatch)
    candidate = replace(candidate, admission_contract_digest=value)
    candidate = replace(
        candidate, proof=replace(candidate.proof, contract_digest=qualification.profile_contract_digest(candidate))
    )
    historical = qualification._profiles()[:5]
    monkeypatch.setattr(qualification, "_profiles", lambda: (*historical, candidate))
    with pytest.raises(ValueError):
        qualification.require_profile(candidate)
