# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from firefly_forge.agentic_runtime import execute_agentic
from firefly_forge.workspace_tools import ToolRuntimeContext


@pytest.mark.asyncio
async def test_native_knowledge_lookup_needs_no_search_key_and_preserves_explicit_refresh_identity(tmp_path):
    calls = []

    async def lookup(query, refresh, request_id):
        calls.append((query, refresh, request_id))
        return {
            "origin": "candidate_reference",
            "untrusted": True,
            "results": [{"source_id": "owned-source", "content": "Scoped reference"}],
            "knowledge_decision": {"reason": "no_candidate", "web_provider_dispatched": False},
        }

    def model(messages, info):
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returns:
            assert {tool.name for tool in info.function_tools} == {"knowledge_lookup", "write_file"}
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "knowledge_lookup",
                        {"query": "pyfly unit of work", "refresh": True, "refresh_request_id": "a" * 32},
                    )
                ]
            )
        if not any(part.tool_name == "write_file" for part in returns):
            returned = next(part.content for part in returns if part.tool_name == "knowledge_lookup")
            assert returned["untrusted"] and not returned["knowledge_decision"]["web_provider_dispatched"]
            return ModelResponse(parts=[ToolCallPart("write_file", {"path": "owned.py", "content": "VALUE=1\n"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Scoped reference inspected"})])

    async def emit(kind, message):
        pass

    await execute_agentic(
        tmp_path,
        "Use a scoped reference",
        "pyfly",
        "none",
        emit,
        model=FunctionModel(model),
        runtime_context=ToolRuntimeContext(
            tmp_path, tmp_path, allowed_tools=frozenset({"knowledge_lookup", "write_file"}), knowledge_lookup=lookup
        ),
    )
    assert calls == [("pyfly unit of work", True, "a" * 32)]
    assert (tmp_path / "owned.py").exists()
