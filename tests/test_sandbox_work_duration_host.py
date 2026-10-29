# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real sandbox intents use selected work caps; command custody and cleanup stay unchanged."""

import pytest
from test_sandbox_command_ordinals_host import command_case as command_case

from firefly_forge.process import ProcessResult
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.work_duration_models import WorkDurationPolicyV2


@pytest.mark.parametrize(
    "policy,requested,expected",
    [
        (WorkDurationPolicyV2(), None, None),
        (WorkDurationPolicyV2(), 864000, 864000),
        (WorkDurationPolicyV2(timeout_seconds=12), None, 12),
        (WorkDurationPolicyV2(timeout_seconds=12), 8, 8),
    ],
)
async def test_new_work_deadline_reaches_backend_and_exact_durable_intent(
    command_case, monkeypatch, policy, requested, expected
):
    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(SandboxInput(work_duration=policy))
    observed = []

    async def execute(config, argv, cwd, timeout, output_limit):
        observed.append(timeout)
        return ProcessResult(argv, 0, "finished")

    monkeypatch.setattr(case.backend, "run", execute)
    result = await case.fresh().execute(case.run_id, case.workspace, case.workspace, ["fixture"], requested, "verify")
    assert result.exit_code == 0 and observed == [expected]
    rows = await case.journal.sandbox_executions(case.run_id)
    assert rows[0]["request"]["request"]["timeout"] == expected
    assert rows[0]["effects"]["cleanup"]["status"] == "done"


async def test_null_cannot_silently_expand_a_legacy_frozen_runner(command_case):
    case = command_case
    before = case.store.run.data["sandbox_snapshot"].copy()
    with pytest.raises(ValueError, match="duration|timeout"):
        await case.fresh().execute(case.run_id, case.workspace, case.workspace, ["fixture"], None, "verify")
    assert case.store.run.data["sandbox_snapshot"] == before
    assert await case.journal.sandbox_executions(case.run_id) == []


async def test_uncapped_plain_docker_requires_independent_owner_watchdog_before_intent(command_case):
    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(
        SandboxInput(
            mode="docker",
            image="fixture/runner@sha256:" + "a" * 64,
            context="controlled-only",
            work_duration=WorkDurationPolicyV2(),
        )
    )
    with pytest.raises(ValueError, match="watchdog"):
        await case.fresh().execute(case.run_id, case.workspace, case.workspace, ["fixture"], None, "verify")
    assert await case.journal.sandbox_executions(case.run_id) == []
    assert case.calls == []


async def test_uncapped_docker_probe_explains_unavailability_without_touching_daemon(command_case):
    case = command_case
    snapshot = capture_sandbox(
        SandboxInput(
            mode="docker",
            image="fixture/runner@sha256:" + "a" * 64,
            context="controlled-only",
            work_duration=WorkDurationPolicyV2(),
        )
    )
    observed = await case.fresh().probe(snapshot)
    assert observed["available"] is False and "watchdog" in observed["reason"]
    assert case.calls == []
