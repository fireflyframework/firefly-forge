# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Worker evidence schema changes require new measured runtime authority."""

from pathlib import Path

from test_source_edit_v14_transition_host import declared_names

from firefly_forge import source_edit_qualification as q


def test_v20_binds_evidence_query_and_native_reader_before_admission():
    assert q.REGISTRY_VERSION > 18
    assert q.LOADER_PROFILE != "codex-exec-restricted-source/v19"
    root = Path(q.__file__).parent
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 80)
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "runtime_contract", 55)
    assert admission[80:82] == runtime[55:57] == ("worker_evidence_query.py", "run_evidence.py")
    assert len(admission) == len(set(admission)) >= 82
    assert len(runtime) == len(set(runtime)) >= 57
    historical = q._profiles()[:15]
    assert len(historical) == 15
    assert historical[-1].loader_profile == "codex-exec-restricted-source/v19"
    assert (
        historical[-1].admission_contract_digest == "0e71f33c49f9d9c045f8846025644feee434c55127d41406f36d54decbb61407"
    )


def test_v20_preserves_all_fifteen_historical_profile_seals():
    for profile in q._profiles()[:15]:
        seal = q.historical_profile_seal(profile)
        assert seal["proof_sha256"] == profile.proof.artifact_sha256


def test_checkpoint_epoch_successor_requires_new_measurement_and_preserves_v20():
    assert q.REGISTRY_VERSION >= 20
    assert q.LOADER_PROFILE != "codex-exec-restricted-source/v20"
    historical = q._profiles()[:16]
    assert len(historical) == 16
    assert historical[-1].loader_profile == "codex-exec-restricted-source/v20"
    for profile in historical:
        seal = q.historical_profile_seal(profile)
        assert seal["proof_sha256"] == profile.proof.artifact_sha256
