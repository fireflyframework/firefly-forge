# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual native verification/effect writers with controlled memory rows, no SQL."""

import copy
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from test_sandbox_command_ordinals_host import command_case as command_case

from firefly_forge.journal import digest
from firefly_forge.project_access import ProjectAccess
from firefly_forge.sandbox_command_slots import _canonical
from firefly_forge.verification_models import verification_scope


@pytest.fixture(params=["red", "green"])
async def completed(command_case, monkeypatch, request):
    c = command_case
    known_red = request.param == "red"
    c.store.configuration = SimpleNamespace(load_snapshot=lambda _: {})

    async def authorized(*args, **kwargs):
        return True

    monkeypatch.setattr(ProjectAccess, "authorize_execution", authorized)
    commands = [{"argv": ["controlled-check", str(i)], "timeout": 2} for i in range(7)]
    pending = {
        "id": "controlled-verification",
        "kind": "verification",
        "status": "pending",
        "task_id": "implement",
        "attempt": 1,
    }
    c.store.run.status = "paused"
    c.store.run.data.update(
        id=c.run_id,
        status="paused",
        agent="bridge",
        commands=commands,
        config_revision="2" * 32,
        implementation_rounds=1,
        attempt=1,
        worker_request=pending,
    )
    task = c.store.tasks.rows[0]
    task.task_id, task.status = "implement", "paused"
    intent = {
        "version": 1,
        "id": uuid4().hex,
        "request_id": pending["id"],
        "request_digest": digest(pending),
        "source_digest": "a" * 64,
        "commands_digest": digest({"commands": commands}),
        "policy_digest": "b" * 64,
        "provider_digest": "c" * 64,
        "scope_digest": verification_scope(c.store.run.data),
        "continuation": 1,
        "max_continuations": 4,
        "commands_count": 7,
        "parent_attempt": 1,
        "parent_round": 1,
        "ordinals": list(range(1, 8)),
        "timeouts": [2] * 7,
        "sandbox_digest": c.store.run.data["sandbox_snapshot"]["digest"],
        "workspace": "/controlled/workspace",
        "cwd": ".",
        "agent": "bridge",
    }
    await c.journal.verification_begin(c.run_id, intent, check_current=lambda _: None)
    for i, command in enumerate(commands):
        ordinal = i + 1
        identity = uuid5(NAMESPACE_URL, f"forge:sandbox:{c.run_id}:implement:1:{ordinal}").hex
        request = {
            "argv": command["argv"],
            "workspace": intent["workspace"],
            "cwd": ".",
            "timeout": 2,
        }
        payload = {
            "parent_attempt": 1,
            "ordinal": ordinal,
            "container_name": "forge-sandbox-" + identity,
            "snapshot_digest": intent["sandbox_digest"],
            "request_digest": digest(request),
            "request": request,
        }
        for effect, evidence in [
            ("create", {"created": True}),
            ("start", {"exit_code": int(known_red and i == 0), "output": "Controlled known result"}),
            ("cleanup", {"removed": True}),
        ]:
            await c.journal.sandbox_effect_intent(c.run_id, "implement", identity, effect=effect, payload=payload)
            await c.journal.sandbox_effect_receipt(c.run_id, "implement", identity, effect=effect, evidence=evidence)
        await c.journal.verification_command(c.run_id, intent, i, check_current=lambda _: None)
    await c.journal.verification_finish(c.run_id, intent, check_current=lambda _: None)
    await c.journal._append(
        c.run_id,
        "handoff",
        uuid4().hex,
        "handoff",
        {
            "automatic": True,
            "verification_handoff_id": intent["id"],
            "agent": "bridge",
            "config_revision": c.store.run.data["config_revision"],
        },
    )
    key = uuid5(NAMESPACE_URL, f"forge:{c.run_id}:implement:2").hex
    await c.journal._append(
        c.run_id,
        "implement",
        key,
        "intent",
        {"config_revision": c.store.run.data["config_revision"]},
    )
    await c.journal._append(
        c.run_id,
        "implement",
        intent["id"] + ":coding",
        "verification_continuation_intent",
        {
            "version": 1,
            "id": intent["id"],
            "request_id": intent["request_id"],
            "parent_round": 1,
            "next_round": 2,
            "provider_digest": intent["provider_digest"],
        },
    )
    run = await c.store.get_run(c.run_id)
    run.update(
        status="failed",
        error="native_agentic_checkpoint_wal_required",
        attempt=2,
        implementation_pending=True,
    )
    entries = [copy.deepcopy(row.data) for row in c.store.records.rows]
    rows = copy.deepcopy(c.store.sandboxes.rows)
    values = {row.id.hex: _canonical(row, entries, c.run_id) for row in rows}
    return SimpleNamespace(run=run, entries=entries, rows=rows, values=values, intent=intent)


def rehash(entries):
    previous = "0" * 64
    for i, e in enumerate(entries, 1):
        e.update(sequence=i, previous_hash=previous)
        e["digest"] = digest({k: v for k, v in e.items() if k != "digest"})
        previous = e["digest"]


def validate(c):
    from firefly_forge.checkpoint_mode_handoff_custody import validate_completed_handoff

    return validate_completed_handoff(c.run, c.entries, c.rows, c.values)


