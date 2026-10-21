# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Observed CLI effects cannot be presented as unused governed-runner capacity."""

import copy
import importlib
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_provider_recovery_service import recovery  # noqa: F401
from test_workspace_dependency_reconciliation import legacy_workspace  # noqa: F401
from test_workspace_reconciliation_journal import environment_journal  # noqa: F401
from test_workspace_reconciliation_service import (  # noqa: F401
    implementation,
    reconciliation_service,
    request,
)

from firefly_forge.activity_models import project_activity
from firefly_forge.sandbox_models import capture_sandbox


def transcript(count=1, thread="fixture-thread"):
    events = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.started"}]
    for number in range(count):
        item = {"id": f"item_{number}", "type": "command_execution", "command": "approved-fixture-command"}
        events.extend(
            [
                {"type": "item.started", "item": {**item, "status": "in_progress", "exit_code": None}},
                {"type": "item.completed", "item": {**item, "status": "completed", "exit_code": 0}},
            ]
        )
    events.append({"type": "turn.completed", "usage": {}})
    return events


def observation(events):
    assert importlib.util.find_spec("firefly_forge.cli_command_budget") is not None
    module = importlib.import_module("firefly_forge.cli_command_budget")
    return module.command_observation(events)


def archive(store, events):
    import json

    rows, payloads = [], {}
    for number, event in enumerate(events, 1):
        event_id = UUID(int=number)
        data, payload = project_activity(store.run.data["id"], event_id.hex, "stdout", json.dumps(event))
        rows.append(SimpleNamespace(id=event_id, data={"id": event_id.hex, "stage": "stdout", **data}))
        payloads[event_id] = payload

    async def provider_events(run_id):
        return rows

    async def payload(run_id, event_id, size, sha256):
        return payloads[event_id]

    store.event_records = SimpleNamespace(provider_budget_events=provider_events)
    store.event_content_records = SimpleNamespace(payload=payload)
    return rows, payloads


def test_command_ids_are_deduplicated_per_thread_without_collapsing_distinct_invocations():
    first = transcript(1)
    first.insert(3, copy.deepcopy(first[2]))
    first.insert(5, copy.deepcopy(first[4]))
    result = observation(first + transcript(1, "different-thread"))
    assert result == {"cli_commands": 2, "cli_invocations": 2}


@pytest.mark.parametrize("change", ["no_start", "no_completion", "conflict", "thread_reused", "not_json_shape"])
def test_ambiguous_or_incomplete_cli_history_never_becomes_zero_spend(change):
    events = transcript()
    if change == "no_start":
        events.pop(2)
    elif change == "no_completion":
        events.pop(3)
    elif change == "conflict":
        duplicate = copy.deepcopy(events[2])
        duplicate["item"]["command"] = "a-different-command"
        events.insert(3, duplicate)
    elif change == "thread_reused":
        events += transcript()
    else:
        events.insert(2, {"type": "unknown-shell-protocol"})
    with pytest.raises(ValueError, match="CLI.*(ambiguous|incomplete|unsupported)"):
        observation(events)


async def test_actual_twenty_one_plus_seven_refuses_candidate_before_any_environment_intent(reconciliation_service):  # noqa: F811
    env = implementation(reconciliation_service)
    archive(env.store, transcript(21))
    env.store.run.data["sandbox_snapshot"] = capture_sandbox({"mode": "host", "command_limit": 35})
    env.store.run.data["commands"] *= 7
    # Keep the existing handoff command cut truthful after this fixture's explicit policy setup.
    from firefly_forge.journal import digest
    from firefly_forge.verification_models import verification_scope

    previous = [copy.deepcopy(row.data) for row in env.journal.records.rows]
    previous[0]["payload"]["commands_digest"] = digest({"commands": env.store.run.data["commands"]})
    previous[0]["payload"]["scope_digest"] = verification_scope(env.store.run.data)
    env.journal.records.rows.clear()
    for entry in previous:
        await env.journal._append(
            env.root.name, entry["task_id"], entry["idempotency_key"], entry["kind"], entry["payload"]
        )

    async def native_checks(run_id):
        return [
            {"execution_id": UUID(int=i).hex, "effects": {"start": {"status": "done", "evidence": {}}}}
            for i in range(1, 8)
        ]

    env.journal.sandbox_executions = native_checks
    before = (env.root / ".git/index").read_bytes()
    with pytest.raises(ValueError, match=r"CLI=21.*Sandbox=7.*final=7.*remaining=7"):
        await env.service.candidate(env.root.name)
    assert (env.root / ".git/index").read_bytes() == before
    assert not any(row.kind.startswith("workspace_reconciliation") for row in env.journal.records.rows)


