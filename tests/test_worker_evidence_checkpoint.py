# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An invalid read must remain correctable at actual checkpoint boundaries."""

import json

import pytest


@pytest.mark.parametrize("invalid", [{"query": {"tool_name": "read_file"}}, '{"query":', "[]", "null"])
async def test_invalid_evidence_query_can_be_corrected_with_checkpoint_capability(tmp_path, monkeypatch, invalid):
    import fireflyframework_agentic.agents as agents
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary, BoundaryCheckpoint
    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.workspace_tools import ToolRuntimeContext

    boundary = AgenticBoundary(lambda: {"files_written": []})
    constructor = agents.FireflyAgent

    def with_boundary(**kwargs):
        kwargs["capabilities"].append(boundary)
        return constructor(**kwargs)

    monkeypatch.setattr(agents, "FireflyAgent", with_boundary)
    calls, turns, checkpoints = [], [], []

    async def read(query):
        calls.append(query)
        return {"items": []}

    def model(messages, info):
        checkpoints.append(boundary.capture(b"controlled-checkpoint-wire"))
        turns.append(messages)
        if len(turns) == 1:
            return ModelResponse(parts=[ToolCallPart("read_run_evidence", invalid)])
        if len(turns) == 2:
            result = messages[-1].parts[-1]
            assert isinstance(result, ToolReturnPart)
            assert result.content["status"] == "invalid_query"
            assert not calls
            return ModelResponse(parts=[ToolCallPart("read_run_evidence", {"query": {"limit": 1}})])
        return ModelResponse(
            parts=[
                TextPart(
                    json.dumps(
                        {
                            "summary": "Evidence read without replay",
                            "worker_feedback": {
                                "id": "review",
                                "role_id": "implementer",
                                "task_id": "implement",
                                "attempt": 1,
                                "disposition": "clarification",
                                "summary": "Review retained work",
                                "request": {
                                    "id": "review",
                                    "kind": "product_definition",
                                    "question": "Proceed?",
                                    "context": "Fixture",
                                },
                            },
                        }
                    )
                )
            ]
        )

    async def emit(*args):
        pass

    with boundary.invocation():
        await execute_agentic(
            tmp_path,
            "Read retained evidence",
            "pyfly",
            "none",
            emit,
            model=FunctionModel(model),
            settings={},
            runtime_context=ToolRuntimeContext(
                tmp_path,
                tmp_path,
                allowed_tools=frozenset({"read_run_evidence"}),
                read_run_evidence=read,
            ),
        )
    assert calls == [{"limit": 1}] and len(turns) == len(checkpoints) == 3
    for checkpoint in checkpoints:
        decoded = BoundaryCheckpoint.decode(checkpoint.payload, checkpoint.digest)
        assert decoded.wire_sha256 == checkpoint.wire_sha256
        assert decoded.resume_kwargs()["usage_limits"].request_limit == 100
        assert decoded.resume_kwargs()["usage_limits"].tool_calls_limit == 300


async def test_restore_after_invalid_query_preserves_history_usage_and_does_not_replay(tmp_path, monkeypatch):
    import fireflyframework_agentic.agents as agents
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from test_worker_evidence_tool_contract import clarification_response

    from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary
    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.workspace_tools import ToolRuntimeContext

    class CutAfterInvalid(BaseException):
        pass

    boundary = AgenticBoundary(lambda: {"files_written": []})
    constructor = agents.FireflyAgent
    saved, checkpoints, native_reads, original_turns, restored_turns = [], [], [], [], []

    def with_boundary(**kwargs):
        kwargs["capabilities"].append(boundary)
        saved.append(kwargs)
        return constructor(**kwargs)

    monkeypatch.setattr(agents, "FireflyAgent", with_boundary)

    async def read(query):
        native_reads.append(query)
        return {"items": []}

    def original_model(messages, info):
        checkpoints.append(boundary.capture(b"controlled-exact-restoration-wire"))
        original_turns.append(messages)
        if len(original_turns) == 1:
            return ModelResponse(parts=[ToolCallPart("read_run_evidence", {"query": {"tool_name": "read_file"}})])
        assert isinstance(messages[-1].parts[-1], ToolReturnPart)
        assert messages[-1].parts[-1].content == {"status": "invalid_query"}
        raise CutAfterInvalid

    async def emit(*args):
        pass

    with boundary.invocation(), pytest.raises(CutAfterInvalid):
        await execute_agentic(
            tmp_path,
            "Read retained evidence",
            "pyfly",
            "none",
            emit,
            model=FunctionModel(original_model),
            settings={},
            runtime_context=ToolRuntimeContext(
                tmp_path,
                tmp_path,
                allowed_tools=frozenset({"read_run_evidence"}),
                read_run_evidence=read,
            ),
        )
    checkpoint = checkpoints[-1]
    assert not native_reads and checkpoint.usage.requests == checkpoint.usage.tool_calls == 1
    restored = AgenticBoundary(lambda: {"files_written": []}, restore=checkpoint)

    def restored_model(messages, info):
        captured = restored.capture(b"controlled-exact-restoration-wire")
        restored_turns.append(messages)
        if len(restored_turns) == 1:
            assert captured.digest == checkpoint.digest and captured.payload == checkpoint.payload
            assert isinstance(messages[-1].parts[-1], ToolReturnPart)
            assert messages[-1].parts[-1].content == {"status": "invalid_query"}
            return ModelResponse(parts=[ToolCallPart("read_run_evidence", {"query": {"limit": 1}})])
        return clarification_response()

    options = {**saved[0], "model": FunctionModel(restored_model)}
    options["capabilities"] = [*options["capabilities"][:-1], restored]
    resumed = constructor(**options)
    with restored.invocation():
        result = await resumed.run(**checkpoint.resume_kwargs())
    assert result.output.summary == "Read retained evidence"
    assert len(original_turns) == len(restored_turns) == 2
    assert native_reads == [{"limit": 1}]
    assert result.usage.requests == 3 and result.usage.tool_calls == 2
