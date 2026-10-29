# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real SDK requests over memory transport; no provider or SQL acceptance claim."""

import json

import httpx2
import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import activated, result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests
from test_bridge_inflight_cancel import RUN

from firefly_forge.agentic_checkpoint_invocation import CheckpointInvocation, checkpoint_invocation
from firefly_forge.agentic_runtime import execute_agentic
from firefly_forge.checkpoint_mode_custody import validate_closed_custody
from firefly_forge.work_duration_models import WorkDurationPolicyV2
from firefly_forge.workspace_tools import ToolRuntimeContext


async def test_sdk_owns_unbounded_work_and_closes_real_v3_custody_without_reset(tmp_path, monkeypatch):
    env = await activated(tmp_path, monkeypatch)
    policy = WorkDurationPolicyV2()
    value = policy.model_dump(mode="json")
    env.store.run.data.update(work_duration=value, status="developing", active_task="implement")
    env.store.run.status = "developing"
    env.store.run.data["sandbox_snapshot"] = {"configuration": {"work_duration": value}}
    run = await env.store.get_run(RUN)
    task = await env.journal.intent(RUN, "implement", payload={"config_revision": run["config_revision"]})
    guards = []

    async def guard():
        await env.store.assert_ownership()
        guards.append(True)

    async def emit(*args):
        pass

    posts = []

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        posts.append(request.content)
        assert len(posts) <= 2
        return httpx2.Response(
            200,
            json=result(tool=True)
            if len(posts) == 1
            else result(json.dumps({"summary": "done", "worker_feedback": None, "qa_report": None})),
        )

    env.model.client._client._transport = httpx2.MockTransport(transport)
    env.model.client._client._mounts = {}
    invocation = CheckpointInvocation(env.store, env.journal, run, task, env.workspace, guard)
    context = ToolRuntimeContext(
        env.workspace, env.workspace, allowed_tools=frozenset({"write_file"}), work_duration=policy
    )
    token = env.principal_context.set(env.principal)
    try:
        with (
            checkpoint_invocation(invocation),
            models.override_allow_model_requests(False),
            mock_model_requests(env.model),
        ):
            summary, usage = await execute_agentic(
                env.workspace,
                "Fictional owned work",
                "pyfly",
                "none",
                emit,
                model=env.model,
                runtime_context=context,
                allow_output_token_cap=False,
                settings={
                    "FORGE_AGENTIC_CHECKPOINTS": "required",
                    "FORGE_BRIDGE_COMPLETION_RECOVERY": "required",
                    "FORGE_PROVIDER_TIMEOUT": "1",
                },
            )
        assert summary == "done" and len(posts) == 2 and guards
        rows = [row.data for row in env.journal.records.rows]
        checkpoints = [row for row in rows if row["kind"] == "agent_checkpoint"]
        consumes = [row for row in rows if row["kind"] == "agent_consume"]
        assert len(checkpoints) == len(consumes) == 2
        assert all(
            row["payload"]["version"] == 3 and row["payload"]["invocation"]["work_deadline_ms"] is None
            for row in checkpoints
        )
        from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint

        final = checkpoints[-1]["payload"]
        captured = BoundaryCheckpoint.decode(final["snapshot"].encode(), final["checkpoint_digest"])
        assert captured.usage.requests == 1 and captured.usage.tool_calls == 1
        assert (env.workspace / "service.py").read_text() == "VALUE = 1\n"
        refs = validate_closed_custody(
            rows,
            RUN,
            env.store.configuration.load_snapshot,
            project_id=run.get("project_id"),
            repository_id=run["repository_id"],
        )
        assert len(refs) == 2
        from test_agent_checkpoint_candidates_host import selectors

        await selectors(env)
        page = await env.restores.candidates(RUN)
        assert len(page["items"]) == 2
        from firefly_forge.agent_checkpoint_models import RestoreInput

        last = checkpoints[-1]
        original_intent = next(
            row
            for row in rows
            if row["kind"] == "bridge_request_intent" and row["idempotency_key"] == last["idempotency_key"]
        )
        original_response = next(
            row
            for row in rows
            if row["kind"] == "bridge_request_response" and row["idempotency_key"] == last["idempotency_key"]
        )
        selection = RestoreInput(
            request_id="f" * 32,
            expected_checkpoint_digest=last["digest"],
            expected_intent_digest=original_intent["digest"],
            source={"kind": "http_response", "record_digest": original_response["digest"]},
        )
        bundle = await env.service.restore_bundle(RUN, last["idempotency_key"], selection)
        assert bundle[1] == last and bundle[3] == original_response["payload"]["response"].encode()
        with pytest.raises(ValueError, match="already consumed"):
            await env.service.consume_restored(run, last["idempotency_key"], selection, "e" * 32, guard)
        assert len(posts) == 2 and [row.data for row in env.journal.records.rows] == rows
        assert all(
            item["invocation_protocol"] == "forge-owned-checkpoint-v3"
            and item["work_duration"] == value
            and item["deadline_ms"] is None
            for item in page["items"]
        )
    finally:
        env.principal_context.reset(token)
        await env.model.client.close()
