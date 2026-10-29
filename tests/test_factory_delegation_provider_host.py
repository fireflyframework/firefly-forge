# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Concurrent configured children never share provider/model policy or profile state."""

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx2
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from firefly_forge.engine import Engine
from firefly_forge.providers import BridgeProvider
from firefly_forge.work_duration_models import WorkDurationPolicyV2

CAPABILITY = {
    "version": 1,
    "protocol": "firefly-bridge-work-duration-v1",
    "nullableTimeout": True,
    "frozenAtAdmission": True,
}


def captured(identity, chosen):
    policy = WorkDurationPolicyV2(timeout_seconds=chosen).model_dump(mode="json")
    return {
        "id": identity,
        "agent": "bridge",
        "config_revision": identity,
        "work_duration": policy,
        "sandbox_snapshot": {"configuration": {"work_duration": copy.deepcopy(policy)}},
    }


def settings(model):
    return {
        "FORGE_BRIDGE_URL": "http://localhost:19191",
        "FORGE_BRIDGE_TOKEN": "fictional-fixture",
        "FORGE_BRIDGE_MODEL": model,
    }


async def test_two_configured_child_profiles_and_nullable_policy_overlap_without_contamination(tmp_path):
    engine = Engine(SimpleNamespace(path=tmp_path / "factory.db", runtime_roles=None), tmp_path / "jobs")
    runs = [captured("a" * 32, None), captured("b" * 32, 864000)]
    selected = [settings("bridge/approved-analysis"), settings("bridge/approved-implementation")]
    providers = [engine.provider_for_run(run, config) for run, config in zip(runs, selected, strict=True)]
    assert all(isinstance(provider, BridgeProvider) for provider in providers)
    assert providers[0] is not providers[1]
    models = [provider.model() for provider in providers]
    assert models[0] is not models[1] and models[0].client is not models[1].client
    posts, released = [], asyncio.Event()

    async def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"workDuration": CAPABILITY})
        body = json.loads(request.content)
        posts.append(body)
        if len(posts) == 2:
            released.set()
        await asyncio.wait_for(released.wait(), 2)
        return httpx2.Response(
            200,
            json={
                "id": "fictional",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": body["model"]}}
                ],
            },
        )

    for model in models:
        model.client._client._transport = httpx2.MockTransport(transport)
        model.client._client._mounts = {}
    selected[0]["FORGE_BRIDGE_MODEL"] = "unapproved-mutable-alias"
    try:
        responses = await asyncio.gather(
            *(
                model.request(
                    [ModelRequest(parts=[UserPromptPart("Controlled child task")])], None, ModelRequestParameters()
                )
                for model in models
            )
        )
        assert len(responses) == len(posts) == 2
        by_model = {body["model"]: body["bridge"]["workDuration"] for body in posts}
        assert by_model == {
            "bridge/approved-analysis": {"version": 1, "timeoutMs": None},
            "bridge/approved-implementation": {"version": 1, "timeoutMs": 864000000},
        }
        assert providers[0].settings["FORGE_BRIDGE_MODEL"] == "bridge/approved-analysis"
        providers[0].configure_work_duration(WorkDurationPolicyV2(timeout_seconds=1))
        assert providers[1].work_duration.timeout_seconds == 864000
        assert models[1].model_name == "bridge/approved-implementation"
    finally:
        await asyncio.gather(*(model.client.close() for model in models))


def test_legacy_provider_capture_has_no_new_duration_marker(tmp_path):
    engine = Engine(SimpleNamespace(path=tmp_path / "factory.db", runtime_roles=None), tmp_path / "jobs")
    provider = engine.provider_for_run({"agent": "bridge", "config_revision": "a" * 32}, settings("bridge/legacy"))
    assert provider.work_duration is None and provider.execution_timeout() == 1800


def test_model_creation_refuses_top_level_frozen_runner_policy_drift(tmp_path):
    engine = Engine(SimpleNamespace(path=tmp_path / "factory.db", runtime_roles=None), tmp_path / "jobs")
    run = captured("a" * 32, None)
    run["sandbox_snapshot"]["configuration"]["work_duration"]["timeout_seconds"] = 1
    with pytest.raises(ValueError):
        engine.provider_for_run(run, settings("bridge/approved"))
