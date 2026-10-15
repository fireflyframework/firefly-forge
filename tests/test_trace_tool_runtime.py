# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native tool admission; a context callback is not Docker execution acceptance."""

import pytest

from firefly_forge.agentic_runtime import execute_agentic
from firefly_forge.workspace_tools import ToolRuntimeContext, tool_catalog


def test_tracing_is_optional_and_requires_the_existing_command_capability():
    absent = {tool["id"]: tool for tool in tool_catalog()}
    assert "trace_python" in absent, "The optional native tracing capability is missing"
    assert absent["trace_python"]["availability"] == "not_configured"
    missing_command = {tool["id"]: tool for tool in tool_catalog({"trace_python": True})}
    assert missing_command["trace_python"]["availability"] == "not_configured"
    ready = {tool["id"]: tool for tool in tool_catalog({"trace_python": True, "execute_command": True})}
    assert ready["trace_python"]["availability"] == "available"
    assert set(ready["trace_python"]["requires"]) == {"trace_python", "execute_command"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grants,configured,expected",
    [
        ([], True, False),
        (["execute_command"], True, False),
        (["trace_python"], True, False),
        (["execute_command", "trace_python"], False, False),
        (["execute_command", "trace_python"], True, True),
    ],
)
async def test_native_agent_only_receives_the_trace_tool_with_both_grants_and_hook(
    tmp_path, grants, configured, expected
):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    observed = []

    def model(messages, info):
        observed.extend(tool.name for tool in info.function_tools)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Inspected native capability"})]
        )

    async def emit(kind, message):
        pass

    async def trace(request):
        raise AssertionError("Capability inspection must not dispatch a workload")

    async def command(argv, timeout):
        raise AssertionError("Capability inspection must not dispatch a command")

    context = ToolRuntimeContext(
        tmp_path,
        tmp_path,
        frozenset(grants),
        command if configured else None,
        python_trace=trace if configured else None,
    )
    with pytest.raises(RuntimeError, match="without writing any project source"):
        await execute_agentic(
            tmp_path,
            "Inspect available tools",
            "pyfly",
            "none",
            emit,
            model=FunctionModel(model),
            runtime_context=context,
        )
    assert ("trace_python" in observed) is expected


@pytest.mark.asyncio
async def test_native_trace_hook_keeps_a_nonzero_observation_and_cannot_approve_qa(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    calls = []
    seen = []

    async def command(argv, timeout):
        raise AssertionError("The trace tool must use its admitted recipe hook")

    async def trace(request):
        calls.append(request.script)
        return {
            "recipe": "forge-python-line-trace-v1",
            "exit_code": 7,
            "output": "Fixture diagnostic observation",
            "observation_only": True,
        }

    def model(messages, info):
        returned = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "trace_python"
        ]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("trace_python", {"request": {"script": "workload.py"}})])
        seen.append(returned[0].content)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name, {"summary": "Observed a failed workload; no source work was done"}
                )
            ]
        )

    async def emit(kind, message):
        pass

    context = ToolRuntimeContext(
        tmp_path, tmp_path, frozenset({"trace_python", "execute_command"}), command, python_trace=trace
    )
    with pytest.raises(RuntimeError, match="without writing any project source"):
        await execute_agentic(
            tmp_path, "Profile a workload", "pyfly", "none", emit, model=FunctionModel(model), runtime_context=context
        )
    assert calls == ["workload.py"]
    assert seen == [
        {
            "recipe": "forge-python-line-trace-v1",
            "exit_code": 7,
            "output": "Fixture diagnostic observation",
            "observation_only": True,
        }
    ]


@pytest.mark.asyncio
async def test_trace_arguments_reject_known_credentials_before_dispatch(tmp_path):
    from fireflyframework_agentic.exceptions import ToolError
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    async def forbidden(*args):
        raise AssertionError("Credential-bearing request cannot dispatch")

    def model(messages, info):
        return ModelResponse(
            parts=[ToolCallPart("trace_python", {"request": {"script": "workload.py", "args": ["owned-secret-token"]}})]
        )

    async def emit(kind, message):
        pass

    with pytest.raises(ToolError, match="credential"):
        await execute_agentic(
            tmp_path,
            "Inspect",
            "pyfly",
            "none",
            emit,
            model=FunctionModel(model),
            runtime_context=ToolRuntimeContext(
                tmp_path,
                tmp_path,
                frozenset({"execute_command", "trace_python"}),
                forbidden,
                credential_registry=lambda: {"FORGE_TOKEN": "owned-secret-token"},
                python_trace=forbidden,
            ),
        )


@pytest.mark.asyncio
async def test_trace_observation_text_is_redacted_before_model_admission(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    observed = []

    async def trace(request):
        return {"output": "owned-secret-token", "exit_code": 0, "observation_only": True}

    async def forbidden(*args):
        raise AssertionError("Only the recipe callback is permitted")

    def model(messages, info):
        returned = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "trace_python"
        ]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("trace_python", {"request": {"script": "workload.py"}})])
        observed.append(returned[0].content)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Observed redacted fixture diagnostic"})]
        )

    async def emit(kind, message):
        pass

    with pytest.raises(RuntimeError, match="without writing any project source"):
        await execute_agentic(
            tmp_path,
            "Inspect",
            "pyfly",
            "none",
            emit,
            model=FunctionModel(model),
            runtime_context=ToolRuntimeContext(
                tmp_path,
                tmp_path,
                frozenset({"execute_command", "trace_python"}),
                forbidden,
                credential_registry=lambda: {"FORGE_TOKEN": "owned-secret-token"},
                python_trace=trace,
            ),
        )
    assert observed == [{"output": "[redacted]", "exit_code": 0, "observation_only": True}]
