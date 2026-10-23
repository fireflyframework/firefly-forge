# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual SQL/tool/Engine custody with an owned CLI/MCP fixture, never a real model or account."""

import asyncio
import copy
import hashlib
import json
import os
from contextlib import asynccontextmanager
from contextvars import Context
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import asyncpg
import pytest
from pyfly.data.transaction import current_unit_of_work
from pyfly.data.transaction.errors import CommitOutcomeUnknownError
from pyfly.data.transaction.synchronization import TransactionSynchronizationAdapter, register_synchronization
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from test_source_budget_service_host import allowance_request
from test_source_completed_output_native import completed, snapshot
from test_source_completed_output_service_host import request as completed_request
from test_source_edit_native import started
from worker_reply_fixtures import authenticated_operator

from firefly_forge import source_budget_allowances as module
from firefly_forge.agent_roles import WorkerResponse
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import AccessDenied
from firefly_forge.source_edit_continuations import SourceEditInvocation
from firefly_forge.source_edit_models import SourceReadRequest
from firefly_forge.source_human_readmission import SourceHumanReadmissions
from firefly_forge.source_result_readmission import SourceCompletedOutputReadmissions
from firefly_forge.store import Store
from firefly_forge.tool_broker import ToolReconciliationRequired


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture(autouse=True)
def measured_witness_directory(tmp_path, monkeypatch):
    if "NATIVE_SQL_FIXTURE" not in os.environ:
        root = tmp_path / "native-sql-witnesses"
        (root / "logs").mkdir(parents=True)
        monkeypatch.setenv("NATIVE_SQL_FIXTURE", str(root))


def measured_witness(kind, values):
    case = os.environ["PYTEST_CURRENT_TEST"].removesuffix(" (call)")
    assert case.startswith("tests/test_source_budget_native.py::")
    path = Path(os.environ["NATIVE_SQL_FIXTURE"]) / "logs/source-budget-witnesses.jsonl"
    assert path.parent.is_dir() and not path.is_symlink()
    value = {
        "version": 1,
        "case": case,
        "kind": kind,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cli_fixture_sha256": hashlib.sha256(
            Path(__file__).with_name("source_budget_cli_fixture.py").read_bytes()
        ).hexdigest(),
        "observed": values,
    }
    with path.open("a") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")


def budget_cli(monkeypatch):
    import test_source_completed_output_native
    import test_source_edit_native

    register = test_source_edit_native.register_fixture_profile
    program = Path(__file__).with_name("source_budget_cli_fixture.py").read_text()

    def registered(patch, executable):
        prefix, _ = executable.read_text().split("else:\n", 1)
        executable.write_text(prefix + "else:\n" + "\n".join(" " + line for line in program.splitlines()) + "\n")
        return register(patch, executable)

    monkeypatch.setattr(test_source_edit_native, "register_fixture_profile", registered)
    monkeypatch.setattr(test_source_completed_output_native, "source_cli_transport", lambda patch: None)


