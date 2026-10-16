# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual Agentic/Bridge production wiring; owned memory transport, never a listener."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import httpx2
import pytest
from pydantic_ai import models
from pyfly.data.transaction import TransactionTemplate, current_unit_of_work
from test_agentic_checkpoint_storage_host import setup
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests
from test_bridge_inflight_cancel import RUN

from firefly_forge.bridge_completion_scope import bridge_completion_scope
from firefly_forge.providers import BridgeProvider


class CutAfterPost(BaseException):
    pass


async def activated(tmp_path, monkeypatch):
    from test_bridge_inflight_cancel import Rows

    from firefly_forge import agentic_checkpoint_invocation as invocation_module
    from firefly_forge import engine as engine_module
    from firefly_forge.agent_restores import AgentRestores
    from firefly_forge.agent_roles import ProjectRoles, RoleRegistry
    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.engine import Engine
    from firefly_forge.execution_requests import ExecutionRequests
    from firefly_forge.identity import Principal
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess

    store, journal, manager, service, *_ = await setup(tmp_path, monkeypatch)
    journal.tasks.rows.clear()
    journal.plans.rows.clear()
    journal.records.rows.clear()
    store.run.data.pop("active_task", None)
    store.schema = "stage3-host-memory"
    store._ownership_pid = 12345
    store.configuration.update("bridge", ProviderConfiguration(values={"checkpoints": "required"}))
    store.run.data.update(store.configuration.snapshot("bridge"))
    roles = RoleRegistry(tmp_path / "roles")
    store.run.data["role_snapshot"] = roles.snapshot(
        ProjectRoles(disabled=["docs"], allowed_tools={"implementer": ["write_file"]})
    )
    principal = Principal(UUID("9" * 32), "host-fixture", "operator", authentication_method="local_bootstrap")
    store.run.data["execution_actor"] = ProjectAccess.actor_snapshot(principal)
    user = SimpleNamespace(id=principal.id, issuer=principal.issuer, subject=principal.subject, enabled=True)

    async def identity(self, key, **kwargs):
        return user if key == user.id else None

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)

    async def campaign(run, **kwargs):
        pass

    store.assert_campaign_current = campaign
    store.credential_registry = dict

    class Session:
        async def execute(self, sql, params):
            assert "pg_advisory_xact_lock" in str(sql)

    @asynccontextmanager
    async def unit():
        async with TransactionTemplate(manager).transaction():
            yield Session()

    store._unit = unit

    class Requests(Rows):
        async def for_run(self, run, kind):
            return [r for r in self.rows if r.run_id == run and r.kind == kind]

        async def state(self, status, limit=50, *, kind=None):
            return [r for r in self.rows if r.status == status and (kind is None or r.kind == kind)][:limit]

        async def pending_count(self):
            return sum(r.status in {"queued", "executing"} for r in self.rows)

    class Events(Rows):
        async def events(self, key):
            return sorted([r for r in self.rows if r.request_id == key], key=lambda r: r.sequence)

    provider = BridgeProvider(store.configuration.load_snapshot(store.run.data["config_revision"]))
    model = provider.model()
    monkeypatch.setattr(provider, "model", lambda **kwargs: model)
    engine = Engine(store, tmp_path / "owned", lambda _: provider, journal=journal)
    workspace = engine.workspaces / RUN
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".git/HEAD").write_text("b" * 40)

    async def head(argv, cwd, **kwargs):
        assert current_unit_of_work() is None
        assert argv[-2:] == ["rev-parse", "HEAD"]
        return (cwd / ".git/HEAD").read_text()

    monkeypatch.setattr(invocation_module, "checked", head)

    async def inspect(*args):
        return {"base_revision": "b" * 40, "head": "b" * 40, "source_tree": "c" * 40}

    async def checked(argv, *args, **kwargs):
        assert current_unit_of_work() is None and argv == ["git", "ls-files", "-z"]
        return ""

    monkeypatch.setattr(engine, "_inspect_workspace", inspect)
    monkeypatch.setattr(engine_module, "checked", checked)
    runtime = SimpleNamespace(require_execution=lambda: None)
    requests = ExecutionRequests(store, runtime, Requests(), Events())
    restores = AgentRestores(store, requests, engine)
    requests.agent_restores = restores
    return SimpleNamespace(
        store=store,
        journal=journal,
        manager=manager,
        service=service,
        provider=provider,
        model=model,
        engine=engine,
        workspace=workspace,
        requests=requests,
        restores=restores,
        user=user,
        principal=principal,
        principal_context=CURRENT_PRINCIPAL,
    )


