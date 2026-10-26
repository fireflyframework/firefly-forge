# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual controlled SDK/checkpoint/terminal producers; memory persistence, no SQL proof."""

import json
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import httpx
import httpx2
import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import activated, controlled_source_snapshot, result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests
from test_bridge_inflight_cancel import RUN
from test_checkpoint_mode_adoption_host import request

from firefly_forge.checkpoint_mode_adoptions import CheckpointModeAdoptions
from firefly_forge.project_access import AccessDenied, ProjectAccess


async def retained_mode_case(tmp_path, monkeypatch):
    env = await activated(tmp_path, monkeypatch)

    from test_sandbox_command_ordinals_host import Rows

    env.journal.sandboxes = Rows()
    token = env.principal_context.set(env.principal)
    env.token = token
    current = env.store.configuration.resolve("bridge")
    original = {
        k: v for k, v in current.items() if k not in {"FORGE_AGENTIC_CHECKPOINTS", "FORGE_BRIDGE_COMPLETION_RECOVERY"}
    }
    old_revision = uuid4().hex
    env.store.configuration._write(env.store.configuration.revisions / (old_revision + ".json"), original)
    await env.store.update(
        RUN,
        id=RUN,
        config_revision=old_revision,
        commands=[{"argv": ["controlled-check", str(index)], "timeout": 30} for index in range(7)],
        repair_budget=2,
        verification=[],
        active_task="implement",
    )
    prepare = await env.journal.intent(RUN, "prepare")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=env.workspace).decode().strip()
    await env.journal.receipt(RUN, prepare["idempotency_key"], {"base_revision": revision})
    await env.journal.intent(RUN, "implement")
    service = CheckpointModeAdoptions(env.store, env.journal, env.engine)

    # Persistence and ACL remain explicit controlled seams; all SDK/native producers are real.
    async def authorize(*args, **kwargs):
        if not env.user.enabled:
            raise ValueError("Controlled operator revoked")
        return True

    monkeypatch.setattr(ProjectAccess, "authorize_actor", authorize)
    monkeypatch.setattr(ProjectAccess, "authorize_execution", authorize)
    monkeypatch.setattr(ProjectAccess, "current_administrator", authorize)
    from checkpoint_mode_fixture import missing_mode_failure

    await missing_mode_failure(env.store, env.journal, env.workspace, old_revision, original)
    candidate = await service.candidate(RUN)
    env.adoption = await service.request(RUN, uuid4().hex, request(candidate))
    env.old_revision = old_revision
    return env


@pytest.mark.parametrize(
    "feedback,publication",
    [
        (False, None),
        (True, None),
        (False, "known"),
        (False, "revoked_before"),
        (False, "revoked_after_push"),
        (False, "checkpoint_read"),
        (False, "checkpoint_revoked"),
    ],
)
async def test_adopted_same_attempt_sdk_terminal_settles_before_native_qa(tmp_path, monkeypatch, feedback, publication):
    env = await retained_mode_case(tmp_path, monkeypatch)
    posts, checks = [], []

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        posts.append(request.content)
        assert len(posts) <= 2
        report = {"summary": "Controlled adopted report", "worker_feedback": None, "qa_report": None}
        if feedback:
            report["worker_feedback"] = {
                "id": "adopted-question",
                "role_id": "implementer",
                "task_id": "implement",
                "attempt": 2,
                "disposition": "blocked",
                "summary": "A current native decision is required",
                "evidence_paths": [],
                "request": {
                    "id": "adopted-request",
                    "kind": "other",
                    "question": "Keep value?",
                    "context": "Controlled known result",
                    "choices": ["Keep", "Review"],
                },
            }
        return httpx2.Response(200, json=result(tool=True) if len(posts) == 1 else result(json.dumps(report)))

    env.model.client._client._transport = httpx2.MockTransport(transport)
    env.model.client._client._mounts = {}

    async def snapshot(*args, **kwargs):
        return ["service.py"], controlled_source_snapshot("Controlled adopted change"), "c" * 40

    async def check(identifier, run, workspace, argv, timeout, task, **kwargs):
        checks.append((task, argv))
        assert not (await env.store.get_run(RUN)).get("bridge_completion_hold")
        return SimpleNamespace(exit_code=0, output="controlled pass")

    if feedback:
        monkeypatch.setattr(env.engine, "_snapshot", snapshot)
    monkeypatch.setattr(env.engine, "_project_command", check)
    try:
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.engine.execute_run(RUN)
        run = await env.store.get_run(RUN)
        assert len(posts) == 2, run.get("error")
        assert run["config_revision"] == env.old_revision
        records = await env.journal.entries(RUN)
        http = [e for e in records if e["kind"] == "bridge_request_intent"]
        assert len(http) == 2 and all(e["payload"]["task_attempt"] == 2 for e in http)
        assert all(
            e["payload"]["config_revision"] == env.adoption["payload"]["execution_config_revision"] for e in http
        )
        accepted = [e for e in records if e["kind"] == "terminal_completion_receipt"]
        assert len(accepted) == 1 and not run.get("bridge_completion_hold"), run.get("error")
        if feedback:
            assert not checks and run["status"] == "paused"
            assert accepted[0]["payload"]["producer"] == "native_worker_feedback"
        else:
            assert len(checks) == 7 and all(t == "verify" for t, _ in checks), run.get("error")
            assert accepted[0]["payload"]["producer"] == "native_implementation_receipt"
            assert run["status"] == "succeeded", run.get("error")
            assert len(run["final_verification"]) == 7 and run["checkpoint_revision"]
            assert any(e["task_id"] == "verify" and e["kind"] == "receipt" for e in records)
            assert any(e["task_id"] == "checkpoint" and e["kind"] == "receipt" for e in records)
            if publication is not None:
                await publish_after_completed_execution(env, monkeypatch, tmp_path, publication)
        assert len([e for e in records if e["kind"] == "checkpoint_mode_claim"]) == 1
    finally:
        await env.model.client.close()
        env.principal_context.reset(env.token)


