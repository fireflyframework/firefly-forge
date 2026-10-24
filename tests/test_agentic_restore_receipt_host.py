# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual repository auto-unit/schema and restored implementation repair-tail controls."""

from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace
from uuid import UUID

import httpx2
import pytest
from pydantic_ai import models
from pyfly.data.transaction import TransactionTemplate
from pyfly.data.transaction.definition import TransactionDefinition
from pyfly.data.transaction.registry import TransactionManagerRegistry
from test_agentic_checkpoint_activation_host import (
    CutAfterPost,
    activated,
    controlled_source_snapshot,
    next_result,
    queue_original,
)
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests
from test_bridge_inflight_cancel import RUN

from firefly_forge import engine as engine_module
from firefly_forge import learning_context
from firefly_forge.persistence import ForgeJournalRepository, ForgeTaskRepository
from firefly_forge.store import Store


async def native_task_schema(env, monkeypatch):
    manager = env.manager
    begin = manager.begin
    schemas = []
    auto_reads = []

    class Session:
        schema = "public"

        async def execute(self, sql, params=None):
            statement = str(sql)
            if statement.startswith("SET LOCAL search_path"):
                self.schema = env.store.schema
            else:
                assert "synchronous_commit" in statement or "pg_advisory_xact_lock" in statement

        async def scalars(self, sql):
            statement = str(sql)
            assert "forge_tasks" in statement or "forge_journal" in statement
            schemas.append(self.schema)
            rows = env.journal.tasks.rows if "forge_tasks" in statement else env.journal.records.rows
            return rows if self.schema == env.store.schema else []

    async def scoped_begin(definition):
        unit = await begin(definition)
        unit.resource = Session()
        return unit

    async def open_auto_unit(*, read_only, autocommit):
        auto_reads.append(read_only)
        unit = await scoped_begin(TransactionDefinition(read_only=read_only))
        unit.auto = True
        return unit

    monkeypatch.setattr(manager, "begin", scoped_begin)
    monkeypatch.setattr(manager, "open_auto_unit", open_auto_unit)

    class Sessions:
        @asynccontextmanager
        async def unit(self):
            async with TransactionTemplate(manager).transaction() as unit:
                yield unit.resource

    env.store._initialized = True
    env.store.session_provider = Sessions()
    env.store._source_budget_context = MethodType(Store._source_budget_context, env.store)
    env.store._unit = MethodType(Store._unit, env.store)
    repository = ForgeTaskRepository(datasource=manager.datasource)
    registry = TransactionManagerRegistry()
    registry.register(manager)
    repository._bind_transaction_managers(registry)
    assert getattr(repository.find_for_run, "__pyfly_repository_operation__", False)
    assert await repository.find_for_run(UUID(RUN)) == []
    assert auto_reads == [True] and schemas == ["public"]
    schemas.clear()
    monkeypatch.setattr(env.journal.tasks, "find_for_run", repository.find_for_run)
    journal_repository = ForgeJournalRepository(datasource=manager.datasource)
    journal_repository._bind_transaction_managers(registry)
    monkeypatch.setattr(env.journal.records, "find_for_run", journal_repository.find_for_run)
    return schemas, auto_reads


@pytest.mark.parametrize(
    "repair,outcome", [(False, "success"), (True, "success"), (True, "verify-failed"), (True, "crash")]
)
async def test_restored_original_receipt_survives_native_schema_and_later_allowed_repair(
    tmp_path, monkeypatch, repair, outcome
):
    env = await activated(tmp_path, monkeypatch)
    try:
        env.store.run.data.update(
            repair_budget=int(repair),
            commands=[{"argv": ["python", "-m", "compileall", "-q", "."], "timeout": 30}],
            verification=[],
        )
        env.store.run.repository_id = UUID(env.store.run.data["repository_id"])
        posts, intent, _, _, request = await queue_original(env, monkeypatch, source="http_response")
        schemas, auto_reads = await native_task_schema(env, monkeypatch)
        checks = []
        checked = engine_module.checked

        async def head(argv, cwd, **kwargs):
            if argv == ["git", "rev-parse", "HEAD"]:
                return "b" * 40
            return await checked(argv, cwd, **kwargs)

        async def snapshot(*args, **kwargs):
            return ["service.py"], controlled_source_snapshot("Fictional retained patch"), "c" * 40

        async def command(*args, **kwargs):
            checks.append(1)
            return SimpleNamespace(
                exit_code=int(repair and (len(checks) == 1 or outcome == "verify-failed")),
                output="Fictional verification",
            )

        async def checkpoint(*args, **kwargs):
            await env.store.update(RUN, checkpoint_revision="d" * 40)

        async def artifact(*args, **kwargs):
            await env.store.update(RUN, artifacts=[])

        async def declaration(argv, cwd, **kwargs):
            assert argv[:3] == ["git", "ls-tree", "-z"]
            return ""

        def transport(request):
            if request.method == "GET":
                return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
            assert request.method == "POST" and repair
            posts.append(request.content)
            if outcome == "crash":
                raise CutAfterPost("Fictional owner loss after a new repair intent")
            return httpx2.Response(200, json=next_result(len(posts) == 3))

        monkeypatch.setattr(engine_module, "checked", head)
        monkeypatch.setattr(learning_context, "checked", declaration)
        monkeypatch.setattr(env.engine, "_snapshot", snapshot)
        monkeypatch.setattr(env.engine, "_project_command", command)
        monkeypatch.setattr(env.engine, "_checkpoint_commit", checkpoint)
        monkeypatch.setattr(env.engine, "_artifact", artifact)
        env.model.client._client._transport = httpx2.MockTransport(transport)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            if outcome == "crash":
                with pytest.raises(CutAfterPost):
                    await env.restores.execute(request.request_id)
            else:
                await env.restores.execute(request.request_id)
        task = next(t for t in env.journal.tasks.rows if t.task_id == "implement")
        ticket = await env.requests.get(request.request_id)
        if outcome == "crash":
            assert ticket["status"] == env.store.run.data["status"] == "needs_reconciliation"
            assert task.data["idempotency_key"] != intent["payload"]["task_key"] and task.attempt == 2
            assert len(posts) == 3 and len(checks) == 1
            assert (
                sum(
                    row.kind == "agent_consume" and row.data["payload"]["version"] == 2
                    for row in env.journal.records.rows
                )
                == 1
            )
            await env.restores.execute(request.request_id)
            assert len(posts) == 3 and (await env.requests.get(request.request_id))["status"] == "needs_reconciliation"
            return
        assert env.store.run.data["status"] == ("failed" if outcome == "verify-failed" else "succeeded"), (
            env.store.run.data.get("error")
        )
        assert len(checks) == 1 + int(repair) and len(posts) == 2 + 2 * int(repair)
        assert (env.workspace / "later.py").exists() == repair
        assert task.attempt == 1 + int(repair)
        assert (task.data["idempotency_key"] != intent["payload"]["task_key"]) == repair
        assert ticket["status"] == "completed"
        assert ticket["result"] == {"task_key": intent["payload"]["task_key"], "attempt": 1}
        assert schemas and set(schemas) == {env.store.schema}
        assert auto_reads == [True]
    finally:
        await env.model.client.close()