def result(content=None, *, tool=False):
    return {
        "id": "chatcmpl-00000000-0000-4000-8000-000000000001",
        "object": "chat.completion",
        "created": 1,
        "model": "bridge/default",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if tool else "stop",
                "message": {
                    "role": "assistant",
                    "content": content,
                    **(
                        {
                            "tool_calls": [
                                {
                                    "id": "write-one",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": '{"path":"service.py","content":"VALUE = 1\\n"}',
                                    },
                                }
                            ]
                        }
                        if tool
                        else {}
                    ),
                },
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }


async def lost_completion(env, monkeypatch, *, http_failure=False, next_tool=False):
    from firefly_forge.agent_checkpoints import AgentCheckpoints

    posts = []
    if http_failure:
        consume = AgentCheckpoints._consume

        async def refuse_second(service, run, intent, body, current, reader):
            if len([r for r in env.journal.records.rows if r.kind == "agent_checkpoint"]) == 2:
                raise RuntimeError("Fictional interruption after actual HTTP retention")
            return await consume(service, run, intent, body, current, reader)

        monkeypatch.setattr(AgentCheckpoints, "_consume", refuse_second)

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        checkpoints = [r for r in env.journal.records.rows if r.kind == "agent_checkpoint"]
        assert len(checkpoints) == len(posts) + 1
        posts.append(request.content)
        if len(posts) == 1:
            return httpx2.Response(200, json=result(tool=True))
        if http_failure:
            return httpx2.Response(200, json=next_result(next_tool))
        raise httpx2.ReadError("Fictional original delivery loss", request=request)

    env.model.client._client._transport = httpx2.MockTransport(transport)
    env.model.client._client._mounts = {}
    with models.override_allow_model_requests(False), mock_model_requests(env.model):
        await env.engine.execute_run(RUN)
    run = await env.store.get_run(RUN)
    assert run["status"] == "needs_reconciliation", run.get("error")
    assert len(posts) == 2
    assert (env.workspace / "service.py").read_text() == "VALUE = 1\n"
    checkpoint = [r.data for r in env.journal.records.rows if r.kind == "agent_checkpoint"][-1]
    intent = next(
        r.data
        for r in env.journal.records.rows
        if r.kind == "bridge_request_intent" and r.idempotency_key == checkpoint["idempotency_key"]
    )
    return posts, intent, checkpoint


def next_result(next_tool):
    value = result(json.dumps({"summary": "Original retained final report"}), tool=next_tool)
    if next_tool:
        call = value["choices"][0]["message"]["tool_calls"][0]
        call["id"] = "write-new"
        call["function"]["arguments"] = '{"path":"later.py","content":"NEW = 2\\n"}'
        value["choices"][0]["message"]["content"] = None
    return value


async def retained_result(env, intent, *, next_tool=False):
    from test_bridge_completion_records import seal, snapshot

    from firefly_forge.bridge_completion_models import ObservationInput

    _, remote = snapshot()
    descriptor = intent["payload"]["descriptor"]
    remote["admission"].update(
        {
            k: descriptor.get(k)
            for k in ("request_id", "principal_id", "model", "wire_sha256", "wire_bytes", "image_revision")
        }
    )
    remote["admission"] = seal(remote["admission"])
    body = json.dumps(next_result(next_tool), separators=(",", ":"))
    remote["terminal"].update(
        admission_digest=remote["admission"]["digest"],
        result_json=body,
        result_sha256=hashlib.sha256(body.encode()).hexdigest(),
        result_bytes=len(body.encode()),
    )
    remote["terminal"] = seal(remote["terminal"])

    class Response:
        status_code = 200

        async def aiter_bytes(self):
            yield json.dumps(remote).encode()

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            assert current_unit_of_work() is None and method == "GET" and url.endswith(intent["idempotency_key"])
            yield Response()

    return (
        body.encode(),
        remote,
        Client,
        ObservationInput(observation_id="a" * 32, expected_intent_digest=intent["digest"]),
    )


