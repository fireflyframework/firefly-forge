# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact pending checkpoint custody; no callbacks, result exposure or ownership."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from openai.types.chat import ChatCompletion
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.usage import RunUsage

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2, ConsumePayload, ConsumePayloadV2
from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.agentic_checkpoint_boundary import USAGE, BoundaryCheckpoint
from firefly_forge.agentic_checkpoint_invocation import STATIC_TOOLS, invocation_cut
from firefly_forge.bridge_completion_models import CompletionIntentPayload, CompletionResponsePayload
from firefly_forge.bridge_completions import _cut
from firefly_forge.checkpoint_mode_runtime import effective_revision
from firefly_forge.invalid_read_continuity import _bytes, _json
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completion_proof import CUSTODY, _schema, _unsettled

Data = dict[str, Any]
HANDOFF = "checkpoint_continuation"
ADMITTED = "checkpoint_continuation_admitted"
_PROOF_CACHE: ContextVar[dict[str, tuple[str, ...]] | None] = ContextVar("checkpoint_handoff_proof_cache", default=None)


def _tool_arguments(call: ToolCallPart, schema: Data) -> tuple[Any, Any]:
    from pydantic import TypeAdapter

    from firefly_forge.agentic_tool_custody import canonical
    from firefly_forge.invalid_read_reconstruction import _ordered_tool

    raw = canonical(call.args)
    if call.tool_name == "read_run_evidence":
        # This tool uses the public from_schema dictionary projection, without signature defaults.
        arguments = call.args_as_dict()
    else:
        validator = _ordered_tool(call.tool_name, schema).function_schema.validator
        arguments = (
            validator.validate_json(raw)
            if isinstance(raw, str)
            else validator.validate_python({} if raw is None else raw)
        )
    adapter: TypeAdapter[Any] = TypeAdapter(Any)
    return raw, canonical(adapter.dump_python(arguments, mode="json"))


def _retained_argument(payload: Data, field: str, expected: Any) -> None:
    if payload.get(field + "_sha256") != digest({field: expected}):
        raise ValueError("Prior tool argument digest differs from its immutable model call")
    if field in payload:
        if field + "_artifact" in payload or _bytes(payload[field]) != _bytes(expected):
            raise ValueError("Prior tool arguments differ from its immutable model call")
    elif field + "_artifact" in payload:
        raw = _bytes(expected)
        descriptor = {
            "version": 1,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "media_type": "application/json",
        }
        if _bytes(payload[field + "_artifact"]) != _bytes(descriptor):
            raise ValueError("Prior tool argument artifact differs from its immutable model call")
    else:
        raise ValueError("Prior tool arguments have no native retained custody")


@contextmanager
def handoff_proof_scope() -> Iterator[dict[str, tuple[str, ...]]]:
    cached = _PROOF_CACHE.get()
    if cached is not None:
        yield cached
        return
    cached = {}
    token = _PROOF_CACHE.set(cached)
    try:
        yield cached
    finally:
        _PROOF_CACHE.reset(token)


