# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""New foreground tools use explicit optional work caps without changing the legacy wire."""

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from firefly_forge.agentic_runtime import execute_agentic
from firefly_forge.process import ProcessResult
from firefly_forge.work_duration_models import WorkDurationPolicyV2
from firefly_forge.workspace_tools import ToolRuntimeContext, WorkspaceTools


@pytest.mark.parametrize(
    "policy,arguments,expected,outer",
    [
        (None, {}, 300, 1.0),
        (WorkDurationPolicyV2(), {}, None, None),
        (WorkDurationPolicyV2(), {"timeout": None}, None, None),
        (WorkDurationPolicyV2(timeout_seconds=8), {}, 8, 8),
        (WorkDurationPolicyV2(timeout_seconds=8), {"timeout": 4}, 4, 8),
    ],
)
async def test_frozen_policy_selects_command_schema_and_actual_executor_timeout(
    tmp_path, monkeypatch, policy, arguments, expected, outer
):
    from fireflyframework_agentic.agents import FireflyAgent

    executed, deadlines, schemas = [], [], []
    original = FireflyAgent.run

    async def run(agent, *args, **kwargs):
        deadlines.append(kwargs["timeout"])
        return await original(agent, *args, **kwargs)

    async def manifest(self):
        return {"service.py": "changed"} if (tmp_path / "service.py").exists() else {}

    async def command(argv, timeout):
        executed.append((argv, timeout))
        (tmp_path / "service.py").write_text("VALUE = 1\n")
        return ProcessResult(argv, 0, "finished")

    def model(messages, info):
        schemas.extend(tool.parameters_json_schema for tool in info.function_tools if tool.name == "execute_command")
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart("execute_command", {"argv": ["fixture"], **arguments})])
        return ModelResponse(parts=[TextPart('{"summary":"done"}')])

    async def emit(*args):
        pass

    monkeypatch.setattr(FireflyAgent, "run", run)
    monkeypatch.setattr(WorkspaceTools, "source_manifest", manifest)
    context = ToolRuntimeContext(
        tmp_path,
        tmp_path,
        frozenset({"execute_command"}),
        command,
        **({"work_duration": policy} if policy is not None else {}),
    )
    summary, _ = await execute_agentic(
        tmp_path,
        "Inspect",
        "pyfly",
        "none",
        emit,
        model=FunctionModel(model),
        allow_output_token_cap=False,
        runtime_context=context,
        settings={"FORGE_PROVIDER_TIMEOUT": "1"},
    )
    assert summary == "done" and executed == [(["fixture"], expected)]
    assert deadlines == [outer]
    timeout_schema = schemas[0]["properties"]["timeout"]
    if policy is None:
        assert timeout_schema == {"default": 300, "type": "integer"}
    else:
        assert timeout_schema == {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}


def test_automatic_verification_has_no_implicit_ten_minute_work_cap(tmp_path):
    from firefly_forge.engine import default_commands

    (tmp_path / "Makefile").write_text("check:\n\t@true\n")
    assert default_commands(tmp_path)[0].timeout == 600
    assert default_commands(tmp_path, work_duration=WorkDurationPolicyV2())[0].timeout is None


def test_cli_provider_duration_uses_the_selected_work_policy_only(tmp_path):
    from firefly_forge.providers import ConfiguredProvider

    provider = ConfiguredProvider({"FORGE_PROVIDER_TIMEOUT": "23"})
    assert provider.execution_timeout() == 23
    provider.configure_runtime(scope_root=tmp_path, repository_root=tmp_path, work_duration=WorkDurationPolicyV2())
    assert provider.execution_timeout() is None
    provider.configure_runtime(
        scope_root=tmp_path, repository_root=tmp_path, work_duration=WorkDurationPolicyV2(timeout_seconds=864000)
    )
    assert provider.execution_timeout() == 864000


def test_provider_work_policy_can_be_selected_without_granting_workspace_tools():
    from firefly_forge.providers import ConfiguredProvider

    provider = ConfiguredProvider({"FORGE_PROVIDER_TIMEOUT": "23"})
    provider.configure_work_duration(WorkDurationPolicyV2())
    assert provider.execution_timeout() is None and provider.runtime_context is None
    provider.configure_work_duration(None)
    assert provider.execution_timeout() == 23 and provider.runtime_context is None


async def test_api_role_without_a_native_runtime_context_has_no_hidden_outer_cap(tmp_path, monkeypatch):
    from fireflyframework_agentic.agents import FireflyAgent

    from firefly_forge.providers import APIProvider

    deadlines = []
    original = FireflyAgent.run

    async def run(agent, *args, **kwargs):
        deadlines.append(kwargs["timeout"])
        return await original(agent, *args, **kwargs)

    def model(messages, info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart("write_file", {"path": "service.py", "content": "VALUE = 1"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"summary": "done"})])

    async def emit(*args):
        pass

    provider = APIProvider({"FORGE_PROVIDER_TIMEOUT": "1"})
    provider.configure_work_duration(WorkDurationPolicyV2())
    monkeypatch.setattr(provider, "model", lambda: FunctionModel(model))
    monkeypatch.setattr(FireflyAgent, "run", run)
    assert (await provider.execute(tmp_path, "Inspect", "pyfly", "none", emit)).summary == "done"
    assert deadlines == [None]
