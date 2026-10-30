# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Model-facing evidence contracts reject malformed input before native reads."""

import json

import pytest

from firefly_forge.run_evidence import EvidenceQuery


@pytest.mark.parametrize("invalid", [{"tool_name": "execute_command"}, {"limit": 11}, {"after": 1}])
async def test_model_can_correct_invalid_evidence_query_before_native_callback(tmp_path, invalid):
    from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.workspace_tools import ToolRuntimeContext

    calls, turns = [], []

    async def read(query):
        EvidenceQuery.model_validate(query)
        assert query.get("limit", 50) <= 10
        calls.append(query)
        return {"items": []}

    def model(messages, info):
        turns.append(messages)
        if len(turns) == 1:
            return ModelResponse(parts=[ToolCallPart("read_run_evidence", {"query": invalid})])
        if len(turns) == 2:
            assert not calls
            assert not any(isinstance(part, RetryPromptPart) for message in messages for part in message.parts)
            result = messages[-1].parts[-1]
            assert isinstance(result, ToolReturnPart) and result.content == {"status": "invalid_query"}
            return ModelResponse(parts=[ToolCallPart("read_run_evidence", {"query": {}})])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "summary": "Read retained evidence",
                        "worker_feedback": {
                            "id": "review",
                            "role_id": "implementer",
                            "task_id": "implement",
                            "attempt": 1,
                            "disposition": "clarification",
                            "summary": "Evidence reviewed",
                            "request": {
                                "id": "review",
                                "kind": "product_definition",
                                "question": "Proceed?",
                                "context": "Fixture",
                            },
                        },
                    },
                )
            ]
        )

    async def emit(*args):
        pass

    await execute_agentic(
        tmp_path,
        "Read evidence",
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
    assert calls == [{"limit": 10}]
    assert len(turns) == 3


def clarification_response(info):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "summary": "Read retained evidence",
                    "worker_feedback": {
                        "id": "review",
                        "role_id": "implementer",
                        "task_id": "implement",
                        "attempt": 1,
                        "disposition": "clarification",
                        "summary": "Evidence reviewed",
                        "request": {
                            "id": "review",
                            "kind": "product_definition",
                            "question": "Proceed?",
                            "context": "Fixture",
                        },
                    },
                },
            )
        ]
    )


async def run_evidence_model(tmp_path, model, read, **context_options):
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.workspace_tools import ToolRuntimeContext

    async def emit(*args):
        pass

    return await execute_agentic(
        tmp_path,
        "Read evidence",
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
            **context_options,
        ),
    )


async def test_advertised_schema_preserves_query_envelope_and_worker_bound(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    calls = []

    async def read(query):
        calls.append(query)
        return {"items": []}

    def model(messages, info):
        schema = info.function_tools[0].parameters_json_schema
        assert set(schema["properties"]) == {"query"}
        assert schema["required"] == ["query"]
        assert schema["additionalProperties"] is False
        query = schema["properties"]["query"]
        if "$ref" in query:
            query = schema["$defs"][query["$ref"].rsplit("/", 1)[1]]
        assert query["additionalProperties"] is False
        limit = query["properties"]["limit"]
        assert limit["default"] == limit["maximum"] == 10
        assert limit["minimum"] == 1 and limit["type"] == "integer"
        assert "tool_name" not in query["properties"]
        assert query["properties"]["kind"]["enum"] == ["all", "journal", "activity", "tool", "artifact"]
        if not calls:
            return ModelResponse(parts=[ToolCallPart("read_run_evidence", {"query": {}})])
        return clarification_response(info)

    await run_evidence_model(tmp_path, model, read)
    assert calls == [{"limit": 10}]


@pytest.mark.parametrize(
    "query",
    [
        {"limit": "1"},
        {"limit": True},
        {"limit": 1.0},
        {"limit": 0},
        {"after": False},
        {"text": 12},
        {"kind": "tools"},
        {"run_id": "a" * 32},
        {"after": 1, "expected_cut": "a" * 64},
        {"after": 1, "expected_cut": "a" * 64, "expected_filter": "invalid"},
        {"record_id": "journal:1", "text": "filter"},
        {"record_id": "tool:" + "a" * 32, "kind": "tool"},
    ],
)
def test_worker_query_retains_strict_types_unknown_fields_and_pagination_invariants(query):
    from pydantic import ValidationError

    from firefly_forge.worker_evidence_query import WorkerEvidenceReadInput

    with pytest.raises(ValidationError):
        WorkerEvidenceReadInput.model_validate({"query": query})


@pytest.mark.parametrize("arguments", [{}, {"limit": 1}, {"query": {}, "run_id": "a" * 32}])
def test_worker_input_requires_exact_query_envelope(arguments):
    from pydantic import ValidationError

    from firefly_forge.worker_evidence_query import WorkerEvidenceReadInput

    with pytest.raises(ValidationError):
        WorkerEvidenceReadInput.model_validate(arguments)


async def test_valid_pagination_is_forwarded_without_expanding_callback_authority(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    query = {"after": 1, "expected_cut": "a" * 64, "expected_filter": "b" * 64, "kind": "tool", "limit": 2}
    calls = []

    async def read(value):
        calls.append(value)
        return {"items": []}

    def model(messages, info):
        if not calls:
            return ModelResponse(parts=[ToolCallPart("read_run_evidence", {"query": query})])
        return clarification_response(info)

    await run_evidence_model(tmp_path, model, read)
    assert calls == [query]


@pytest.mark.parametrize(
    "calls_per_turn,limit,turn_count", [(1, "request_limit of 100", 100), (4, "tool_calls_limit of 300", 76)]
)
async def test_invalid_input_consumes_existing_usage_budgets_without_native_reads(
    tmp_path, calls_per_turn, limit, turn_count
):
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart

    turns, calls = [], []

    async def read(query):
        calls.append(query)
        return {"items": []}

    def model(messages, info):
        turns.append(messages)
        assert not any(isinstance(part, RetryPromptPart) for message in messages for part in message.parts)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "read_run_evidence",
                    {"query": {"tool_name": "execute_command"}},
                    tool_call_id=f"invalid-{len(turns)}-{index}",
                )
                for index in range(calls_per_turn)
            ]
        )

    with pytest.raises(UsageLimitExceeded, match=limit):
        await run_evidence_model(tmp_path, model, read)
    assert calls == []
    assert len(turns) == turn_count


