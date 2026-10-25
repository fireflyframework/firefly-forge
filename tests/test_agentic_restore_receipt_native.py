# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED, UNRUN: real PostgreSQL/AutoUnit/Engine receipt and failure controls.

Only the sole native operator may execute these node IDs with the isolated _test
DB fixture. Git inspection, verification commands and delivery are owned seams;
Agentic, SDK parsing, real file tools, native SQL/transactions/ACLs are not mocked.
"""

import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from pydantic_ai import models
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_agentic_checkpoint_activation_host import CutAfterPost, controlled_source_snapshot, next_result, result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests, prepared

from firefly_forge import agentic_checkpoint_invocation as invocation_module
from firefly_forge import engine as engine_module
from firefly_forge import learning_context
from firefly_forge.agent_checkpoint_models import RestoreInput
from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.agent_restores import AgentRestores, RestoreExecution
from firefly_forge.agent_roles import ProjectRoles, RoleRegistry
from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.engine import Engine
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.git_workspace import git_environment, metadata_digest, trusted_git
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.models import RunInput
from firefly_forge.persistence import ForgeTaskRepository
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.providers import BridgeProvider
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.store import identifier


def seed_restore_workspace(workspace):
    workspace.mkdir(parents=True)
    environment = git_environment({"PATH": "/usr/bin:/bin"})
    for argv in (
        ["git", "init", "-q"],
        [
            "git",
            "-c",
            "user.name=Controlled Source",
            "-c",
            "user.email=source@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "Seed source",
        ],
        ["git", "read-tree", "HEAD"],
    ):
        subprocess.run(trusted_git(argv), cwd=workspace, env=environment, check=True, capture_output=True, timeout=10)
    return (
        subprocess.check_output(trusted_git(["git", "rev-parse", "HEAD"]), cwd=workspace, env=environment, timeout=10)
        .decode()
        .strip()
    )


@asynccontextmanager
async def native_restoration(
    store, tmp_path, monkeypatch, *, repair=False, outcome="success", grants=None, next_tool=False
):
    async with prepared(store, tmp_path) as (_, actor, seeded, _, _, _, _, _):
        store.configuration.update("bridge", ProviderConfiguration(values={"checkpoints": "required"}))
        run = await store.create_run(
            RunInput(
                repository_id=seeded["repository_id"],
                project_id=seeded["project_id"],
                component_id="api",
                agent="bridge",
                brief="Native original receipt and repair control",
            )
        )
        run_id = run["id"]
        roles = RoleRegistry(tmp_path / "native-roles")
        catalog = roles.tools()
        if grants is not None:
            for tool in catalog:
                if tool["id"] in grants:
                    tool["availability"] = "worker_unverified"
        await store.update(
            run_id,
            **store.configuration.snapshot("bridge"),
            role_snapshot=roles.snapshot(
                ProjectRoles(disabled=["docs"], allowed_tools={"implementer": grants or ["write_file"]}),
                tool_catalog=catalog,
            ),
            repair_budget=int(repair),
            resume_existing=True,
            commands=[{"argv": ["python", "-m", "compileall", "-q", "."], "timeout": 30}],
        )
        journal = Journal(store)
        registry = store.session_provider._registry()
        assert registry is not None
        for repository in (journal.records, journal.tasks):
            repository._bind_transaction_managers(registry)
        provider = BridgeProvider(store.configuration.load_snapshot((await store.get_run(run_id))["config_revision"]))
        model = provider.model()
        monkeypatch.setattr(provider, "model", lambda **kwargs: model)
        engine = Engine(store, tmp_path / "native-owned", lambda _: provider, journal=journal)
        workspace = engine.workspaces / run_id
        revision = seed_restore_workspace(workspace)
        await store.update(run_id, git_metadata_digest=metadata_digest(workspace))

        async def head(argv, cwd, **kwargs):
            assert current_unit_of_work() is None
            assert argv[-2:] == ["rev-parse", "HEAD"]
            return revision

        async def engine_git(argv, cwd, **kwargs):
            assert current_unit_of_work() is None
            if argv == ["git", "ls-files", "-z"]:
                return ""
            return await head(argv, cwd, **kwargs)

        async def declaration(argv, cwd, **kwargs):
            assert current_unit_of_work() is None and argv[:3] == ["git", "ls-tree", "-z"]
            return ""

        async def inspect(*args, **kwargs):
            return {"base_revision": revision, "head": revision, "source_tree": "c" * 40}

        async def snapshot(*args, **kwargs):
            return ["service.py"], controlled_source_snapshot("Fictional native SQL control patch"), "c" * 40

        checks, posts, schemas = [], [], []

        async def command(*args, **kwargs):
            checks.append(1)
            return SimpleNamespace(
                exit_code=int(repair and (len(checks) == 1 or outcome == "verify-failed")),
                output="Fictional command result",
            )

        async def checkpoint(*args, **kwargs):
            await store.update(run_id, checkpoint_revision="d" * 40)

        async def artifact(*args, **kwargs):
            await store.update(run_id, artifacts=[])

        find = journal.records.find_for_run

        async def audited_find(key):
            unit = current_unit_of_work()
            assert unit is not None, "Native journal receipt read escaped the Forge unit"
            schemas.append(await unit.resource.scalar(text("SELECT current_schema()")))
            assert schemas[-1] == store.schema
            return await find(key)

        monkeypatch.setattr(invocation_module, "checked", head)
        monkeypatch.setattr(engine_module, "checked", engine_git)
        monkeypatch.setattr(learning_context, "checked", declaration)
        monkeypatch.setattr(engine, "_inspect_workspace", inspect)
        monkeypatch.setattr(engine, "_snapshot", snapshot)
        monkeypatch.setattr(engine, "_project_command", command)
        monkeypatch.setattr(engine, "_checkpoint_commit", checkpoint)
        monkeypatch.setattr(engine, "_artifact", artifact)
        monkeypatch.setattr(journal.records, "find_for_run", audited_find)
        runtime = RuntimeRoles(store)
        await runtime.start()
        requests = ExecutionRequests(store, runtime)
        restores = AgentRestores(store, requests, engine)
        requests.agent_restores = restores
        consume = AgentCheckpoints._consume

        async def interrupt_second(service, current_run, intent, body, current, reader):
            async with store._unit():
                count = (await journal.records.checkpoint_budget(identifier(run_id)))["count"]
            if count == 2:
                raise RuntimeError("Native control interrupts after original HTTP retention")
            return await consume(service, current_run, intent, body, current, reader)

        monkeypatch.setattr(AgentCheckpoints, "_consume", interrupt_second)

        def transport(request):
            if request.method == "GET":
                return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
            assert request.method == "POST"
            posts.append(request.content)
            if len(posts) == 1:
                return httpx2.Response(200, json=result(tool=True))
            if len(posts) == 2:
                return httpx2.Response(200, json=next_result(next_tool))
            if next_tool and not repair:
                return httpx2.Response(200, json=next_result(False))
            assert repair, "Original native result attempted a replacement POST"
            if outcome == "crash":
                raise CutAfterPost("Native control owner loss after repair intent")
            return httpx2.Response(200, json=next_result(len(posts) == 3))

        model.client._client._transport = httpx2.MockTransport(transport)
        model.client._client._mounts = {}
        try:
            with models.override_allow_model_requests(False), mock_model_requests(model):
                await engine.execute_run(run_id)
            assert (await store.get_run(run_id))["status"] == "needs_reconciliation"
            assert len(posts) == 2 and (workspace / "service.py").read_text() == "VALUE = 1\n"
            entries = await journal.entries(run_id)
            captured = [r for r in entries if r["kind"] == "agent_checkpoint"][-1]
            intent = next(
                r
                for r in entries
                if r["kind"] == "bridge_request_intent" and r["idempotency_key"] == captured["idempotency_key"]
            )
            response = next(
                r
                for r in entries
                if r["kind"] == "bridge_request_response" and r["idempotency_key"] == captured["idempotency_key"]
            )
            operator = Principal(
                uuid4(), actor.issuer, "native-restore-operator", authentication_method="local_bootstrap"
            )
            async with store._unit():
                await ForgeIdentityUserRepository().save(
                    ForgeIdentityUserEntity(
                        id=operator.id, issuer=operator.issuer, subject=operator.subject, enabled=True
                    )
                )
            token = CURRENT_PRINCIPAL.set(operator)
            try:
                request = RestoreInput.model_validate(
                    {
                        "request_id": uuid4().hex,
                        "expected_checkpoint_digest": captured["digest"],
                        "expected_intent_digest": intent["digest"],
                        "source": {"kind": "http_response", "record_digest": response["digest"]},
                    }
                )
                ticket = await restores.request(run_id, captured["idempotency_key"], request)
                assert ticket["status"] == "queued"
                yield SimpleNamespace(
                    store=store,
                    journal=journal,
                    engine=engine,
                    restores=restores,
                    requests=requests,
                    model=model,
                    actor=actor,
                    operator=operator,
                    run_id=run_id,
                    request=request,
                    intent=intent,
                    posts=posts,
                    checks=checks,
                    schemas=schemas,
                    workspace=workspace,
                    registry=registry,
                )
            finally:
                CURRENT_PRINCIPAL.reset(token)
        finally:
            await runtime.stop()
            await model.client.close()


@pytest.mark.parametrize(
    "repair,outcome", [(False, "success"), (True, "success"), (True, "verify-failed"), (True, "crash")]
)
async def test_native_original_receipt_uses_forge_schema_after_real_repair_tail(
    store, tmp_path, monkeypatch, repair, outcome
):
    async with native_restoration(store, tmp_path, monkeypatch, repair=repair, outcome=outcome) as env:
        repository = ForgeTaskRepository()
        repository._bind_transaction_managers(env.registry)
        # The real repository wrapper opens an automatic read unit, never a Forge unit.
        # A dedicated DB may have no public forge_tasks table or an unrelated one.
        try:
            unscoped = await repository.find_for_run(identifier(env.run_id))
            assert unscoped == []
        except DBAPIError as error:
            assert getattr(error.orig, "sqlstate", None) == "42P01"
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            if outcome == "crash":
                with pytest.raises(CutAfterPost):
                    await env.restores.execute(env.request.request_id)
            else:
                await env.restores.execute(env.request.request_id)
        ticket = await env.restores.get(env.run_id, env.intent["idempotency_key"], env.request.request_id)
        run = await store.get_run(env.run_id)
        async with store._unit():
            tasks = await env.journal.tasks.find_for_run(identifier(env.run_id))
        task = next(t for t in tasks if t.task_id == "implement")
        entries = await env.journal.entries(env.run_id)
        assert Journal.verify_chain(entries)
        original = [
            r for r in entries if r["kind"] == "receipt" and r["idempotency_key"] == env.intent["payload"]["task_key"]
        ]
        assert len(original) == 1 and original[0]["payload"]["implementation_round"] == 1
        with pytest.raises(DBAPIError):
            async with store._unit() as session:
                await session.execute(
                    text(
                        "UPDATE forge_journal SET data='{}'::jsonb "
                        "WHERE run_id=:run AND kind='receipt' AND idempotency_key=:key"
                    ),
                    {"run": identifier(env.run_id), "key": env.intent["payload"]["task_key"]},
                )
        assert await env.journal.entries(env.run_id) == entries
        assert env.schemas and set(env.schemas) == {store.schema}
        assert task.attempt == 1 + int(repair)
        assert (task.data["idempotency_key"] != env.intent["payload"]["task_key"]) == repair
        if outcome == "crash":
            assert ticket["status"] == run["status"] == "needs_reconciliation"
        else:
            assert ticket["status"] == "completed"
            assert ticket["result"] == {"task_key": env.intent["payload"]["task_key"], "attempt": 1}
            assert run["status"] == ("failed" if outcome == "verify-failed" else "succeeded")
        before = len(env.posts)
        await env.restores.execute(env.request.request_id)
        assert (
            len(env.posts) == before and (await env.requests.get(env.request.request_id))["status"] == ticket["status"]
        )


async def test_native_restore_consume_insert_failure_rolls_back_without_rearming_ticket(store, tmp_path, monkeypatch):
    async with native_restoration(store, tmp_path, monkeypatch) as env:
        save = env.journal.records.save

        async def fail_after_insert(row):
            await save(row)
            if row.kind == "agent_consume" and row.data["payload"]["version"] == 2:
                raise RuntimeError("Native SQL failure after consume insert")

        monkeypatch.setattr(env.journal.records, "save", fail_after_insert)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(env.request.request_id)
        entries = await env.journal.entries(env.run_id)
        assert not any(r["kind"] == "agent_consume" and r["payload"]["version"] == 2 for r in entries)
        assert (await env.requests.get(env.request.request_id))["status"] == "needs_reconciliation"
        assert not any(
            r["kind"] == "receipt" and r["idempotency_key"] == env.intent["payload"]["task_key"] for r in entries
        )
        await env.restores.execute(env.request.request_id)
        assert len(env.posts) == 2 and Journal.verify_chain(await env.journal.entries(env.run_id))


async def test_native_owner_restart_after_committed_consume_preserves_used_boundary(store, tmp_path, monkeypatch):
    async with native_restoration(store, tmp_path, monkeypatch) as env:
        consume, finish = RestoreExecution.consume, env.restores.finish

        async def crash_after_consume(restoration, *args):
            await consume(restoration, *args)
            raise CutAfterPost("Native owner death before original parser exposure")

        async def owner_died(*args, **kwargs):
            raise CutAfterPost("Native owner cannot persist its cleanup")

        monkeypatch.setattr(RestoreExecution, "consume", crash_after_consume)
        monkeypatch.setattr(env.restores, "finish", owner_died)
        with models.override_allow_model_requests(False), mock_model_requests(env.model), pytest.raises(CutAfterPost):
            await env.restores.execute(env.request.request_id)
        assert (await env.requests.get(env.request.request_id))["status"] == "executing"
        previous_pid = store._ownership_pid
        await store.release_ownership()
        await store._engine.dispose()
        await store.acquire_ownership()
        assert store._ownership_pid != previous_pid
        monkeypatch.setattr(env.restores, "finish", finish)
        await env.restores.recover()
        assert (await env.requests.get(env.request.request_id))["status"] == "needs_reconciliation"
        entries = await env.journal.entries(env.run_id)
        assert sum(r["kind"] == "agent_consume" and r["payload"]["version"] == 2 for r in entries) == 1
        await env.restores.execute(env.request.request_id)
        assert len(env.posts) == 2 and Journal.verify_chain(await env.journal.entries(env.run_id))


@pytest.mark.parametrize("authority", ["original", "operator"])
async def test_native_late_distinct_identity_revocation_prevents_original_result_exposure(
    store, tmp_path, monkeypatch, authority
):
    async with native_restoration(store, tmp_path, monkeypatch) as env:
        admit, changed = store.admit_credentials, []
        revoked = env.actor if authority == "original" else env.operator

        async def revoke(value):
            assert current_unit_of_work() is None
            await admit(value)
            if not changed and isinstance(value, tuple) and len(value) == 3:
                async with store._unit():
                    users = ForgeIdentityUserRepository()
                    user = await users.find_by_id(revoked.id, lock=LockMode.PESSIMISTIC_WRITE)
                    assert user is not None
                    user.enabled = False
                    await users.save(user)
                changed.append(authority)

        monkeypatch.setattr(store, "admit_credentials", revoke)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(env.request.request_id)
        assert changed == [authority]
        async with store._unit():
            assert (
                await env.journal.records.checkpoint_record(
                    identifier(env.run_id), env.intent["idempotency_key"], "agent_consume"
                )
                is None
            )
            users = ForgeIdentityUserRepository()
            user = await users.find_by_id(revoked.id, lock=LockMode.PESSIMISTIC_WRITE)
            user.enabled = True
            await users.save(user)
        assert (await env.requests.get(env.request.request_id))["status"] == "needs_reconciliation"
        await env.restores.execute(env.request.request_id)
        assert len(env.posts) == 2 and Journal.verify_chain(await env.journal.entries(env.run_id))
