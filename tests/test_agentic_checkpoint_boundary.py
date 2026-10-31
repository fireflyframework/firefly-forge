# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual Agentic/SDK boundaries with owned in-memory HTTP responses only."""

import hashlib
import importlib
import json
from dataclasses import replace

import httpx2
import pytest
from pydantic_ai import models
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import BinaryContent, InstructionPart, ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import UsageLimits
from pyfly.agentic.agents import Agent
from pyfly.agentic.config import AgenticConfig
from pyfly.agentic.tools import agent_tool
from test_bridge_completion_native import mock_model_requests

from firefly_forge.prompts import prompt_text
from firefly_forge.providers import BridgeProvider


class CutAfterOriginalPost(BaseException):
    pass


def boundary_api():
    name = "firefly_forge.agentic_checkpoint_boundary"
    assert importlib.util.find_spec(name) is not None, "No opt-in model-boundary checkpoint adapter exists"
    return importlib.import_module(name)


def completion(message, *, tokens, finish="stop"):
    return json.dumps(
        {
            "id": "chatcmpl-fictional-original",
            "object": "chat.completion",
            "created": 1,
            "model": "bridge/default",
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}],
            "usage": {"prompt_tokens": tokens, "completion_tokens": 3, "total_tokens": tokens + 3},
        },
        separators=(",", ":"),
    ).encode()


