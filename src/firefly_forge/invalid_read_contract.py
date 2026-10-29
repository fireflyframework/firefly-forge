# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Capture the installed worker's real contract without dispatch or tool execution."""

from __future__ import annotations

import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from firefly_forge.agentic_checkpoint_invocation import CheckpointInvocation, checkpoint_invocation
from firefly_forge.agentic_runtime import execute_agentic
from firefly_forge.invalid_read_continuity import PreparedInvalidRead
from firefly_forge.prompts import prompt_text
from firefly_forge.worker_evidence_query import WorkerEvidenceReadInput
from firefly_forge.workspace_tools import ToolRuntimeContext


class _ContractCaptured(BaseException):
    def __init__(self, contract: dict[str, Any]) -> None:
        self.contract = contract


async def actual_worker_contract(prepared: PreparedInvalidRead) -> dict[str, Any]:
    from firefly_forge.engine import Engine

    data = prepared.data
    binding = data["checkpoint"]["invocation"]
    run = data["source_run"]

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("Contract inspection cannot execute a tool or native effect")

    async def emit(*args: Any) -> None:
        return None

    with TemporaryDirectory(prefix="forge-continuity-contract-") as directory:
        root = Path(directory)
        invocation = CheckpointInvocation(
            SimpleNamespace(credential_registry=dict),
            SimpleNamespace(scope=lambda *args, **kwargs: None),
            run,
            {},
            root,
            forbidden,
        )

        async def stop(request: httpx2.Request) -> httpx2.Response:
            if request.method != "POST" or invocation.boundary is None:
                raise ValueError("Contract inspection permits only its first sealed request boundary")
            captured = invocation.boundary.capture(await request.aread())
            raise _ContractCaptured(copy.deepcopy(captured.record.contract))

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(stop), follow_redirects=False) as http:
            client = AsyncOpenAI(
                base_url="http://contract.invalid/v1", api_key="offline-contract-only", max_retries=0, http_client=http
            )
            model = OpenAIChatModel(
                data["model"],
                provider=OpenAIProvider(openai_client=client),
                profile=OpenAIModelProfile(openai_unsupported_model_settings=["max_tokens"]),
            )
            context = ToolRuntimeContext(
                root,
                root,
                frozenset(binding["grants"]),
                command_executor=forbidden,
                knowledge_lookup=forbidden,
                read_run_evidence=forbidden,
                qa_references=tuple(Engine._qa_reference_descriptors(run)),
            )
            try:
                with checkpoint_invocation(invocation):
                    await execute_agentic(
                        root,
                        binding["brief"],
                        binding["backend"],
                        binding["frontend"],
                        emit,
                        model=model,
                        runtime_context=context,
                        allow_output_token_cap=False,
                        settings={
                            "FORGE_AGENTIC_CHECKPOINTS": "required",
                            "FORGE_BRIDGE_COMPLETION_RECOVERY": "required",
                        },
                    )
            except _ContractCaptured as captured:
                return captured.contract
    raise ValueError("Installed worker did not stop before its first request")


def compatible_contract(original: dict[str, Any], current: dict[str, Any]) -> None:
    expected = copy.deepcopy(original)
    old = [item for item in expected["parameters"]["function_tools"] if item["name"] == "read_run_evidence"]
    new = [item for item in current["parameters"]["function_tools"] if item["name"] == "read_run_evidence"]
    if (
        len(old) != 1
        or len(new) != 1
        or old[0]["description"] != prompt_text("tools/read_run_evidence", version="1.0.0")
    ):
        raise ValueError("No exact historical evidence-tool description is approved")
    if new[0]["description"] != prompt_text("tools/read_run_evidence", version="1.0.1"):
        raise ValueError("The installed strict evidence-tool description differs")
    old[0]["parameters_json_schema"] = WorkerEvidenceReadInput.model_json_schema()
    old[0]["description"] = new[0]["description"]
    if expected != current:
        raise ValueError("Migration changed more than the approved strict evidence schema and description")
