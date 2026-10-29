# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Monitor capability needs a new host policy and fresh explicit tool grants."""

import pytest

from firefly_forge.sandbox_models import SandboxInput, capture_sandbox, validate_sandbox_snapshot
from firefly_forge.workspace_tools import tool_catalog


def test_monitor_is_default_off_without_changing_legacy_snapshot():
    assert SandboxInput().process_monitor is False
    assert "process_monitor" not in capture_sandbox(None)["configuration"]
    original = capture_sandbox(None)
    assert validate_sandbox_snapshot(original).process_monitor is False


def test_explicit_host_policy_admits_five_separate_monitor_capabilities():
    frozen = capture_sandbox(SandboxInput(work_duration={}, process_monitor=True))
    assert frozen["configuration"]["process_monitor"] is True
    assert validate_sandbox_snapshot(frozen).process_monitor is True
    tools = {row["id"]: row for row in tool_catalog({"execute_command": True, "process_monitor": True})}
    for name in ("start_process", "process_status", "process_logs", "wait_process", "cancel_process"):
        assert tools[name]["availability"] == "available"
        assert tools[name]["requires"] == ["execute_command", name]
    default = {row["id"]: row for row in tool_catalog()}
    assert default["start_process"]["availability"] == "not_configured"


def test_monitor_policy_rejects_docker_and_non_boolean_configuration():
    with pytest.raises(ValueError):
        SandboxInput(process_monitor="true")
    with pytest.raises(ValueError, match="host"):
        SandboxInput(mode="docker", context="owned", image="owned/image@sha256:" + "a" * 64, process_monitor=True)


def test_wait_evidence_is_typed_and_does_not_infer_success_from_exit():
    from firefly_forge.runner_monitor_models import MonitorObservation, MonitorWaitEvidence

    observed = MonitorObservation(monitor_id="a" * 32, status="exited", exit_code=3)
    assert observed.exit_code == 3
    evidence = MonitorWaitEvidence(condition={"kind": "exit"}, outcome="satisfied", observation=observed)
    assert evidence.observation.exit_code == 3 and "success" not in evidence.model_dump()
    with pytest.raises(ValueError):
        MonitorObservation(monitor_id="a" * 32, status="exited", exit_code=True)
    with pytest.raises(ValueError):
        MonitorWaitEvidence(condition={"kind": "exit"}, outcome="success", observation=observed)


def test_frozen_controls_distinguish_uncapped_work_from_owner_liveness():
    assert capture_sandbox(None)["enforced_controls"] == ["timeout", "output"]
    uncapped = capture_sandbox(SandboxInput(work_duration={}))
    assert uncapped["enforced_controls"] == ["output"]
    explicit = capture_sandbox(SandboxInput(work_duration={"timeout_seconds": 99999}))
    assert explicit["enforced_controls"] == ["timeout", "output"]
    monitored = capture_sandbox(SandboxInput(work_duration={}, process_monitor=True))
    assert monitored["enforced_controls"] == ["output", "process_monitor_owner_liveness"]
    with pytest.raises(ValueError, match="version-two"):
        SandboxInput(process_monitor=True)