async def test_engine_checkpoint_admission_precedes_post_and_normal_consume_precedes_tools(tmp_path, monkeypatch):
    from firefly_forge.workspace_tools import WorkspaceTools

    env = await activated(tmp_path, monkeypatch)
    write = WorkspaceTools.write_file

    async def admitted_write(tools, path, content):
        assert any(r.kind == "agent_consume" for r in env.journal.records.rows)
        return await write(tools, path, content)

    monkeypatch.setattr(WorkspaceTools, "write_file", admitted_write)
    try:
        posts, _, checkpoint = await lost_completion(env, monkeypatch)
        assert checkpoint["payload"]["version"] == 2
        consumes = [r.data for r in env.journal.records.rows if r.kind == "agent_consume"]
        assert len(consumes) == 1 and consumes[0]["payload"]["version"] == 1
        assert json.loads(posts[1])["messages"][-1]["role"] == "tool"
    finally:
        await env.model.client.close()


async def queue_original(env, monkeypatch, *, source="bridge_observation", next_tool=False):
    from firefly_forge import bridge_completions as completions_module
    from firefly_forge.agent_checkpoint_models import RestoreInput

    posts, intent, checkpoint = await lost_completion(
        env, monkeypatch, http_failure=source == "http_response", next_tool=next_tool
    )
    token = env.principal_context.set(env.principal)
    try:
        if source == "http_response":
            record = next(
                r.data
                for r in env.journal.records.rows
                if r.kind == "bridge_request_response" and r.idempotency_key == intent["idempotency_key"]
            )
            reference = {"kind": source, "record_digest": record["digest"]}
            body = record["payload"]["response"].encode()
        else:
            body, remote, client, observation_input = await retained_result(env, intent, next_tool=next_tool)
            monkeypatch.setattr(completions_module.httpx, "AsyncClient", client)
            observation = await env.service.completions.observe(RUN, intent["idempotency_key"], observation_input)
            reference = {
                "kind": source,
                "record_digest": observation["digest"],
                "observation_id": observation_input.observation_id,
                "terminal_digest": remote["terminal"]["digest"],
            }
        request = RestoreInput.model_validate(
            {
                "request_id": "b" * 32,
                "expected_checkpoint_digest": checkpoint["digest"],
                "expected_intent_digest": intent["digest"],
                "source": reference,
            }
        )
        ticket = await env.restores.request(RUN, intent["idempotency_key"], request)
        assert await env.restores.request(RUN, intent["idempotency_key"], request) == ticket
    finally:
        env.principal_context.reset(token)
    return posts, intent, checkpoint, body, request


@pytest.mark.parametrize("drift", ["operator", "source", "generation", "owner", "deadline"])
async def test_restore_refuses_late_admission_drift_before_result_exposure(tmp_path, monkeypatch, drift):
    from firefly_forge import agent_restores as restore_module

    env = await activated(tmp_path, monkeypatch)
    try:
        posts, _, _, _, request = await queue_original(env, monkeypatch)
        admit = env.store.admit_credentials
        changed = []

        async def late_change(value):
            await admit(value)
            if not changed and isinstance(value, tuple) and len(value) == 3:
                assert current_unit_of_work() is None
                changed.append(drift)
                if drift == "operator":
                    env.user.enabled = False
                elif drift == "source":
                    (env.workspace / "service.py").write_text("DRIFT = 2\n")
                elif drift == "generation":
                    env.requests.records.rows[0].data["generation"] = "e" * 32
                elif drift == "owner":
                    env.store._ownership_pid = 54321
                else:
                    current = restore_module.time.time()
                    monkeypatch.setattr(restore_module.time, "time", lambda: current + 10000)

        env.store.admit_credentials = late_change
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(request.request_id)
        assert changed == [drift]
        assert len(posts) == 2
        assert not any(
            r.kind == "agent_consume" and r.data["payload"]["version"] == 2 for r in env.journal.records.rows
        )
        assert env.store.run.data["status"] == "needs_reconciliation"
        assert (await env.requests.get(request.request_id))["status"] == "needs_reconciliation"
    finally:
        await env.model.client.close()


