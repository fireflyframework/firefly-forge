# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned operation policy, repository queries and current access are material authority."""

from pathlib import Path

import pytest
from test_source_edit_v14_transition_host import declared_names

from firefly_forge import source_edit_qualification as qualification
from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest


@pytest.mark.parametrize(
    "name",
    [
        "tool_operation_models.py",
        "tool_operation_lease.py",
        "tool_operations.py",
        "tool_persistence.py",
        "project_access.py",
    ],
)
def test_owned_tool_authority_changes_revoke_both_current_material_contracts(monkeypatch, name):
    root = Path(qualification.__file__).parent
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "runtime_contract", 21)
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 35)
    assert runtime.count(name) == admission.count(name) == 1
    old_runtime, old_admission = runtime_contract(), admission_contract_digest()
    read = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: read(path) + b"\nchanged-owned-authority\n" if path.name == name else read(path),
    )
    assert runtime_contract() != old_runtime and admission_contract_digest() != old_admission
