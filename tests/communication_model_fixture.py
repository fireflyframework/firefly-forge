# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Schema-routed controlled drafting and semantic-review responses; no provider calls."""

import copy
import json

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel


def communication_phase(info):
    schemas = [tool.parameters_json_schema for tool in info.output_tools]
    definition = info.model_request_parameters.output_object
    if definition is not None:
        schemas.append(definition.json_schema)
    for schema in schemas:
        fields = schema.get("properties", {})
        if "verdict" in fields:
            return "review"
        if {"title", "paragraphs"} <= fields.keys():
            return "draft"
    raise AssertionError("Controlled communication model received an unknown output schema")


def reviewed_model(draft, *, verdict="supported", issues=None, fact_ids=None, calls=None, correction=None):
    reviewed = False
    candidate = None

    def model(messages, info):
        nonlocal reviewed, candidate
        phase = communication_phase(info)
        if calls is not None:
            calls.append((phase, copy.deepcopy(messages), info))
        if phase == "draft":
            value = correction if reviewed and correction is not None else draft
            if callable(value):
                response = value(messages, info)
                part = response.parts[0]
                candidate = part.args_as_dict() if isinstance(part, ToolCallPart) else json.loads(part.content)
                return response
            candidate = copy.deepcopy(value)
            payload = candidate
        else:
            assert candidate is not None, "Semantic review requires a generated candidate"
            reviewed = True
            references = sorted(
                {
                    fact
                    for paragraph in [candidate.get("title", {}), *candidate.get("paragraphs", [])]
                    for fact in paragraph.get("fact_ids", [])
                }
            )
            payload = {
                "verdict": verdict,
                "issues": (
                    [] if verdict == "supported" else ["uncertain" if verdict == "uncertain" else "unsupported_claim"]
                )
                if issues is None
                else issues,
                "fact_ids": references if fact_ids is None else fact_ids,
                "rationale": "The bounded native facts support this message."
                if verdict == "supported"
                else "The message contains an unsupported claim.",
            }
        part = ToolCallPart(info.output_tools[0].name, payload) if info.output_tools else TextPart(json.dumps(payload))
        return ModelResponse(parts=[part])

    return FunctionModel(model)