@pytest.mark.parametrize("next_tool", [False, True])
async def test_original_response_restores_public_agent_without_repeating_tool_or_post(tmp_path, monkeypatch, next_tool):
    api = boundary_api()
    writes, posts, captures, original = [], [], [], []
    consumed = []

    async def write_file(path: str, content: str) -> str:
        writes.append(path)
        (tmp_path / path).write_text(content)
        return path

    tool = agent_tool("write_file", description=prompt_text("tools/write_file"), auto_register=False)(write_file)

    def agent(model, boundary):
        return Agent(
            name="checkpoint-proof",
            model=model,
            instructions=prompt_text("implementation/tool-rules"),
            tools=[tool],
            auto_register=False,
            config=AgenticConfig(default_temperature=None, rate_limit_max_retries=0),
            capabilities=[ProcessHistory(lambda messages: messages), boundary],
        )

    def transport(request):
        assert request.method == "POST" and request.url.path == "/v1/chat/completions"
        posts.append(request.content)
        if len(posts) == 1:
            return httpx2.Response(
                200,
                content=completion(
                    {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "write-once",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path":"service.py","content":"VALUE = 1\\n"}',
                                },
                            }
                        ],
                    },
                    tokens=11,
                    finish="tool_calls",
                ),
            )
        original.append(
            completion(
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "new-write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": '{"path":"new.py","content":"NEW = 1\\n"}',
                            },
                        }
                    ],
                }
                if next_tool
                else {"content": "Original final answer"},
                tokens=17,
                finish="tool_calls" if next_tool else "stop",
            )
        )
        raise CutAfterOriginalPost()

    boundary = api.AgenticBoundary(lambda: {"files_written": list(writes)})
    model = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}).model()
    model.client._client._transport = httpx2.MockTransport(transport)
    model.client._client._mounts = {}
    send = model.client._send_request

    async def capture(request, **kwargs):
        captures.append(boundary.capture(await request.aread()))
        return await send(request, **kwargs)

    monkeypatch.setattr(model.client, "_send_request", capture)
    try:
        with (
            models.override_allow_model_requests(False),
            mock_model_requests(model),
            boundary.invocation(),
            pytest.raises(CutAfterOriginalPost),
        ):
            await agent(model, boundary).run(
                "Fictional source fixture", usage_limits=UsageLimits(request_limit=4, tool_calls_limit=2)
            )
        assert writes == ["service.py"] and len(posts) == 2
        checkpoint = api.BoundaryCheckpoint.decode(captures[-1].payload, captures[-1].digest)
        assert checkpoint.state["files_written"] == ["service.py"]
        assert checkpoint.usage.requests == 1 and checkpoint.usage.tool_calls == 1
        assert checkpoint.usage.input_tokens == 11 and checkpoint.usage.output_tokens == 3
        assert checkpoint.wire_sha256 == hashlib.sha256(posts[-1]).hexdigest()
        assert checkpoint.history[-1].kind == "request"
        assert boundary.current() is None

        async def consume(checkpoint_digest, response_digest):
            consumed.append((checkpoint_digest, response_digest))

        replay = api.OriginalCompletion(checkpoint, original[0], consume)
        restored = api.AgenticBoundary(lambda: {"files_written": list(writes)}, restore=checkpoint)
        restored_model = BridgeProvider(
            {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
        ).model()

        def later_completion(request):
            assert next_tool, "Restoration attempted a replacement HTTP request"
            assert request.content != posts[-1]
            posts.append(request.content)
            return httpx2.Response(200, content=completion({"content": "Original final answer"}, tokens=19))

        restored_model.client._client._transport = httpx2.MockTransport(later_completion)
        restored_model.client._client._mounts = {}
        restored_send = restored_model.client._send_request
        replayed = []

        async def retained_response(request, **kwargs):
            body = await request.aread()
            snapshot = restored.capture(body)
            if not replayed:
                assert body == posts[-1]
                response = await replay.expose(body)
                replayed.append(snapshot)
                assert consumed, "Response reached the parser before its consumption callback"
                return httpx2.Response(200, content=response, request=request)
            assert snapshot.wire_sha256 != checkpoint.wire_sha256
            return await restored_send(request, **kwargs)

        monkeypatch.setattr(restored_model.client, "_send_request", retained_response)
        try:
            with (
                models.override_allow_model_requests(False),
                mock_model_requests(restored_model),
                restored.invocation(),
            ):
                result = await agent(restored_model, restored).run(**checkpoint.resume_kwargs())
            assert result.output == "Original final answer"
            assert result.usage.requests == (3 if next_tool else 2)
            assert result.usage.tool_calls == (2 if next_tool else 1)
            assert result.usage.input_tokens == (47 if next_tool else 28)
            assert result.usage.output_tokens == (9 if next_tool else 6)
            assert writes == (["service.py", "new.py"] if next_tool else ["service.py"])
            assert len(posts) == (3 if next_tool else 2)
            assert (tmp_path / "service.py").read_text() == "VALUE = 1\n"
            assert consumed == [(checkpoint.digest, hashlib.sha256(original[0]).hexdigest())]
            with pytest.raises(ValueError, match="consum"):
                await replay.expose(posts[1])
            assert restored.current() is None
        finally:
            await restored_model.client.close()
    finally:
        await model.client.close()


@pytest.mark.parametrize("reason", ["vision", "deferred", "dynamic", "unknown"])
async def test_ineligible_public_history_refuses_before_the_function_model(reason):
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.messages import ModelRequest, RetryPromptPart
    from pydantic_ai.tools import ToolDefinition

    api = boundary_api()
    calls = []

    class Unsupported(AbstractCapability):
        async def before_model_request(self, ctx, request_context):
            if reason == "deferred":
                request_context.model_request_parameters.function_tools = [
                    ToolDefinition(name="later", kind="external")
                ]
            elif reason == "dynamic":
                request_context.model_request_parameters.instruction_parts = [
                    InstructionPart(content="Fictional dynamic fixture", dynamic=True)
                ]
            elif reason == "unknown":
                request_context.messages[-1] = ModelRequest(parts=[RetryPromptPart("Fictional unsupported part")])
                ctx.messages[:] = request_context.messages
            return request_context

    def model(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[TextPart("Must not be reached")])

    boundary = api.AgenticBoundary(lambda: {"files_written": []})
    agent = Agent(
        name="ineligible-checkpoint",
        model=FunctionModel(model),
        auto_register=False,
        config=AgenticConfig(default_temperature=None, rate_limit_max_retries=0),
        capabilities=[Unsupported(), boundary],
    )
    prompt = [BinaryContent(data=b"fictional", media_type="image/png")] if reason == "vision" else "Fictional input"
    with boundary.invocation(), pytest.raises(ValueError):
        await agent.run(prompt)
    assert calls == [] and boundary.current() is None


async def test_actual_output_retry_is_ineligible_before_another_model_request():
    api = boundary_api()
    calls = []

    def model(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[TextPart("Not a valid integer")])

    boundary = api.AgenticBoundary(lambda: {"files_written": []})
    agent = Agent(
        name="retry-checkpoint",
        model=FunctionModel(model),
        output_type=int,
        auto_register=False,
        config=AgenticConfig(default_temperature=None, rate_limit_max_retries=0),
        capabilities=[boundary],
    )
    with boundary.invocation(), pytest.raises(ValueError, match="retry"):
        await agent.run("Fictional integer fixture")
    assert len(calls) == 1 and boundary.current() is None


async def test_consumption_failure_never_exposes_response_or_rearms_the_same_gate():
    import asyncio

    api = boundary_api()
    captures = []
    boundary = api.AgenticBoundary(lambda: {"files_written": []})

    def model(messages, info):
        captures.append(boundary.capture(b"fictional-wire"))
        return ModelResponse(parts=[TextPart("Original")])

    agent = Agent(name="gate-fixture", model=FunctionModel(model), auto_register=False, capabilities=[boundary])
    with boundary.invocation():
        await agent.run("Fictional gate fixture")
    for failure in (ValueError("Commit was refused"), asyncio.CancelledError()):
        calls = []

        async def consume(*args, calls=calls, failure=failure):
            calls.append(args)
            raise failure

        gate = api.OriginalCompletion(captures[0], b"original", consume)
        with pytest.raises(type(failure)):
            await gate.expose(b"fictional-wire")
        with pytest.raises(ValueError, match="consum"):
            await gate.expose(b"fictional-wire")
        assert len(calls) == 1


async def test_checkpoint_copy_is_detached_and_wire_mismatch_refuses_before_consumption():
    api = boundary_api()
    state, captures = {"files_written": []}, []
    boundary = api.AgenticBoundary(lambda: state)

    def model(messages, info):
        captures.append(boundary.capture(b"original-wire"))
        return ModelResponse(parts=[TextPart("Original")])

    agent = Agent(name="detached-fixture", model=FunctionModel(model), auto_register=False, capabilities=[boundary])
    with boundary.invocation():
        await agent.run("Fictional detached fixture")
    checkpoint = captures[0]
    state["files_written"].append("late.py")
    checkpoint.history.clear()
    checkpoint.usage.requests = 999
    assert checkpoint.state == {"files_written": []}
    assert checkpoint.history and checkpoint.usage.requests == 0
    calls = []

    async def consume(*args):
        calls.append(args)

    gate = api.OriginalCompletion(checkpoint, b"original-response", consume)
    with pytest.raises(ValueError, match="wire"):
        await gate.expose(b"changed-wire")
    assert not calls
    corrupted = replace(checkpoint, payload=checkpoint.payload + b" ")
    with pytest.raises(ValueError, match="digest"):
        corrupted.resume_kwargs()


@pytest.mark.parametrize("change", ["tool-state", "budget"])
async def test_restore_rejects_lost_local_state_even_when_model_wire_is_unchanged(change):
    api = boundary_api()
    captured = []
    original = api.AgenticBoundary(lambda: {"files_written": ["original.py"]})

    def capture(messages, info):
        captured.append(original.capture(b"same-wire"))
        return ModelResponse(parts=[TextPart("Original")])

    with original.invocation():
        await Agent(
            name="original-state", model=FunctionModel(capture), auto_register=False, capabilities=[original]
        ).run("Fictional state fixture", usage_limits=UsageLimits(request_limit=3))
    checkpoint = captured[0]
    restored = api.AgenticBoundary(
        lambda: {"files_written": []} if change == "tool-state" else checkpoint.state, restore=checkpoint
    )
    exposed = []

    def restore(messages, info):
        restored.capture(b"same-wire")
        exposed.append(True)
        return ModelResponse(parts=[TextPart("Must not be exposed")])

    arguments = checkpoint.resume_kwargs()
    if change == "budget":
        arguments["usage_limits"] = UsageLimits(request_limit=100)
    with restored.invocation(), pytest.raises(ValueError, match="state"):
        await Agent(
            name="restored-state", model=FunctionModel(restore), auto_register=False, capabilities=[restored]
        ).run(**arguments)
    assert not exposed


async def test_checkpoint_rejects_boolean_usage_instead_of_treating_it_as_one_request():
    api = boundary_api()
    captured = []
    boundary = api.AgenticBoundary(lambda: {"files_written": []})

    def capture(messages, info):
        captured.append(boundary.capture(b"wire"))
        return ModelResponse(parts=[TextPart("Original")])

    with boundary.invocation():
        await Agent(
            name="usage-schema", model=FunctionModel(capture), auto_register=False, capabilities=[boundary]
        ).run("Fictional schema fixture")
    data = json.loads(captured[0].payload)
    data["usage"]["requests"] = True
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="usage"):
        api.BoundaryCheckpoint.decode(payload, hashlib.sha256(payload).hexdigest())


