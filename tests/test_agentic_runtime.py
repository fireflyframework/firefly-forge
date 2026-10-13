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


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["blocked", "clarification"])
async def test_native_worker_returns_typed_request_without_claiming_source_writes(tmp_path, disposition):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    feedback = {
        "id": "framework-question",
        "role_id": "implementer",
        "task_id": "implement",
        "attempt": 1,
        "disposition": disposition,
        "summary": "The product needs an inventory ownership decision",
        "request": {
            "id": "inventory-owner",
            "kind": "product_definition",
            "question": "Which organization owns the inventory?",
            "context": "The brief names inventory without defining its owner",
        },
    }

    def model(messages, info):
        return ModelResponse(
            parts=[
                ToolCallPart(info.output_tools[0].name, {"summary": feedback["summary"], "worker_feedback": feedback})
            ]
        )

    async def emit(kind, message):
        pass

    summary, usage = await execute_agentic(
        tmp_path, "Build inventory", "pyfly", "none", emit, model=FunctionModel(model)
    )
    assert summary == feedback["summary"]
    assert usage["worker_feedback"]["disposition"] == disposition
    assert usage["worker_feedback"]["request"]["id"] == "inventory-owner"
    assert usage["files_written"] == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_native_worker_cannot_claim_completed_without_writing_source(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    def model(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Done"})])

    async def emit(kind, message):
        pass

    with pytest.raises(RuntimeError, match="without writing"):
        await execute_agentic(tmp_path, "Build inventory", "pyfly", "none", emit, model=FunctionModel(model))


@pytest.mark.asyncio
async def test_bridge_feedback_uses_prompted_output_and_keeps_local_pattern_validation(tmp_path):
    import json

    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    calls = 0
    feedback = {
        "id": "valid-request",
        "role_id": "implementer",
        "task_id": "implement",
        "attempt": 1,
        "disposition": "clarification",
        "summary": "Inventory ownership needs clarification",
        "request": {
            "id": "owner-question",
            "kind": "product_definition",
            "question": "Who owns the inventory?",
            "context": "The brief omits inventory ownership",
        },
    }

    def model(messages, info):
        nonlocal calls
        calls += 1
        assert not info.output_tools
        assert '"pattern"' not in json.dumps([tool.parameters_json_schema for tool in info.function_tools])
        report = {"summary": feedback["summary"], "worker_feedback": dict(feedback)}
        if calls == 1:
            report["worker_feedback"]["id"] = "invalid identifier with spaces"
        return ModelResponse(parts=[TextPart(json.dumps(report))])

    async def emit(kind, message):
        pass

    _, usage = await execute_agentic(
        tmp_path,
        "Build inventory",
        "pyfly",
        "none",
        emit,
        model=FunctionModel(model),
        settings={},
        allow_output_token_cap=False,
    )
    assert calls == 2
    assert usage["worker_feedback"]["id"] == "valid-request"
    assert usage["files_written"] == []


@pytest.mark.asyncio
async def test_native_tool_grants_register_only_selected_actual_tools(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.workspace_tools import ToolRuntimeContext

    def model(messages, info):
        assert {tool.name for tool in info.function_tools} == {"search_text", "edit_lines"}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Inspect only"})])

    async def emit(kind, message):
        pass

    with pytest.raises(RuntimeError, match="without writing"):
        await execute_agentic(
            tmp_path,
            "Inspect",
            "pyfly",
            "none",
            emit,
            model=FunctionModel(model),
            runtime_context=ToolRuntimeContext(tmp_path, tmp_path, frozenset({"search_text", "edit_lines"})),
        )


@pytest.mark.asyncio
async def test_native_command_tool_uses_injected_executor_and_counts_real_source_changes(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.process import ProcessResult
    from firefly_forge.workspace_tools import ToolRuntimeContext

    calls = []

    async def executor(argv, timeout):
        calls.append((argv, timeout))
        (tmp_path / "service.py").write_text("VALUE = 2\n")
        return ProcessResult(argv, 0, "created")

    def model(messages, info):
        assert {tool.name for tool in info.function_tools} == {"execute_command"}
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(
                parts=[ToolCallPart("execute_command", {"argv": ["python", "build.py"], "timeout": 10})]
            )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Built with bounded runner"})])

    async def emit(kind, message):
        pass

    summary, usage = await execute_agentic(
        tmp_path,
        "Build",
        "pyfly",
        "none",
        emit,
        model=FunctionModel(model),
        runtime_context=ToolRuntimeContext(tmp_path, tmp_path, frozenset({"execute_command"}), executor),
    )
    assert summary == "Built with bounded runner"
    assert calls == [(["python", "build.py"], 10)]
    assert usage["files_written"] == ["service.py"]


@pytest.mark.asyncio
async def test_native_tool_boundary_redacts_known_source_credentials_before_model_admission(tmp_path, monkeypatch):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.workspace_tools import ToolRuntimeContext

    secret = "private-operator-credential-native"
    (tmp_path / "settings.py").write_text(f'TOKEN="{secret}"\n')
    seen = []

    def model(messages, info):
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "settings.py"})])
        seen.append(str(returned[0].content))
        assert secret not in seen[-1]
        if not any(part.tool_name == "write_file" for part in returned):
            return ModelResponse(parts=[ToolCallPart("write_file", {"path": "safe.py", "content": "VALUE=1\n"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Safe source read"})])

    async def emit(kind, message):
        pass

    await execute_agentic(
        tmp_path,
        "Inspect source",
        "pyfly",
        "none",
        emit,
        model=FunctionModel(model),
        runtime_context=ToolRuntimeContext(tmp_path, tmp_path, credential_registry=lambda: {"FORGE_TOKEN": secret}),
    )
    assert "[redacted]" in seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["search_text", "git_diff", "execute_command", "internet_search"])
async def test_native_every_text_tool_redacts_configured_credentials(tmp_path, monkeypatch, tool_name):
    import json

    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.process import ProcessResult
    from firefly_forge.workspace_tools import ToolRuntimeContext

    secret = "private-snapshot-key-model-boundary"
    (tmp_path / "settings.py").write_text("needle " + secret + "\n")

    async def diff(self):
        return {"diff": secret, "truncated": False}

    monkeypatch.setattr(WorkspaceTools, "git_diff", diff)

    async def command(argv, timeout):
        return ProcessResult(argv, 0, secret)

    async def search(query, count):
        return {"results": [{"snippet": secret}]}

    arguments = {
        "search_text": {"text": "needle"},
        "git_diff": {},
        "execute_command": {"argv": ["python", "-V"]},
        "internet_search": {"query": "PyFly docs"},
    }[tool_name]

    def model(messages, info):
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart(tool_name, arguments)])
        result = next(part.content for part in returns if part.tool_name == tool_name)
        assert secret not in json.dumps(result) and "[redacted]" in json.dumps(result)
        if not any(part.tool_name == "write_file" for part in returns):
            return ModelResponse(parts=[ToolCallPart("write_file", {"path": "safe.py", "content": "VALUE=1\n"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Safe tool output"})])

    async def emit(kind, message):
        pass

    await execute_agentic(
        tmp_path,
        "Inspect source",
        "pyfly",
        "none",
        emit,
        model=FunctionModel(model),
        runtime_context=ToolRuntimeContext(
            tmp_path,
            tmp_path,
            command_executor=command,
            internet_search=search,
            credential_registry=lambda: {"FORGE_API_KEY": secret},
        ),
    )


@pytest.mark.asyncio
async def test_native_known_credential_write_input_is_refused_before_file_effect(tmp_path):
    from fireflyframework_agentic.exceptions import ToolError
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.workspace_tools import ToolRuntimeContext

    secret = "private-key-never-write"

    def model(messages, info):
        return ModelResponse(parts=[ToolCallPart("write_file", {"path": "leaked.py", "content": secret})])

    async def emit(kind, message):
        pass

    with pytest.raises(ToolError, match="credential"):
        await execute_agentic(
            tmp_path,
            "Write",
            "pyfly",
            "none",
            emit,
            model=FunctionModel(model),
            runtime_context=ToolRuntimeContext(tmp_path, tmp_path, credential_registry=lambda: {"FORGE_TOKEN": secret}),
        )
    assert not (tmp_path / "leaked.py").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [False, True])
async def test_journey_tool_delivers_real_binary_parts_only_for_verified_image_input(tmp_path, verified):
    import base64
    import hashlib

    from pydantic_ai.messages import BinaryContent, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.qa import EvidenceImage, JourneyEvidence
    from firefly_forge.workspace_tools import ToolRuntimeContext

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    metadata = {
        "evidence_id": "actual-shot",
        "journey_id": "reserve",
        "state_id": "reserved",
        "screenshot_sha256": hashlib.sha256(png).hexdigest(),
        "viewport": {"width": 390, "height": 844},
        "theme": "dark",
        "zoom": 100,
    }

    async def callback(plan):
        return JourneyEvidence(
            {
                "journey_id": "reserve",
                "states": [{"state_id": "reserved", "success": True}],
                "not_reviewed": [],
                "images": [metadata],
            },
            [EvidenceImage(png, metadata)],
        )

    def model(messages, info):
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returned:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "browser_journey",
                        {
                            "plan": {
                                "journey_id": "reserve",
                                "name": "Reserve",
                                "width": 390,
                                "height": 844,
                                "theme": "dark",
                                "expected_states": [{"id": "reserved", "expected": "Success"}],
                                "actions": [
                                    {
                                        "kind": "assert",
                                        "state_id": "reserved",
                                        "selector": "#status",
                                        "assertion": "visible",
                                        "screenshot": True,
                                    }
                                ],
                            }
                        },
                    )
                ]
            )
        if not any(part.tool_name == "write_file" for part in returned):
            parts = [
                item
                for message in messages
                for part in message.parts
                if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
                for item in part.content
            ]
            images = [item for item in parts if isinstance(item, BinaryContent)]
            assert len(images) == int(verified)
            if verified:
                assert images[0].data == png and images[0].media_type == "image/png"
            result = next(part.content for part in returned if part.tool_name == "browser_journey")
            assert result["image_mode"] == ("vision" if verified else "dom_only")
            return ModelResponse(
                parts=[ToolCallPart("write_file", {"path": "qa.md", "content": "Reviewed scoped journey"})]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "summary": "Journey reviewed",
                        "qa_report": {
                            "mode": "vision" if verified else "dom_only",
                            "reviewed_states": ["reserved"],
                            "not_reviewed": [],
                            "findings": [],
                            "summary": "Only the declared state reviewed",
                        },
                    },
                )
            ]
        )

    async def emit(kind, message):
        pass

    _, usage = await execute_agentic(
        tmp_path,
        "Inspect",
        "pyfly",
        "none",
        emit,
        model=FunctionModel(model),
        runtime_context=ToolRuntimeContext(
            tmp_path,
            tmp_path,
            frozenset({"browser_journey", "write_file"}),
            browser_journey=callback,
            image_capability={"status": "verified" if verified else "unverified"},
        ),
    )
    assert usage["qa_report"]["mode"] == ("vision" if verified else "dom_only")


