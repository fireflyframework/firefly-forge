# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Kubernetes and object publication delegates remain current authority inputs."""

from pathlib import Path

import pytest
from test_source_edit_v14_transition_host import declared_names

from firefly_forge import source_edit_qualification as qualification
from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest


@pytest.mark.parametrize(
    "name",
    [
        "runner_kubernetes.py",
        "runner_kubernetes_models.py",
        "runner_kubernetes_exec.py",
        "runner_kubernetes_activation.py",
        "runner_dispatch.py",
        "runner_kubernetes_qualification.py",
        "runner_kubernetes_dispatch.py",
        "runner_pool_bundle.py",
        "runner_bundle_models.py",
        "runner_git_snapshot.py",
        "runner_controller_config.py",
        "workspace_tools.py",
        "credential_text_stream.py",
        "runner_controller_runtime.py",
        "runner_pool_worker.py",
        "object_storage.py",
        "runner_artifact_publications.py",
        "runner_object_artifacts.py",
    ],
)
def test_control_and_object_authority_drift_revokes_runtime_and_admission(monkeypatch, name):
    root = Path(qualification.__file__).parent
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "runtime_contract", 21)
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 35)
    assert runtime.count(name) == admission.count(name) == 1
    before = runtime_contract(), admission_contract_digest()
    read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda p: read(p) + b"\nchanged-authority\n" if p.name == name else read(p))
    assert runtime_contract() != before[0]
    assert admission_contract_digest() != before[1]