def pending_checkpoint(run: Data, task: Data, entries: list[Data]) -> Data:
    if (
        not entries
        or not Journal.verify_chain(entries)
        or any(e["run_id"] != run["id"] for e in entries)
        or run.get("agent") != "bridge"
        or task.get("id") != "implement"
        or task.get("attempt") != run.get("attempt")
        or (run.get("cancel_request") or {}).get("status") == "requested"
    ):
        raise ValueError("Pending checkpoint requires intact original execution custody")
    grouped = _unsettled(entries, run["id"], pending=True)
    keys = [e["idempotency_key"] for e in grouped[CUSTODY[0]]]
    expected_hold = {
        "version": 1,
        "reason": "graph_checkpoint_unavailable",
        "request_id": keys[0],
        "intent_digest": grouped[CUSTODY[0]][0]["digest"],
    }
    if run.get("bridge_completion_hold") != expected_hold:
        handoffs = [e for e in entries if e["kind"] == HANDOFF]
        if not handoffs or _bytes(run.get("bridge_completion_hold")) != _bytes(
            handoffs[-1]["payload"]["run"].get("bridge_completion_hold")
        ):
            raise ValueError("Pending checkpoint has a different original hold")
    previous = None
    expected_calls: set[str] = set()
    prior_responses = []
    previous_snapshot = None
    first_written = None
    last = None
    for index, (intent, checkpoint, response) in enumerate(zip(*(grouped[k] for k in CUSTODY[:3]), strict=True)):
        ip = CompletionIntentPayload.model_validate(intent["payload"])
        cp = CheckpointPayloadV2.model_validate(checkpoint["payload"])
        rp = CompletionResponsePayload.model_validate(response["payload"])
        bound = cp.invocation
        if (
            any(e["task_id"] != "implement" for e in (intent, checkpoint, response))
            or ip.run_id != run["id"]
            or ip.task_key != task["idempotency_key"]
            or ip.task_attempt != task["attempt"]
            or ip.project_id != run.get("project_id")
            or ip.repository_id != run.get("repository_id")
            or ip.config_revision != effective_revision(run)
            or ip.scope_digest != _cut(run, "config_revision")
            or ip.descriptor.request_id != intent["idempotency_key"]
            or cp.request_id != ip.descriptor.request_id
            or cp.intent_digest != intent["digest"]
            or bound.run_cut != invocation_cut(run)
            or bound.round != task["attempt"]
            or not bound.grants
            or not set(bound.grants) <= STATIC_TOOLS
            or (
                previous is not None
                and (
                    bound.ordinal != previous.invocation.ordinal + 1
                    or bound.predecessor != previous.checkpoint_digest
                    or bound.invocation_id != previous.invocation.invocation_id
                    or bound.grants != previous.invocation.grants
                )
            )
            or not intent["sequence"] < checkpoint["sequence"] < response["sequence"]
            or rp.request_id != cp.request_id
            or rp.intent_digest != intent["digest"]
            or rp.response_sha256 != hashlib.sha256(rp.response.encode()).hexdigest()
            or rp.response_bytes != len(rp.response.encode())
        ):
            raise ValueError("Pending completion changed its task, invocation or immutable response")
        AgentCheckpoints._validate_checkpoint(checkpoint, intent)
        captured = BoundaryCheckpoint.decode(cp.snapshot.encode(), cp.checkpoint_digest)
        if first_written is None:
            first_written = set(captured.state["files_written"])
        if previous_snapshot is not None and (
            _bytes(captured.record.history[: len(previous_snapshot.record.history)])
            != _bytes(previous_snapshot.record.history)
            or _bytes(captured.record.limits) != _bytes(previous_snapshot.record.limits)
        ):
            raise ValueError("Pending checkpoint changed retained history or limits")
        previous_snapshot = captured
        completion = ChatCompletion.model_validate_json(rp.response, strict=True)
        if (
            len(completion.choices) != 1
            or completion.model != ip.descriptor.model
            or completion.choices[0].finish_reason not in {"stop", "tool_calls"}
        ):
            raise ValueError("Pending completion needs one complete supported response")
        calls = completion.choices[0].message.tool_calls or []
        schemas = {
            t["name"]: t["parameters_json_schema"] for t in captured.record.contract["parameters"]["function_tools"]
        }
        if len({c.id for c in calls}) != len(calls):
            raise ValueError("Pending response has duplicated tool identities")
        for call in calls:
            if call.type != "function" or call.function.name not in bound.grants or call.function.name not in schemas:
                raise ValueError("Pending response requests an ungranted function")
            _schema(schemas[call.function.name], _json(call.function.arguments))
        if index < len(keys) - 1:
            consume = grouped[CUSTODY[-1]][index]
            raw = consume["payload"]
            consumed = (ConsumePayload if raw.get("version") == 1 else ConsumePayloadV2).model_validate(raw)
            record_digest = (
                consumed.response_record_digest
                if isinstance(consumed, ConsumePayload)
                else consumed.source.record_digest
            )
            if (
                consumed.request_id != cp.request_id
                or consumed.intent_digest != intent["digest"]
                or consumed.checkpoint_record_digest != checkpoint["digest"]
                or record_digest != response["digest"]
                or consumed.response_sha256 != rp.response_sha256
                or consumed.response_bytes != rp.response_bytes
                or consume["sequence"] <= response["sequence"]
                or consume["task_id"] != "implement"
            ):
                raise ValueError("Prior completion consumption differs from its retained result")
            if not calls:
                raise ValueError("Prior completion is not an intermediate tool turn")
            expected_calls.update(c.id for c in calls)
            prior_responses.append((cp, ip, rp, [c for c in calls if c.type == "function"]))
        previous = cp
        last = {"intent": intent, "checkpoint": checkpoint, "response": response}
    assert last is not None
    cp = CheckpointPayloadV2.model_validate(last["checkpoint"]["payload"])
    if any(
        e["sequence"] > last["checkpoint"]["sequence"]
        and e["kind"] not in {"bridge_request_response", "verification_handoff_refused", HANDOFF}
        for e in entries
    ):
        raise ValueError("Later native activity prevents pending checkpoint continuation")
    captured = BoundaryCheckpoint.decode(cp.snapshot.encode(), cp.checkpoint_digest)
    returns = {p.tool_call_id: p for m in captured.history for p in m.parts if isinstance(p, ToolReturnPart)}
    effects = [
        e for e in entries if e["kind"] == "agentic_tool_intent" and e["sequence"] > grouped[CUSTODY[0]][0]["sequence"]
    ]
    if {e["payload"].get("tool_call_id") for e in effects} != expected_calls or len(effects) != len(expected_calls):
        raise ValueError("Prior tool calls have missing or duplicate native effects")
    from fireflyframework_agentic.model_utils import deserialize_model_messages, serialize_model_messages
    from pydantic_ai.messages import TextPart

    from firefly_forge.readonly_observation_reconstruction import PreparedObservation, observation_projection

    arguments_by_call: dict[str, tuple[Any, Any]] = {}
    for prior_cp, prior_ip, prior_response, prior_calls in prior_responses:
        model_matches = [
            m
            for m in captured.history
            if isinstance(m, ModelResponse)
            and any(isinstance(p, ToolCallPart) and p.tool_call_id == prior_calls[0].id for p in m.parts)
        ]
        if len(model_matches) != 1:
            raise ValueError("Prior response is missing from full checkpoint history")
        message = model_matches[0]
        data = dict(
            checkpoint=prior_cp.model_dump(mode="json"),
            response=prior_response.response,
            endpoint=prior_ip.descriptor.endpoint,
            model=prior_ip.descriptor.model,
            consume_created_at=message.timestamp.isoformat(),
            outcomes={c.id: {"name": c.function.name, "result": None} for c in prior_calls},
        )
        projection, _ = observation_projection(PreparedObservation(_bytes(data)))
        expected_message = deserialize_model_messages(projection[:1])[0]
        if not isinstance(expected_message, ModelResponse):
            raise ValueError("Prior response projection has an unsupported model message")
        raw_completion = ChatCompletion.model_validate_json(prior_response.response, strict=True)
        content = raw_completion.choices[0].message.content
        if content:
            expected_message.parts = [TextPart(content), *expected_message.parts]
        expected_data = serialize_model_messages([expected_message])[0]
        actual_data = serialize_model_messages([message])[0]
        for field in ("timestamp", "run_id", "conversation_id"):
            expected_data.pop(field, None)
            actual_data.pop(field, None)
        if _bytes(expected_data) != _bytes(actual_data):
            raise ValueError("Prior response metadata differs from retained model result")
        prior_checkpoint = BoundaryCheckpoint.decode(prior_cp.snapshot.encode(), prior_cp.checkpoint_digest)
        definitions = {
            item["name"]: item["parameters_json_schema"]
            for item in prior_checkpoint.record.contract["parameters"]["function_tools"]
        }
        for call_part in message.parts:
            if isinstance(call_part, ToolCallPart):
                if call_part.tool_call_id is None:
                    raise ValueError("Prior model call has no exact native tool identity")
                arguments_by_call[call_part.tool_call_id] = _tool_arguments(call_part, definitions[call_part.tool_name])
    total = RunUsage()
    responses = [m for m in captured.history if isinstance(m, ModelResponse)]
    for message in responses:
        total.incr(message.usage)
        total.requests += 1
    total.tool_calls = sum(isinstance(p, ToolReturnPart) for m in captured.history for p in m.parts)
    usage = USAGE.dump_python(total, mode="json")
    for field in ("output_reasoning_tokens", "cache_write_1h_tokens"):
        if field in captured.record.usage:
            usage[field] = sum(getattr(m.usage, field, 0) for m in responses)
    if _bytes(usage) != _bytes(captured.record.usage):
        raise ValueError("Pending checkpoint usage differs from full native consumption")
    written = set(first_written or ())
    for effect in effects:
        p = effect["payload"]
        matches = [
            e
            for e in entries
            if e["kind"] in {"agentic_tool_result", "agentic_tool_unknown"}
            and e["idempotency_key"] == effect["idempotency_key"]
        ]
        if len(matches) != 1 or matches[0]["kind"] != "agentic_tool_result":
            raise ValueError("An earlier tool effect has no exact retained result")
        end = matches[0]
        q = end["payload"]
        part = returns.get(p["tool_call_id"])
        raw_arguments, public_arguments = arguments_by_call[p["tool_call_id"]]
        _retained_argument(p, "raw_arguments", raw_arguments)
        _retained_argument(p, "arguments", public_arguments)
        if (
            q.get("intent_digest") != effect["digest"]
            or q.get("status") != "retained"
            or not effect["sequence"] < end["sequence"] < last["checkpoint"]["sequence"]
            or part is None
            or part.tool_name != p["tool"]
            or p.get("task_key") != task["idempotency_key"]
            or p.get("task_attempt") != task["attempt"]
            or p.get("binding_sha256") != invocation_cut(run)
            or q.get("arguments_sha256") != digest({"arguments": public_arguments})
            or q.get("raw_arguments_sha256") != digest({"raw_arguments": raw_arguments})
            or q.get("result_sha256") != digest({"result": part.content})
            or ("result" in q and _bytes(q["result"]) != _bytes(part.content))
        ):
            raise ValueError("Known tool history differs from its immutable native result")
        if p["tool"] in {"write_file", "edit_lines"}:
            written.add(public_arguments["path"])
    if written != set(captured.state["files_written"]):
        raise ValueError("Pending checkpoint lost a previously written path")
    return {
        **last,
        "closed_requests": keys[:-1],
        "frontier_digest": digest({"entries": entries}),
        "usage": captured.record.usage,
        "limits": captured.record.limits,
        "state": captured.state,
    }


