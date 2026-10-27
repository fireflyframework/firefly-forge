# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Every recovery authority implementation changes both admitted source contracts."""

from pathlib import Path

import pytest

from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest


@pytest.mark.parametrize(
    "name",
    [
        "invalid_read_recoveries.py",
        "invalid_read_recovery_events.py",
        "invalid_read_recovery_models.py",
        "invalid_read_dispatch_guard.py",
        "controllers.py",
        "journal.py",
        "invalid_read_continuity_controllers.py",
        "execution_request_persistence.py",
    ],
)
def test_recovery_module_bytes_invalidate_runtime_and_admission(name, monkeypatch):
    before = runtime_contract(), admission_contract_digest()
    read = Path.read_bytes

    def altered(path):
        raw = read(path)
        return raw + b"\n# controlled source mutation\n" if path.name == name else raw

    monkeypatch.setattr(Path, "read_bytes", altered)
    after = runtime_contract(), admission_contract_digest()
    assert before[0] != after[0] and before[1] != after[1]