async def answered_thirteen(store, tmp_path, monkeypatch):
    budget_cli(monkeypatch)
    env, parent, catalog = await completed(store, tmp_path, monkeypatch)
    source = SourceCompletedOutputReadmissions(env.service)
    review = await source.candidate(env.run_id, parent["request"]["request_id"], str(catalog))
    parent = await source.admit(env.run_id, parent["request"]["request_id"], completed_request(review, str(catalog)))
    env.engine.configured_provider = True
    await env.engine.execute_run(env.run_id)
    current = await store.get_run(env.run_id)
    assert current["status"] == "paused", current.get("error")
    assert current["implementation_rounds"] == 4 and current["worker_request"]["attempt"] == 4
    assert current["worker_request"]["id"] == "native-read-budget-question"
    assert current["provider_recovery"]["status"] == "consumed"
    key = uuid5(NAMESPACE_URL, f"forge:{env.run_id}:implement:4").hex
    assert current["provider_recovery"]["task_key"] == key and current["provider_recovery"]["attempt"] == 4
    async with store._unit():
        task = env.service.recoveries._task_view(await env.journal._task(env.run_id, "implement"))
        entries = await env.service.recoveries._entries(env.run_id)
        dispatches = [e for e in entries if e["kind"] == "provider_dispatch_intent"]
        exact = [e for e in dispatches if e["idempotency_key"] == key + ":provider"]
        assert len(exact) == 1 and dispatches[-1]["digest"] == exact[0]["digest"]
        dispatch = exact[0]
        assert dispatch["payload"]["task_key"] == key and dispatch["payload"]["attempt"] == 4
        assert dispatch["payload"]["recovery_id"] == current["provider_recovery"]["request_id"]
        assert task["status"] == "paused" and task["known_worker_response"] is True
        assert task["worker_feedback"] == current["worker_feedback"]
        feedbacks = [
            e
            for e in entries
            if e["kind"] == "worker_feedback"
            and (e["payload"].get("request") or {}).get("id") == "native-read-budget-question"
        ]
        assert len(feedbacks) == 1 and feedbacks[0]["payload"] == current["worker_feedback"]
        assert dispatch["sequence"] < feedbacks[0]["sequence"]
        assert not any(e["kind"] == "receipt" and e["idempotency_key"] == key for e in entries)
        turn = await source._observation(current, dispatch)
    assert turn["dispatch_digest"] == dispatch["digest"]
    assert turn["summary_sha256"] == hashlib.sha256(current["summary"].encode()).hexdigest()
    assert turn["usage_digest"] == module.digest(current["usage"])
    assert turn["implementation_success"] is False and turn["process_exit_attested"] is False
    history = await env.engine.tool_broker.invocations(env.run_id)
    assert len(history) == 13 and all(r["status"] == "succeeded" for r in history)
    assert sum(r["admission"]["tool"] == "read_file" for r in history) == 10
    assert len(env.checks) == 7 and len(await env.journal.sandbox_executions(env.run_id)) == 7
    await env.journal.worker_response(
        env.run_id,
        "native-read-budget-question",
        WorkerResponse(
            id="native-read-budget-answer",
            actor="Authenticated native operator",
            answer="The refusal is definite. Continue only after an explicit finite native allowance is reviewed.",
        ),
    )
    ordinary_before = await snapshot(env)
    with pytest.raises(ValueError, match="finite ordinary successor"):
        await SourceHumanReadmissions(env.service).candidate(env.run_id, parent["request"]["request_id"], str(catalog))
    assert await snapshot(env) == ordinary_before
    observation = json.loads(current["summary"])["source_budget_observation"]
    refusal = observation["read_refusal"]
    assert refusal["code"] == "tool_budget_exhausted" and refusal["effect_started"] is False
    assert refusal["retryable"] is False and observation["shared_refusal"] is None
    assert refusal["budget"]["total"] == {"used": len(history), "limit": 30, "remaining": 17}
    assert refusal["budget"]["tools"] == {
        "read_file": {"used": 10, "limit": 10, "remaining": 0},
        "list_files": {"used": 2, "limit": 10, "remaining": 8},
        "search_text": {"used": 1, "limit": 10, "remaining": 9},
    }
    measured_witness(
        "ordinary",
        {
            "read_file_count": sum(r["admission"]["tool"] == "read_file" for r in history),
            "lifetime_count": len(history),
            "settled_attempt": current["implementation_rounds"],
            "provider_recovery_status": current["provider_recovery"]["status"],
            "paused_task_status": task["status"],
            "dispatch_digest": dispatch["digest"],
            "completed_turn_digest": module.digest(turn),
            "ordinary_candidate_refused": True,
            "original13_sha256": digest(history),
            "read_refusal": refusal,
        },
    )
    module.write_policy(
        store.configuration,
        module.SourceBudgetPolicy(
            enabled=True, max_read_file_lifetime_limit=20, max_activation_ceiling=5, max_additional_activations=1
        ),
    )
    return env, parent, catalog, history