async def publish_after_completed_execution(env, monkeypatch, tmp_path, phase):
    """Call the actual later publication method with controlled SCM/model transports."""
    if phase.startswith("checkpoint_"):
        before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=env.workspace)
        if phase == "checkpoint_revoked":
            env.user.enabled = False
            with pytest.raises((ValueError, AccessDenied)):
                await env.engine._checkpoint_commit(RUN, env.workspace)
        else:
            await env.engine._checkpoint_commit(RUN, env.workspace)
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=env.workspace) == before
        return
    from communication_model_fixture import reviewed_model
    from test_communication_writer import configured_store

    from firefly_forge import engine as engine_module
    from firefly_forge.communication import CommunicationWriter
    from firefly_forge.store import Store

    configured = await configured_store(tmp_path / "separate-communication-profile")
    env.store.task_router = configured.task_router
    env.store.update_publication = Store.update_publication.__get__(env.store)
    env.engine.communication_writer = CommunicationWriter(
        env.store,
        model=reviewed_model(
            {
                "title": {"text": "Update the controlled service", "fact_ids": ["publication"]},
                "paragraphs": [
                    {
                        "text": "The earlier verification batch contains a known failure. Seven current checks passed.",
                        "fact_ids": ["verification", "delivery", "factory_verification", "check_batch"],
                    }
                ],
            }
        ),
    )

    async def repository(identifier):
        return {
            "id": identifier,
            "provider": "github",
            "url": "https://github.com/org/controlled",
            "default_branch": "main",
        }

    env.store.repository = repository
    await env.store.update(RUN, pr_url="")
    effects = []

    async def push(*args, **kwargs):
        effects.append("push")
        if phase == "revoked_after_push":
            env.user.enabled = False

    original_client = httpx.AsyncClient

    def transport(request):
        effects.append("pr")
        return httpx.Response(201, json={"html_url": "https://github.com/org/controlled/pull/1"})

    monkeypatch.setattr(engine_module, "push", push)
    monkeypatch.setattr(
        engine_module.httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(transport), **kw)
    )
    monkeypatch.setenv("FORGE_GITHUB_TOKEN", "controlled-publication-transport-token")
    if phase == "revoked_before":
        env.user.enabled = False
    if phase == "known":
        try:
            url = await env.engine.publish(RUN)
        except ValueError:
            current = await env.store.get_run(RUN)
            raise AssertionError(current.get("publication_draft", {}).get("communication", {}).get("reason")) from None
        assert url == "https://github.com/org/controlled/pull/1"
        assert effects == ["push", "pr"]
        records = await env.journal.entries(RUN)
        assert any(e["kind"] == "receipt" and e["task_id"] == "publish" for e in records)
        assert (await env.store.get_run(RUN))["config_revision"] == env.old_revision
    else:
        with pytest.raises((ValueError, AccessDenied)):
            await env.engine.publish(RUN)
        assert effects == (["push"] if phase == "revoked_after_push" else [])
        assert not any(e.kind == "receipt" and e.task_id == "publish" for e in env.journal.records.rows)


async def test_adopted_checkpoint_restore_prepares_modes_before_claim_and_consumes_original_once(tmp_path, monkeypatch):
    from test_agentic_checkpoint_activation_host import queue_original

    from firefly_forge.configuration import ProviderConfiguration

    env = await retained_mode_case(tmp_path, monkeypatch)
    try:
        posts, intent, checkpoint, body, request = await queue_original(env, monkeypatch, source="http_response")
        before = (env.workspace / "service.py").stat().st_mtime_ns
        env.store.configuration.update("bridge", ProviderConfiguration(values={"checkpoints": "disabled"}))
        with pytest.raises(ValueError):
            await env.restores.execute(request.request_id)
        assert (await env.requests.get(request.request_id))["status"] == "queued"
        assert not any(e["kind"] == "worker_claim" for e in await env.requests.events(request.request_id))
        env.store.configuration.update("bridge", ProviderConfiguration(values={"checkpoints": "required"}))

        def transport(request):
            if request.method == "GET":
                return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
            raise AssertionError("Original adopted completion must never be reposted")

        env.model.client._client._transport = httpx2.MockTransport(transport)

        async def check(*args, **kwargs):
            return SimpleNamespace(exit_code=0, output="controlled restored QA")

        monkeypatch.setattr(env.engine, "_project_command", check)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(request.request_id)
        assert (await env.requests.get(request.request_id))["status"] == "completed"
        run = await env.store.get_run(RUN)
        assert run["status"] == "succeeded" and run["config_revision"] == env.old_revision, run.get("error")
        assert len(posts) == 2 and (env.workspace / "service.py").stat().st_mtime_ns == before
        records = await env.journal.entries(RUN)
        assert len([e for e in records if e["kind"] == "checkpoint_mode_claim"]) == 1
        assert len([e for e in records if e["kind"] == "bridge_request_intent"]) == 2
        restored = [e for e in records if e["kind"] == "agent_consume" and e["payload"]["version"] == 2]
        assert len(restored) == 1 and restored[0]["payload"]["restore_request_id"] == request.request_id
        assert restored[0]["payload"]["checkpoint_record_digest"] == checkpoint["digest"]
        assert intent["payload"]["config_revision"] == env.adoption["payload"]["execution_config_revision"]
        assert body
    finally:
        await env.model.client.close()
        env.principal_context.reset(env.token)
