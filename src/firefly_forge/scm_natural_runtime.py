# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One tool-free classification through the configured communication model."""

from __future__ import annotations

import json
import re
from functools import cache
from importlib.resources import files
from typing import Any

from firefly_forge.prompts import PromptCatalog
from firefly_forge.providers import APIProvider, BridgeProvider
from firefly_forge.scm_natural_models import NaturalDecision, question_choices
from firefly_forge.skills import inject_skills


class ConfirmedInvalidReply(ValueError):
    """One complete provider response failed its output contract; no transport outcome is inferred."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__("A complete natural reply failed its typed output contract")


def _validation_errors(error: BaseException) -> list[dict[str, Any]]:
    from pydantic import ValidationError

    current: BaseException | None = error
    for _ in range(8):
        if isinstance(current, ValidationError):
            result = []
            for row in current.errors(include_input=False, include_context=False)[:16]:
                kind = row["type"]
                location = row["loc"]
                if not re.fullmatch(r"[a-z_]{1,100}", kind) or any(
                    not (type(item) is int and 0 <= item <= 200 or item in {"intent", "message", "choice"})
                    for item in location
                ):
                    return [{"type": "invalid_typed_reply", "loc": []}]
                result.append({"type": kind, "loc": list(location)})
            return result
        current = current.__cause__ if current is not None else None
    return [{"type": "invalid_typed_reply", "loc": []}]


@cache
def catalog() -> PromptCatalog:
    return PromptCatalog(files("firefly_forge").joinpath("resources/prompts/scm-natural-v1"))


async def classify(
    message: str, context: dict[str, Any], settings: dict[str, str], *, model: Any = None
) -> NaturalDecision:
    from fireflyframework_agentic.agents import FireflyAgent
    from fireflyframework_agentic.config import FireflyAgenticConfig
    from pydantic_ai import PromptedOutput, capture_run_messages
    from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.usage import UsageLimits

    bridge = settings.get("ASSISTANT_PROVIDER") == "bridge"
    if model is None:
        model = BridgeProvider(settings).model() if bridge else APIProvider(settings).model(single_request=True)
    prompt = catalog().text(
        "scm-natural/request",
        context=json.dumps({**context, "question_choices": question_choices(context)}, ensure_ascii=False),
        message=message,
    )
    if len(prompt.encode()) > 100000:
        raise ValueError("Natural conversation exceeds its bounded context")
    output_spec: Any = PromptedOutput(NaturalDecision) if bridge else NaturalDecision
    agent: FireflyAgent[None, NaturalDecision] = FireflyAgent(
        name="forge-natural-task-intent",
        model=model,
        output_type=output_spec,
        instructions=inject_skills(catalog().text("scm-natural/system", version="1.0.3"), ["humanizer"]),
        tools=[],
        retries=0,
        auto_register=False,
        config=FireflyAgenticConfig(default_temperature=None, max_retries=0, rate_limit_max_retries=0),
    )
    with capture_run_messages() as messages:
        try:
            result = await agent.run(
                prompt,
                timeout=min(120, float(settings.get("FORGE_PROVIDER_TIMEOUT", "60"))),
                usage_limits=UsageLimits(request_limit=1, tool_calls_limit=0),
            )
        except (UnexpectedModelBehavior, UsageLimitExceeded) as error:
            responses = [message for message in messages if isinstance(message, ModelResponse)]
            if len(responses) != 1 or responses[0].state != "complete":
                raise
            raise ConfirmedInvalidReply(_validation_errors(error)) from None
    return NaturalDecision.model_validate(result.output)
