# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Public SDK serialization of a closed pending request; no response or effect replay."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2
from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary, BoundaryCheckpoint
from firefly_forge.bridge_completion_models import CompletionDescriptor
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.invalid_read_continuity import PreparedInvalidRead, _bytes
from firefly_forge.invalid_read_contract import actual_worker_contract
from firefly_forge.invalid_read_reconstruction import _agent, _PendingMetadata


class _PendingWire(BaseException):
    def __init__(self, wire: bytes) -> None:
        self.wire = wire


async def reconstruct_pending_request(
    payload: CheckpointPayloadV2, descriptor: CompletionDescriptor, run: dict[str, Any]
) -> bytes:
    _outside_unit()
    checkpoint = BoundaryCheckpoint.decode(payload.snapshot.encode(), payload.checkpoint_digest)
    if (checkpoint.wire_sha256, checkpoint.record.wire_bytes) != (descriptor.wire_sha256, descriptor.wire_bytes):
        raise ValueError("The pending checkpoint does not bind the exact original wire")
    reference = PreparedInvalidRead(
        _bytes({"checkpoint": payload.model_dump(), "source_run": run, "model": descriptor.model})
    )
    if await actual_worker_contract(reference) != checkpoint.record.contract:
        raise ValueError("Current worker tools, instructions or settings differ from the pending checkpoint")
    boundary = AgenticBoundary(lambda: copy.deepcopy(checkpoint.state), restore=checkpoint)

    async def stop(request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST" or request.url.path != "/v1/chat/completions":
            raise ValueError("Pending reconstruction permits only its sealed request serializer")
        wire = await request.aread()
        if len(wire) != descriptor.wire_bytes or hashlib.sha256(wire).hexdigest() != descriptor.wire_sha256:
            raise ValueError("The public SDK did not reconstruct the exact original provider request")
        if boundary.capture(wire).digest != checkpoint.digest:
            raise ValueError("Pending reconstruction changed recorded history, usage, limits or state")
        raise _PendingWire(wire)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(stop), follow_redirects=False) as http:
        client = AsyncOpenAI(
            base_url=descriptor.endpoint, api_key="sealed-pending-request-only", max_retries=0, http_client=http
        )
        model = OpenAIChatModel(
            descriptor.model,
            provider=OpenAIProvider(openai_client=client),
            profile=OpenAIModelProfile(openai_unsupported_model_settings=["max_tokens"]),
        )
        agent, outcomes = _agent(
            model,
            boundary,
            checkpoint,
            migrate=True,
            metadata=_PendingMetadata(checkpoint),
            target_contract=checkpoint.record.contract,
        )
        if outcomes:
            raise ValueError("A pending request cannot authorize original tool execution")
        try:
            with boundary.invocation():
                await agent.run(**checkpoint.resume_kwargs())
        except _PendingWire as captured:
            return captured.wire
    raise ValueError("The SDK did not reach the exact pending request boundary")
