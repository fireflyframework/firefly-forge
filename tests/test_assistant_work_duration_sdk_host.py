# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real agent/SDK calls terminate only in owned in-memory HTTP transports."""

import asyncio
import json

import httpx
import httpx2
import pytest
from pydantic_ai import models

from firefly_forge import product_assistant_runtime as runtime
from firefly_forge.model_operation_client import ModelOperationClient
from firefly_forge.providers import APIProvider
from firefly_forge.work_duration_models import WorkDurationPolicyV2

CAPABILITY = {
    "version": 1,
    "protocol": "firefly-bridge-work-duration-v1",
    "nullableTimeout": True,
    "frozenAtAdmission": True,
}


def response():
    return {
        "id": "controlled-assistant",
        "object": "chat.completion",
        "created": 1,
        "model": "fixture",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": '{"message":"Controlled structured response"}',
                },
            }
        ],
    }


@pytest.mark.parametrize("provider_name", ["bridge", "agentic"])
@pytest.mark.parametrize("chosen", [None, 864000])
async def test_new_policy_reaches_real_agent_and_owned_sdk_without_hidden_read_cap(monkeypatch, provider_name, chosen):
    import openai

    calls, clients = [], []

    async def respond(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"workDuration": CAPABILITY})
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=response())

    original = openai.AsyncOpenAI

    class BridgeClient(original):
        def __init__(self, **kwargs):
            if "http_client" in kwargs:
                super().__init__(**kwargs)
                return
            http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            clients.append(http)
            super().__init__(**kwargs, http_client=http)

    class ControlledAPI(APIProvider):
        def model(self, **kwargs):
            async def api_response(request):
                received = await respond(request)
                return httpx2.Response(received.status_code, json=received.json())

            http = ModelOperationClient(transport=httpx2.MockTransport(api_response))
            clients.append(http)
            kwargs["http_client"] = http
            # Prompted output makes this offline response independent of tool-call JSON.
            model = super().model(**kwargs)
            return model

    class ControlledBridgeHTTP(ModelOperationClient):
        def __init__(self):
            async def bridge_response(request):
                received = await respond(request)
                return httpx2.Response(received.status_code, json=received.json())

            super().__init__(transport=httpx2.MockTransport(bridge_response))
            clients.append(self)

    real_wait = asyncio.wait_for

    async def short_legacy_wait(future, timeout):
        return await real_wait(future, 0.02 if timeout == 300 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", short_legacy_wait)
    monkeypatch.setattr(openai, "AsyncOpenAI", BridgeClient)
    monkeypatch.setattr(runtime, "APIProvider", ControlledAPI)
    monkeypatch.setattr(runtime, "ModelOperationClient", ControlledBridgeHTTP)
    policy = WorkDurationPolicyV2(timeout_seconds=chosen)
    settings = {
        "ASSISTANT_PROVIDER": provider_name,
        "ASSISTANT_WORK_DURATION_POLICY": policy.model_dump_json(),
        "FORGE_BRIDGE_URL": "http://127.0.0.1:19191",
        "FORGE_BRIDGE_TOKEN": "fictional-fixture",
        "FORGE_AGENTIC_MODEL": "openai-chat:fixture",
        "FORGE_AGENTIC_API_KEY": "fictional-fixture",
    }
    if provider_name == "agentic":
        import pyfly.agentic.agents as agents
        from pydantic_ai import PromptedOutput

        original_agent = agents.Agent

        def prompted_agent(**kwargs):
            kwargs["output_type"] = PromptedOutput(kwargs["output_type"])
            return original_agent(**kwargs)

        monkeypatch.setattr(agents, "Agent", prompted_agent)
    with models.override_allow_model_requests(True):
        reply = await runtime.design_reply("Design the fictional fixture", settings)
    assert reply.message == "Controlled structured response"
    posts = [item for item in calls if item.method == "POST"]
    assert len(posts) == 1
    assert posts[0].extensions["timeout"]["read"] is None
    if provider_name == "bridge":
        assert json.loads(posts[0].content)["bridge"]["workDuration"] == {
            "version": 1,
            "timeoutMs": None if chosen is None else chosen * 1000,
        }
        assert [request.method for request in calls] == ["GET", "POST"]
    assert clients and all(client.is_closed for client in clients)
