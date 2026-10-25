# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real retained SDK restoration plus terminal acceptance; owned memory backend."""

import json

import httpx2
import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import activated, queue_original, result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests
from test_bridge_inflight_cancel import RUN
from test_native_terminal_completion_host import blocked_feedback


@pytest.mark.parametrize("source", ["http_response", "bridge_observation"])
async def test_restored_blocked_feedback_completes_ticket_with_atomic_native_pause(tmp_path, monkeypatch, source):
    import importlib

    from test_agentic_checkpoint_activation_host import __name__ as activation_name

    activation = importlib.import_module(activation_name)
    monkeypatch.setattr(
        activation,
        "next_result",
        lambda _: result(
            json.dumps(
                {
                    "summary": "Original retained final report",
                    "worker_feedback": blocked_feedback(),
                    "qa_report": None,
                }
            )
        ),
    )
    env = await activated(tmp_path, monkeypatch)
    try:
        posts, _, _, _, request = await queue_original(env, monkeypatch, source=source)
        before = (env.workspace / "service.py").stat().st_mtime_ns

        def transport(request):
            assert request.method == "GET", "A restored final response cannot issue a replacement POST"
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})

        env.model.client._client._transport = httpx2.MockTransport(transport)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(request.request_id)
        run = await env.store.get_run(RUN)
        assert run["status"] == "paused", (
            [r.kind for r in env.journal.records.rows if r.kind in {"worker_feedback", "terminal_completion_receipt"}],
            (await env.requests.get(request.request_id))["result"],
        )
        assert run["worker_request"]["status"] == "pending"
        assert not run["bridge_completion_hold"]
        assert (await env.requests.get(request.request_id))["status"] == "completed"
        assert len(posts) == 2
        assert (env.workspace / "service.py").stat().st_mtime_ns == before
        receipt = next(r.data for r in env.journal.records.rows if r.kind == "terminal_completion_receipt")
        assert receipt["payload"]["producer"] == "native_worker_feedback"
        restored = next(item for item in receipt["payload"]["proof"]["completions"] if item.get("consume_version") == 2)
        assert restored["restore_request_id"] == request.request_id
        assert restored["source_kind"] == source
    finally:
        await env.model.client.close()


@pytest.mark.parametrize("change", ["generation", "ticket_scope", "claim", "source", "lease", "authority", "rollback"])
async def test_changed_restored_custody_never_accepts_or_replays_original_result(tmp_path, monkeypatch, change):
    from test_agentic_checkpoint_activation_host import controlled_source_snapshot

    env = await activated(tmp_path, monkeypatch)
    try:
        posts, _, _, _, request = await queue_original(env, monkeypatch, source="http_response")
        original_admit = env.store.admit_credentials
        revoked = False
        access = env.restores.access

        async def current_access(*args, **kwargs):
            if revoked:
                raise ValueError("Controlled current restore authority revoked")
            return await access(*args, **kwargs)

        monkeypatch.setattr(env.restores, "access", current_access)

        async def alter(value):
            nonlocal revoked
            await original_admit(value)
            if not isinstance(value, dict) or "report" not in value or "checkpoint" not in value:
                return
            row = next(row for row in env.requests.records.rows if row.id.hex == request.request_id)
            if change == "generation":
                row.data["generation"] = "e" * 32
            elif change == "ticket_scope":
                row.data["request"]["input"]["expected_intent_digest"] = "d" * 64
            elif change == "claim":
                event = next(row for row in env.requests.journal.rows if row.data["kind"] == "worker_claim")
                event.data["evidence"]["owner_pid"] += 1
            elif change == "source":
                env.store.run.data["source_tree"] = "d" * 40
            elif change == "lease":
                env.store.execution_ownership_token = object()
            elif change == "authority":
                revoked = True

        env.store.admit_credentials = alter
        append = env.journal._append

        async def reject_insert(run, task, key, kind, payload):
            if kind == "terminal_completion_receipt" and change == "rollback":
                raise ValueError("Controlled restored terminal receipt storage failure")
            return await append(run, task, key, kind, payload)

        monkeypatch.setattr(env.journal, "_append", reject_insert)

        async def snapshot(*args, **kwargs):
            return ["service.py"], controlled_source_snapshot("Controlled restored source"), "c" * 40

        monkeypatch.setattr(env.engine, "_snapshot", snapshot)

        def no_replacement(request):
            assert request.method == "GET", "A retained response cannot be replaced"
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})

        env.model.client._client._transport = httpx2.MockTransport(no_replacement)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(request.request_id)
        run = await env.store.get_run(RUN)
        assert run["bridge_completion_hold"]
        assert len(posts) == 2
        assert (
            len(
                [
                    row
                    for row in env.journal.records.rows
                    if row.kind == "agent_consume" and row.data["payload"]["version"] == 2
                ]
            )
            == 1
        )
        assert not any(row.kind == "terminal_completion_receipt" for row in env.journal.records.rows)
        assert not any(row.kind == "receipt" and row.task_id == "implement" for row in env.journal.records.rows)
    finally:
        await env.model.client.close()
