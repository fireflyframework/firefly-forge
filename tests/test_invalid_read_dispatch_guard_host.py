# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Generic routes retain same-ticket custody; controlled host memory, never SQL authority."""

import copy
import json
from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace
from uuid import UUID

import pytest
from test_bridge_inflight_cancel import RUN, MemoryStore, Rows
from test_invalid_read_native_protocol_host import admit

pytest_plugins = ["test_invalid_read_native_protocol_host"]


async def attach(c):
    c.store.execution_requests = c.requests
    c.store.run.data["id"] = RUN
    c.store.run.repository_id = UUID(c.store.run.data["repository_id"])

    async def budget(self, key):
        rows = [row for row in self.rows if row.request_id == key]
        return len(rows), sum(len(json.dumps(row.data, ensure_ascii=False).encode()) for row in rows)

    c.requests.journal.custody_budget = MethodType(budget, c.requests.journal)

    async def pending():
        return [row for row in c.journal.tasks.rows if row.status in {"running", "needs_reconciliation"}]

    c.journal.tasks.find_pending_recovery = pending


@pytest.fixture
async def failed_ticket(continuity_case):
    c = continuity_case
    await attach(c)
    ticket, _ = await admit(c)
    await c.continuity._finish(ticket["id"], "failed", {"detail": "Controlled failure before worker claim"})
    assert not c.store.run.data.get("bridge_completion_hold")
    await c.journal.recover()
    return c, ticket


@pytest.mark.parametrize("route", ["retry", "resume", "handoff", "profile", "reconcile_retry", "reconcile_completed"])
async def test_failed_ticket_blocks_generic_dispatch_before_mutation_or_provider_reads(
    failed_ticket, monkeypatch, route
):
    from firefly_forge.controllers import RunService

    c, ticket = failed_ticket
    await c.store.update(RUN, status="failed" if route == "retry" else "needs_reconciliation")

    def state():
        return copy.deepcopy(
            (
                c.store.run.data,
                [(r.task_id, r.status, r.attempt, r.data) for r in c.journal.tasks.rows],
                [r.data for r in c.journal.records.rows],
            )
        )

    before = state()
    accesses = []

    def forbidden(*args, **kwargs):
        accesses.append(True)
        raise AssertionError("Failed continuity must hold before reading a different provider")

    async def route_profile(*args):
        forbidden()

    if route == "handoff":
        monkeypatch.setattr(c.store.configuration, "resolve", forbidden)
    if route == "profile":
        c.store.task_router = SimpleNamespace(resolve=route_profile)
    with pytest.raises(ValueError, match="continuity|same-ticket|recover"):
        if route == "retry":
            await RunService(c.store, c.engine).retry(RUN)
        elif route == "resume":
            await c.journal.resume(RUN)
        elif route == "handoff":
            await c.journal.resume(RUN, agent="claude")
        elif route == "profile":
            await c.journal.resume(RUN, model_profile_id="unrelated")
        else:
            await c.journal.reconcile(
                RUN,
                ticket["task_key"],
                evidence={"source": "controlled observation"},
                outcome=route.removeprefix("reconcile_"),
            )
    assert not accesses and not c.store.created and len(c.posts) == 1
    assert state() == before


@pytest.mark.parametrize("route", ["retry", "resume"])
async def test_marker_without_native_request_service_cannot_fall_back_to_generic_routes(tmp_path, route):
    from firefly_forge.controllers import RunService
    from firefly_forge.journal import Journal

    store = MemoryStore(tmp_path / "fixture")
    store.run.status = "failed" if route == "retry" else "needs_reconciliation"
    store.run.data.update(
        status=store.run.status,
        invalid_read_continuity={
            "request_id": "a" * 32,
            "settlement_digest": "b" * 64,
            "claim_digest": "c" * 64,
            "lease_generation": "d" * 32,
        },
    )

    async def campaign(*args, **kwargs):
        return None

    store.assert_campaign_current = campaign
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=Rows())
    with pytest.raises(ValueError, match="continuity|same-ticket|recover"):
        if route == "retry":
            await RunService(store, None).retry(RUN)
        else:
            await journal.resume(RUN)
    assert not store.created and not journal.records.rows


async def test_completed_ticket_allows_genuine_later_answer_and_resume_without_old_lease_revalidation(
    continuity_case, monkeypatch
):
    from firefly_forge import invalid_read_tickets
    from firefly_forge.agent_roles import WorkerResponse

    c = continuity_case
    await attach(c)
    ticket, _ = await admit(c)
    await c.continuity.execute(ticket["id"])
    assert (await c.continuity.get(RUN, ticket["id"]))["status"] == "completed"
    assert c.store.run.data["status"] == "paused"
    before = copy.deepcopy([row.data for row in c.journal.records.rows])
    monkeypatch.setattr(invalid_read_tickets.time, "time", lambda: ticket["lease"]["expires_at_ms"] / 1000 + 10000)
    await c.journal.worker_response(
        RUN,
        "continued-review",
        WorkerResponse(
            id="answer-current", actor="Fictional operator", answer="Proceed within the existing product scope"
        ),
    )
    resumed = await c.journal.resume(RUN)
    assert resumed["status"] == "queued" and len(c.posts) == 4
    assert (await c.journal._task(RUN, "implement")).attempt == 3
    assert [row.data for row in c.journal.records.rows][: len(before)] == before