async def test_shared_capability_keeps_concurrent_invocations_and_request_contexts_separate():
    import asyncio
    from contextvars import ContextVar

    api = boundary_api()
    actor_state = ContextVar("fictional_tool_state")
    boundary = api.AgenticBoundary(actor_state.get)
    captures = []

    async def run(label):
        token = actor_state.set({"files_written": [label + ".py"]})

        async def model(messages, info):
            await asyncio.sleep(0)
            snapshot = boundary.capture(label.encode())
            captures.append(snapshot)
            assert snapshot.state == {"files_written": [label + ".py"]}
            return ModelResponse(parts=[TextPart(label)])

        try:
            with boundary.invocation():
                result = await Agent(
                    name=label, model=FunctionModel(model), auto_register=False, capabilities=[boundary]
                ).run(label)
                assert result.output == label
            assert boundary.current() is None
        finally:
            actor_state.reset(token)

    await asyncio.gather(run("left"), run("right"))
    assert {snapshot.history[0].parts[0].content for snapshot in captures} == {"left", "right"}
    assert len({snapshot.digest for snapshot in captures}) == 2
    assert boundary.current() is None


async def test_capture_refuses_unfinished_tool_history_before_a_new_request():
    api = boundary_api()
    from pydantic_ai.messages import ModelRequest, ToolCallPart, UserPromptPart

    boundary = api.AgenticBoundary(lambda: {"files_written": []})
    checkpointed = []

    def model(messages, info):
        checkpointed.append(True)
        return ModelResponse(parts=[TextPart("Must not be reached")])

    history = [
        ModelRequest(parts=[UserPromptPart("Fictional unfinished effect")]),
        ModelResponse(parts=[ToolCallPart("write_file", {"path": "a.py"}, "unfinished")]),
        ModelRequest(parts=[UserPromptPart("No retained tool result")]),
    ]
    with boundary.invocation(), pytest.raises(ValueError, match="unfinished"):
        await Agent(
            name="unfinished-checkpoint", model=FunctionModel(model), auto_register=False, capabilities=[boundary]
        ).run(None, message_history=history)
    assert not checkpointed


async def test_changed_tool_catalog_between_requests_is_not_a_static_checkpoint_contract():
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.messages import ToolCallPart

    api = boundary_api()
    captured, written = [], []
    boundary = api.AgenticBoundary(lambda: {"files_written": list(written)})

    class ChangingCatalog(AbstractCapability):
        async def before_model_request(self, ctx, request_context):
            if ctx.usage.requests:
                request_context.model_request_parameters.function_tools = []
            return request_context

    def write_file(path: str) -> str:
        written.append(path)
        return path

    def model(messages, info):
        captured.append(boundary.capture(b"fictional-wire"))
        if len(captured) == 1:
            return ModelResponse(parts=[ToolCallPart("write_file", {"path": "once.py"}, "once")])
        return ModelResponse(parts=[TextPart("Must be refused")])

    agent = Agent(
        name="changing-tools",
        model=FunctionModel(model),
        tools=[write_file],
        auto_register=False,
        capabilities=[ChangingCatalog(), boundary],
    )
    with boundary.invocation(), pytest.raises(ValueError, match="dynamic"):
        await agent.run("Fictional catalog fixture")
    assert written == ["once.py"] and len(captured) == 1