def settled_checkpoint_handoff(entry: Data, entries: list[Data]) -> list[str]:
    if not Journal.verify_chain(entries) or entry not in entries:
        raise ValueError("Checkpoint handoff prefix changed")
    with handoff_proof_scope() as cache:
        key = entry["digest"]
        if key not in cache:
            cache[key] = tuple(_settled_checkpoint_handoff(entry, entries))
        return list(cache[key])


def _settled_checkpoint_handoff(entry: Data, entries: list[Data]) -> list[str]:
    from firefly_forge.checkpoint_continuations import validate_witness

    validate_witness(entry, entries)
    p = entry["payload"]
    frontier = pending_checkpoint(p["run"], p["task"], entries[: entry["sequence"] - 1])
    if (
        frontier["checkpoint"]["digest"] != p["selection"]["expected_checkpoint_digest"]
        or frontier["response"]["digest"] != p["selection"]["source"]["record_digest"]
    ):
        raise ValueError("Handoff selected a different pending frontier")
    c = p["candidate"]
    cp = frontier["checkpoint"]["payload"]
    if (
        _bytes(c["usage"]) != _bytes(frontier["usage"])
        or _bytes(c["limits"]) != _bytes(frontier["limits"])
        or c["source_digest"] != digest(cp["invocation"]["workspace"])
        or type(c["original_deadline_ms"]) is not int
        or c["original_deadline_ms"] != cp["invocation"]["deadline_ms"]
    ):
        raise ValueError("Checkpoint candidate changed retained usage, limits or source")
    return list(frontier["closed_requests"])