async def test_completed_known_red_requires_all_native_command_and_cleanup_receipts(
    completed,
):
    c = completed
    before = copy.deepcopy((c.run, c.entries, c.values))
    result = validate(c)
    assert result["outcome_digest"] == next(
        e["digest"] for e in c.entries if e["kind"] == "verification_handoff_outcome"
    )
    assert all(isinstance(value, str) and len(value) == 64 for value in result.values())
    assert "Controlled known result" not in str(result)
    assert (c.run, c.entries, c.values) == before


@pytest.mark.parametrize(
    "change",
    [
        "only_projection",
        "outcome",
        "response",
        "response_actor",
        "resume",
        "coding",
        "implement_intent",
        "command",
        "command_exit",
        "command_index",
        "duplicate_command",
        "create_receipt",
        "start_receipt",
        "cleanup_receipt",
        "cleanup_intent",
        "wrong_argv",
        "wrong_timeout",
        "wrong_cwd",
        "wrong_snapshot",
        "row_column",
        "value_drift",
        "unknown_start",
        "interrupted",
        "chain",
        "cross_run",
        "failed_projection",
    ],
)
async def test_rehashed_missing_or_changed_native_custody_refuses(completed, change):
    c = completed
    if change == "only_projection":
        c.entries = c.entries[-2:]
    elif change in {
        "outcome",
        "response",
        "resume",
        "coding",
        "implement_intent",
        "command",
    }:
        kind = {
            "outcome": "verification_handoff_outcome",
            "response": "worker_response",
            "resume": "handoff",
            "coding": "verification_continuation_intent",
            "implement_intent": "intent",
            "command": "verification_handoff_command",
        }[change]
        c.entries.remove(next(e for e in c.entries if e["kind"] == kind))
    elif change in {
        "create_receipt",
        "start_receipt",
        "cleanup_receipt",
        "cleanup_intent",
    }:
        effect, kind = change.split("_")
        c.entries.remove(
            next(e for e in c.entries if e["kind"] == "sandbox_" + kind and e["payload"]["effect"] == effect)
        )
    elif change == "response_actor":
        next(e for e in c.entries if e["kind"] == "worker_response")["payload"]["responding_actor"]["handoff_id"] = (
            "f" * 32
        )
    elif change in {"command_exit", "command_index"}:
        part = "exit_code" if change == "command_exit" else "command_index"
        next(e for e in c.entries if e["kind"] == "verification_handoff_command")["payload"][part] += 1
    elif change == "duplicate_command":
        e = copy.deepcopy(next(e for e in c.entries if e["kind"] == "verification_handoff_command"))
        c.entries.insert(2, e)
    elif change in {"wrong_argv", "wrong_timeout", "wrong_cwd", "wrong_snapshot"}:
        field = {
            "wrong_argv": "argv",
            "wrong_timeout": "timeout",
            "wrong_cwd": "cwd",
            "wrong_snapshot": "snapshot_digest",
        }[change]
        target = c.rows[0].data["request"] if change == "wrong_snapshot" else c.rows[0].data["request"]["request"]
        target[field] = ["other"] if change == "wrong_argv" else 1 if change == "wrong_timeout" else "other"
    elif change == "row_column":
        c.rows[0].ordinal = 8
    elif change == "value_drift":
        c.values[c.rows[0].id.hex]["effects"]["start"]["evidence"]["exit_code"] = 7
    elif change == "unknown_start":
        c.rows[0].data["effects"]["start"] = {"status": "intent"}
    elif change == "interrupted":
        c.rows[0].data["effects"]["cleanup"]["evidence"]["outcome"] = "interrupted"
    elif change == "cross_run":
        c.entries[0]["run_id"] = UUID(int=99).hex
    elif change == "failed_projection":
        c.run["verification_handoff"]["failed_commands"] = 1 - c.run["verification_handoff"]["failed_commands"]
        next(e for e in c.entries if e["kind"] == "verification_handoff_outcome")["payload"] = copy.deepcopy(
            c.run["verification_handoff"]
        )
    if change in {"wrong_argv", "wrong_timeout", "wrong_cwd", "wrong_snapshot"}:
        row = c.rows[0]
        row.data["request"]["request_digest"] = digest(row.data["request"]["request"])
        if change == "wrong_snapshot":
            row.snapshot_digest = row.data["request"]["snapshot_digest"]
        for entry in c.entries:
            if entry["kind"] == "sandbox_intent" and entry["payload"].get("execution_id") == row.id.hex:
                entry["payload"]["request"] = copy.deepcopy(row.data["request"])
    if change == "unknown_start":
        c.entries.remove(
            next(
                e
                for e in c.entries
                if e["kind"] == "sandbox_receipt"
                and e["payload"].get("effect") == "start"
                and e["payload"].get("execution_id") == c.rows[0].id.hex
            )
        )
    if change == "interrupted":
        c.rows[0].status = "interrupted"
        next(
            e
            for e in c.entries
            if e["kind"] == "sandbox_receipt"
            and e["payload"].get("effect") == "cleanup"
            and e["payload"].get("execution_id") == c.rows[0].id.hex
        )["payload"]["evidence"]["outcome"] = "interrupted"
    if change == "chain":
        c.entries[-1]["digest"] = "f" * 64
    else:
        rehash(c.entries)
    if change in {"wrong_argv", "wrong_timeout", "wrong_cwd", "wrong_snapshot", "unknown_start", "interrupted"}:
        c.values = {row.id.hex: _canonical(row, c.entries, c.run["id"]) for row in c.rows}
    with pytest.raises(ValueError):
        validate(c)
