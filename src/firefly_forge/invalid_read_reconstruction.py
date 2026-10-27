# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Sealed local SDK parsing and capture; this module cannot dispatch live work."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import httpx2
from fireflyframework_agentic.agents import FireflyAgent
from fireflyframework_agentic.config import FireflyAgenticConfig
from openai import AsyncOpenAI
from pydantic_ai import PromptedOutput
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, ToolReturnPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import RunContext, Tool

from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary, BoundaryCheckpoint, OriginalCompletion
from firefly_forge.agentic_runtime import RuntimeReport
from firefly_forge.invalid_read_continuity import PreparedInvalidRead
from firefly_forge.journal import digest
from firefly_forge.worker_evidence_query import WorkerEvidenceReadInput


@dataclass(frozen=True)
class ReconstructedInvalidRead:
    derived: BoundaryCheckpoint
    migrated: BoundaryCheckpoint
    receipt_bytes: bytes

    @property
    def receipt(self) -> dict[str, Any]:
        import json

        return dict(json.loads(self.receipt_bytes))


class _Captured(BaseException):
    def __init__(self, checkpoint: BoundaryCheckpoint) -> None:
        self.checkpoint = checkpoint


class _PendingMetadata(AbstractCapability[Any]):
    def __init__(self, checkpoint: BoundaryCheckpoint) -> None:
        self.timestamp = checkpoint.history[-1].timestamp

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        # Public resume reconstructs this request timestamp; preserve the completed frontier's metadata.
        for messages in (ctx.messages, request_context.messages):
            messages[-1].timestamp = self.timestamp
        return request_context


class _NegativeResultMetadata(AbstractCapability[Any]):
    def __init__(self, checkpoint: BoundaryCheckpoint, outcomes: dict[str, Any], consumed_at: str) -> None:
        self.requests = checkpoint.usage.requests
        self.prefix_count = len(checkpoint.history)
        self.run_id = checkpoint.history[-1].run_id
        self.conversation_id = checkpoint.history[-1].conversation_id
        self.outcomes = outcomes
        self.timestamp = datetime.fromisoformat(consumed_at)
        if self.timestamp.utcoffset() is None:
            raise ValueError("Native consumption time must have a timezone")

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if ctx.usage.requests == self.requests:
            return request_context
        # A derived outcome uses the immutable consume time, not the time of a repeat inspection.
        for messages in (ctx.messages, request_context.messages):
            suffix = messages[self.prefix_count :]
            if len(suffix) != 2 or not isinstance(suffix[0], ModelResponse):
                raise ValueError("Negative validation must append exactly one known model response")
            for message in suffix:
                message.timestamp = self.timestamp
                message.run_id = self.run_id
                message.conversation_id = self.conversation_id
            pending = messages[-1]
            parts = [part for part in pending.parts if isinstance(part, ToolReturnPart)]
            if len(parts) != len(pending.parts) or len(parts) != len(self.outcomes):
                raise ValueError("Negative validation must close every exact original read call")
            if len({p.tool_call_id for p in parts}) != len(self.outcomes):
                raise ValueError("Negative observation outcomes have duplicate call identities")
            for part in parts:
                if part.tool_call_id is None:
                    raise ValueError("Negative observations require exact original call identities")
                expected = self.outcomes.get(part.tool_call_id)
                if expected is None or part.tool_name != expected["name"] or part.content != expected["result"]:
                    raise ValueError("Negative validation or unavailable observation changed before capture")
                part.timestamp = self.timestamp
            pending.timestamp = self.timestamp
        return request_context