@pytest.mark.parametrize("window", ["before-consume", "after-consume", "after-new-tool"])
async def test_restore_consume_does_not_rearm_after_interruption(tmp_path, monkeypatch, window):
    from firefly_forge.agent_restores import RestoreExecution
    from firefly_forge.controllers import RunService

    env = await activated(tmp_path, monkeypatch)
    try:
        posts, _, _, _, request = await queue_original(env, monkeypatch, next_tool=window == "after-new-tool")
        consume = RestoreExecution.consume

        async def cut(restoration, checkpoint_digest, response_digest):
            if window == "after-consume":
                await consume(restoration, checkpoint_digest, response_digest)
            raise RuntimeError("Fictional interruption before parser exposure")

        if window == "after-new-tool":
            save = env.journal.records.save

            async def refuse_next_checkpoint(record):
                if record.kind == "agent_checkpoint" and record.data["payload"]["invocation"]["ordinal"] == 3:
                    raise RuntimeError("Fictional interruption after a genuinely new file tool")
                await save(record)

            monkeypatch.setattr(env.journal.records, "save", refuse_next_checkpoint)
        else:
            monkeypatch.setattr(RestoreExecution, "consume", cut)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(request.request_id)
        used = [r for r in env.journal.records.rows if r.kind == "agent_consume" and r.data["payload"]["version"] == 2]
        assert len(used) == int(window != "before-consume")
        assert (env.workspace / "later.py").exists() == (window == "after-new-tool")
        assert env.store.run.data["bridge_completion_hold"] and len(posts) == 2
        assert env.store.run.data.get("summary") is None
        with pytest.raises(ValueError, match="graph checkpoint"):
            await RunService(env.store, env.engine).retry(RUN)
        assert env.store.created == []
        await env.restores.execute(request.request_id)
        assert len(posts) == 2 and len([r for r in env.journal.records.rows if r.kind == "agent_consume"]) == 1 + len(
            used
        )
    finally:
        await env.model.client.close()


async def test_restore_final_real_credential_scan_refuses_late_callback_secret(tmp_path, monkeypatch):
    from types import MethodType

    from firefly_forge import agentic_checkpoint_invocation as invocation_module
    from firefly_forge.store import Store

    env = await activated(tmp_path, monkeypatch)
    try:
        posts, _, _, _, request = await queue_original(env, monkeypatch)
        registered = {}
        checked = invocation_module.checked
        acquire = type(env.store.configuration.lock).acquire

        def credentials():
            assert current_unit_of_work() is None
            return {**env.store.configuration.resolve("bridge"), **registered}

        def outside_lock(lock, *args, **kwargs):
            assert current_unit_of_work() is None
            return acquire(lock, *args, **kwargs)

        async def change_after_consume(argv, cwd, **kwargs):
            value = await checked(argv, cwd, **kwargs)
            if any(r.kind == "agent_consume" and r.data["payload"]["version"] == 2 for r in env.journal.records.rows):
                registered["CURRENT_TOKEN"] = "Original retained final report"
            return value

        env.store.credential_registry = credentials
        env.store.reject_credentials = MethodType(Store.reject_credentials, env.store)
        monkeypatch.setattr(type(env.store.configuration.lock), "acquire", outside_lock)
        monkeypatch.setattr(invocation_module, "checked", change_after_consume)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(request.request_id)
        assert registered and len(posts) == 2
        assert env.store.run.data.get("summary") is None
        assert (
            sum(r.kind == "agent_consume" and r.data["payload"]["version"] == 2 for r in env.journal.records.rows) == 1
        )
        assert (await env.requests.get(request.request_id))["status"] == "needs_reconciliation"
    finally:
        await env.model.client.close()


