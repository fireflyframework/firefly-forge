# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Sealed reconstruction keeps an unknown pure observation distinct from retained results."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx2
from fireflyframework_agentic.model_utils import serialize_model_messages
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RequestUsage

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2, ConsumePayload
from firefly_forge.agentic_checkpoint_boundary import USAGE, AgenticBoundary, BoundaryCheckpoint, OriginalCompletion
from firefly_forge.agentic_checkpoint_invocation import invocation_cut
from firefly_forge.agentic_tool_custody import EFFECTS, INTENT, RESULT, UNKNOWN
from firefly_forge.bridge_completion_models import CompletionIntentPayload, CompletionResponsePayload
from firefly_forge.bridge_completions import _cut, _outside_unit
from firefly_forge.checkpoint_mode_runtime import effective_revision
from firefly_forge.checkpoint_work_duration import checkpoint_payload
from firefly_forge.invalid_read_continuity import _bytes, _json
from firefly_forge.invalid_read_reconstruction import _agent, _Captured, _NegativeResultMetadata
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completion_proof import CUSTODY, _schema, _unsettled
from firefly_forge.worker_evidence_query import WorkerEvidenceReadInput
from firefly_forge.workspace_tools import WorkspaceTools

Data = dict[str, Any]
UNAVAILABLE = {"status": "observation_unavailable", "reason": "original_read_result_not_retained"}
_COMMON = {
    "version",
    "custody_id",
    "tool_call_id",
    "tool",
    "effect",
    "invocation_id",
    "task_key",
    "task_attempt",
    "request_step",
    "arguments_sha256",
    "raw_arguments_sha256",
    "binding_sha256",
    "request_context_sha256",
    "checkpoint_digest",
}


@dataclass(frozen=True)
class PreparedObservation:
    encoded: bytes

    @property
    def data(self) -> Data:
        return cast(Data, json.loads(self.encoded))

    @property
    def checkpoint(self) -> BoundaryCheckpoint:
        p = checkpoint_payload(self.data["checkpoint"])
        return BoundaryCheckpoint.decode(p.snapshot.encode(), p.checkpoint_digest)