def proposal():
    return module.SourceBudgetProposal(
        version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
    )


async def review_allowance(env, parent, catalog):
    service = module.SourceBudgetReadmissions(env.service)
    review = await service.candidate(env.run_id, parent["request"]["request_id"], str(catalog), proposal())
    return service, review, allowance_request(review, str(catalog))


async def retained_tool_rows(env):
    # Read-only diagnostics must not reauthorize a suspended fixture caller.
    async with env.store._unit() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        return [
            {**copy.deepcopy(row.data), "status": row.status, "correlation_id": row.correlation_id}
            for row in await env.engine.tool_broker.records.for_run(UUID(env.run_id))
        ]


async def test_native_allowance_preserves13_then_real_cli_stops_read20_shared30_and_finishes_final7(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env, parent, catalog, history = await answered_thirteen(store, tmp_path, monkeypatch)
        service, review, body = await review_allowance(env, parent, catalog)
        before, task, wal = await snapshot(env)
        append = env.journal._append
        witnesses = []

        async def witnessed(*args, **kwargs):
            if args[3] == "workspace_reconciliation_intent":
                async with store._unit() as session:
                    pid = await session.scalar(text("SELECT pg_backend_pid()"))
                    assert pid == store._ownership_pid
                    witnesses.append(pid)
            return await append(*args, **kwargs)

        monkeypatch.setattr(env.journal, "_append", witnessed)
        receipt = await service.admit(env.run_id, parent["request"]["request_id"], body)
        assert len(witnesses) == 1
        assert receipt == await service.admit(env.run_id, parent["request"]["request_id"], body)
        assert receipt == await service.get(env.run_id, parent["request"]["request_id"], body.request_id)
        _, queued, entries = await snapshot(env)
        assert queued["attempt"] == 5 and queued["status"] == "queued" and entries[: len(wal)] == wal
        assert (await store.get_run(env.run_id))["error"] is None
        env.engine.configured_provider = True
        owned = store.source_budget_admission_unit
        begin_pids = []

        @asynccontextmanager
        async def witnessed_begin_unit():
            async with owned() as session:
                pid = await session.scalar(text("SELECT pg_backend_pid()"))
                assert pid == store._ownership_pid
                begin_pids.append(pid)
                yield session

        monkeypatch.setattr(store, "source_budget_admission_unit", witnessed_begin_unit)
        owner = asyncio.current_task()
        assert owner is not None and env.run_id not in env.engine.active
        env.engine.active[env.run_id] = owner
        try:
            await env.engine.execute_run(env.run_id)
        finally:
            env.engine.active.pop(env.run_id, None)
        final = await store.get_run(env.run_id)
        assert begin_pids == [store._ownership_pid]
        assert final["status"] == "succeeded", final.get("error")
        assert final["provider_recovery"] == before["provider_recovery"]
        assert final["provider_recovery"]["status"] == "consumed"
        assert final["attempt"] == 5 and final["implementation_rounds"] == 5
        rows = await env.engine.tool_broker.invocations(env.run_id)
        assert rows[:13] == history and len(rows) == 30 and all(r["status"] == "succeeded" for r in rows)
        assert sum(r["admission"]["tool"] == "read_file" for r in rows) == 20
        assert len(env.checks) == 14 and len(await env.journal.sandbox_executions(env.run_id)) == 14
        assert final["sandbox_snapshot"]["configuration"]["command_limit"] == 35
        assert before["commands"] == final["commands"] and final["repair_budget"] == 0
        assert await service.get(env.run_id, parent["request"]["request_id"], body.request_id) == receipt
        observation = json.loads(final["summary"])["source_budget_observation"]
        read_refusal, shared_refusal = observation["read_refusal"], observation["shared_refusal"]
        for refusal in (read_refusal, shared_refusal):
            assert refusal["code"] == "tool_budget_exhausted" and refusal["effect_started"] is False
            assert refusal["retryable"] is False
        assert read_refusal["budget"]["tools"]["read_file"] == {"used": 20, "limit": 20, "remaining": 0}
        assert shared_refusal["budget"]["total"] == {"used": len(rows), "limit": 30, "remaining": 0}
        assert shared_refusal["budget"]["tools"] == {
            "read_file": {"used": 20, "limit": 20, "remaining": 0},
            "list_files": {"used": 8, "limit": 10, "remaining": 2},
            "search_text": {"used": 2, "limit": 10, "remaining": 8},
        }
        measured_witness(
            "amended",
            {
                "read_file_count": sum(r["admission"]["tool"] == "read_file" for r in rows),
                "lifetime_count": len(rows),
                "canonical_attempt": final["attempt"],
                "implementation_rounds": final["implementation_rounds"],
                "verification_executions_added": len(await env.journal.sandbox_executions(env.run_id)) - 7,
                "command_limit": final["sandbox_snapshot"]["configuration"]["command_limit"],
                "original13_sha256": digest(history),
                "retained13_sha256": digest(rows[:13]),
                "read_refusal": read_refusal,
                "shared_refusal": shared_refusal,
            },
        )


@pytest.mark.parametrize("outcome", ["revoked", "policy", "source_cas", "cancel", "receipt_rollback"])
async def test_native_allowance_failure_keeps_original13_and_rolls_back_new_queue(
    store, tmp_path, monkeypatch, outcome
):
    async with authenticated_operator(store) as actor:
        env, parent, catalog, history = await answered_thirteen(store, tmp_path, monkeypatch)
        service, review, body = await review_allowance(env, parent, catalog)
        before = await snapshot(env)
        if outcome == "receipt_rollback":
            append = env.journal._append

            async def interrupted(*args, **kwargs):
                value = await append(*args, **kwargs)
                if args[3] == "workspace_reconciliation_receipt" and args[2] == body.request_id:
                    raise OSError("Controlled final receipt rollback")
                return value

            monkeypatch.setattr(env.journal, "_append", interrupted)
        else:
            admit = store.vault_source_patch_admission

            async def changed(patch):
                if admit is not None:
                    await admit(patch)
                if outcome == "revoked":
                    async with store._unit():
                        user = await ForgeIdentityUserRepository().find_by_id(actor.id)
                        user.enabled = False
                        await ForgeIdentityUserRepository().save(user)
                elif outcome == "policy":
                    module.write_policy(store.configuration, module.SourceBudgetPolicy())
                elif outcome == "source_cas":
                    (env.workspace / "partial.py").write_text("UNREVIEWED=1\n")
                else:
                    await store.update(env.run_id, cancel_request={"status": "requested"})

            monkeypatch.setattr(store, "vault_source_patch_admission", changed)
        with pytest.raises((ValueError, AccessDenied, OSError)):
            await service.admit(env.run_id, parent["request"]["request_id"], body)
        after = await snapshot(env)
        expected = copy.deepcopy(before)
        if outcome == "cancel":
            expected[0]["cancel_request"] = {"status": "requested"}
            expected[0]["updated_at"] = after[0]["updated_at"]
        assert after == expected
        assert await retained_tool_rows(env) == history and len(env.checks) == 7


async def test_native_amended_reader_unknown_keeps_original_receipts_and_never_replays(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env, parent, catalog, history = await answered_thirteen(store, tmp_path, monkeypatch)
        service, review, body = await review_allowance(env, parent, catalog)
        await service.admit(env.run_id, parent["request"]["request_id"], body)
        reader = SourceEditInvocation(env.service.recoveries, env.run_id, await started(env))
        request = SourceReadRequest(tool="read_file", path=".git/config", request_id=uuid4().hex)
        with pytest.raises(ToolReconciliationRequired):
            await reader.run(request)
        with pytest.raises(ToolReconciliationRequired):
            await reader.run(request)
        with pytest.raises(ToolReconciliationRequired):
            await reader.run(SourceReadRequest(tool="list_files", request_id=uuid4().hex))
        rows = await env.engine.tool_broker.invocations(env.run_id)
        assert rows[:13] == history and len(rows) == 14 and rows[-1]["status"] == "unknown"
        assert len(env.checks) == 7
        assert rows[-1]["correlation_id"] == request.request_id
        measured_witness(
            "unknown_never_replayed",
            {
                "original13_sha256": digest(history),
                "retained13_sha256": digest(rows[:13]),
                "lifetime_count": len(rows),
                "unknown_count": sum(row["status"] == "unknown" for row in rows),
                "unknown_request_id": rows[-1]["correlation_id"],
                "expected_request_id": request.request_id,
            },
        )


async def test_native_allowance_lost_lease_cannot_replace_successor_run_or_create_receipt(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env, parent, catalog, history = await answered_thirteen(store, tmp_path, monkeypatch)
        service, review, body = await review_allowance(env, parent, catalog)
        before = await snapshot(env)
        successor = None
        captured = {}
        admit = store.vault_source_patch_admission

        async def takeover(patch):
            nonlocal successor
            if admit is not None:
                await admit(patch)

            async def new_owner():
                nonlocal successor
                await store.release_ownership()
                successor = Store(store.path)
                await successor.initialize()
                await successor.acquire_ownership()
                await successor.update(env.run_id, source_budget_owner="public-successor-custody")
                captured.update(await successor.get_run(env.run_id))

            await asyncio.create_task(new_owner(), context=Context())

        monkeypatch.setattr(store, "vault_source_patch_admission", takeover)
        try:
            with pytest.raises((ValueError, AccessDenied, RuntimeError)):
                await service.admit(env.run_id, parent["request"]["request_id"], body)
            assert successor is not None and await successor.get_run(env.run_id) == captured
            after = await snapshot(env)
            assert (
                after[1:] == before[1:]
                and after[0]["source_edit_continuation"] == before[0]["source_edit_continuation"]
            )
            assert await env.engine.tool_broker.invocations(env.run_id) == history
        finally:
            if successor is not None:
                await successor.close()


def test_native_source_budget_api_di_strict_consent_and_exact_receipt(tmp_path, postgres_schema, monkeypatch):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.engine import Engine
    from firefly_forge.runtime_roles import RuntimeRoles
    from firefly_forge.source_budget_allowances import SourceBudgetPolicies
    from firefly_forge.workspace_reconciliations import WorkspaceReconciliations

    token = "public-source-budget-api-fixture"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52550),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        api_store = http.app.state.store
        context = http.app.state.runtime.context
        engine = context.get_bean(Engine)
        assert context.get_bean(SourceBudgetPolicies).store is api_store
        assert context.get_bean(WorkspaceReconciliations).recoveries.store is api_store

        async def seed():
            assert api_store.runtime_roles.role == "api" and api_store.runtime_roles.started
            with pytest.raises(RuntimeError, match="API role"):
                api_store.runtime_roles.require_execution()
            await api_store.release_ownership()
            seeded = Store(api_store.path)
            seeded.configuration = api_store.configuration
            runtime = RuntimeRoles(seeded, "worker")
            seeded.runtime_roles = runtime
            try:
                await seeded.initialize()
                await runtime.start()
                runtime.require_execution()
                await seeded.acquire_ownership()
                async with authenticated_operator(seeded):
                    env, parent, catalog, _ = await answered_thirteen(seeded, tmp_path / "public-fixture", monkeypatch)
                    engine.workspaces = env.engine.workspaces
                    values = env.run_id, parent["request"]["request_id"], str(catalog)
            finally:
                await runtime.stop()
                await seeded.close()
            await api_store.acquire_ownership()
            return values

        (tmp_path / "public-fixture").mkdir()
        run, parent, path = http.portal.call(seed)
        policy = "/api/v1/source-budget/policy"
        assert http.get(policy).status_code == 200
        assert (
            http.put(
                policy,
                json=module.SourceBudgetPolicy(
                    enabled=True,
                    max_read_file_lifetime_limit=20,
                    max_activation_ceiling=5,
                    max_additional_activations=1,
                ).model_dump(),
            ).status_code
            == 200
        )
        endpoint = f"/api/v1/runs/{run}/source-edit-continuations/{parent}"
        review = http.get(
            endpoint + "/budget-allowance-candidate",
            params={"previous_catalog_path": path, **proposal().model_dump(exclude={"version"})},
        )
        assert review.status_code == 200, review.text
        body = allowance_request(review.json(), path).model_dump()
        assert http.post(endpoint + "/budget-allowances", json=body | {"command_limit": 99}).status_code == 422
        assert (
            http.post(endpoint + "/budget-allowances", json=body | {"acknowledge_no_replay": False}).status_code == 422
        )
        result = http.post(endpoint + "/budget-allowances", json=body)
        assert result.status_code == 201, result.text
        assert http.get(endpoint + "/budget-allowances/" + body["request_id"]).json() == result.json()
        assert http.post(endpoint + "/budget-allowances", json=body).json() == result.json()
        assert http.get(f"/api/v1/runs/{run}").json()["status"] == "queued"


async def test_native_allowance_lease_loss_before_final_commit_rolls_back_intent_queue_and_receipt(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env, parent, catalog, history = await answered_thirteen(store, tmp_path, monkeypatch)
        service, review, body = await review_allowance(env, parent, catalog)
        before = await snapshot(env)
        append = env.journal._append
        witnessed = []
        killed = []

        class TerminateBeforeCommit(TransactionSynchronizationAdapter):
            def __init__(self, unit, session, pid):
                self.unit, self.session, self.pid = unit, session, pid

            async def before_commit(self, read_only):
                assert not read_only and current_unit_of_work() is self.unit
                assert store._ownership_lock.locked()
                assert await self.session.scalar(text("SELECT pg_backend_pid()")) == self.pid
                await store.assert_ownership()
                # The callback is inline; an explicit child still cannot inherit Store admission authority.
                with pytest.raises(RuntimeError, match="tasks, stores or units"):
                    await asyncio.create_task(store.assert_ownership())
                url = make_url(os.environ["FORGE_TEST_DATABASE_URL"])
                assert url.get_backend_name() == "postgresql" and (url.database or "").endswith("_test")
                assert url.host in {"127.0.0.1", "::1", "localhost"}
                connection = await asyncpg.connect(
                    host=url.host,
                    port=url.port,
                    user=url.username,
                    password=url.password,
                    database=url.database,
                    timeout=3,
                    command_timeout=3,
                )
                try:
                    assert await connection.fetchval("SELECT pg_terminate_backend($1, 3000)", self.pid) is True
                    assert (
                        await connection.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid=$1", self.pid) == 0
                    )
                    killed.append(self.pid)
                finally:
                    await connection.close()

        async def witnessed_append(*args, **kwargs):
            value = await append(*args, **kwargs)
            if args[3] == "workspace_reconciliation_intent":
                async with store._unit() as session:
                    unit = current_unit_of_work()
                    pid = await session.scalar(text("SELECT pg_backend_pid()"))
                    assert unit is not None and unit.resource is session and pid == store._ownership_pid
                    witnessed.append(pid)
                    register_synchronization(TerminateBeforeCommit(unit, session, pid))
            return value

        monkeypatch.setattr(env.journal, "_append", witnessed_append)
        with pytest.raises((DBAPIError, CommitOutcomeUnknownError)):
            await service.admit(env.run_id, parent["request"]["request_id"], body)
        assert len(witnessed) == 1 and killed == witnessed
        assert not store._ownership_lock.locked()
        # The backend was proven gone before COMMIT, not an uncertain in-flight commit to replay.
        assert await snapshot(env) == before
        assert await retained_tool_rows(env) == history and len(env.checks) == 7
        measured_witness(
            "lease_backend_terminated_before_commit",
            {
                "same_lease_pid": True,
                "before_commit_termination_count": len(killed),
                "original13_sha256": digest(history),
                "intent_queue_receipt_rolled_back": True,
                "verification_count": len(env.checks),
            },
        )