@pytest.mark.parametrize("failure", ["authorization", "integrity", "credential", "reconciliation"])
async def test_native_security_failures_are_terminal_without_model_retry(tmp_path, failure):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pyfly.agentic.exceptions import ToolError

    from firefly_forge.project_access import AccessDenied
    from firefly_forge.run_evidence import EvidenceUnavailable
    from firefly_forge.tool_broker import ToolReconciliationRequired

    errors = {
        "authorization": AccessDenied("execution denied"),
        "integrity": EvidenceUnavailable("evidence_integrity"),
        "credential": ValueError("credential_admission"),
        "reconciliation": ToolReconciliationRequired("retained effect requires reconciliation"),
    }
    turns, calls = [], []

    async def read(query):
        calls.append(query)
        raise errors[failure]

    def model(messages, info):
        turns.append(messages)
        return ModelResponse(parts=[ToolCallPart("read_run_evidence", {"query": {}})])

    expected = ToolReconciliationRequired if failure == "reconciliation" else ToolError
    with pytest.raises(expected):
        await run_evidence_model(tmp_path, model, read)
    assert calls == [{"limit": 10}]
    assert len(turns) == 1


async def test_credential_admission_refuses_before_native_callback_without_retry(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pyfly.agentic.exceptions import ToolError

    turns, calls = [], []

    async def read(query):
        calls.append(query)
        return {"items": []}

    async def refuse(value):
        if isinstance(value, list) and len(value) == 2 and isinstance(value[1], dict) and "query" in value[1]:
            raise ValueError("credential_admission")

    def model(messages, info):
        turns.append(messages)
        return ModelResponse(parts=[ToolCallPart("read_run_evidence", {"query": {}})])

    with pytest.raises(ToolError):
        await run_evidence_model(tmp_path, model, read, credential_admission=refuse)
    assert calls == []
    assert len(turns) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"limit": 1},
        {"query": None},
        {"query": []},
        {"query": {}, "run_id": "a" * 32},
        {"query": {"text": "fictional-private-input", "tool_name": "unregistered"}},
    ],
)
async def test_malformed_envelope_returns_only_fixed_error_before_native_read(tmp_path, arguments):
    from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart

    turns, calls = [], []

    async def read(query):
        calls.append(query)
        return {"items": []}

    def model(messages, info):
        turns.append(messages)
        if len(turns) == 1:
            return ModelResponse(parts=[ToolCallPart("read_run_evidence", arguments)])
        assert not any(isinstance(part, RetryPromptPart) for message in messages for part in message.parts)
        result = messages[-1].parts[-1]
        assert isinstance(result, ToolReturnPart) and result.content == {"status": "invalid_query"}
        return clarification_response(info)

    await run_evidence_model(tmp_path, model, read)
    assert calls == [] and len(turns) == 2


