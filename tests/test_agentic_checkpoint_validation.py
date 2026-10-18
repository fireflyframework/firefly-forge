# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Causal lifecycle, budget, and immutable-byte controls for the inactive boundary."""

import asyncio
import hashlib
import json
from contextlib import suppress

import pytest
from fireflyframework_agentic.agents import FireflyAgent
from fireflyframework_agentic.model_utils import serialize_model_messages
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary, BoundaryCheckpoint, OriginalCompletion


@pytest.fixture
async def checkpoint():
    boundary = AgenticBoundary(lambda: {"files_written": []})
    captures = []

    def model(messages, info):
        captures.append(boundary.capture(b"fictional-wire"))
        return ModelResponse(parts=[TextPart("Original")])

    with boundary.invocation():
        await FireflyAgent(
            name="validation-fixture", model=FunctionModel(model), auto_register=False, capabilities=[boundary]
        ).run("Fictional checkpoint fixture")
    return captures[0]


def reseal(data):
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return payload, hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("state", ["interrupted", "incomplete", "suspended", "response-interrupted"])
def test_decode_refuses_noncomplete_native_lifecycle(checkpoint, state):
    messages = checkpoint.history
    if state == "interrupted":
        messages[-1].state = "interrupted"
    else:
        messages.insert(
            0,
            ModelResponse(
                parts=[TextPart("Partial response")],
                state="interrupted" if state == "response-interrupted" else state,
            ),
        )
    data = json.loads(checkpoint.payload)
    data["history"] = data["request_history"] = serialize_model_messages(messages)
    with pytest.raises(ValueError, match="lifecycle"):
        BoundaryCheckpoint.decode(*reseal(data))


@pytest.mark.parametrize("state", ["interrupted", "incomplete", "suspended", "response-interrupted"])
async def test_noncomplete_lifecycle_refuses_before_the_public_function_model(state):
    calls = []

    class NoncompleteHistory(AbstractCapability):
        async def before_model_request(self, ctx, request_context):
            if state == "interrupted":
                request_context.messages[-1].state = "interrupted"
            else:
                request_context.messages.insert(
                    0,
                    ModelResponse(
                        parts=[TextPart("Partial response")],
                        state="interrupted" if state == "response-interrupted" else state,
                    ),
                )
            ctx.messages[:] = request_context.messages
            return request_context

    def model(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[TextPart("Must not be reached")])

    boundary = AgenticBoundary(lambda: {"files_written": []})
    with boundary.invocation(), pytest.raises(ValueError, match="lifecycle"):
        await FireflyAgent(
            name="lifecycle-refusal",
            model=FunctionModel(model),
            auto_register=False,
            capabilities=[NoncompleteHistory(), boundary],
        ).run("Fictional lifecycle fixture")
    assert not calls and boundary.current() is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requests", -1),
        ("tool_calls", -1),
        ("input_tokens", -1),
        ("output_tokens", -1),
        ("cache_read_tokens", -1),
        ("input_audio_tokens", -1),
        ("audio_seconds", -0.5),
        ("details", {"reasoning_tokens": -1}),
        ("cost", "-1"),
        ("requests", 2**53),
    ],
)
def test_decode_refuses_understated_or_unbounded_native_usage(checkpoint, field, value):
    data = json.loads(checkpoint.payload)
    data["usage"][field] = value
    with pytest.raises(ValueError, match="usage"):
        BoundaryCheckpoint.decode(*reseal(data))


@pytest.mark.parametrize(("field", "value"), [("request_limit", -1), ("tool_calls_limit", 2**53), ("cost_limit", "-1")])
def test_decode_refuses_invalid_native_budget_limits(checkpoint, field, value):
    data = json.loads(checkpoint.payload)
    data["limits"][field] = value
    with pytest.raises(ValueError, match="limits"):
        BoundaryCheckpoint.decode(*reseal(data))


async def test_expose_returns_the_same_bytes_whose_digest_was_consumed(checkpoint):
    entered, release = asyncio.Event(), asyncio.Event()
    consumed = []

    async def consume(checkpoint_digest, response_digest):
        consumed.append(response_digest)
        entered.set()
        await release.wait()

    gate = OriginalCompletion(checkpoint, b"Original response", consume)
    task = asyncio.create_task(gate.expose(b"fictional-wire"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with suppress(AttributeError):
            gate.response = b"Swapped response"
        release.set()
        result = await task
        assert result == b"Original response"
        assert consumed == [hashlib.sha256(result).hexdigest()]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("entry", ["decode", "construct", "response"])
def test_mutable_bytes_are_refused_at_boundary_construction(checkpoint, entry):
    async def consume(*args):
        pass

    with pytest.raises(ValueError, match="bytes"):
        if entry == "decode":
            BoundaryCheckpoint.decode(bytearray(checkpoint.payload), checkpoint.digest)
        elif entry == "construct":
            BoundaryCheckpoint(bytearray(checkpoint.payload), checkpoint.digest)
        else:
            OriginalCompletion(checkpoint, bytearray(b"Original response"), consume)


def test_finite_native_usage_and_optional_limits_keep_their_exact_values(checkpoint):
    data = json.loads(checkpoint.payload)
    data["usage"].update(
        requests=1,
        tool_calls=2,
        input_tokens=7,
        cache_read_tokens=2,
        output_tokens=3,
        audio_seconds=0.5,
        details={"reasoning_tokens": 2},
        cost="0.25",
    )
    data["limits"].update(request_limit=3, tool_calls_limit=4, cost_limit="1.00")
    decoded = BoundaryCheckpoint.decode(*reseal(data))
    assert decoded.usage.requests == 1 and decoded.usage.tool_calls == 2
    assert decoded.usage.input_tokens == 7 and decoded.usage.cache_read_tokens == 2
    assert str(decoded.usage.cost) == "0.25" and decoded.usage.details == {"reasoning_tokens": 2}
    limits = decoded.resume_kwargs()["usage_limits"]
    assert limits.request_limit == 3 and limits.tool_calls_limit == 4 and str(limits.cost_limit) == "1.00"
    assert limits.input_tokens_limit is None
