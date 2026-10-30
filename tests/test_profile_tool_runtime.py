# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native tool admission; a context callback is not Docker execution acceptance."""

import pytest

from firefly_forge.agentic_runtime import execute_agentic
from firefly_forge.workspace_tools import ToolRuntimeContext, tool_catalog


def test_profiling_is_optional_and_requires_the_existing_command_capability():
    absent = {tool["id"]: tool for tool in tool_catalog()}
    assert "profile_python" in absent, "The optional native profiling capability is missing"
    assert absent["profile_python"]["availability"] == "not_configured"
    missing_command = {tool["id"]: tool for tool in tool_catalog({"profile_python": True})}
    assert missing_command["profile_python"]["availability"] == "not_configured"
    ready = {tool["id"]: tool for tool in tool_catalog({"profile_python": True, "execute_command": True})}
    assert ready["profile_python"]["availability"] == "available"
    assert set(ready["profile_python"]["requires"]) == {"profile_python", "execute_command"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grants,configured,expected",
    [
        ([], True, False),
        (["execute_command"], True, False),
        (["profile_python"], True, False),
        (["execute_command", "profile_python"], False, False),
        (["execute_command", "profile_python"], True, True),
    ],
)
async def test_native_agent_only_receives_the_profile_tool_with_both_grants_and_hook(
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

    async def profile(request):
        raise AssertionError("Capability inspection must not dispatch a workload")

    async def command(argv, timeout):
        raise AssertionError("Capability inspection must not dispatch a command")

    context = ToolRuntimeContext(
        tmp_path,
        tmp_path,
        frozenset(grants),
        command if configured else None,
        python_profile=profile if configured else None,
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
    assert ("profile_python" in observed) is expected


@pytest.mark.asyncio
async def test_native_profile_hook_keeps_a_nonzero_observation_and_cannot_approve_qa(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    calls = []
    seen = []

    async def command(argv, timeout):
        raise AssertionError("The profile tool must use its admitted recipe hook")

    async def profile(request):
        calls.append(request.script)
        return {
            "recipe": "forge-python-cprofile-v1",
            "exit_code": 7,
            "output": "Fixture diagnostic observation",
            "observation_only": True,
        }

    def model(messages, info):
        returned = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "profile_python"
        ]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("profile_python", {"request": {"script": "workload.py"}})])
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
        tmp_path, tmp_path, frozenset({"profile_python", "execute_command"}), command, python_profile=profile
    )
    with pytest.raises(RuntimeError, match="without writing any project source"):
        await execute_agentic(
            tmp_path, "Profile a workload", "pyfly", "none", emit, model=FunctionModel(model), runtime_context=context
        )
    assert calls == ["workload.py"]
    assert seen == [
        {
            "recipe": "forge-python-cprofile-v1",
            "exit_code": 7,
            "output": "Fixture diagnostic observation",
            "observation_only": True,
        }
    ]


@pytest.mark.asyncio
async def test_profile_arguments_reject_known_credentials_before_dispatch(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pyfly.agentic.exceptions import ToolError

    async def forbidden(*args):
        raise AssertionError("Credential-bearing request cannot dispatch")

    def model(messages, info):
        return ModelResponse(
            parts=[
                ToolCallPart("profile_python", {"request": {"script": "workload.py", "args": ["owned-secret-token"]}})
            ]
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
                frozenset({"execute_command", "profile_python"}),
                forbidden,
                credential_registry=lambda: {"FORGE_TOKEN": "owned-secret-token"},
                python_profile=forbidden,
            ),
        )


@pytest.mark.asyncio
async def test_profile_observation_text_is_redacted_before_model_admission(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    observed = []

    async def profile(request):
        return {"output": "owned-secret-token", "exit_code": 0, "observation_only": True}

    async def forbidden(*args):
        raise AssertionError("Only the recipe callback is permitted")

    def model(messages, info):
        returned = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "profile_python"
        ]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("profile_python", {"request": {"script": "workload.py"}})])
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
                frozenset({"execute_command", "profile_python"}),
                forbidden,
                credential_registry=lambda: {"FORGE_TOKEN": "owned-secret-token"},
                python_profile=profile,
            ),
        )
    assert observed == [{"output": "[redacted]", "exit_code": 0, "observation_only": True}]
