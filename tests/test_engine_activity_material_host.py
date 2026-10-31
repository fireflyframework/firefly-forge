# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Stage-observation implementation belongs to prospective runtime material."""

from pathlib import Path

import pytest

from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest


@pytest.mark.parametrize("contract", [runtime_contract, admission_contract_digest])
@pytest.mark.parametrize("name", ["engine_activity.py", "engine_activity_persistence.py"])
def test_stage_observation_material_change_invalidates_its_current_contract(contract, name, monkeypatch):
    before = contract()
    original = Path.read_bytes

    def changed(path):
        value = original(path)
        return value + b"\n# controlled observation mutation\n" if path.name == name else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert contract() != before
