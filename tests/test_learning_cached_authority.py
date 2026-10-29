# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Cached local evidence has a serialized, current admission without replay."""

import asyncio
import copy
from contextvars import Context
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_learning_ledger import completed

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal, digest
from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager
from firefly_forge.knowledge_first import KnowledgeFirst
from firefly_forge.learning import LearningLedger
from firefly_forge.learning_persistence import ForgeLearningRecordEntity
from firefly_forge.models import RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.search_tools import SearchTools
from firefly_forge.store import identifier
from firefly_forge.tool_broker import ToolInvocationBroker
from firefly_forge.workspace_tools import tool_catalog


@pytest_asyncio.fixture
async def cached_case(store, tmp_path, request):
    engine, origin = await completed(store, tmp_path, scoped=True)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(origin["id"])
    fixture_kind = getattr(request, "param", None)
    if fixture_kind == "near_expiry":
        # A newly inserted, aged fixture retains real Git/WAL references; historical evidence is untouched.
        async with store._unit():
            at = await ledger.records_repository.database_time()
            data = {key: copy.deepcopy(value) for key, value in record.items() if key not in {"id", "state", "kind"}}
            correlation = uuid4().hex
            data["envelope"]["correlation_id"] = correlation
            data["verified_at"] = (at - timedelta(hours=72) + timedelta(seconds=5)).isoformat()
            data["observed_at"] = data["verified_at"]
            data["expires_at"] = (at + timedelta(seconds=5)).isoformat()
            row = ForgeLearningRecordEntity(
                id=uuid4(),
                repository_id=identifier(origin["repository_id"]),
                run_id=identifier(origin["id"]),
                correlation_id=correlation,
                state="verified",
                kind="local_outcome",
                data=data,
            )
            await ledger.records_repository.save(row)
            record = {"id": row.id.hex, "state": "verified", "kind": "local_outcome", **data}
    calls = []

    def response(request):
        calls.append(request.url.path)
        assert request.url.path == "/api/v1/search"
        return httpx.Response(200, json={"elapsed_ms": 1, "hits": []})

    knowledge = KnowledgeManager(store, transport=httpx.MockTransport(response))
    knowledge.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "required": True,
                "auto_memory": False,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "owned",
                "framework_workspace": "frameworks",
                "api_key": "fixture-required-knowledge-key",
            }
        )
    )
    actor = Principal(
        uuid4(),
        "https://fixture.invalid",
        "owned-reader",
        api_roles=frozenset({"forge-admin"}),
        api_audience="owned-factory",
    )
    access = ProjectAccess(store)
    await access.set_membership(origin["project_id"], actor, "developer")
    async with store._unit():
        users = ForgeIdentityUserRepository()
        user = await users.find_by_id(actor.id)
        at = await ledger.records_repository.database_time()
        user.api_roles = ["forge-admin"]
        user.api_roles_audience = "owned-factory"
        user.api_roles_issued_at = at.timestamp()
        user.api_roles_expires_at = at + timedelta(hours=1)
        await users.save(user)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        run = await store.create_run(
            RunInput(
                repository_id=origin["repository_id"],
                project_id=origin["project_id"],
                component_id="stock",
                brief=origin["brief"],
                agent="bridge",
                frontend="none",
                skills=[],
                roles=ProjectRoles(allowed_tools={"implementer": ["knowledge_lookup"]}),
            ),
            prepared_tools=tool_catalog({"knowledge_lookup": True}),
        )
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert run["work_duration"] == origin["work_duration"]
    assert run["sandbox_snapshot"] == origin["sandbox_snapshot"]
    journal = Journal(store)
    broker = ToolInvocationBroker(store, journal, store.path.parent)
    first = KnowledgeFirst(store, journal, ledger, broker, SearchTools(tmp_path / "no-web-key"))
    first.knowledge = knowledge
    await store.acquire_ownership()
    await journal.intent(run["id"], "implement")
    await store.update(run["id"], status="developing", attempt=1)
    workspace = engine.workspaces / origin["id"]
    source = await engine._inspect_workspace(origin["id"], workspace, {})

    async def lookup():
        return await first.search(
            run["id"], "implement", origin["brief"], 5, source=source, workspace=workspace, allow_external=False
        )

    native_invoke = broker.invoke
    original = await lookup()
    if fixture_kind in {"statement", "source_tree", "run_id", "citation_id"}:

        async def malformed_receipt(*args, **kwargs):
            validation = kwargs["validate_receipt"]

            async def malformed_result(result):
                result["results"][0][fixture_kind] = (
                    "Unverified replacement" if fixture_kind == "statement" else "a" * 40
                )
                return await validation(result)

            return await native_invoke(*args, **{**kwargs, "validate_receipt": malformed_result})

        broker.invoke = malformed_receipt
    assert original["knowledge_decision"]["reason"] == "verified_exact_match"
    return ledger, broker, run, record, calls, lookup, original, actor, access


