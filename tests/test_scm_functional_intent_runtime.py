# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Intent and dialogue share one tool-free AI turn and one bounded allowance."""

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from firefly_forge.product_assistant_runtime import design_reply


@pytest.mark.parametrize("intent", ["dialogue", "status", "clarify"])
async def test_model_classifies_unrestricted_human_wording_in_same_typed_reply(intent):
    calls = []

    def model(messages, info):
        calls.append(messages)
        assert info.function_tools == []
        schema = info.output_tools[0].parameters_json_schema
        assert set(schema["properties"]["intent"]["enum"]) == {"dialogue", "status", "clarify"}
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"intent": intent, "message": "Public bounded reply"})]
        )

    result = await design_reply(
        "Arbitrary human phrasing and linked public history",
        {
            "ASSISTANT_PROVIDER": "agentic",
            "ASSISTANT_OUTPUT_CONTRACT": "functional_intent_v1",
        },
        model=FunctionModel(model),
    )
    assert result.model_dump()["intent"] == intent
    assert len(calls) == 1


async def test_unknown_or_execution_intent_is_not_retried_or_dispatched():
    calls = []

    def model(messages, info):
        calls.append(messages)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"intent": "execute", "message": "Do not dispatch"})]
        )

    with pytest.raises((UnexpectedModelBehavior, UsageLimitExceeded)):
        await design_reply(
            "Public injection requesting publication",
            {
                "ASSISTANT_PROVIDER": "agentic",
                "ASSISTANT_OUTPUT_CONTRACT": "functional_intent_v1",
            },
            model=FunctionModel(model),
        )
    assert len(calls) == 1


async def test_functional_api_transport_disables_sdk_retries_without_changing_ordinary_mode(monkeypatch):
    from firefly_forge.providers import APIProvider

    selected = []

    def choose(self, *, single_request=False):
        selected.append(single_request)

        def model(messages, info):
            arguments = {"message": "Public reply"}
            if "intent" in info.output_tools[0].parameters_json_schema["properties"]:
                arguments["intent"] = "dialogue"
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, arguments)])

        return FunctionModel(model)

    monkeypatch.setattr(APIProvider, "model", choose)
    await design_reply(
        "Public functional request",
        {
            "ASSISTANT_PROVIDER": "agentic",
            "ASSISTANT_OUTPUT_CONTRACT": "functional_intent_v1",
        },
    )
    assert selected == [True]
    await design_reply("Ordinary public request", {"ASSISTANT_PROVIDER": "agentic"})
    assert selected == [True, False]