async def test_completed_ticket_requires_exact_marker_native_columns_and_complete_claimed_history(
    continuity_case, monkeypatch
):
    from firefly_forge import invalid_read_continuities
    from firefly_forge.execution_requests import digest
    from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed

    c = continuity_case
    await attach(c)
    ticket, _ = await admit(c)
    await c.continuity.execute(ticket["id"])
    assert (await c.continuity.get(RUN, ticket["id"]))["status"] == "completed"

    def unqualified():
        raise AssertionError("Archived completion must not demand a fresh execution profile or lease")

    monkeypatch.setattr(invalid_read_continuities, "current_profile_seal", unqualified)
    await assert_generic_dispatch_allowed(c.store, c.journal, await c.store.get_run(RUN))
    saved = copy.deepcopy((c.store.run.data, c.requests.records.rows, c.requests.journal.rows, c.journal.records.rows))
    for change in (
        "missing_marker",
        "extra_marker",
        "bad_marker",
        "old_marker",
        "missing_service",
        "row_kind",
        "row_run",
        "row_id",
        "row_status",
        "row_key",
        "ticket_seal",
        "admission_event",
        "missing_claim",
        "claim_generation",
        "false_result",
        "later_settlement",
    ):
        c.store.run.data, c.requests.records.rows, c.requests.journal.rows, c.journal.records.rows = copy.deepcopy(
            saved
        )
        c.store.execution_requests = c.requests
        row = c.requests.records.rows[0]
        if change == "missing_marker":
            c.store.run.data.pop("invalid_read_continuity")
        elif change == "extra_marker":
            c.store.run.data["invalid_read_continuity"]["extra"] = True
        elif change == "bad_marker":
            c.store.run.data["invalid_read_continuity"]["request_id"] = "UNKNOWN"
        elif change == "old_marker":
            c.store.run.data["invalid_read_continuity"]["lease_generation"] = "f" * 32
        elif change == "missing_service":
            c.store.execution_requests = None
        elif change == "row_kind":
            row.kind = "publish"
        elif change == "row_run":
            row.run_id = UUID(int=123)
        elif change == "row_id":
            row.data["id"] = "f" * 32
        elif change == "row_status":
            row.status = "failed"
        elif change == "row_key":
            row.request_key = "f" * 32
        elif change == "ticket_seal":
            row.data["request"]["input"]["valid_for_seconds"] = 1
        elif change == "later_settlement":
            await c.journal._append(
                RUN, "implement", "untrusted-later", "invalid_read_settlement", {"request_id": "f" * 32}
            )
        else:
            events = c.requests.journal.rows
            if change == "admission_event":
                events[0].data["evidence"]["admission_seal"] = "f" * 64
            elif change == "missing_claim":
                events.pop(1)
            elif change == "claim_generation":
                row.data["generation"] = None
                events[1].data["evidence"]["generation"] = None
            else:
                row.data["result"] = {"run_status": "failed", "same_task_attempt": 2}
                events[-1].data["evidence"] = row.data["result"]
            previous = "0" * 64
            for index, event in enumerate(events, 1):
                event.sequence = event.data["sequence"] = index
                event.data["previous_hash"] = previous
                event.data["digest"] = previous = digest(
                    {key: value for key, value in event.data.items() if key != "digest"}
                )
        with pytest.raises(ValueError, match="continuity|same-ticket|recover"):
            await assert_generic_dispatch_allowed(c.store, c.journal, await c.store.get_run(RUN))


@pytest.mark.parametrize("count", [0, 1])
async def test_real_store_without_mounted_service_checks_native_reservations(monkeypatch, count):
    from firefly_forge.execution_request_persistence import ForgeExecutionRequestRepository
    from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed
    from firefly_forge.store import Store

    store = object.__new__(Store)
    store.execution_requests = None
    run = {"id": RUN, "status": "failed"}
    seen = []

    @asynccontextmanager
    async def unit():
        yield None

    async def native_count(self, run_id, kind):
        seen.append((run_id, kind))
        return count

    async def get_run(run_id):
        assert run_id == RUN
        return run

    store._unit, store.get_run = unit, get_run
    monkeypatch.setattr(ForgeExecutionRequestRepository, "count_for_run", native_count)
    if count:
        with pytest.raises(ValueError, match="continuity|same-ticket|recover"):
            await assert_generic_dispatch_allowed(store, None, run)
    else:
        await assert_generic_dispatch_allowed(store, None, run)
    assert seen == [(UUID(RUN), "invalid_read")]


async def test_retry_rechecks_current_continuity_after_awaited_delivery_inspection(continuity_case, monkeypatch):
    from firefly_forge.controllers import RunService
    from firefly_forge.models import RunInput

    c = continuity_case
    await attach(c)
    ticket, _ = await admit(c)
    await c.continuity.execute(ticket["id"])
    assert (await c.continuity.get(RUN, ticket["id"]))["status"] == "completed"
    await c.store.update(RUN, status="failed", git_delivery_policy={"enabled": True})
    seen = []

    async def delivery(run):
        seen.append(True)
        c.store.run.data["invalid_read_continuity"] = {**run["invalid_read_continuity"], "request_id": "f" * 32}

    c.engine.git_deliveries = SimpleNamespace(assert_continuable=delivery)

    def clone_input(*args, **kwargs):
        raise AssertionError("Latest continuity custody must be checked before constructing clone input")

    monkeypatch.setattr(RunInput, "model_validate", clone_input)
    with pytest.raises(ValueError, match="continuity|same-ticket|recover"):
        await RunService(c.store, c.engine).retry(RUN)
    assert seen == [True] and not c.store.created and len(c.posts) == 4
