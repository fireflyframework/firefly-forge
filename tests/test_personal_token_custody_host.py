# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Credential-format source changes invalidate prospective admission contracts."""

from pathlib import Path

import pytest

from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest


@pytest.mark.parametrize("contract", [runtime_contract, admission_contract_digest])
@pytest.mark.parametrize("change", ["pattern", "stream_overlap"])
def test_credential_format_drift_changes_runtime_and_admission_digest(contract, change, monkeypatch):
    before = contract()
    original = Path.read_bytes

    def changed(path):
        value = original(path)
        if path.name == "personal_token_format.py":
            if change == "pattern":
                return value.replace(b"{43}", b"{42}")
            return value.replace(b"+ 32 + 1 + 43", b"+ 32 + 1 + 42")
        return value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert contract() != before


@pytest.mark.parametrize("contract", [runtime_contract, admission_contract_digest])
@pytest.mark.parametrize(
    "name",
    [
        "personal_token_format.py",
        "personal_token_controllers.py",
        "personal_tokens.py",
        "personal_token_models.py",
        "personal_token_persistence.py",
        "factory_dashboard.py",
        "factory_dashboard_controllers.py",
        "factory_dashboard_models.py",
        "factory_dashboard_persistence.py",
        "identity.py",
        "identity_persistence.py",
        "access_persistence.py",
        "access_routes.py",
        "product_provenance.py",
    ],
)
def test_observation_and_credential_authority_material_changes_digest(contract, name, monkeypatch):
    before = contract()
    original = Path.read_bytes

    def changed(path):
        value = original(path)
        return value + b"\n# controlled material mutation\n" if path.name == name else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert contract() != before
