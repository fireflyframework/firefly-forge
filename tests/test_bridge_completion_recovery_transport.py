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
        with pytest.raises(ProviderOutcomeUnknown):
            await model.request([ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters())
        assert client.max_retries == 0
        assert len(calls) == 1, "The SDK forwarded a second completion POST after the redirect"
        assert calls[0].method == "POST"
        assert calls[0].url.path == "/v1/chat/completions"
    finally:
        await client.close()