def observation_projection(prepared: PreparedObservation) -> tuple[list[Any], Data]:
    """Validate the complete pinned SDK message and usage projection without executing a callback."""
    data, checkpoint = prepared.data, prepared.checkpoint
    response = ChatCompletion.model_validate_json(data["response"], strict=True)
    calls = response.choices[0].message.tool_calls
    if calls is None or response.usage is None or any(call.type != "function" for call in calls):
        raise ValueError("Observation projection requires the exact retained function response")
    usage_data = response.usage.model_dump(exclude_none=True)
    details = {
        k: v
        for k, v in usage_data.items()
        if k not in {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens"}
        and isinstance(v, int)
    }
    if response.usage.completion_tokens_details is not None:
        details.update(response.usage.completion_tokens_details.model_dump(exclude_none=True))
    provider_url = data["endpoint"].rstrip("/") + "/"
    response_usage = RequestUsage.extract(
        {"model": data["model"], "usage": usage_data},
        provider="openai",
        provider_url=provider_url,
        provider_fallback="openai",
        api_flavor="chat",
        details=details,
    )
    input_details = usage_data.get("prompt_tokens_details")
    if isinstance(input_details, dict) and isinstance(input_details.get("cache_write_tokens"), int):
        response_usage.cache_write_tokens = input_details["cache_write_tokens"]
    timestamp = datetime.fromisoformat(data["consume_created_at"])
    pending = checkpoint.history[-1]
    provider_details: Data = {"finish_reason": response.choices[0].finish_reason}
    if response.created:
        provider_details["timestamp"] = datetime.fromtimestamp(response.created, UTC)
    if response.service_tier:
        provider_details["service_tier"] = response.service_tier
    projected = ModelResponse(
        parts=[
            ToolCallPart(call.function.name, call.function.arguments, tool_call_id=call.id)
            for call in calls
            if call.type == "function"
        ],
        usage=response_usage,
        model_name=response.model,
        timestamp=timestamp,
        provider_name="openai",
        provider_url=provider_url,
        provider_response_id=response.id or None,
        provider_details=provider_details,
        finish_reason="tool_call",
        run_id=pending.run_id,
        conversation_id=pending.conversation_id,
    )
    if response_usage.cost is None:
        with suppress(LookupError, ValueError):
            response_usage.cost = projected.cost().total_price
    request = ModelRequest(
        parts=[
            ToolReturnPart(
                data["outcomes"][call.id]["name"],
                data["outcomes"][call.id]["result"],
                tool_call_id=call.id,
                timestamp=timestamp,
            )
            for call in calls
        ],
        timestamp=timestamp,
        instructions="\n\n".join(
            part["content"] for part in checkpoint.record.contract["parameters"].get("instruction_parts") or []
        ).strip()
        or None,
        run_id=pending.run_id,
        conversation_id=pending.conversation_id,
    )
    usage = checkpoint.usage
    usage.incr(response_usage)
    usage.requests += 1
    usage.tool_calls += len(calls)
    return serialize_model_messages([projected, request]), cast(Data, USAGE.dump_python(usage, mode="json"))


def prepare_observation(run: Data, task: Data, entries: list[Data], *, accepted_runtime: str) -> PreparedObservation:
    if (
        not entries
        or len(entries) > 10000
        or len(_bytes(entries)) > 16 * 1024 * 1024
        or not Journal.verify_chain(entries)
        or any(e["run_id"] != run["id"] for e in entries)
        or run.get("agent") != "bridge"
        or run.get("active_task") != "implement"
        or run.get("status") != "needs_reconciliation"
        or task.get("status") != "needs_reconciliation"
        or task.get("id") != "implement"
        or task.get("attempt") != run.get("attempt")
        or (run.get("cancel_request") or {}).get("status") == "requested"
    ):
        raise ValueError("Observation recovery requires the intact held original task")
    grouped = _unsettled(entries, run["id"])
    if any(len(grouped[k]) != 1 for k in CUSTODY):
        raise ValueError("Observation recovery requires exactly one unsettled consumed model response")
    intent, record, response, consume = (grouped[k][0] for k in CUSTODY)
    ip = CompletionIntentPayload.model_validate(intent["payload"])
    cp = CheckpointPayloadV2.model_validate(record["payload"])
    body = CompletionResponsePayload.model_validate(response["payload"])
    spent = ConsumePayload.model_validate(consume["payload"])
    checkpoint = BoundaryCheckpoint.decode(cp.snapshot.encode(), cp.checkpoint_digest)
    if (
        record["task_id"] != intent["task_id"]
        or checkpoint.wire_sha256 != ip.descriptor.wire_sha256
        or checkpoint.record.wire_bytes != ip.descriptor.wire_bytes
        or ip.run_id != run["id"]
        or ip.task_id != "implement"
        or ip.task_key != task["idempotency_key"]
        or ip.task_attempt != task["attempt"]
        or ip.project_id != run.get("project_id")
        or ip.repository_id != run.get("repository_id")
        or ip.config_revision != effective_revision(run)
        or ip.scope_digest != _cut(run, "config_revision")
        or cp.invocation.contract != accepted_runtime
        or cp.invocation.run_cut != invocation_cut(run)
        or cp.invocation.round != task["attempt"]
        or not intent["sequence"] < record["sequence"] < response["sequence"] < consume["sequence"]
        or body.request_id != ip.descriptor.request_id
        or body.intent_digest != intent["digest"]
        or cp.request_id != body.request_id
        or cp.intent_digest != intent["digest"]
        or spent.request_id != body.request_id
        or spent.intent_digest != intent["digest"]
        or spent.checkpoint_record_digest != record["digest"]
        or spent.response_record_digest != response["digest"]
        or spent.response_sha256 != body.response_sha256
        or spent.response_bytes != body.response_bytes
        or hashlib.sha256(body.response.encode()).hexdigest() != body.response_sha256
        or len(body.response.encode()) != body.response_bytes
        or any(e["task_id"] != "implement" for e in (intent, record, response, consume))
        or run.get("bridge_completion_hold")
        != {
            "version": 1,
            "reason": "graph_checkpoint_unavailable",
            "request_id": body.request_id,
            "intent_digest": intent["digest"],
        }
    ):
        raise ValueError("Observation boundary differs from original consumed custody")
    completion = ChatCompletion.model_validate_json(body.response, strict=True)
    calls = completion.choices[0].message.tool_calls if len(completion.choices) == 1 else None
    if (
        not calls
        or len(calls) > 2
        or completion.choices[0].finish_reason != "tool_calls"
        or completion.choices[0].message.content not in {None, ""}
        or completion.choices[0].message.refusal
        or completion.choices[0].message.audio is not None
        or completion.model != ip.descriptor.model
        or completion.usage is None
        or completion.choices[0].logprobs is not None
        or getattr(completion, "moderation", None)
        or len({c.id for c in calls}) != len(calls)
    ):
        raise ValueError("Only a complete pure-read function response is eligible")
    definitions = {t["name"]: t for t in checkpoint.record.contract["parameters"]["function_tools"]}
    tail = [e for e in entries if e["sequence"] > consume["sequence"]]
    outcomes: Data = {}
    used: list[str] = []
    unknown: list[str] = []
    for call in calls:
        if (
            call.type != "function"
            or call.function.name not in {"list_files", "read_run_evidence"}
            or call.function.name not in cp.invocation.grants
            or call.function.name not in definitions
        ):
            raise ValueError("Writable, network or ungranted observations cannot use this protocol")
        name = call.function.name
        args = _json(call.function.arguments)
        _schema(definitions[name]["parameters_json_schema"], args)
        if name == "read_run_evidence":
            WorkerEvidenceReadInput.model_validate(args)
            validated = args
        else:
            signature = inspect.signature(WorkspaceTools.list_files_page)
            bound = signature.bind(None, **args)
            bound.apply_defaults()
            validated = {k: v for k, v in bound.arguments.items() if k != "self"}
        rows = [
            e for e in tail if e["kind"] in {INTENT, RESULT, UNKNOWN} and e["payload"].get("tool_call_id") == call.id
        ]
        intents = [e for e in rows if e["kind"] == INTENT]
        ends = [e for e in rows if e["kind"] in {RESULT, UNKNOWN}]
        if len(intents) != 1 or len(ends) != 1:
            raise ValueError("Every original read needs one exact durable intent and outcome")
        original, end = intents[0], ends[0]
        p, q = original["payload"], end["payload"]
        common = {k: p.get(k) for k in _COMMON}
        key = digest(
            {
                "run_id": run["id"],
                "task_key": task["idempotency_key"],
                "task_attempt": task["attempt"],
                "tool_call_id": call.id,
            }
        )
        if (
            set(p) != _COMMON | {"status", "arguments", "raw_arguments", "source_before", "authority"}
            or type(p["version"]) is not int
            or p["version"] != 1
            or p["status"] != "pending"
            or type(p["request_step"]) is not int
            or p["request_step"] < 1
            or p["custody_id"] != key
            or any(e["idempotency_key"] != key or e["task_id"] != "implement" for e in rows)
            or p["tool"] != name
            or p["effect"] != EFFECTS[name]
            or p["task_key"] != task["idempotency_key"]
            or type(p["task_attempt"]) is not int
            or p["task_attempt"] != task["attempt"]
            or p["checkpoint_digest"] != cp.checkpoint_digest
            or p["invocation_id"] != cp.invocation.invocation_id
            or p["binding_sha256"] != invocation_cut(run)
            or p["source_before"] != cp.invocation.workspace
            or p["raw_arguments"] != call.function.arguments
            or _bytes(p["arguments"]) != _bytes(validated)
            or p["arguments_sha256"] != digest({"arguments": validated})
            or p["raw_arguments_sha256"] != digest({"raw_arguments": call.function.arguments})
            or p["authority"]
            != {
                "actor": run.get("execution_actor"),
                "configuration": run.get("config_revision"),
                "source": run.get("source_tree"),
            }
            or _bytes({k: q.get(k) for k in _COMMON}) != _bytes(common)
            or q.get("intent_digest") != original["digest"]
            or end["sequence"] <= original["sequence"]
        ):
            raise ValueError("Read call, arguments, source or original identity changed")
        if end["kind"] == RESULT:
            if (
                name != "list_files"
                or set(q)
                != _COMMON
                | {
                    "status",
                    "intent_digest",
                    "source_before",
                    "source_after",
                    "result",
                    "result_sha256",
                    "parent_effects",
                }
                or q["status"] != "retained"
                or q["source_before"] != cp.invocation.workspace
                or q["source_after"] != cp.invocation.workspace
                or q["parent_effects"] != []
                or q["result_sha256"] != digest({"result": q["result"]})
            ):
                raise ValueError("The known sibling result has no complete pure-read custody")
            outcome = q["result"]
        else:
            if (
                name != "read_run_evidence"
                or set(q) != _COMMON | {"status", "intent_digest", "reason"}
                or q["status"] != "unknown"
                or q["reason"] != "callback_result_not_durably_admitted"
            ):
                raise ValueError("Only the exact unknown evidence observation can be reviewed")
            unknown.append(key)
            outcome = dict(UNAVAILABLE)
        outcomes[call.id] = {"name": name, "arguments": args, "result": outcome, "returned": False}
        used.extend(e["digest"] for e in rows)
    if len(unknown) != 1 or len(used) != len(tail) or set(used) != {e["digest"] for e in tail}:
        raise ValueError("Unaccounted later effects or ambiguous unknown observations remain held")
    value = dict(
        checkpoint=cp.model_dump(),
        response=body.response,
        endpoint=ip.descriptor.endpoint,
        model=ip.descriptor.model,
        source_run=copy.deepcopy(run),
        consume_created_at=consume["created_at"],
        request_id=body.request_id,
        outcomes=outcomes,
        unknown_custody=unknown,
        closure={
            k: e["digest"]
            for k, e in zip(
                ("intent", "checkpoint", "response", "consume"), (intent, record, response, consume), strict=True
            )
        },
        read_records=used,
    )
    return PreparedObservation(_bytes(value))


async def reconstruct_observation(prepared: PreparedObservation) -> BoundaryCheckpoint:
    _outside_unit()
    from firefly_forge.invalid_read_contract import actual_worker_contract

    data, checkpoint = prepared.data, prepared.checkpoint
    if _bytes(await actual_worker_contract(cast(Any, prepared))) != _bytes(checkpoint.record.contract):
        raise ValueError("Current worker contract differs from the original read checkpoint")
    boundary = AgenticBoundary(lambda: copy.deepcopy(checkpoint.state), restore=checkpoint)
    response = data["response"].encode()

    async def consumed(checkpoint_digest: str, response_digest: str) -> None:
        if checkpoint_digest != checkpoint.digest or response_digest != hashlib.sha256(response).hexdigest():
            raise ValueError("The already-consumed model result changed")

    original = OriginalCompletion(checkpoint, response, consumed)

    async def transport(request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST" or request.url.path != "/v1/chat/completions":
            raise ValueError("Only the sealed public SDK serializer is permitted")
        wire = await request.aread()
        captured = boundary.capture(wire)
        if original.claimed:
            raise _Captured(captured)
        return httpx2.Response(200, content=await original.expose(wire), request=request)

    outcomes = copy.deepcopy(data["outcomes"])
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport), follow_redirects=False) as http:
        client = AsyncOpenAI(
            base_url=data["endpoint"], api_key="sealed-read-observation-only", max_retries=0, http_client=http
        )
        model = OpenAIChatModel(
            data["model"],
            provider=OpenAIProvider(openai_client=client),
            profile=OpenAIModelProfile(openai_unsupported_model_settings=["max_tokens"]),
        )
        agent, arguments = _agent(
            model,
            boundary,
            checkpoint,
            migrate=False,
            current_contract=True,
            metadata=_NegativeResultMetadata(checkpoint, outcomes, data["consume_created_at"]),
        )
        arguments.update(outcomes)
        try:
            with boundary.invocation():
                await agent.run(**checkpoint.resume_kwargs())
        except _Captured as stopped:
            derived = stopped.checkpoint
            expected_history, expected_usage = observation_projection(prepared)
            full_history = [*checkpoint.record.history, *expected_history]
            if (
                not all(v["returned"] for v in arguments.values())
                or _bytes(derived.record.history) != _bytes(full_history)
                or _bytes(derived.record.request_history) != _bytes(full_history)
                or _bytes(derived.record.usage) != _bytes(expected_usage)
                or derived.usage.requests != checkpoint.usage.requests + 1
                or derived.usage.tool_calls != checkpoint.usage.tool_calls + len(outcomes)
                or _bytes(derived.record.limits) != _bytes(checkpoint.record.limits)
                or _bytes(derived.state) != _bytes(checkpoint.state)
                or _bytes(derived.record.contract) != _bytes(checkpoint.record.contract)
            ):
                raise ValueError(
                    "Observation reconstruction changed native history, state, limits or spent usage"
                ) from None
            return derived
    raise ValueError("Observation reconstruction did not stop before its next model request")
