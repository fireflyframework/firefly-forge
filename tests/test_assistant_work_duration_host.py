# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Conversation work policy is explicit; retained unmarked receipts remain finite."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from firefly_forge import product_assistant as assistant
from firefly_forge import product_assistant_runtime as runtime
from firefly_forge.client import ForgeClient
from firefly_forge.work_duration_models import WorkDurationPolicyV2

POLICY_KEY = "ASSISTANT_WORK_DURATION_POLICY"


@pytest.mark.parametrize("timeout", [None, 1, 864000])
def test_new_conversation_input_has_explicit_optional_duration(timeout):
    request = assistant.AssistantSessionInput(work_duration=WorkDurationPolicyV2(timeout_seconds=timeout))
    assert request.model_dump()["work_duration"] == {"protocol": "forge-work-duration-v2", "timeout_seconds": timeout}
    assert assistant.AssistantSessionInput().work_duration == WorkDurationPolicyV2()
    assert "work_duration" not in assistant.AssistantSessionInput().model_dump(exclude_unset=True)


@pytest.mark.parametrize("timeout", [True, False, 0, -1, 1.5, "300"])
def test_new_conversation_rejects_untyped_or_nonpositive_deadline(timeout):
    with pytest.raises(ValueError):
        assistant.AssistantSessionInput(
            work_duration={"protocol": "forge-work-duration-v2", "timeout_seconds": timeout}
        )


@pytest.mark.parametrize("timeout", [None, 1, 864000])
async def test_runtime_uses_selected_deadline_without_legacy_clamp(monkeypatch, timeout):
    import pyfly.agentic.agents as agents

    calls = []

    class Agent:
        def __init__(self, **kwargs):
            assert kwargs["retries"] == 0
            assert kwargs["config"].max_retries == kwargs["config"].rate_limit_max_retries == 0

        async def run(self, prompt, **kwargs):
            calls.append(kwargs["timeout"])
            return SimpleNamespace(output={"message": "Recorded model response"})

    monkeypatch.setattr(agents, "Agent", Agent)
    settings = {
        "FORGE_PROVIDER_TIMEOUT": "2",
        POLICY_KEY: WorkDurationPolicyV2(timeout_seconds=timeout).model_dump_json(),
    }
    await runtime.design_reply("Scoped design request", settings, model=object())
    assert calls == [timeout]


@pytest.mark.parametrize("configured,expected", [(None, 300), ("1", 1), ("864000", 300)])
async def test_unmarked_runtime_keeps_original_deadline(monkeypatch, configured, expected):
    import pyfly.agentic.agents as agents

    calls = []

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run(self, prompt, **kwargs):
            calls.append(kwargs["timeout"])
            return SimpleNamespace(output={"message": "Retained model response"})

    monkeypatch.setattr(agents, "Agent", Agent)
    await runtime.design_reply(
        "Scoped request", {"FORGE_PROVIDER_TIMEOUT": configured} if configured else {}, model=object()
    )
    assert calls == [expected]


@pytest.mark.parametrize("provider_name", ["bridge", "agentic"])
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_selected_policy_reaches_provider_before_model(monkeypatch, provider_name, selected, outcome):
    import pyfly.agentic.agents as agents

    calls = []

    class Model:
        @property
        def client(self):
            return self

        async def close(self):
            calls.append("closed")

        async def __aenter__(self):
            calls.append("enter")
            return self

        async def __aexit__(self, *args):
            calls.append("exit")

    class Provider:
        def __init__(self, settings):
            calls.append("created")

        def configure_work_duration(self, policy):
            calls.append(policy.model_dump())

        def model(self, **kwargs):
            calls.append("model")
            return Model()

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            if outcome == "failed":
                raise RuntimeError("Controlled unknown response")
            if outcome == "cancelled":
                raise asyncio.CancelledError
            return SimpleNamespace(output={"message": "Provider result"})

    class API(Provider):
        pass

    class Bridge(Provider):
        pass

    monkeypatch.setattr(runtime, "APIProvider", API)
    monkeypatch.setattr(runtime, "BridgeProvider", Bridge)
    monkeypatch.setattr(agents, "Agent", Agent)
    settings = {"ASSISTANT_PROVIDER": provider_name}
    if selected:
        settings[POLICY_KEY] = WorkDurationPolicyV2().model_dump_json()
    if outcome == "completed":
        await runtime.design_reply("Scoped request", settings)
    else:
        with pytest.raises(RuntimeError if outcome == "failed" else asyncio.CancelledError):
            await runtime.design_reply("Scoped request", settings)
    assert calls == [
        "created",
        *([WorkDurationPolicyV2().model_dump()] if selected else []),
        "model",
        *(["enter", "exit"] if selected else []),
        *(["closed"] if selected and provider_name == "bridge" else []),
    ]


def test_frozen_policy_requires_exact_creation_receipt_and_does_not_reinterpret_legacy():
    validate = getattr(assistant, "conversation_work_duration", None)
    assert callable(validate), "The immutable conversation policy validator is missing"
    assert validate({}, {}) is None
    raw = WorkDurationPolicyV2().model_dump()
    session, receipt = {"work_duration": raw}, {"work_duration_digest": assistant.digest(raw)}
    before = copy.deepcopy((session, receipt))
    assert validate(session, receipt) == WorkDurationPolicyV2()
    assert (session, receipt) == before
    for data, created in [
        (session, {}),
        ({}, receipt),
        ({"work_duration": None}, receipt),
        ({"work_duration": {**raw, "timeout_seconds": False}}, receipt),
        ({"work_duration": {**raw, "timeout_seconds": 1}}, receipt),
        ({"work_duration": {"timeout_seconds": None}}, receipt),
    ]:
        with pytest.raises(ValueError, match="duration"):
            validate(data, created)


def test_sdk_new_policy_creation_is_optional_and_turn_wait_follows_frozen_policy(monkeypatch):
    client = object.__new__(ForgeClient)
    calls = []
    session = {}

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return session if method == "GET" else {}

    monkeypatch.setattr(client, "request", request)
    client.create_assistant_conversation("a" * 32)
    assert calls[-1][2]["json"] == {"task": "product"}
    client.create_assistant_conversation("a" * 32, work_duration=WorkDurationPolicyV2())
    assert calls[-1][2]["json"]["work_duration"] == WorkDurationPolicyV2().model_dump()
    turn = {"request_id": "b" * 32, "message": "Scoped request"}
    client.assistant_turn("a" * 32, "c" * 32, turn)
    assert calls[-1][2]["timeout"] == 360
    session["work_duration"] = WorkDurationPolicyV2().model_dump()
    client.assistant_turn("a" * 32, "c" * 32, turn)
    assert calls[-1][2]["timeout"] is None
    assert json.dumps(calls[-1][2]["json"], sort_keys=True) == json.dumps(
        {**turn, "attachment_ids": None}, sort_keys=True
    )
