# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Same native request and controlled actual Engine continuation; no PostgreSQL."""

import copy
import json
from types import MethodType

import pytest
from test_bridge_inflight_cancel import RUN
from test_invalid_read_native_protocol_host import admit

from firefly_forge.invalid_read_recovery_models import InvalidReadRecoveryInputV1

pytest_plugins = ["test_invalid_read_native_protocol_host"]


async def failed_preclaim(c):
    ticket, _ = await admit(c)
    await c.continuity._finish(
        ticket["id"], "failed", {"detail": "Continuity stopped; inspect its one-use lease without replay"}
    )

    async def budget(self, key):
        rows = [row for row in self.rows if row.request_id == key]
        return len(rows), sum(len(json.dumps(row.data).encode()) for row in rows)

    c.requests.journal.custody_budget = MethodType(budget, c.requests.journal)
    return await c.continuity.get(RUN, ticket["id"])


def recovery_input(c):
    return InvalidReadRecoveryInputV1(
        version=1,
        candidate_digest=c["digest"],
        expected_admission_seal=c["original_admission_seal"],
        expected_failed_event_digest=c["failed_event_digest"],
        valid_for_seconds=180,
        acknowledge_never_claimed=True,
        acknowledge_same_request_and_attempt=True,
        acknowledge_preserved_consumption_and_budgets=True,
        acknowledge_renewed_same_attempt_lease=True,
    )


@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_never_claimed_same_request_recovers_and_reaches_actual_engine_without_replay(continuity_case, startup):
    c = continuity_case
    before = await failed_preclaim(c)
    if startup:

        async def pending():
            return [await c.journal._task(RUN, "implement")]

        c.journal.tasks.find_pending_recovery = pending
        startup_state = copy.deepcopy(
            (
                c.store.run.data,
                [(r.task_id, r.status, r.attempt, r.data) for r in c.journal.tasks.rows],
                [r.data for r in c.journal.records.rows],
            )
        )
        assert await c.journal.recover() == 0
        assert await c.journal.recover() == 0
        assert (
            c.store.run.data,
            [(r.task_id, r.status, r.attempt, r.data) for r in c.journal.tasks.rows],
            [r.data for r in c.journal.records.rows],
        ) == startup_state
    history = copy.deepcopy([row.data for row in c.journal.records.rows])
    commands = copy.deepcopy([row.data for row in c.journal.sandboxes.rows])
    candidate = await c.continuity.recoveries.candidate(RUN, before["id"])
    assert candidate["admission"] == "READY"
    result = await c.continuity.recoveries.request(RUN, before["id"], "b" * 32, recovery_input(candidate))
    assert result["request_id"] == before["id"] and result["status"] == "queued"
    assert await c.continuity.recoveries.request(RUN, before["id"], "b" * 32, recovery_input(candidate)) == result
    queued = await c.continuity.get(RUN, before["id"])

    def immutable(data):
        return {k: v for k, v in data.items() if k not in {"status", "generation", "owner_pid", "result", "updated_at"}}

    assert immutable(queued) == immutable(before)
    await c.continuity.execute(before["id"])
    after = await c.continuity.get(RUN, before["id"])
    assert after["status"] == "completed", after.get("result")
    assert (await c.continuity.recoveries.status(RUN, before["id"]))["status"] == "completed"
    assert immutable(after) == immutable(before)
    assert len(c.posts) == 7 and len(c.journal.sandboxes.rows) == 13
    assert [row.data for row in c.journal.sandboxes.rows][:11] == commands
    assert [row.data for row in c.journal.records.rows][: len(history)] == history
    assert (
        c.store.run.data["attempt"] == 2
        and c.store.run.data["repair_budget"] == 2
        and len(c.requests.records.rows) == 1
    )
    events = await c.requests.events(before["id"])
    assert [e["kind"] for e in events] == [
        "request_admitted",
        "worker_failed",
        "request_recovered_before_claim",
        "worker_claim",
        "worker_completed",
    ]
    await c.continuity.execute(before["id"])
    assert len(c.posts) == 7 and len(c.journal.sandboxes.rows) == 13