async def retained_history(case):
    _, broker, run, _, calls, _, original, _, _ = case
    assert len(calls) == 2, "Required-service requests cannot be replayed on a cached read"
    rows = await broker.invocations(run["id"])
    assert len(rows) == 1 and rows[0]["status"] == "succeeded" and rows[0]["result"] == original
    entries = await broker.journal.entries(run["id"])
    assert Journal.verify_chain(entries) and sum(e["kind"] == "tool_receipt" for e in entries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["invalidated", "superseded"])
async def test_same_unit_candidate_changes_after_validation_deny_cached_return(cached_case, store, monkeypatch, change):
    ledger, _, _, record, _, lookup, _, _, _ = cached_case
    native_lookup, native_authorize = ledger.lookup, ProjectAccess.authorize_execution
    armed, changed = False, False

    async def validate_then_arm(*args, **kwargs):
        nonlocal armed
        result = await native_lookup(*args, **kwargs)
        armed = result["eligible"]
        return result

    async def authorize_then_change(access, run):
        nonlocal changed
        result = await native_authorize(access, run)
        if armed and not changed:
            changed = True
            if change == "invalidated":
                await ledger.invalidate(
                    record["id"], reason="Owned native review invalidated this procedure", request_id="a" * 32
                )
            else:
                async with store._unit():
                    row = await ledger.records_repository.find_by_id(
                        identifier(record["id"]), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    row.state = "superseded"
                    await ledger.records_repository.save(row)
        return result

    monkeypatch.setattr(ledger, "lookup", validate_then_arm)
    monkeypatch.setattr(ProjectAccess, "authorize_execution", authorize_then_change)
    with pytest.raises(ValueError, match="Retained local evidence"):
        await lookup()
    assert changed
    # This adversarial writer joined the cached-read unit; denial rolls its write back.
    assert (await ledger.records(record["envelope"]["context"]["repository_id"]))[0] == record
    await retained_history(cached_case)


@pytest.mark.asyncio
async def test_invalidation_committed_before_candidate_lock_denies_cached_return(cached_case, monkeypatch):
    ledger, _, _, record, _, lookup, _, _, _ = cached_case
    original = ledger.records_repository.find_by_id
    committed = False

    async def invalidate_before_shared_read(*args, **kwargs):
        nonlocal committed
        if kwargs.get("lock") == LockMode.PESSIMISTIC_READ and not committed:
            await asyncio.create_task(
                ledger.invalidate(record["id"], reason="Independent native review", request_id="b" * 32),
                context=Context(),
            )
            committed = True
        return await original(*args, **kwargs)

    monkeypatch.setattr(ledger.records_repository, "find_by_id", invalidate_before_shared_read)
    with pytest.raises(ValueError, match="Retained local evidence"):
        await lookup()
    assert (
        committed
        and (await ledger.records(record["envelope"]["context"]["repository_id"]))[0]["state"] == "invalidated"
    )
    await retained_history(cached_case)


@pytest.mark.asyncio
async def test_independent_invalidation_waits_for_cached_admission_then_blocks_next_read(
    cached_case, store, monkeypatch
):
    ledger, _, _, record, _, lookup, original, _, _ = cached_case
    native_lookup, native_authorize = ledger.lookup, ProjectAccess.authorize_execution
    armed = False
    started = asyncio.Event()
    writer = None
    backend_pid = None
    observed_lock = False
    monitor = create_async_engine(store.database_url)

    async def independent_review():
        nonlocal backend_pid
        async with store._unit() as session:
            backend_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            await ledger.invalidate(record["id"], reason="Serialized independent review", request_id="c" * 32)

    async def validate_then_arm(*args, **kwargs):
        nonlocal armed
        result = await native_lookup(*args, **kwargs)
        armed = result["eligible"]
        return result

    async def authorize_then_review(access, run):
        nonlocal writer, observed_lock
        result = await native_authorize(access, run)
        if armed and writer is None:
            writer = asyncio.create_task(independent_review(), context=Context())
            await started.wait()
            async with monitor.connect() as connection, asyncio.timeout(3):
                while not observed_lock:
                    observed_lock = await connection.scalar(
                        text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"),
                        {"pid": backend_pid},
                    )
                    await asyncio.sleep(0)
            assert not writer.done(), "Native review must wait for the exact retained candidate's shared lock"
        return result

    monkeypatch.setattr(ledger, "lookup", validate_then_arm)
    monkeypatch.setattr(ProjectAccess, "authorize_execution", authorize_then_review)
    try:
        assert await lookup() == original
        assert observed_lock and writer is not None
        await asyncio.wait_for(writer, 3)
        assert (await ledger.records(record["envelope"]["context"]["repository_id"]))[0]["state"] == "invalidated"
        with pytest.raises(ValueError, match="Retained local evidence"):
            await lookup()
        await retained_history(cached_case)
    finally:
        if writer is not None and not writer.done():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        await monitor.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_case", ["near_expiry"], indirect=True)
async def test_evidence_expiring_during_final_actor_await_is_not_returned(cached_case, store, monkeypatch):
    ledger, _, _, record, _, lookup, _, _, _ = cached_case
    native_lookup, native_authorize = ledger.lookup, ProjectAccess.authorize_execution
    armed = False
    waited = False

    async def validate_then_arm(*args, **kwargs):
        nonlocal armed
        result = await native_lookup(*args, **kwargs)
        armed = result["eligible"]
        return result

    async def authorize_then_expire(access, run):
        nonlocal waited
        result = await native_authorize(access, run)
        if armed and not waited:
            waited = True
            at = await ledger.records_repository.database_time()
            remaining = (datetime.fromisoformat(record["expires_at"]) - at).total_seconds()
            await asyncio.sleep(max(0, remaining) + 0.1)
        return result

    monkeypatch.setattr(ledger, "lookup", validate_then_arm)
    monkeypatch.setattr(ProjectAccess, "authorize_execution", authorize_then_expire)
    with pytest.raises(ValueError, match="Retained local evidence"):
        await lookup()
    assert waited
    await retained_history(cached_case)


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_case", ["statement", "source_tree", "run_id", "citation_id"], indirect=True)
async def test_cached_payload_must_match_the_exact_native_outcome(cached_case):
    _, _, _, _, _, lookup, _, _, _ = cached_case
    with pytest.raises(ValueError, match="Retained local evidence"):
        await lookup()
    await retained_history(cached_case)


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["actor", "administrator"])
async def test_actor_authority_lost_during_candidate_validation_still_denies_cached_read(
    cached_case, store, monkeypatch, loss
):
    ledger, _, _, _, _, lookup, _, actor, _ = cached_case
    original = ledger.lookup
    changed = False

    async def validate_then_revoke(*args, **kwargs):
        nonlocal changed
        result = await original(*args, **kwargs)
        if result["eligible"] and not changed:
            changed = True
            users = ForgeIdentityUserRepository()
            row = await users.find_by_id(actor.id)
            if loss == "actor":
                row.enabled = False
            else:
                row.api_roles = []
            await users.save(row)
        return result

    monkeypatch.setattr(ledger, "lookup", validate_then_revoke)
    with pytest.raises(AccessDenied):
        await lookup()
    assert changed
    await retained_history(cached_case)


@pytest.mark.asyncio
async def test_eligible_cached_evidence_returns_with_current_actor_and_no_replay(cached_case):
    _, _, _, _, _, lookup, original, _, _ = cached_case
    assert await lookup() == original
    await retained_history(cached_case)


@pytest.mark.asyncio
async def test_consumer_policy_changed_during_validation_cannot_admit_old_cached_result(
    cached_case, store, monkeypatch
):
    ledger, _, run, _, _, lookup, _, _, _ = cached_case
    original = ledger.lookup
    changed = False

    async def validate_then_disable(*args, **kwargs):
        nonlocal changed
        result = await original(*args, **kwargs)
        if result["eligible"] and not changed:
            changed = True
            snapshot = copy.deepcopy(run["learning_snapshot"])
            snapshot["settings"]["enabled"] = False
            snapshot["digest"] = digest({key: value for key, value in snapshot.items() if key != "digest"})
            await store.update(run["id"], learning_snapshot=snapshot)
        return result

    monkeypatch.setattr(ledger, "lookup", validate_then_disable)
    with pytest.raises(ValueError, match="Retained local evidence"):
        await lookup()
    assert changed
    await retained_history(cached_case)
