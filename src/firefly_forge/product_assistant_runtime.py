# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A real, tool-free Firefly Agentic product designer with bounded structured output."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_forge.collaboration import ProductDefinition
from firefly_forge.model_operation_client import (
    ModelOperationClient,
    close_operation_http,
    close_owned_model_client,
)
from firefly_forge.prompts import prompt_text
from firefly_forge.providers import APIProvider, BridgeProvider
from firefly_forge.work_duration_models import WorkDurationPolicyV2

WORK_DURATION_SETTING = "ASSISTANT_WORK_DURATION_POLICY"


class AssistantReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=12000)
    questions: list[str] = Field(default_factory=list, max_length=10)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    proposal: ProductDefinition | None = None
    citation_ids: list[str] = Field(default_factory=list, max_length=50)


class FunctionalAssistantReply(AssistantReply):
    intent: Literal["dialogue", "status", "clarify"]

    @model_validator(mode="after")
    def proposal_requires_dialogue(self) -> FunctionalAssistantReply:
        if self.intent != "dialogue" and self.proposal is not None:
            raise ValueError("Only functional dialogue can return an unreviewed proposal")
        return self


async def design_reply(
    prompt: str,
    settings: dict[str, str],
    *,
    model: Any = None,
    images: list[dict[str, Any]] | None = None,
    image_input_receipt: dict[str, Any] | None = None,
) -> AssistantReply:
    from fireflyframework_agentic.agents import FireflyAgent
    from fireflyframework_agentic.config import FireflyAgenticConfig
    from pydantic_ai import BinaryContent, PromptedOutput
    from pydantic_ai.usage import UsageLimits

    work_duration = (
        WorkDurationPolicyV2.model_validate_json(settings[WORK_DURATION_SETTING])
        if WORK_DURATION_SETTING in settings
        else None
    )
    functional = settings.get("ASSISTANT_OUTPUT_CONTRACT") == "functional_intent_v1"
    async with AsyncExitStack() as resources:
        if model is None:
            provider = (
                BridgeProvider(settings) if settings.get("ASSISTANT_PROVIDER") == "bridge" else APIProvider(settings)
            )
            if work_duration is not None:
                provider.configure_work_duration(work_duration)
            options: dict[str, Any] = {}
            if work_duration is not None and isinstance(provider, BridgeProvider):
                http_client = ModelOperationClient()
                resources.push_async_callback(close_operation_http, http_client)
                options["http_client"] = http_client
            if isinstance(provider, BridgeProvider) and images:
                if not image_input_receipt or image_input_receipt.get("status") != "verified":
                    raise ValueError("The verified image-input receipt is unavailable")
                model = provider.model(image_capability=image_input_receipt, **options)
            elif isinstance(provider, APIProvider) and functional:
                model = provider.model(single_request=True)
            else:
                model = provider.model(**options)
            if work_duration is not None:
                if isinstance(provider, BridgeProvider):
                    resources.push_async_callback(close_owned_model_client, model.client, http_client)
                model = await resources.enter_async_context(model)
        single_attempt = functional or work_duration is not None
        reply_type = FunctionalAssistantReply if functional else AssistantReply
        output_spec: Any = PromptedOutput(reply_type) if settings.get("ASSISTANT_PROVIDER") == "bridge" else reply_type
        agent: FireflyAgent[None, AssistantReply] = FireflyAgent(
            name="forge-product-designer",
            model=model,
            output_type=output_spec,
            instructions=prompt_text("assistant/system")
            + ("\n" + prompt_text("assistant/functional-policy") if functional else ""),
            retries=0 if single_attempt else None,
            auto_register=False,
            tools=[],
            config=FireflyAgenticConfig(default_temperature=None, max_retries=0, rate_limit_max_retries=0)
            if single_attempt
            else FireflyAgenticConfig(default_temperature=None),
        )
        input_content: Any = (
            [prompt, *(BinaryContent(data=item["image"], media_type=item["mime_type"]) for item in images)]
            if images
            else prompt
        )
        result = await agent.run(
            input_content,
            timeout=work_duration.timeout_seconds
            if work_duration is not None
            else min(300, float(settings.get("FORGE_PROVIDER_TIMEOUT", "300"))),
            usage_limits=UsageLimits(request_limit=1 if functional else 3, tool_calls_limit=0 if functional else 3),
        )
        return reply_type.model_validate(result.output)
