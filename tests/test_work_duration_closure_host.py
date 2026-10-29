# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Duration, ownership and dispatch authority are sealed material inputs."""

from pathlib import Path

import pytest
from test_source_edit_v14_transition_host import declared_names

from firefly_forge import source_edit_qualification as qualification
from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest

MATERIAL = (
    "store.py",
    "work_duration_models.py",
    "models.py",
    "process.py",
    "assurance.py",
    "runner_configuration.py",
    "runner_pool_owner.py",
    "runner_pool_launcher.py",
    "runner_pool_dispatch.py",
    "runner_pool_execution.py",
    "runner_pool_artifacts.py",
    "runner_controller_client.py",
    "runner_controller.py",
    "runner_pools.py",
    "runner_pool_persistence.py",
    "runner_factory_authority.py",
    "runner_pool_models.py",
    "runner_factory_models.py",
    "runner_run_liveness.py",
    "runner_run_authority.py",
    "runner_run_owner.py",
)


@pytest.mark.parametrize("name", MATERIAL)
def test_each_duration_authority_input_revokes_runtime_and_admission(monkeypatch, name):
    root = Path(qualification.__file__).parent
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "runtime_contract", 21)
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 35)
    assert runtime.count(name) == admission.count(name) == 1
    before_runtime, before_admission = runtime_contract(), admission_contract_digest()
    read = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda path: read(path) + b"\nchanged-authority\n" if path.name == name else read(path)
    )
    assert runtime_contract() != before_runtime
    assert admission_contract_digest() != before_admission
