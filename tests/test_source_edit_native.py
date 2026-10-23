# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real native SQL/Git/runner control; the CLI and its metadata are explicit public fixtures."""

import asyncio
import sys
from uuid import UUID, uuid4

import pytest
from source_edit_qualification_fixtures import register_fixture_profile
from test_cli_command_budget_native import retain_cli
from test_workspace_reconciliation_native import committed_intents, retained_environment_refusal
from worker_reply_fixtures import authenticated_operator

from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import AccessDenied
from firefly_forge.source_edit_continuations import SourceEditInvocation
from firefly_forge.source_edit_models import SourceEditContinuationInput, SourceReadRequest
from firefly_forge.tool_broker import ToolInvocationBroker, ToolReconciliationRequired


async def prepared(store, tmp_path, monkeypatch):
    executable = tmp_path / "public-native-cli-fixture"
    executable.write_text(
        "#!" + sys.executable + "\nimport json,pathlib,sys,tomllib\nargs=sys.argv[1:]\n"
        "if 'app-server' in args:\n"
        " from firefly_forge.source_edit_filesystem_policy import EMPTY_NATIVE_HOOKS,native_api_permissions\n"
        " assert '--strict-config' in args\n"
        " overrides=tomllib.loads('\\n'.join(args[n+1] for n,arg in enumerate(args) if arg=='-c'))\n"
        " config=overrides.copy()\n"
        " config['permissions']=native_api_permissions(overrides['permissions'])\n"
        " config['hooks']=EMPTY_NATIVE_HOOKS | overrides['hooks']\n"
        " config['tools']={'web_search':None}\n"
        " config['agents']={'enabled':None,'max_concurrent_threads_per_session':None,'max_depth':None,"
        "'default_subagent_model':None,'default_subagent_reasoning_effort':None,"
        "'job_max_runtime_seconds':None,'interrupt_message':None} | overrides['agents']\n"
        " init=json.loads(sys.stdin.readline()); assert init['id']==1 and init['method']=='initialize'\n"
        " print(json.dumps({'id':1,'result':{}}),flush=True)\n"
        " assert json.loads(sys.stdin.readline())['method']=='initialized'\n"
        " read=json.loads(sys.stdin.readline()); assert read['id']==2 and read['method']=='config/read'\n"
        " assert read['params']=={'includeLayers':False,'cwd':str(pathlib.Path.cwd())}\n"
        " requirements=json.loads(sys.stdin.readline())\n"
        " assert requirements['id']==3 and requirements['method']=='configRequirements/read'\n"
        " print(json.dumps({'id':3,'result':{'requirements':None}}),flush=True)\n"
        " print(json.dumps({'id':2,'result':{'config':config}}),flush=True)\n"
        " assert sys.stdin.read()==''\n"
        "else:\n"
        " sys.stdin.read()\n"
        " pathlib.Path('partial.py').write_text('VALUE = 2\\n')\n"
        " for event in [{'type':'thread.started','thread_id':'source-only-fixture'},{'type':'turn.started'},"
        "{'type':'item.completed','item':{'id':'final','type':'agent_message','text':'Changed public source'}},"
        "{'type':'turn.completed','usage':{}}]: print(json.dumps(event))\n"
    )
    executable.chmod(0o700)
    register_fixture_profile(monkeypatch, executable)
    store.configuration.update(
        "codex", ProviderConfiguration(values={"executable": str(executable), "model": "fixture-model"})
    )
    env = await retained_environment_refusal(store, tmp_path / "retained", monkeypatch)
    await retain_cli(store, env.run_id)
    env.engine.tool_broker = ToolInvocationBroker(store, env.journal, store.path.parent)
    env.engine.provider_recoveries = env.service.recoveries
    return env


def activation(candidate):
    return SourceEditContinuationInput(
        version=1,
        request_id=uuid4().hex,
        candidate_digest=candidate["digest"],
        acknowledge_environment_only=True,
        acknowledge_resume_existing_task=True,
        acknowledge_existing_read_scope=True,
        acknowledge_no_commands=True,
    )


async def started(env):
    await env.store.update(env.run_id, status="developing")
    run = await env.store.get_run(env.run_id)
    intent = await env.engine._begin_task(
        env.run_id, "implement", env.workspace, env.store.configuration.load_snapshot(run["config_revision"])
    )
    return intent["idempotency_key"]


