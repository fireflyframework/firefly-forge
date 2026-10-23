# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Configured tool-free coordinator responses through the real structured runtime."""

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from firefly_forge.model_profiles import ModelProfileInput
from firefly_forge.scm_natural_runtime import classify
from firefly_forge.task_routing import TaskRoutingInput


def native_coordinator(case, monkeypatch, *, intent="status"):
    router = case.store.task_router
    router.profiles.save(
        "controlled-coordinator",
        ModelProfileInput(
            name="Controlled natural coordinator",
            provider="agentic",
            values={"model": "openai-responses:public-coordinator", "api_key": "owned-coordinator-fixture-key"},
        ),
    )
    bindings = {**router.current()["bindings"], "communication": "controlled-coordinator"}
    case.http.portal.call(router.save, TaskRoutingInput(bindings=bindings))
    calls = []

    async def controlled(message, context, settings):
        assert settings["ASSISTANT_PROVIDER"] == "agentic"
        assert settings["FORGE_AGENTIC_MODEL"] == "openai-responses:public-coordinator"

        def model(messages, info):
            assert not info.function_tools
            assert "run_id" not in info.output_tools[0].parameters_json_schema["properties"]
            calls.append((message, context))
            if case.state.get("coordinator_unknown"):
                raise RuntimeError("Controlled unknown coordinator outcome")
            selection = context["status_context"]["selection"]
            language = "es" if "cómo" in message else "en"
            text = (
                "No encuentro una ejecución vinculada a este ticket."
                if language == "es"
                else "No execution is linked to this ticket."
            )
            if selection == "ambiguous":
                text = "There is more than one linked execution. Which one do you mean?"
            elif selection == "run":
                text = "This is the current authenticated ticket execution."
            return ModelResponse(
                parts=[
                    ToolCallPart(info.output_tools[0].name, {"intent": intent, "message": text, "language": language})
                ]
            )

        return await classify(message, context, settings, model=FunctionModel(model))

    monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", controlled)
    return calls
