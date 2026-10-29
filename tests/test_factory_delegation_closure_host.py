# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Team approval, admission and source custody are current material authority."""

from pathlib import Path

import pytest
from test_source_edit_v14_transition_host import declared_names

from firefly_forge import source_edit_qualification as qualification
from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest


@pytest.mark.parametrize(
    "name",
    [
        "project_models.py",
        "project_snapshot.py",
        "delegation_models.py",
        "delegation_budget.py",
        "delegation_contract.py",
        "delegation_sources.py",
        "delegation_ledger.py",
        "factory_delegations.py",
        "delegation_lifecycle.py",
        "delegation_runtime.py",
        "delegation_waits.py",
        "delegation_agent_budget.py",
    ],
)
def test_every_team_authority_module_is_sealed_once_and_changes_both_material_contracts(monkeypatch, name):
    root = Path(qualification.__file__).parent
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "runtime_contract", 21)
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 35)
    assert runtime.count(name) == admission.count(name) == 1
    before_runtime, before_admission = runtime_contract(), admission_contract_digest()
    read = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda path: read(path) + b"\nchanged-team-authority\n" if path.name == name else read(path)
    )
    assert runtime_contract() != before_runtime
    assert admission_contract_digest() != before_admission
