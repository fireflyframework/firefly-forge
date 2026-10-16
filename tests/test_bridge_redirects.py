# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline real-SDK controls; every HTTP request terminates in MockTransport."""

import httpx2
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from firefly_forge.providers import BridgeProvider, ProviderOutcomeUnknown


@pytest.mark.parametrize("status", [307, 308])
async def test_sdk_retries_disabled_does_not_authorize_a_redirected_completion_post(status):
    model = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}).model()
    client = model.client
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx2.Response(status, headers={"location": "/v1/redirected/chat/completions"})
        return httpx2.Response(
            200,
            json={
                "id": "fictional",
                "object": "chat.completion",
                "created": 1,
                "model": "bridge/default",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Fictional"}}
                ],
            },
        )

    # Keep the actual SDK client's redirect configuration; replace only its I/O.
    client._client._transport = httpx2.MockTransport(respond)
    client._client._mounts = {}
    try:
        caught = None
        try:
            await model.request([ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters())
        except Exception as error:
            caught = error
        assert client.max_retries == 0
        assert len(calls) == 1, "The SDK forwarded a second completion POST after the redirect"
        assert calls[0].method == "POST"
        assert isinstance(caught, ProviderOutcomeUnknown)
    finally:
        await client.close()


@pytest.mark.parametrize("status", [307, 308])
async def test_native_sdk_redirect_positive_proves_retries_are_a_separate_control(status):
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url="http://localhost:19191/v1", api_key="fictional", max_retries=0)
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx2.Response(status, headers={"location": "/v1/redirected/chat/completions"})
        return httpx2.Response(
            200,
            json={
                "id": "fictional",
                "object": "chat.completion",
                "created": 1,
                "model": "bridge/default",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Fictional"}}
                ],
            },
        )

    client._client._transport = httpx2.MockTransport(respond)
    client._client._mounts = {}
    try:
        result = await client.chat.completions.create(
            model="bridge/default", messages=[{"role": "user", "content": "Offline"}]
        )
        assert result.choices[0].message.content == "Fictional"
        assert client.max_retries == 0
        assert client._client.follow_redirects is True
        assert len(calls) == 2 and all(request.method == "POST" for request in calls)
    finally:
        await client.close()


async def test_bridge_503_keeps_sdk_retries_disabled_with_redirect_guard():
    from firefly_forge.providers import ProviderOutcomeUnknown

    model = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}).model()
    client = model.client
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(503, json={"error": {"code": "unavailable", "message": "Fictional"}})

    client._client._transport = httpx2.MockTransport(respond)
    client._client._mounts = {}
    try:
        with pytest.raises(ProviderOutcomeUnknown):
            await model.request([ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters())
        assert client.max_retries == 0
        assert len(calls) == 1 and calls[0].method == "POST"
    finally:
        await client.close()


async def test_bridge_send_overrides_an_explicit_sdk_redirect_option():
    from openai import APIStatusError

    model = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}).model()
    client = model.client
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx2.Response(307, headers={"location": "/v1/redirected/chat/completions"})
        return httpx2.Response(200, json={"redirected": True})

    client._client._transport = httpx2.MockTransport(respond)
    client._client._mounts = {}
    try:
        try:
            await client.post(
                "/chat/completions", cast_to=dict, body={"model": "bridge/default"}, options={"follow_redirects": True}
            )
        except APIStatusError as error:
            assert error.status_code == 307
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("status", [307, 308])
async def test_redirect_reaches_real_agent_engine_as_retained_unknown_not_retryable_failure(
    tmp_path, monkeypatch, status
):
    from test_bridge_inflight_cancel import RUN, MemoryStore, Rows

    from firefly_forge import engine as engine_module
    from firefly_forge.controllers import RunService
    from firefly_forge.engine import Engine
    from firefly_forge.journal import Journal

    store = MemoryStore(tmp_path / "not-a-database")
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=Rows())
    provider = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"})
    model = provider.model()
    calls = []

    def respond(request):
        calls.append(request)
        assert len(calls) == 1, "No redirected inference may be submitted"
        return httpx2.Response(status, headers={"location": "/v1/redirected/chat/completions"})

    model.client._client._transport = httpx2.MockTransport(respond)
    model.client._client._mounts = {}
    monkeypatch.setattr(provider, "model", lambda **kwargs: model)
    engine = Engine(store, tmp_path / "workspaces", lambda _: provider, journal=journal)
    workspace = engine.workspaces / RUN
    workspace.mkdir()
    (workspace / "partial.txt").write_text("Retained partial work\n")

    async def inspect(*args):
        return {"base_revision": "b" * 40, "head": "b" * 40, "source_tree": "c" * 40}

    async def checked(argv, *args, **kwargs):
        assert argv == ["git", "ls-files", "-z"]
        return ""

    monkeypatch.setattr(engine, "_inspect_workspace", inspect)
    monkeypatch.setattr(engine_module, "checked", checked)
    try:
        await engine.execute_run(RUN)
        run = await store.get_run(RUN)
        assert len(calls) == 1
        assert run["status"] == "needs_reconciliation"
        assert run["pause"]["kind"] == "provider_outcome_unknown"
        implementation = next(row for row in journal.tasks.rows if row.task_id == "implement")
        assert implementation.status == "needs_reconciliation"
        error = next(row.data for row in journal.records.rows if row.kind == "error")
        assert error["idempotency_key"] == implementation.data["idempotency_key"]
        assert error["payload"]["automatic"] is False and error["payload"]["retry_at"] is None
        assert Journal.verify_chain([row.data for row in journal.records.rows])
        assert (workspace / "partial.txt").read_text() == "Retained partial work\n"
        with pytest.raises(ValueError, match="Bridge.*reconcil"):
            await journal.resume(RUN)
        with pytest.raises(ValueError, match="Bridge.*reconcil"):
            await RunService(store, engine).retry(RUN)
        assert len(calls) == 1 and store.created == []
    finally:
        await model.client.close()
