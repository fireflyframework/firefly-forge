# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline real-SDK negotiation; all requests terminate in the recorded transport."""

import json

import httpx2
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from firefly_forge.providers import BridgeProvider, ProviderAdmissionRefused
from firefly_forge.work_duration_models import WorkDurationPolicyV2

CAPABILITY = {
    "version": 1,
    "protocol": "firefly-bridge-work-duration-v1",
    "nullableTimeout": True,
    "frozenAtAdmission": True,
}
RESPONSE = {
    "id": "fictional",
    "object": "chat.completion",
    "created": 1,
    "model": "bridge/default",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}],
}


@pytest.mark.parametrize("chosen", [None, 864000])
async def test_selected_new_duration_is_negotiated_and_forwarded_without_sdk_read_cap(tmp_path, chosen):
    provider = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"})
    provider.configure_runtime(
        scope_root=tmp_path, repository_root=tmp_path, work_duration=WorkDurationPolicyV2(timeout_seconds=chosen)
    )
    model = provider.model()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(200, json={"workDuration": CAPABILITY} if request.method == "GET" else RESPONSE)

    model.client._client._transport = httpx2.MockTransport(respond)
    model.client._client._mounts = {}
    try:
        await model.request([ModelRequest(parts=[UserPromptPart("New owned job")])], None, ModelRequestParameters())
        assert [request.method for request in calls] == ["GET", "POST"]
        assert calls[0].url.path == "/v1/bridge/status"
        bridge = json.loads(calls[1].content)["bridge"]
        assert bridge["workDuration"] == {"version": 1, "timeoutMs": None if chosen is None else chosen * 1000}
        assert bridge["waitTimeoutMs"] is None
        assert model.client.timeout.read is None and model.client.timeout.connect == 5
        assert calls[-1].extensions["timeout"]["read"] is None
    finally:
        await model.client.close()


@pytest.mark.parametrize(
    "capability", [None, {**CAPABILITY, "nullableTimeout": False}, {**CAPABILITY, "version": True}]
)
async def test_legacy_or_malformed_bridge_cannot_silently_apply_a_hidden_work_cap(tmp_path, capability):
    provider = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"})
    provider.configure_runtime(scope_root=tmp_path, repository_root=tmp_path, work_duration=WorkDurationPolicyV2())
    model = provider.model()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(200, json={"workDuration": capability} if request.method == "GET" else RESPONSE)

    model.client._client._transport = httpx2.MockTransport(respond)
    model.client._client._mounts = {}
    try:
        with pytest.raises(ProviderAdmissionRefused, match="duration"):
            await model.request([ModelRequest(parts=[UserPromptPart("No downgrade")])], None, ModelRequestParameters())
        assert [request.method for request in calls] == ["GET"]
    finally:
        await model.client.close()