@pytest.mark.asyncio
async def test_model_cannot_claim_vision_review_without_actual_image_evidence(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    def model(messages, info):
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("write_file", {"path": "source.py", "content": "x = 1"})])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"summary": "Claiming visual review", "qa_report": {"mode": "vision", "summary": "All good"}},
                )
            ]
        )

    async def emit(kind, message):
        pass

    with pytest.raises(ValueError, match="without verified attached"):
        await execute_agentic(tmp_path, "Build", "pyfly", "none", emit, model=FunctionModel(model))


@pytest.mark.asyncio
async def test_typed_journey_arguments_reject_known_credentials_before_callback(tmp_path):
    from fireflyframework_agentic.exceptions import ToolError
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.workspace_tools import ToolRuntimeContext

    calls = []

    async def callback(plan):
        calls.append(plan)
        raise ValueError("Callback must never receive configured credentials")

    def model(messages, info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "browser_journey",
                    {
                        "plan": {
                            "journey_id": "private",
                            "name": "Fill field",
                            "expected_states": [{"id": "entered", "expected": "Entered"}],
                            "actions": [
                                {
                                    "kind": "fill",
                                    "state_id": "entered",
                                    "selector": "#field",
                                    "value": "operator-secret",
                                }
                            ],
                        }
                    },
                )
            ]
        )

    async def emit(kind, message):
        pass

    with pytest.raises(ToolError, match="arguments cannot contain"):
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
                frozenset({"browser_journey"}),
                browser_journey=callback,
                credential_registry=lambda: {"FORGE_TOKEN": "operator-secret"},
            ),
        )
    assert calls == []