async def test_credential_guard_refuses_invalid_query_before_manual_validation(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pyfly.agentic.exceptions import ToolError

    turns, calls = [], []
    secret = "fictional-configured-evidence-token"

    async def read(query):
        calls.append(query)
        return {"items": []}

    def model(messages, info):
        turns.append(messages)
        return ModelResponse(parts=[ToolCallPart("read_run_evidence", {"query": {"tool_name": secret}})])

    with pytest.raises(ToolError, match="cannot contain configured credentials"):
        await run_evidence_model(tmp_path, model, read, credential_registry=lambda: {"FORGE_TOKEN": secret})
    assert calls == [] and len(turns) == 1


async def test_malformed_json_returns_checkpointable_result_without_retry(tmp_path, monkeypatch):
    import pyfly.agentic.agents as agents
    from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart

    from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary, BoundaryCheckpoint

    boundary = AgenticBoundary(lambda: {"files_written": []})
    constructor = agents.Agent

    def with_boundary(**kwargs):
        kwargs["capabilities"].append(boundary)
        return constructor(**kwargs)

    monkeypatch.setattr(agents, "Agent", with_boundary)
    turns, calls, checkpoints = [], [], []

    async def read(query):
        calls.append(query)
        return {"items": []}

    def model(messages, info):
        checkpoints.append(boundary.capture(b"fictional-malformed-json-wire"))
        turns.append(messages)
        if len(turns) == 1:
            return ModelResponse(parts=[ToolCallPart("read_run_evidence", '{"query":')])
        assert not calls
        assert not any(isinstance(part, RetryPromptPart) for message in messages for part in message.parts)
        result = messages[-1].parts[-1]
        assert isinstance(result, ToolReturnPart) and result.content == {"status": "invalid_query"}
        return clarification_response(info)

    with boundary.invocation():
        await run_evidence_model(tmp_path, model, read)
    assert calls == [] and len(turns) == len(checkpoints) == 2
    restored = BoundaryCheckpoint.decode(checkpoints[1].payload, checkpoints[1].digest).resume_kwargs()
    assert restored["usage"].requests == restored["usage"].tool_calls == 1
    assert restored["usage_limits"].request_limit == 100
    assert restored["usage_limits"].tool_calls_limit == 300


@pytest.mark.parametrize("credential_guard", ["registry", "admission"])
async def test_malformed_json_credentials_are_refused_before_recovery(tmp_path, credential_guard):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    turns, calls, admitted = [], [], []
    secret = "fictional-malformed-json-token"

    async def read(query):
        calls.append(query)
        return {"items": []}

    async def admit(value):
        admitted.append(value)
        if secret in json.dumps(value):
            raise ValueError("credential_admission")

    def model(messages, info):
        turns.append(messages)
        return ModelResponse(parts=[ToolCallPart("read_run_evidence", '{"query":"' + secret)])

    options = (
        {"credential_registry": lambda: {"FORGE_TOKEN": secret}}
        if credential_guard == "registry"
        else {
            "credential_admission": admit,
        }
    )
    with pytest.raises(ValueError, match="configured credentials|credential_admission"):
        await run_evidence_model(tmp_path, model, read, **options)
    assert calls == [] and len(turns) == 1
    if credential_guard == "admission":
        assert any(secret in json.dumps(value) for value in admitted)


def test_versioned_evidence_prompt_explains_rejected_queries_without_empty_page_claim():
    from firefly_forge.prompts import prompt_text

    description = prompt_text("tools/read_run_evidence", version="1.0.1")
    assert "invalid_query" in description and "no evidence" in description
    assert "checkpoint" in description and "ten" in description


@pytest.mark.parametrize("arguments", ["[]", "null", "true", "17", '"invalid-envelope"'])
async def test_non_object_json_arguments_return_invalid_query_without_native_reads(tmp_path, arguments):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart

    turns, calls = [], []

    async def read(query):
        calls.append(query)
        return {"items": []}

    def model(messages, info):
        turns.append(messages)
        if len(turns) == 1:
            return ModelResponse(parts=[ToolCallPart("read_run_evidence", arguments)])
        result = messages[-1].parts[-1]
        assert isinstance(result, ToolReturnPart) and result.content == {"status": "invalid_query"}
        return clarification_response(info)

    await run_evidence_model(tmp_path, model, read)
    assert not calls and len(turns) == 2
