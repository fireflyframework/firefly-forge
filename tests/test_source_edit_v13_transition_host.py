# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Shared-history V3 needs fresh native authority; prior eight profiles remain provenance."""

from pathlib import Path

import pytest
from test_source_edit_v12_transition_host import HISTORICAL_SEALS

from firefly_forge import source_edit_qualification as q
from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest


def test_v13_requires_fresh_authority_without_rewriting_eight_historical_seals():
    assert q.REGISTRY_VERSION == 12
    assert q.LOADER_PROFILE == "codex-exec-restricted-source/v13"
    profiles = q._profiles()
    assert len(profiles) == 9
    expected = (*HISTORICAL_SEALS, "eb5be5fec427f70580264fc9a391626d5bdaf6c207818271aca71f72e6466cc7")
    for profile, fingerprint in zip(profiles[:8], expected, strict=True):
        assert q.historical_profile_seal(profile)["profile_digest"] == fingerprint
        with pytest.raises(ValueError):
            q.require_profile(profile)


def test_shared_history_decoder_mutation_invalidates_admission_and_retained_graph(monkeypatch):
    admission, graph, loader = admission_contract_digest(), runtime_contract(), q.loader_hashes()
    read = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda p: read(p) + b"\nchanged\n" if p.name == "agentic_checkpoint_boundary.py" else read(p),
    )
    assert admission_contract_digest() != admission
    assert runtime_contract() != graph
    assert q.loader_hashes() == loader