async def test_legacy_checkpoint_cannot_authorize_a_restore_ticket(tmp_path, monkeypatch):
    from firefly_forge.agent_checkpoint_models import RestoreInput

    _, _, _, service, scope, descriptor, _, body, _ = await setup(tmp_path, monkeypatch)
    intent = await scope.admit(descriptor, body)
    checkpoint = await service._record(RUN, descriptor["request_id"], "agent_checkpoint")
    request = RestoreInput.model_validate(
        {
            "request_id": "b" * 32,
            "expected_checkpoint_digest": checkpoint["digest"],
            "expected_intent_digest": intent["digest"],
            "source": {"kind": "http_response", "record_digest": "e" * 64},
        }
    )
    with pytest.raises(ValueError, match="version-2 checkpoint"):
        await service.restore_bundle(RUN, descriptor["request_id"], request)


async def test_checkpoint_append_failure_rolls_back_before_real_transport_post(tmp_path, monkeypatch):
    env = await activated(tmp_path, monkeypatch)
    posts = []
    original = env.journal.records.save

    async def refuse_checkpoint(record):
        if record.kind == "agent_checkpoint":
            raise ValueError("Fictional checkpoint append refusal")
        await original(record)

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        posts.append(request.content)
        pytest.fail("A refused checkpoint must not send a POST")

    env.journal.records.save = refuse_checkpoint
    env.model.client._client._transport = httpx2.MockTransport(transport)
    env.model.client._client._mounts = {}
    try:
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.engine.execute_run(RUN)
        assert not posts and "bridge_completion_hold" not in env.store.run.data
        assert not any(r.kind in {"agent_checkpoint", "bridge_request_intent"} for r in env.journal.records.rows)
        assert "rollback" in env.manager.events
    finally:
        await env.model.client.close()


@pytest.mark.parametrize("profile", [False, True])
def test_blank_checkpoint_form_field_preserves_absence_and_saved_selection(tmp_path, profile):
    from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration
    from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles

    config = ConfigurationStore(tmp_path)
    profiles = ModelProfiles(config)

    def save(values):
        if profile:
            profiles.save("builder", ModelProfileInput(name="Builder", provider="bridge", values=values))
            return profiles.resolve("builder")[1]
        config.update("bridge", ProviderConfiguration(values=values))
        return config.resolve("bridge")

    assert "FORGE_AGENTIC_CHECKPOINTS" not in save({"checkpoints": ""})
    save({"completion_recovery": "required", "checkpoints": "required"})
    assert save({"model": "bridge/updated", "checkpoints": ""})["FORGE_AGENTIC_CHECKPOINTS"] == "required"
    assert save({"checkpoints": "disabled"})["FORGE_AGENTIC_CHECKPOINTS"] == "disabled"


def test_checkpoint_selection_preserves_absent_fields_and_refuses_unsupported_paths(tmp_path):
    from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration, validate_checkpoints

    config = ConfigurationStore(tmp_path)
    snapshot = config.snapshot("bridge")
    assert "FORGE_AGENTIC_CHECKPOINTS" not in config.load_snapshot(snapshot["config_revision"])
    assert "checkpoints" not in snapshot["execution_config"]
    with pytest.raises(ValueError, match="required completion recovery"):
        config.update("bridge", ProviderConfiguration(values={"checkpoints": "required"}))
    for provider, task in [("agentic", "implementation"), ("bridge", "review")]:
        with pytest.raises(ValueError, match="only Bridge implementation"):
            validate_checkpoints(
                {"FORGE_AGENTIC_CHECKPOINTS": "required", "FORGE_BRIDGE_COMPLETION_RECOVERY": "required"},
                provider=provider,
                task=task,
            )


