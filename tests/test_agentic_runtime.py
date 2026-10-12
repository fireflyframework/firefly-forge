# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import pytest

from firefly_forge.agentic_runtime import WorkspaceTools, execute_agentic


@pytest.mark.asyncio
async def test_workspace_tools_write_and_read_real_application_source(tmp_path):
    tools = WorkspaceTools(tmp_path)
    await tools.write_file("src/calculator.py", "def add(a, b):\n    return a + b\n")
    assert "return a + b" in await tools.read_file("src/calculator.py")
    assert "src/calculator.py" in await tools.list_files()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        "/tmp/escape.py",
        ".git/config",
        "a/../../escape.py",
        ".env.local",
        "credentials.json",
        ".forge/state.json",
        "private.pem",
    ],
)
async def test_workspace_tools_reject_escape_and_git_metadata(tmp_path, path):
    with pytest.raises(ValueError):
        await WorkspaceTools(tmp_path).write_file(path, "bad")


@pytest.mark.asyncio
async def test_workspace_tools_do_not_follow_symlinks_even_inside_workspace(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        await WorkspaceTools(tmp_path).write_file("link/file.py", "bad")


@pytest.mark.asyncio
async def test_workspace_tools_cannot_overwrite_a_hardlink_to_external_file(tmp_path):
    import os

    outside = tmp_path.parent / f"{tmp_path.name}-external.py"
    outside.write_text("preserve me")
    os.link(outside, tmp_path / "linked.py")
    with pytest.raises(ValueError, match="link"):
        await WorkspaceTools(tmp_path).write_file("linked.py", "changed")
    assert outside.read_text() == "preserve me"


@pytest.mark.asyncio
async def test_workspace_tool_size_is_bounded(tmp_path):
    with pytest.raises(ValueError, match="limit"):
        await WorkspaceTools(tmp_path, max_file_bytes=4).write_file("big.py", "12345")


@pytest.mark.asyncio
async def test_native_firefly_agent_calls_workspace_tools_and_returns_actual_usage(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    def model(messages, info):
        written = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "write_file"
            for message in messages
            for part in message.parts
        )
        if not written:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "write_file",
                        {"path": "src/service.py", "content": "def stock_alert(qty):\n    return qty < 5\n"},
                    )
                ]
            )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Stock alert implemented"})])

    async def emit(kind, message):
        pass

    summary, usage = await execute_agentic(
        tmp_path, "Implement low stock alerts", "pyfly", "none", emit, model=FunctionModel(model)
    )
    assert summary == "Stock alert implemented"
    assert (tmp_path / "src/service.py").is_file()
    assert usage["requests"] == 2
    assert usage["files_written"] == ["src/service.py"]


@pytest.mark.asyncio
async def test_agent_can_recover_from_missing_repository_instruction_file(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    def model(messages, info):
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "AGENTS.md"})])
        if not any(part.tool_name == "write_file" for part in returns):
            assert "unavailable" in str(returns[0].content).lower()
            return ModelResponse(parts=[ToolCallPart("write_file", {"path": "service.py", "content": "VALUE = 1\n"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Recovered and built"})])

    async def emit(kind, message):
        pass

    summary, usage = await execute_agentic(
        tmp_path, "Create service", "pyfly", "none", emit, model=FunctionModel(model)
    )
    assert summary == "Recovered and built"
    assert usage["requests"] == 3