async def test_native_source_activation_commits_original_intent_and_runs_final_seven_only(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await prepared(store, tmp_path, monkeypatch)
        candidate = await env.service.source_candidate(env.run_id)
        assert candidate["budget"]["remaining_commands"] == 7 and candidate["source_edit"]["cli_commands"] == 21
        body = activation(candidate)
        from firefly_forge import workspace_reconciliations as module

        original = module.apply_dependency_plan
        observed = []

        def effect(workspace, plan):
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as pool:
                observed.append(
                    pool.submit(asyncio.run, committed_intents(store.schema, env.run_id, body.request_id)).result()
                )
            return original(workspace, plan)

        monkeypatch.setattr(module, "apply_dependency_plan", effect)
        receipt = await env.service.activate_source_edit(env.run_id, body)
        assert observed == [1] and await env.service.get(env.run_id, body.request_id) == receipt
        assert await env.service.activate_source_edit(env.run_id, body) == receipt and observed == [1]
        env.engine.configured_provider = True
        await env.engine.execute_run(env.run_id)
        result = await store.get_run(env.run_id)
        assert result["status"] == "succeeded", result.get("error")
        assert len(env.checks) == 14 and len(env.calls) == 1
        assert result["sandbox_snapshot"]["configuration"]["command_limit"] == 35
        assert result["implementation_rounds"] == 2
        assert len(await env.journal.sandbox_executions(env.run_id)) == 14


async def test_native_source_activation_revoked_during_admission_never_creates_intent(store, tmp_path, monkeypatch):
    async with authenticated_operator(store) as actor:
        env = await prepared(store, tmp_path, monkeypatch)
        body = activation(await env.service.source_candidate(env.run_id))
        old = (env.workspace / ".git/index").read_bytes()
        original = store.admit_credentials

        async def revoke(value):
            await original(value)
            async with store._unit():
                user = await ForgeIdentityUserRepository().find_by_id(actor.id)
                user.enabled = False
                await ForgeIdentityUserRepository().save(user)

        monkeypatch.setattr(store, "admit_credentials", revoke)
        with pytest.raises(AccessDenied):
            await env.service.activate_source_edit(env.run_id, body)
        assert (env.workspace / ".git/index").read_bytes() == old
        async with store._unit():
            assert not any(
                e.kind == "workspace_reconciliation_intent"
                for e in await env.journal.records.find_for_run(UUID(env.run_id))
            )


async def test_native_source_unknown_environment_effect_never_replays_or_queues(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await prepared(store, tmp_path, monkeypatch)
        body = activation(await env.service.source_candidate(env.run_id))
        from firefly_forge import workspace_reconciliations as module

        effects = []

        def interrupted(*args):
            effects.append(1)
            raise asyncio.CancelledError()

        monkeypatch.setattr(module, "apply_dependency_plan", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await env.service.activate_source_edit(env.run_id, body)
        assert (await env.service.get(env.run_id, body.request_id))["status"] == "needs_reconciliation"
        with pytest.raises(ValueError, match="unknown"):
            await env.service.activate_source_edit(env.run_id, body)
        assert effects == [1] and (await store.get_run(env.run_id))["status"] == "failed"


async def test_native_reader_receipts_keep_source_identity_and_unknown_blocks_new_calls(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await prepared(store, tmp_path, monkeypatch)
        await env.service.activate_source_edit(env.run_id, activation(await env.service.source_candidate(env.run_id)))
        runtime = SourceEditInvocation(env.service.recoveries, env.run_id, await started(env))
        request = SourceReadRequest(tool="read_file", path="partial.py", request_id=uuid4().hex)
        first = await runtime.run(request)
        assert first["content"] == "VALUE = 1\n" and await runtime.run(request) == first
        assert len(await env.engine.tool_broker.invocations(env.run_id)) == 1
        (env.workspace / "partial.py").write_text("VALUE = 2\n")
        with pytest.raises(ValueError, match="another invocation"):
            await runtime.run(request)
        with pytest.raises(ToolReconciliationRequired):
            await runtime.run(SourceReadRequest(tool="read_file", path=".git/config", request_id=uuid4().hex))
        with pytest.raises(ToolReconciliationRequired):
            await runtime.run(SourceReadRequest(tool="list_files", request_id=uuid4().hex))
        rows = await env.engine.tool_broker.invocations(env.run_id)
        assert [row["status"] for row in rows] == ["succeeded", "unknown"]


async def test_native_source_channel_absence_keeps_provider_dispatch_unconsumed(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await prepared(store, tmp_path, monkeypatch)
        await env.service.activate_source_edit(env.run_id, activation(await env.service.source_candidate(env.run_id)))
        key = await started(env)
        wal = await env.journal.entries(env.run_id)
        with pytest.raises(ValueError, match="channel"):
            await env.service.recoveries.normal_dispatch(env.run_id, key)
        assert await env.journal.entries(env.run_id) == wal and len(env.calls) == 1 and len(env.checks) == 7


def test_native_source_api_authentication_exact_receipt_and_strict_scope(tmp_path, postgres_schema, monkeypatch):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.engine import Engine
    from firefly_forge.store import Store
    from firefly_forge.workspace_reconciliations import WorkspaceReconciliations

    token = "synthetic-source-reader-owner"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52550),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        api_store = http.app.state.store
        service = http.app.state.runtime.context.get_bean(WorkspaceReconciliations)
        engine = http.app.state.runtime.context.get_bean(Engine)
        assert service.recoveries.store is api_store and service.recoveries.engine is engine

        async def seed():
            fixture_store = Store(tmp_path / "native-seed" / "factory.db")
            await fixture_store.initialize()
            fixture_store.configuration = api_store.configuration
            try:
                async with authenticated_operator(fixture_store):
                    env = await prepared(fixture_store, tmp_path / "public-seed", monkeypatch)
                    engine.workspaces = env.engine.workspaces
                    run_id = env.run_id
            finally:
                await fixture_store.close()
            await api_store.acquire_ownership()
            return run_id

        (tmp_path / "public-seed").mkdir()
        run_id = http.portal.call(seed)
        endpoint = f"/api/v1/runs/{run_id}/source-edit-continuations"
        candidate = http.get(endpoint + "/candidate")
        assert candidate.status_code == 200, candidate.text
        body = activation(candidate.json()).model_dump()
        receipt = http.post(endpoint, json=body)
        assert receipt.status_code == 201, receipt.text
        assert http.get(endpoint + "/" + body["request_id"]).json() == receipt.json()
        assert http.post(endpoint, json=body).json() == receipt.json()
        assert http.post(endpoint, json=body | {"command_limit": 99}).status_code == 422
        http.portal.call(api_store.release_ownership)
        assert http.get(endpoint + "/candidate").status_code >= 400