@pytest.mark.parametrize("source", ["http_response", "bridge_observation"])
@pytest.mark.parametrize("next_tool", [False, True])
async def test_explicit_ticket_restores_actual_agent_without_prior_tool_or_post_replay(
    tmp_path, monkeypatch, source, next_tool
):
    env = await activated(tmp_path, monkeypatch)
    try:
        posts, intent, _, body, request = await queue_original(env, monkeypatch, source=source, next_tool=next_tool)

        async def snapshot(*args, **kwargs):
            return ["service.py"], "Fictional retained patch", "c" * 40

        def transport(request):
            if request.method == "GET":
                return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
            assert next_tool, "Original completion attempted a replacement POST"
            assert request.content != posts[-1]
            posts.append(request.content)
            return httpx2.Response(200, json=next_result(False))

        monkeypatch.setattr(env.engine, "_snapshot", snapshot)
        env.model.client._client._transport = httpx2.MockTransport(transport)
        before = (env.workspace / "service.py").stat().st_mtime_ns
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            if source == "http_response" and not next_tool:
                finished = asyncio.Event()
                finish = env.restores.finish

                async def worker_finished(*args, **kwargs):
                    await finish(*args, **kwargs)
                    finished.set()

                monkeypatch.setattr(env.restores, "finish", worker_finished)
                env.requests.runtime.executes_jobs = True
                try:
                    await env.requests.start()
                    await asyncio.wait_for(finished.wait(), timeout=5)
                    await asyncio.gather(*env.requests.active.values())
                finally:
                    await env.requests.stop()
            else:
                await env.restores.execute(request.request_id)
        assert len(posts) == (3 if next_tool else 2)
        assert (env.workspace / "service.py").stat().st_mtime_ns == before
        task = next(r for r in env.journal.tasks.rows if r.task_id == "implement")
        assert task.status == "done" and task.attempt == intent["payload"]["task_attempt"]
        assert env.store.run.data["summary"] == "Original retained final report"
        assert env.store.run.data["usage"]["requests"] == (3 if next_tool else 2)
        assert env.store.run.data["usage"]["files_written"] == (
            ["later.py", "service.py"] if next_tool else ["service.py"]
        )
        consume = next(
            r.data for r in env.journal.records.rows if r.kind == "agent_consume" and r.data["payload"]["version"] == 2
        )
        assert consume["payload"]["source"]["kind"] == source
        assert consume["payload"]["response_sha256"] == hashlib.sha256(body).hexdigest()
        assert (await env.requests.get(request.request_id))["status"] == "completed"
        assert env.store.run.data["bridge_completion_hold"]
    finally:
        await env.model.client.close()


async def test_required_checkpoint_cannot_post_through_an_unactivated_provider(tmp_path, monkeypatch):
    store, journal, _, service, *_ = await setup(tmp_path, monkeypatch)
    task = next(row for row in journal.tasks.rows if row.task_id == "implement")
    scope = service.completions.scope(await store.get_run(RUN), {"idempotency_key": task.data["idempotency_key"]})
    settings = store.configuration.load_snapshot(store.run.data["config_revision"])
    settings["FORGE_AGENTIC_CHECKPOINTS"] = "required"
    provider = BridgeProvider(settings)
    provider.configure_runtime(scope_root=tmp_path, repository_root=tmp_path, allowed_tools={"write_file"})
    model = provider.model()
    posts = []

    def respond(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        posts.append(request.content)
        return httpx2.Response(503, json={"error": {"code": "unavailable"}})

    model.client._client._transport = httpx2.MockTransport(respond)
    model.client._client._mounts = {}
    monkeypatch.setattr(provider, "model", lambda **kwargs: model)

    async def emit(*args):
        pass

    try:
        with (
            models.override_allow_model_requests(False),
            mock_model_requests(model),
            bridge_completion_scope(scope),
            pytest.raises(ValueError, match="checkpoint.*invocation"),
        ):
            await provider.execute(tmp_path, "Fictional text task", "pyfly", "none", emit)
        assert posts == []
    finally:
        await model.client.close()
