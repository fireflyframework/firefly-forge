# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Communication uses the selected profile deadline, with a bounded maximum."""

import asyncio

import pytest
from communication_model_fixture import reviewed_model
from test_communication_writer import configured_store

from firefly_forge.communication import CommunicationWriter
from firefly_forge.model_profiles import ModelProfileInput


@pytest.mark.parametrize("configured,expected", [("20", 20), ("60", 60), ("7200", 120)])
async def test_profile_deadline_reaches_both_agent_and_outer_cancellation(tmp_path, monkeypatch, configured, expected):
    import fireflyframework_agentic.agents as agents

    store = await configured_store(tmp_path, "bridge")
    store.task_router.profiles.save(
        "updates",
        ModelProfileInput(
            name="Project updates",
            provider="bridge",
            values={
                "endpoint": "http://127.0.0.1:12345/v1",
                "token": "public-fixture-bridge-token",
                "timeout": configured,
            },
        ),
    )
    outer = []
    agent_deadlines = []
    original_timeout = asyncio.timeout
    original_run = agents.FireflyAgent.run

    def observe_timeout(delay):
        outer.append(delay)
        return original_timeout(delay)

    async def observe_run(self, *args, **kwargs):
        agent_deadlines.append(kwargs.get("timeout"))
        return await original_run(self, *args, **kwargs)

    monkeypatch.setattr(asyncio, "timeout", observe_timeout)
    monkeypatch.setattr(agents.FireflyAgent, "run", observe_run)
    output = {
        "title": {"text": "Request queued", "fact_ids": ["state"]},
        "paragraphs": [{"text": "The request is queued.", "fact_ids": ["state"]}],
    }
    result = await CommunicationWriter(store, model=reviewed_model(output)).write(
        {"status": "queued"},
        "Exact native fallback",
        lambda value: value,
    )
    assert result["communication"]["mode"] == "ai"
    assert outer[0] == expected
    assert len(agent_deadlines) == 2
    assert agent_deadlines[0] == expected
    assert 0 < agent_deadlines[1] <= expected
    assert result["communication"]["timeout_seconds"] == expected


async def test_expired_profile_cancels_the_single_inflight_generation_without_retry(tmp_path):
    from pydantic_ai.models.function import FunctionModel

    store = await configured_store(tmp_path)
    store.task_router.profiles.save(
        "updates",
        ModelProfileInput(
            name="Project updates",
            provider="agentic",
            values={
                "model": "openai-responses:fixture",
                "api_key": "public-fixture-model-key",
                "timeout": "1",
            },
        ),
    )
    calls = []
    cancelled = asyncio.Event()

    async def slow_generation(messages, info):
        calls.append(1)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("Generation must be cancelled by the profile deadline")

    result = await asyncio.wait_for(
        CommunicationWriter(store, model=FunctionModel(slow_generation)).write(
            {"status": "queued"},
            "Exact native fallback",
            lambda value: value,
        ),
        timeout=3,
    )
    assert calls == [1] and cancelled.is_set()
    assert result["body"] == ""
    assert result["communication"]["mode"] == "held"
    assert result["communication"]["reason"] == "timeout"
    assert result["communication"]["timeout_seconds"] == 1


async def test_review_consumes_the_same_total_deadline_instead_of_starting_a_fresh_one(tmp_path):
    import json

    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    store = await configured_store(tmp_path, "bridge")
    store.task_router.profiles.save(
        "updates",
        ModelProfileInput(
            name="Project updates",
            provider="bridge",
            values={"timeout": "1"},
        ),
    )
    calls = []
    cancelled = asyncio.Event()

    async def bounded_model(messages, info):
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(0.35)
            value = {
                "title": {"text": "Request queued", "fact_ids": ["state"]},
                "paragraphs": [{"text": "The request is queued.", "fact_ids": ["state"]}],
            }
        else:
            try:
                await asyncio.sleep(0.8)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            value = {
                "reader_checks": {
                    "answers_question": True,
                    "relevant_detail": True,
                    "no_redundancy": True,
                    "plain_language": True,
                    "useful_excerpts": True,
                },
                "verdict": "supported",
                "issues": [],
                "fact_ids": ["state"],
                "rationale": "Grounded.",
            }
        return ModelResponse(parts=[TextPart(json.dumps(value))])

    result = await asyncio.wait_for(
        CommunicationWriter(store, model=FunctionModel(bounded_model)).write(
            {"status": "queued"},
            "Exact native fallback",
            lambda value: value,
        ),
        timeout=3,
    )
    assert calls == [1, 1] and cancelled.is_set()
    assert result["body"] == ""
    assert result["communication"]["mode"] == "held"
    assert result["communication"]["reason"] == "timeout"
    assert result["communication"]["timeout_seconds"] == 1