async def test_recovery_refuses_claimed_source_actor_and_later_effects(continuity_case, monkeypatch):
    from firefly_forge.project_access import AccessDenied

    c = continuity_case
    before = await failed_preclaim(c)
    row = c.requests.records.rows[0]
    original = copy.deepcopy(row.data)
    for key, value in (("generation", "c" * 32), ("owner_pid", 123)):
        row.data = {**original, key: value}
        with pytest.raises(ValueError, match="never-claimed"):
            await c.continuity.recoveries.candidate(RUN, before["id"])
        row.data = copy.deepcopy(original)
    path = c.workspace / "existing.py"
    source = path.read_bytes()
    path.write_bytes(source + b"# changed\n")
    with pytest.raises(ValueError, match="source"):
        await c.continuity.recoveries.candidate(RUN, before["id"])
    path.write_bytes(source)
    c.user.enabled = False
    with pytest.raises(AccessDenied):
        await c.continuity.recoveries.candidate(RUN, before["id"])
    c.user.enabled = True
    await c.journal._append(RUN, "implement", "unexpected-read", "unrelated", {"effect": "unknown"})
    with pytest.raises(ValueError, match="annotation"):
        await c.continuity.recoveries.candidate(RUN, before["id"])
    assert len(c.posts) == 1 and len(c.requests.records.rows) == 1


async def test_recovery_witness_binds_original_counters_scope_and_exact_request(continuity_case):
    from test_invalid_read_continuity_host import rechain

    from firefly_forge.invalid_read_recoveries import validate_recovery_record
    from firefly_forge.journal import digest

    c = continuity_case
    before = await failed_preclaim(c)
    candidate = await c.continuity.recoveries.candidate(RUN, before["id"])
    body = recovery_input(candidate)
    await c.continuity.recoveries.request(RUN, before["id"], "b" * 32, body)
    queued = await c.continuity.get(RUN, before["id"])
    entries = await c.journal.entries(RUN)
    events = await c.requests.events(before["id"])
    assert validate_recovery_record(queued, entries, events)["kind"] == "invalid_read_recovery"
    with pytest.raises(ValueError, match="another"):
        await c.continuity.recoveries.request(RUN, before["id"], "c" * 32, body)
    for key in (
        "settings_digest",
        "protected_scope_digest",
        "command_custody_digest",
        "source_digest",
        "checkpoint_usage_digest",
    ):
        changed = copy.deepcopy(entries)
        record = changed[-1]
        record["payload"][key] = "f" * 64
        record["payload"]["digest"] = digest({k: v for k, v in record["payload"].items() if k != "digest"})
        rechain(changed)
        with pytest.raises(ValueError):
            validate_recovery_record(queued, changed, events)
    assert len(c.posts) == 1


async def test_identical_recovery_winner_during_read_fence_returns_exact_receipt(continuity_case, monkeypatch):
    c = continuity_case
    before = await failed_preclaim(c)
    service = c.continuity.recoveries
    candidate = await service.candidate(RUN, before["id"])
    body = recovery_input(candidate)
    prepare = service._prepare
    waiting = True
    winner = []

    async def racing(*args):
        nonlocal waiting
        value = await prepare(*args)
        if waiting:
            waiting = False
            check = value["read_guard"]

            async def changed():
                winner.append(await service.request(RUN, before["id"], "b" * 32, body))
                await check()

            value["read_guard"] = changed
        return value

    monkeypatch.setattr(service, "_prepare", racing)
    reply = await service.request(RUN, before["id"], "b" * 32, body)
    assert reply == winner[0] and reply["status"] == "queued"
    assert len(await c.requests.events(before["id"])) == 3 and len(c.posts) == 1


@pytest.mark.parametrize("change", ["policy", "profile", "source", "cancel", "lease"])
async def test_recovered_worker_refuses_revoked_current_authority_before_claim(continuity_case, monkeypatch, change):
    from firefly_forge import invalid_read_continuities as native
    from firefly_forge import invalid_read_recoveries as recovery
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    c = continuity_case
    before = await failed_preclaim(c)
    candidate = await c.continuity.recoveries.candidate(RUN, before["id"])
    await c.continuity.recoveries.request(RUN, before["id"], "b" * 32, recovery_input(candidate))
    if change == "policy":
        write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False, max_window_seconds=180))
    elif change == "profile":
        monkeypatch.setattr(native, "current_profile_seal", lambda: {"profile_id": "different", "digest": "f" * 64})
    elif change == "source":
        (c.workspace / "existing.py").write_text("VALUE = 99\n")
    elif change == "cancel":
        c.store.run.data["cancel_request"] = {"status": "requested"}
    else:
        clock = recovery.time.time
        monkeypatch.setattr(recovery.time, "time", lambda: clock() + 1000)
    await c.continuity.execute(before["id"])
    observed = await c.continuity.get(RUN, before["id"])
    assert observed["status"] == "failed" and observed["generation"] is None and observed["owner_pid"] is None
    assert len(c.posts) == 1 and [e["kind"] for e in await c.requests.events(before["id"])] == [
        "request_admitted",
        "worker_failed",
        "request_recovered_before_claim",
        "worker_failed",
    ]