def _ordered_schema(name: str, captured: dict[str, Any]) -> dict[str, Any]:
    """Canonical snapshots lose object insertion order; regenerate only an exactly equal public schema."""

    async def list_files(path_prefix: str = "", cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        raise ValueError("Schema template cannot execute")

    async def read_file(path: str) -> str:
        raise ValueError("Schema template cannot execute")

    async def write_file(path: str, content: str) -> str:
        raise ValueError("Schema template cannot execute")

    async def search_text(
        text: str, path_prefix: str = "", max_results: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        raise ValueError("Schema template cannot execute")

    async def edit_lines(
        path: str, start_line: int, end_line: int, replacement: str, expected_sha256: str
    ) -> dict[str, str]:
        raise ValueError("Schema template cannot execute")

    async def git_diff() -> dict[str, Any]:
        raise ValueError("Schema template cannot execute")

    async def execute_command(argv: list[str], timeout: int = 300) -> dict[str, Any]:
        raise ValueError("Schema template cannot execute")

    async def knowledge_lookup(
        query: str, refresh: bool = False, refresh_request_id: str | None = None
    ) -> dict[str, Any]:
        raise ValueError("Schema template cannot execute")

    async def read_run_evidence(query: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("Schema template cannot execute")

    templates: dict[str, Any] = dict(
        list_files=list_files,
        read_file=read_file,
        write_file=write_file,
        search_text=search_text,
        edit_lines=edit_lines,
        git_diff=git_diff,
        execute_command=execute_command,
        knowledge_lookup=knowledge_lookup,
        read_run_evidence=read_run_evidence,
    )
    if name not in templates:
        raise ValueError("No pinned schema template exists for this original tool")
    generated = Tool(templates[name], name=name).tool_def.parameters_json_schema
    if generated != captured:
        raise ValueError("The public schema template differs from the frozen original")
    return generated


def _agent(
    model: Any,
    boundary: AgenticBoundary,
    checkpoint: BoundaryCheckpoint,
    *,
    migrate: bool,
    metadata: AbstractCapability[Any] | None = None,
    target_contract: dict[str, Any] | None = None,
) -> Any:
    contract = target_contract if migrate and target_contract is not None else checkpoint.record.contract
    parameters = contract["parameters"]
    if parameters["output_tools"] or parameters["output_mode"] != "prompted":
        raise ValueError("Continuity supports the pinned prompted RuntimeReport contract only")
    definitions = parameters["function_tools"]
    if len({t["name"] for t in definitions}) != len(definitions):
        raise ValueError("Continuity tools have duplicate names")
    selected = [item for item in definitions if item["name"] == "read_run_evidence"]
    if len(selected) != 1:
        raise ValueError("Continuity has no unique frozen evidence tool")
    tools = []
    for definition in definitions:
        name = definition["name"]

        def sealed_handler(tool_name: str) -> Any:
            async def respond(ctx: RunContext[Any], **arguments: Any) -> Any:
                from firefly_forge.invalid_read_continuity import _bytes

                if ctx.tool_call_id is None:
                    raise ValueError("Sealed observations require their exact original call identity")
                expected = prepared_arguments.get(ctx.tool_call_id)
                if (
                    migrate
                    or expected is None
                    or expected["name"] != tool_name
                    or _bytes(arguments) != _bytes(expected["arguments"])
                    or expected["returned"]
                ):
                    raise ValueError("No replay or unbound native tool invocation is permitted during migration")
                expected["returned"] = True
                return copy.deepcopy(expected["result"])

            return respond

        schema = (
            WorkerEvidenceReadInput.model_json_schema()
            if migrate and name == "read_run_evidence"
            else _ordered_schema(name, definition["parameters_json_schema"])
        )
        tool = Tool.from_schema(
            sealed_handler(name),
            name=name,
            description=definition.get("description"),
            json_schema=schema,
            takes_ctx=True,
        )
        frozen_return = copy.deepcopy(definition.get("return_schema"))

        async def prepare_return(ctx: RunContext[Any], tool_definition: Any, returns: Any = frozen_return) -> Any:
            return replace(tool_definition, return_schema=copy.deepcopy(returns))

        tool.prepare = prepare_return
        for attribute in ("strict", "metadata", "requires_approval", "defer_loading"):
            if attribute in definition:
                setattr(tool, attribute, copy.deepcopy(definition[attribute]))
        tools.append(tool)
    # The exact known arguments are installed per reconstruction, never supplied by a later model request.
    prepared_arguments: dict[str, Any] = {}
    capabilities: list[Any] = (
        [metadata] if metadata is not None else [_PendingMetadata(checkpoint)] if migrate else []
    ) + [boundary]
    instructions = []
    for part in parameters.get("instruction_parts") or []:
        if part.get("dynamic") or not isinstance(part.get("content"), str):
            raise ValueError("Continuity cannot reconstruct dynamic instructions")
        instructions.append(part["content"])
    output_spec: Any = PromptedOutput(RuntimeReport)
    agent: FireflyAgent[None, RuntimeReport] = FireflyAgent(
        name="invalid-read-offline-inspection",
        model=model,
        output_type=output_spec,
        instructions=instructions,
        tools=tools,
        auto_register=False,
        config=FireflyAgenticConfig(default_temperature=None, rate_limit_max_retries=0),
        model_settings=contract["settings"],
        capabilities=capabilities,
    )
    return agent, prepared_arguments


async def _capture(
    prepared: PreparedInvalidRead,
    checkpoint: BoundaryCheckpoint,
    *,
    migrate: bool,
    target_contract: dict[str, Any] | None = None,
) -> BoundaryCheckpoint:
    from firefly_forge.invalid_read_continuity import _json

    data = prepared.data
    boundary = AgenticBoundary(lambda: copy.deepcopy(checkpoint.state), restore=None if migrate else checkpoint)
    response = data["response"].encode()

    async def known_consumption(checkpoint_digest: str, response_digest: str) -> None:
        if checkpoint_digest != prepared.checkpoint.digest or response_digest != hashlib.sha256(response).hexdigest():
            raise ValueError("Known consumed response binding changed")
        # This inspects an existing consume witness; it never invokes AgentCheckpoints.consume_restored.

    original = OriginalCompletion(checkpoint, response, known_consumption)

    async def transport(request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST" or request.url.path != "/v1/chat/completions":
            raise ValueError("Continuity transport supports only its sealed parser invocation")
        wire = await request.aread()
        captured = boundary.capture(wire)
        if migrate or original.claimed:
            raise _Captured(captured)
        return httpx2.Response(200, content=await original.expose(wire), request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport), follow_redirects=False) as http:
        client = AsyncOpenAI(
            base_url=data["endpoint"], api_key="offline-sealed-continuity-only", max_retries=0, http_client=http
        )
        model = OpenAIChatModel(
            data["model"],
            provider=OpenAIProvider(openai_client=client),
            profile=OpenAIModelProfile(openai_unsupported_model_settings=["max_tokens"]),
        )
        from openai.types.chat import ChatCompletion

        from firefly_forge.invalid_read_continuity import UNAVAILABLE_OBSERVATION

        calls = ChatCompletion.model_validate_json(response, strict=True).choices[0].message.tool_calls
        if not calls:
            raise ValueError("The sealed response no longer identifies its exact known function calls")
        outcomes: dict[str, Any] = {}
        unavailable = {o["tool_call_id"]: o for o in prepared.proof.get("unavailable_observations", [])}
        for call in calls:
            if call.type != "function":
                raise ValueError("The sealed response contains an unsupported tool call kind")
            args = _json(call.function.arguments)
            if call.id == prepared.proof["tool_call_id"] and call.function.name == "read_run_evidence":
                result = {"status": "invalid_query"}
            elif call.id in unavailable and call.function.name == "list_files":
                if unavailable[call.id]["arguments_digest"] != digest(args):
                    raise ValueError("Unavailable observation arguments changed")
                result = dict(UNAVAILABLE_OBSERVATION)
            else:
                raise ValueError("The sealed response contains an unbound tool outcome")
            if call.id in outcomes:
                raise ValueError("The sealed response duplicates a call identity")
            outcomes[call.id] = {"name": call.function.name, "arguments": args, "result": result, "returned": False}
        metadata = None if migrate else _NegativeResultMetadata(checkpoint, outcomes, data["consume_created_at"])
        agent, arguments = _agent(
            model, boundary, checkpoint, migrate=migrate, metadata=metadata, target_contract=target_contract
        )
        arguments.update(outcomes)
        try:
            with boundary.invocation():
                await agent.run(**checkpoint.resume_kwargs())
        except _Captured as stopped:
            if not migrate and not all(value["returned"] for value in arguments.values()):
                raise ValueError("Not every original observation received its exact negative outcome") from None
            return stopped.checkpoint
    raise ValueError("Continuity did not stop at its pending model boundary")


async def reconstruct_and_migrate(prepared: PreparedInvalidRead) -> ReconstructedInvalidRead:
    import json

    original = prepared.checkpoint
    derived = await _capture(prepared, original, migrate=False)
    from firefly_forge.invalid_read_contract import actual_worker_contract, compatible_contract

    target_contract = await actual_worker_contract(prepared)
    compatible_contract(derived.record.contract, target_contract)
    migrated = await _capture(prepared, derived, migrate=True, target_contract=target_contract)
    for field in ("history", "request_history", "usage", "limits", "state", "compatibility"):
        if getattr(derived.record, field) != getattr(migrated.record, field):
            raise ValueError("Tool contract migration changed protected native state: " + field)
    expected = copy.deepcopy(derived.record.contract)
    matches = [t for t in expected["parameters"]["function_tools"] if t["name"] == "read_run_evidence"]
    if len(matches) != 1:
        raise ValueError("Migration has no unique evidence schema")
    matches[0]["parameters_json_schema"] = WorkerEvidenceReadInput.model_json_schema()
    from firefly_forge.prompts import prompt_text

    matches[0]["description"] = prompt_text("tools/read_run_evidence", version="1.0.1")
    if migrated.record.contract != expected:
        raise ValueError("Migration changed more than the approved strict evidence schema and description")
    if (derived.usage.requests, derived.usage.tool_calls) != (
        original.usage.requests + 1,
        original.usage.tool_calls + 1 + len(prepared.proof.get("unavailable_observations", [])),
    ):
        raise ValueError("Continuity did not account for exactly one original response and validation outcome")
    receipt = dict(
        version=prepared.proof["version"],
        proof_digest=prepared.proof["digest"],
        original_checkpoint=prepared.proof["checkpoint_digest"],
        derived_checkpoint=derived.digest,
        migrated_checkpoint=migrated.digest,
        original_tool_contract=digest(derived.record.contract),
        migrated_tool_contract=digest(migrated.record.contract),
        known_consumption_reused=prepared.proof["consume_record_digest"],
        metadata_normalization="native-consume-time-and-original-message-run-v1",
        external_requests=0,
        native_tool_callbacks=0,
        admission="HOLD",
        holds=prepared.proof["holds"],
    )
    from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2
    from firefly_forge.invalid_read_continuity import compatibility_migration

    if prepared.proof["version"] == 2:
        receipt["unavailable_observations"] = prepared.proof["unavailable_observations"]
        receipt["native_tool_custody_digest"] = prepared.proof["native_tool_custody_digest"]
    migration = compatibility_migration(CheckpointPayloadV2.model_validate(prepared.data["checkpoint"]))
    if migration is not None:
        receipt["compatibility_migration"] = migration
    receipt["digest"] = digest(receipt)
    return ReconstructedInvalidRead(
        derived, migrated, json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    )
