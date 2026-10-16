# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real SDK transport under scoped callbacks; no sockets or native persistence claims."""

import asyncio
import hashlib

import httpx2
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from firefly_forge.bridge_completion_scope import (
    BridgeCompletionScope,
    bridge_completion_scope,
    current_completion_scope,
)
from firefly_forge.providers import BridgeProvider, ProviderAdmissionRefused, ProviderError, classify_provider_failure

CAPABILITY = {
    "version": 1,
    "principal_id": "fixture-principal",
    "header": "X-Bridge-Completion-Recovery",
    "value": "1",
    "request_id_pattern": "^fbr1_[a-f0-9]{32}$",
    "limits": {"admissions": 1000, "admissionBytes": 4096, "terminalBytes": 262144, "retainedBytes": 16777216},
}
RESULT = {
    "id": "fixture",
    "object": "chat.completion",
    "created": 1,
    "model": "bridge/default",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Original response"}}
    ],
}


async def call(model, text="fictional"):
    return await model.request([ModelRequest(parts=[UserPromptPart(text)])], None, ModelRequestParameters())


def fixture_model(respond):
    model = BridgeProvider(
        {
            "FORGE_BRIDGE_URL": "http://localhost:19191",
            "FORGE_BRIDGE_TOKEN": "fictional-token",
            "FORGE_BRIDGE_COMPLETION_RECOVERY": "required",
        }
    ).model()
    model.client._client._transport = httpx2.MockTransport(respond)
    model.client._client._mounts = {}
    return model


def callbacks(events, *, deny=None):
    async def prepare(body):
        events.append(("prepare", hashlib.sha256(body).hexdigest()))
        if deny == "prepare":
            raise ValueError("credential admission refused")

    async def admit(descriptor, body):
        events.append(("intent", descriptor))
        assert descriptor["wire_sha256"] == hashlib.sha256(body).hexdigest()
        assert descriptor["wire_bytes"] == len(body)
        if deny == "intent":
            raise ValueError("capacity refused")
        return {"digest": "d" * 64, "payload": descriptor}

    async def guard(intent, body):
        events.append(("guard", intent))
        if deny == "guard":
            raise ValueError("current authority refused")

    async def response(intent, body):
        events.append(("response", hashlib.sha256(body).hexdigest()))

    return BridgeCompletionScope(prepare, admit, guard, response)


@pytest.mark.parametrize("deny", ["prepare", "intent", "guard"])
async def test_refused_native_callback_never_dispatches_a_completion(deny):
    calls, events = [], []

    def respond(request):
        calls.append(request.method)
        return httpx2.Response(200, json={"completionRecovery": CAPABILITY})

    model = fixture_model(respond)
    try:
        with bridge_completion_scope(callbacks(events, deny=deny)), pytest.raises(ValueError):
            await call(model)
        assert "POST" not in calls
        assert current_completion_scope() is None
    finally:
        await model.client.close()


@pytest.mark.parametrize(
    "mutation", [{"version": True}, {"version": 2}, {"principal_id": ""}, {"limits": {}}, {"extra": 1}]
)
async def test_unqualified_capability_cannot_fall_back_to_a_completion(mutation):
    calls = []

    def respond(request):
        calls.append(request.method)
        return httpx2.Response(200, json={"completionRecovery": {**CAPABILITY, **mutation}})

    model = fixture_model(respond)
    try:
        with bridge_completion_scope(callbacks([])), pytest.raises(ProviderError, match="capability"):
            await call(model)
        assert calls == ["GET"]
    finally:
        await model.client.close()


async def test_concurrent_completions_commit_distinct_intents_before_exact_sdk_posts():
    events, requests = [], []

    async def respond(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        requests.append(request)
        descriptor = next(
            item[1]
            for item in events
            if item[0] == "intent" and item[1]["request_id"] == request.headers["idempotency-key"]
        )
        assert descriptor["wire_sha256"] == hashlib.sha256(request.content).hexdigest()
        assert request.headers["x-bridge-completion-recovery"] == "1"
        assert any(item[0] == "guard" and item[1]["payload"] == descriptor for item in events)
        await asyncio.sleep(0)
        return httpx2.Response(200, json=RESULT)

    model = fixture_model(respond)
    try:
        with bridge_completion_scope(callbacks(events)):
            await asyncio.gather(call(model, "first"), call(model, "second"))
        assert len(requests) == 2
        assert len({request.headers["idempotency-key"] for request in requests}) == 2
        assert current_completion_scope() is None
    finally:
        await model.client.close()


async def test_known_recovery_full_is_manual_admission_refusal_not_quota_or_unknown():
    calls, events = [], []

    def respond(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        calls.append(request)
        return httpx2.Response(
            429, headers={"retry-after": "60"}, json={"error": {"code": "completion_recovery_full", "message": "full"}}
        )

    model = fixture_model(respond)
    try:
        with bridge_completion_scope(callbacks(events)), pytest.raises(ProviderAdmissionRefused) as caught:
            await call(model)
        assert len(calls) == 1
        observation = caught.value.observation
        assert classify_provider_failure(caught.value) == ("bridge_admission_refused", None)
        assert observation["admission"] == "admission_refused"
        assert observation["code"] == "completion_recovery_full"
        assert observation["completion_request_id"] == calls[0].headers["idempotency-key"]
        assert "retry_after" not in observation and "job_id" not in observation
    finally:
        await model.client.close()


async def test_required_response_admission_refusal_never_emits_raw_result():
    events = []

    async def refuse(intent, body):
        assert b"Original response" in body
        raise ValueError("Current credential admission refused")

    base = callbacks(events)
    # A native result sink is mandatory; transport success alone is not a task receipt.
    scope = BridgeCompletionScope(base.prepare, base.admit, base.guard, refuse)

    def respond(request):
        return httpx2.Response(200, json={"completionRecovery": CAPABILITY} if request.method == "GET" else RESULT)

    model = fixture_model(respond)
    try:
        with bridge_completion_scope(scope), pytest.raises(ProviderError):
            await call(model)
        assert any(event[0] == "intent" for event in events)
    finally:
        await model.client.close()


async def test_capability_transport_failure_is_known_zero_submission_not_remote_unknown():
    def respond(request):
        assert request.method == "GET"
        raise httpx2.ReadTimeout("Fictional status failure", request=request)

    model = fixture_model(respond)
    try:
        with bridge_completion_scope(callbacks([])), pytest.raises(ProviderError) as caught:
            await call(model)
        assert type(caught.value) is ProviderError
    finally:
        await model.client.close()
