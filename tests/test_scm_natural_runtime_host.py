# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The configured model classifies public wording without tools or invented task IDs."""

import importlib
import importlib.util
import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel


@pytest.mark.parametrize("provider", ["agentic", "bridge"])
@pytest.mark.parametrize("context_bytes", [101900, 1500000])
async def test_long_natural_context_reaches_configured_model_without_truncation(provider, context_bytes):
    from pydantic_ai.messages import TextPart, UserPromptPart

    from firefly_forge.scm_natural_runtime import catalog, classify

    evidence = "First retained fact. " + "á" * context_bytes + " Last retained fact."
    context = {"history": [{"human": evidence}], "candidates": []}
    expected = catalog().text(
        "scm-natural/request",
        context=json.dumps({**context, "question_choices": []}, ensure_ascii=False),
        message="Status?",
    )
    calls = []

    def model(messages, info):
        calls.append(messages)
        prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        assert expected in prompts
        assert not info.function_tools
        value = {"intent": "status", "message": "I will check the recorded progress."}
        return ModelResponse(
            parts=[TextPart(json.dumps(value))]
            if provider == "bridge"
            else [ToolCallPart(info.output_tools[0].name, value)]
        )

    result = await classify("Status?", context, {"ASSISTANT_PROVIDER": provider}, model=FunctionModel(model))
    assert result.intent == "status" and len(calls) == 1


@pytest.mark.parametrize("provider", ["agentic", "bridge"])
async def test_natural_intent_uses_one_configured_tool_free_model_response(provider):
    assert importlib.util.find_spec("firefly_forge.scm_natural_runtime"), "Natural classifier is missing"
    runtime = importlib.import_module("firefly_forge.scm_natural_runtime")
    calls = []

    def model(messages, info):
        calls.append(messages)
        assert not info.function_tools
        from firefly_forge.community_skills import community_catalog

        skill = next(item for item in community_catalog() if item["id"] == "humanizer")
        assert skill["instructions"] in info.instructions
        value = {"intent": "answer", "message": "I will retain your answer.", "choice": None}
        if provider == "bridge":
            import json

            from pydantic_ai.messages import TextPart

            return ModelResponse(parts=[TextPart(json.dumps(value))])
        schema = info.output_tools[0].parameters_json_schema
        assert set(schema["properties"]) == {
            "intent",
            "message",
            "choice",
            "language",
            "questions",
            "proposal",
            "selection_evidence",
        }
        assert "product" in schema["properties"]["intent"]["enum"]
        assert "run_id" not in schema["properties"] and "conversation_id" not in schema["properties"]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, value)])

    result = await runtime.classify(
        "El equipo de ventas debe revisarlo.",
        {"questions": [], "history": []},
        {"ASSISTANT_PROVIDER": provider},
        model=FunctionModel(model),
    )
    assert result.intent == "answer" and len(calls) == 1
    assert "El equipo de ventas" in repr(calls)


async def test_execution_intent_or_identifier_injection_has_no_retry():
    assert importlib.util.find_spec("firefly_forge.scm_natural_runtime"), "Natural classifier is missing"
    runtime = importlib.import_module("firefly_forge.scm_natural_runtime")
    calls = []

    def model(messages, info):
        calls.append(messages)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "intent": "publish",
                        "message": "Unsafe",
                        "run_id": "f" * 32,
                    },
                )
            ]
        )

    with pytest.raises(runtime.ConfirmedInvalidReply):
        await runtime.classify("Publish it", {}, {"ASSISTANT_PROVIDER": "agentic"}, model=FunctionModel(model))
    assert len(calls) == 1


async def test_confirmed_invalid_complete_reply_can_be_distinguished_from_unknown_transport():
    runtime = importlib.import_module("firefly_forge.scm_natural_runtime")
    assert hasattr(runtime, "ConfirmedInvalidReply"), "Confirmed invalid output needs a separate repair contract"

    def invalid(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"intent": "publish", "message": "No"})])

    with pytest.raises(runtime.ConfirmedInvalidReply) as failure:
        await runtime.classify("Public message", {}, {"ASSISTANT_PROVIDER": "agentic"}, model=FunctionModel(invalid))
    assert failure.value.errors and "publish" not in str(failure.value.errors)

    def unknown(messages, info):
        import httpx

        raise httpx.ReadError("Controlled unknown provider outcome")

    import httpx

    with pytest.raises(httpx.ReadError):
        await runtime.classify("Public message", {}, {"ASSISTANT_PROVIDER": "agentic"}, model=FunctionModel(unknown))
