# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Historical native seals remain provenance; a changed loader needs a fresh qualified profile."""

from dataclasses import replace

import pytest

from firefly_forge import source_edit_qualification as module
from firefly_forge.journal import digest
from firefly_forge.source_edit_supported_profiles import PROFILES


def test_historical_profile_payload_preserves_exact_original_seal_bytes():
    old = PROFILES[0]
    assert module.historical_profile_seal(old) == {
        "registry_version": 4,
        "profile_id": old.profile_id,
        "profile_digest": "17320f82c0f2f9ab4b65c7e3bd8b89741a3716a8896dd5f674f6409ac1490c3c",
        "catalog_digest": digest({"catalog": old.catalog}),
        "proof_sha256": old.proof.artifact_sha256,
        "loader_profile": old.loader_profile,
    }


def test_new_native_profile_measures_budget_modules_and_keeps_old_literal_historical():
    assert module.LOADER_PROFILE == "codex-exec-restricted-source/v15"
    assert {"source_budget_allowances.py", "source_budget_output.py", "source_tool_budget.py", "tool_broker.py"} <= set(
        module.LOADER_MODULES
    )
    assert len(module.LOADER_MODULES) == 22
    assert PROFILES[0].loader_profile == "codex-exec-restricted-source/v5"
    with pytest.raises(ValueError):
        module.require_profile(PROFILES[0])


def test_an_unqualified_profile_cannot_grant_the_requested_budget():
    old = PROFILES[0]
    with pytest.raises(ValueError):
        module.source_budget_limits(old)


def test_declared_budget_cannot_exceed_its_numeric_native_proof(tmp_path, monkeypatch):
    from source_edit_qualification_fixtures import register_fixture_profile

    executable = tmp_path / "fixture-native"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    profile = register_fixture_profile(monkeypatch, executable)
    changed = replace(profile, source_budget_limits=(("read_file_lifetime_limit", 30), ("activation_ceiling", 32)))
    changed = replace(changed, proof=replace(changed.proof, contract_digest=module.profile_contract_digest(changed)))
    monkeypatch.setattr(module, "_profiles", lambda: (changed,))
    with pytest.raises(ValueError, match="proof|qualification|budget"):
        module.source_budget_limits(changed)