@pytest.mark.parametrize("boundary", ["dispatch", "launch"])
async def test_reconciled_dispatch_rechecks_archive_before_intent_or_child(recovery, boundary):  # noqa: F811
    env = recovery
    env.memory.run.status = env.memory.run.data["status"] = "developing"
    env.memory.run.data["backend"] = "pyfly"
    env.memory.run.data["sandbox_snapshot"] = capture_sandbox({"mode": "host", "command_limit": 35})
    env.memory.run.data["commands"] = [{"argv": ["approved"], "timeout": 10}]
    env.memory.run.data["workspace_reconciliation"] = {"request_id": "f" * 32}
    archive(env.memory, [])
    if boundary == "launch":
        await env.service.normal_dispatch(env.memory.id.hex, "b" * 32)
    archive(env.memory, transcript(21))
    before = copy.deepcopy([row.data for row in env.memory.rows])
    with pytest.raises(ValueError, match="Unmetered retained CLI"):
        if boundary == "dispatch":
            await env.service.normal_dispatch(env.memory.id.hex, "b" * 32)
        else:
            await env.service.assert_launch(env.memory.id.hex, "b" * 32)
    assert [row.data for row in env.memory.rows] == before


async def test_late_cli_archive_holds_before_environment_intent_or_effect(reconciliation_service):  # noqa: F811
    env = implementation(reconciliation_service)
    archive(env.store, [])
    body = request(await env.service.candidate(env.root.name))
    before = (env.root / ".git/index").read_bytes()

    async def admission(value):
        archive(env.store, transcript())

    env.store.admit_credentials = admission
    with pytest.raises(ValueError, match="Unmetered retained CLI"):
        await env.service.admit(env.root.name, body)
    assert (env.root / ".git/index").read_bytes() == before
    assert not any(row.kind.startswith("workspace_reconciliation") for row in env.journal.records.rows)


async def test_cli_archive_after_effect_holds_queue_and_preserves_unknown_receipt(reconciliation_service):  # noqa: F811
    env = implementation(reconciliation_service)
    archive(env.store, [])
    body = request(await env.service.candidate(env.root.name))

    async def admission(value):
        if isinstance(value, str):
            archive(env.store, transcript())

    env.store.admit_credentials = admission
    with pytest.raises(ValueError, match="Unmetered retained CLI"):
        await env.service.admit(env.root.name, body)
    assert env.store.run.status == "failed" and env.task.attempt == 2
    assert (await env.service.get(env.root.name, body.request_id))["status"] == "needs_reconciliation"
    assert not any(row.kind == "workspace_reconciliation_queued" for row in env.journal.records.rows)


@pytest.mark.parametrize("change", ["descriptor", "payload", "missing_payload", "duplicate_event", "budget"])
async def test_native_archive_refuses_unverifiable_content_before_environment_effect(reconciliation_service, change):  # noqa: F811
    from firefly_forge.activity_models import ActivityUnavailable

    env = implementation(reconciliation_service)
    rows, payloads = archive(env.store, transcript())
    if change == "descriptor":
        rows[0].data.pop("activity_content")
    elif change == "payload":
        payloads[rows[0].id] += b" "
    elif change == "missing_payload":

        async def missing(*args):
            raise ActivityUnavailable("activity_content_missing")

        env.store.event_content_records.payload = missing
    elif change == "duplicate_event":
        rows.append(rows[-1])
    else:
        rows *= 1024
    before = (env.root / ".git/index").read_bytes()
    with pytest.raises(ValueError):
        await env.service.candidate(env.root.name)
    assert (env.root / ".git/index").read_bytes() == before
    assert not any(row.kind.startswith("workspace_reconciliation") for row in env.journal.records.rows)
