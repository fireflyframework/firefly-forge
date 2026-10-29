# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Only new-run preparation stamps the duration protocol; retained defaults stay untouched."""

import copy
from types import SimpleNamespace

import pytest

from firefly_forge.runner_configuration import RunnerConfiguration
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.work_duration_models import WorkDurationPolicyV2


@pytest.mark.parametrize(
    "selected", [None, SandboxInput(), SandboxInput(work_duration=WorkDurationPolicyV2(timeout_seconds=864000))]
)
async def test_new_run_freezes_default_unlimited_or_explicit_chosen_work_policy(tmp_path, monkeypatch, selected):
    saved = {"sandbox": SandboxInput().model_dump(mode="json"), "revision": "old-default-revision"}
    old = copy.deepcopy(saved)
    search = SimpleNamespace(credential_registry=lambda: {})
    service = RunnerConfiguration(
        SimpleNamespace(path=tmp_path / "state"), SimpleNamespace(), search, SimpleNamespace()
    )

    async def configuration(repository_id):
        return copy.deepcopy(saved)

    monkeypatch.setattr(service, "configuration", configuration)
    snapshot = await service.capture_run("fixture", selected)
    expected = (
        selected.work_duration
        if selected is not None and selected.work_duration is not None
        else WorkDurationPolicyV2()
    )
    assert snapshot["work_duration"] == expected.model_dump(mode="json")
    assert snapshot["sandbox_snapshot"]["configuration"]["work_duration"] == snapshot["work_duration"]
    assert saved == old
    assert snapshot["sandbox_snapshot"]["configuration"]["timeout"] == 300
    assert "work_duration" not in capture_sandbox(None)["configuration"]


async def test_uncapped_plain_docker_is_readable_as_unavailable_but_not_admitted(tmp_path, monkeypatch):
    config = SandboxInput(
        mode="docker",
        image="fixture/runner@sha256:" + "a" * 64,
        context="controlled-only",
        work_duration=WorkDurationPolicyV2(),
    )
    saved = {"sandbox": config.model_dump(mode="json"), "revision": "saved-only"}
    store = SimpleNamespace(path=tmp_path / "state", runtime_roles=SimpleNamespace(role="api"))
    search = SimpleNamespace(credential_registry=lambda: {}, configuration=lambda: {})
    service = RunnerConfiguration(store, SimpleNamespace(), search, SimpleNamespace())

    async def configuration(repository_id):
        return copy.deepcopy(saved)

    async def tools(*args):
        return []

    monkeypatch.setattr(service, "configuration", configuration)
    monkeypatch.setattr(service, "capabilities", tools)
    with pytest.raises(ValueError, match="watchdog") as refused:
        await service.capture_run("fixture")
    readiness = await service.readiness("fixture")
    assert readiness["execution"]["available"] is False
    assert readiness["execution"]["reason"] == str(refused.value)
    assert saved["revision"] == "saved-only"
