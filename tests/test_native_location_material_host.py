# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline location authority and physical dispatch are explicit sealed inputs."""

from pathlib import Path

import pytest
from test_source_edit_v14_transition_host import declared_names

from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest

MATERIAL = (
    "native_location_cli.py",
    "native_location_files.py",
    "native_location_maintenance.py",
    "native_location_models.py",
    "native_location_projection.py",
    "native_location_proof.py",
    "native_location_runtime.py",
    "runtime_roles.py",
    "runtime_role_persistence.py",
    "agent_roles.py",
    "policies.py",
)


@pytest.mark.parametrize("name", MATERIAL)
def test_location_authority_changes_revoke_both_material_seals(monkeypatch, name):
    import firefly_forge.agentic_checkpoint_invocation as module

    root = Path(module.__file__).parent
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "runtime_contract", 21)
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 35)
    assert runtime.count(name) == admission.count(name) == 1
    before = runtime_contract(), admission_contract_digest()
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda p: original(p) + b"\nchanged\n" if p.name == name else original(p))
    assert runtime_contract() != before[0]
    assert admission_contract_digest() != before[1]
