# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Known HTTP custody prepared outside a writer and checked atomically inside it."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from openai.types.chat import ChatCompletion

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2, ConsumePayload
from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.agent_roles import BINDINGS, worker_feedback_model
from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
from firefly_forge.agentic_checkpoint_invocation import invocation_cut, runtime_contract
from firefly_forge.agentic_runtime import RuntimeReport
from firefly_forge.bridge_completion_models import CompletionIntentPayload, CompletionResponsePayload
from firefly_forge.bridge_completions import _cut, _outside_unit
from firefly_forge.configuration import validate_endpoint
from firefly_forge.journal import Journal, digest

Data = dict[str, Any]
SETTLEMENT = "worker_completion_receipt"
CUSTODY = ("bridge_request_intent", "agent_checkpoint", "bridge_request_response", "agent_consume")


def _json(value: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> Data:
        result: Data = {}
        for key, item in items:
            if key in result:
                raise ValueError("Final worker output has duplicate JSON keys")
            result[key] = item
        return result

    def invalid(_: str) -> None:
        raise ValueError("Final worker output is not finite JSON")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def _schema(schema: Data, value: Any) -> None:
    pending: list[Any] = [schema]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            if any(
                key in current and not str(current[key]).startswith("#/")
                for key in ("$ref", "$dynamicRef", "$recursiveRef")
            ):
                raise ValueError("Final worker schema cannot resolve external references")
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    Draft202012Validator(schema).validate(value)


def _final_report(completion: ChatCompletion, captured: BoundaryCheckpoint) -> Data:
    parameters = captured.record.contract["parameters"]
    choice = completion.choices[0]
    if parameters.get("output_mode") == "prompted":
        definition = parameters.get("output_object")
        if choice.finish_reason != "stop" or choice.message.tool_calls or not isinstance(definition, dict):
            raise ValueError("Known worker outcome requires its declared final JSON output")
        value = _json(choice.message.content or "")
        _schema(definition["json_schema"], value)
    elif parameters.get("output_mode") == "tool":
        calls = choice.message.tool_calls or []
        definitions = {item["name"]: item for item in parameters.get("output_tools", [])}
        if choice.finish_reason != "tool_calls" or len(calls) != 1:
            raise ValueError("Known worker outcome requires one declared final output tool")
        call = calls[0]
        if call.type != "function" or call.function.name not in definitions:
            raise ValueError("Known worker outcome requires its declared final output tool")
        value = _json(call.function.arguments)
        _schema(definitions[call.function.name]["parameters_json_schema"], value)
    else:
        raise ValueError("Worker output has no supported declared final contract")
    return RuntimeReport.model_validate(value).model_dump(mode="json")


def _final_feedback(completion: ChatCompletion, captured: BoundaryCheckpoint) -> Data:
    report = RuntimeReport.model_validate(_final_report(completion, captured))
    if report.worker_feedback is None:
        raise ValueError("Known worker outcome has no typed final feedback")
    raw = report.worker_feedback.model_dump(mode="json")
    return worker_feedback_model(raw).model_validate(raw).model_dump(mode="json")


def _scope(run: Data, task: Data) -> str:
    return digest(
        {
            "invocation": invocation_cut(run),
            "hold": run.get("bridge_completion_hold"),
            "active_task": run.get("active_task"),
            "attempt": run.get("attempt"),
            "task": {key: task.get(key) for key in ("id", "attempt", "idempotency_key")},
        }
    )


def _eligible(run: Data, task: Data, feedback: Data | None, *, accepted: bool = False) -> None:
    if (
        run.get("agent") != "bridge"
        or run.get("active_task") != "implement"
        or task.get("id") != "implement"
        or task.get("attempt") != run.get("attempt")
        or not task.get("idempotency_key")
        or task.get("status")
        not in (
            {"running", "paused", "needs_reconciliation", "done"}
            if accepted
            else {"running", "paused", "needs_reconciliation"}
        )
        or run.get("status") in {"cancelled", "succeeded"}
        or (run.get("cancel_request") or {}).get("status") == "requested"
        or not run.get("bridge_completion_hold")
        or feedback is not None
        and (
            feedback.get("task_id") != task["id"]
            or feedback.get("attempt") != task["attempt"]
            or feedback.get("task_id") not in BINDINGS[feedback["role_id"]][1]
        )
    ):
        raise ValueError("Known worker outcome requires its original current held task attempt")
    roles = (run.get("role_snapshot") or {}).get("roles", [])
    if (
        feedback is not None
        and roles
        and not any(item["id"] == feedback["role_id"] and item["enabled"] for item in roles)
    ):
        raise ValueError("Final worker role is not enabled in the frozen scope")


@dataclass(frozen=True)
class PreparedWorkerCompletion:
    prefix_count: int
    prefix_digest: str
    scope_digest: str
    feedback: Data
    completions: tuple[Data, ...]
    output_sha256: str
    runtime_contract: str
    existing_feedback_digest: str | None
    seal: str
    report: Data | None = None


def _unsettled(entries: list[Data], run_id: str, *, restored: bool = False) -> dict[str, list[Data]]:
    closed: set[str] = set()
    for entry in entries:
        if entry["kind"] not in {SETTLEMENT, "terminal_completion_receipt"}:
            continue
        if entry["kind"] == "terminal_completion_receipt":
            from firefly_forge.native_terminal_completion_models import validate_native_receipt

            validate_native_receipt(entry, entries)
        else:
            from firefly_forge.known_worker_completion_models import WorkerCompletionReceiptV1

            WorkerCompletionReceiptV1.model_validate(entry["payload"])
        proof = entry["payload"].get("proof") or {}
        if (
            proof.get("version") != 1
            or proof.get("run_id") != run_id
            or proof.get("verification") not in {"worker_output_known_only", "native_terminal_output_known_only"}
        ):
            raise ValueError("Prior completion receipt has invalid scope")
        if digest({k: v for k, v in proof.items() if k != "digest"}) != proof.get("digest"):
            raise ValueError("Prior completion receipt proof changed")
        for item in proof.get("completions", []):
            if item["request_id"] in closed:
                raise ValueError("A completion cannot be settled twice")
            for kind, field in zip(
                CUSTODY,
                ("intent_digest", "checkpoint_record_digest", "response_record_digest", "consume_record_digest"),
                strict=True,
            ):
                if kind == "bridge_request_response" and item.get("source_kind") == "bridge_observation":
                    kind = "bridge_request_observation"
                matches = [
                    record
                    for record in entries
                    if record["kind"] == kind
                    and (
                        record["digest"] == item[field]
                        if kind == "bridge_request_observation"
                        else record["idempotency_key"] == item["request_id"]
                    )
                ]
                if (
                    len(matches) != 1
                    or matches[0]["digest"] != item[field]
                    or matches[0]["sequence"] >= entry["sequence"]
                ):
                    raise ValueError("Prior completion receipt lost its immutable custody")
            closed.add(item["request_id"])
    grouped = {
        kind: [entry for entry in entries if entry["kind"] == kind and entry["idempotency_key"] not in closed]
        for kind in CUSTODY
    }
    keys = [entry["idempotency_key"] for entry in grouped[CUSTODY[0]]]
    if restored:
        selected = []
        for consume in grouped["agent_consume"]:
            if consume["payload"].get("version") == 2:
                source = consume["payload"]["source"]
                kind = (
                    "bridge_request_observation"
                    if source["kind"] == "bridge_observation"
                    else "bridge_request_response"
                )
                matches = [
                    entry for entry in entries if entry["kind"] == kind and entry["digest"] == source["record_digest"]
                ]
            else:
                matches = [
                    entry
                    for entry in grouped["bridge_request_response"]
                    if entry["idempotency_key"] == consume["idempotency_key"]
                ]
            if len(matches) != 1:
                raise ValueError("Native terminal consumption lacks one exact original result")
            selected.append(matches[0])
        if any(entry not in selected for entry in grouped["bridge_request_response"]):
            raise ValueError("Native terminal closure has an unselected original response")
        grouped["bridge_request_response"] = selected
    if (
        not keys
        or len(set(keys)) != len(keys)
        or any(
            [entry["idempotency_key"] for entry in grouped[kind]] != keys
            for kind in CUSTODY[1:]
            if not restored or kind != "bridge_request_response"
        )
        or len(grouped["bridge_request_response"]) != len(keys)
    ):
        raise ValueError("Original completion closure is incomplete, duplicated or unknown")
    return grouped


def prepare_worker_completion(
    run: Data,
    task: Data,
    entries: list[Data],
    *,
    feedback: Data | None = None,
    terminal_report: Data | None = None,
    restored: Any = None,
) -> PreparedWorkerCompletion:
    _outside_unit()
    if not entries or not Journal.verify_chain(entries):
        raise ValueError("Known worker completion requires an intact native journal")
    report = (
        RuntimeReport.model_validate(terminal_report).model_dump(mode="json") if terminal_report is not None else None
    )
    selected = (
        (
            worker_feedback_model(feedback or run.get("worker_feedback") or {})
            .model_validate(feedback or run.get("worker_feedback") or {})
            .model_dump(mode="json")
        )
        if report is None or report.get("worker_feedback") is not None
        else {}
    )
    if report is not None and report.get("worker_feedback") is not None:
        selected = (
            worker_feedback_model(report["worker_feedback"])
            .model_validate(report["worker_feedback"])
            .model_dump(mode="json")
        )
    _eligible(run, task, selected or None, accepted=report is not None)
    grouped = _unsettled(entries, run["id"], restored=restored is not None)
    keys = [entry["idempotency_key"] for entry in grouped[CUSTODY[0]]]
    if run["bridge_completion_hold"] != {
        "version": 1,
        "reason": "graph_checkpoint_unavailable",
        "request_id": keys[0],
        "intent_digest": grouped[CUSTODY[0]][0]["digest"],
    }:
        raise ValueError("Observed hold does not identify the first unsettled completion")
    contract = runtime_contract()
    previous = None
    previous_digest = invocation_id = None
    closures = []
    final = captured = None
    from firefly_forge.checkpoint_mode_runtime import effective_revision

    config = run.get("execution_config") or {}
    endpoint = validate_endpoint(config.get("endpoint", ""))
    endpoint = endpoint if endpoint.endswith("/v1") else endpoint + "/v1"
    from firefly_forge.agentic_checkpoint_invocation import STATIC_TOOLS

    for intent, checkpoint, response, consume in zip(*(grouped[kind] for kind in CUSTODY), strict=True):
        original = CompletionIntentPayload.model_validate(intent["payload"])
        payload = CheckpointPayloadV2.model_validate(checkpoint["payload"])
        binding = payload.invocation
        if (
            any(
                entry["run_id"] != run["id"] or entry["task_id"] != task["id"]
                for entry in (intent, checkpoint, response, consume)
            )
            or payload.request_id != intent["idempotency_key"]
            or payload.intent_digest != intent["digest"]
            or original.run_id != run["id"]
            or original.task_id != task["id"]
            or original.task_key != task["idempotency_key"]
            or original.task_attempt != task["attempt"]
            or original.project_id != run.get("project_id")
            or original.repository_id != run.get("repository_id")
            or original.profile_id != (run.get("model_selection") or {}).get("profile_id")
            or original.config_field != "config_revision"
            or original.config_revision != effective_revision(run)
            or original.scope_digest != _cut(run, "config_revision")
            or original.descriptor.model != config.get("model", "bridge/default")
            or original.descriptor.endpoint != endpoint
            or original.descriptor.request_id != intent["idempotency_key"]
            or binding.contract != contract
            or binding.run_cut != invocation_cut(run)
            or binding.round != task["attempt"]
            or not binding.grants
            or not set(binding.grants) <= STATIC_TOOLS
            or (previous is None and (binding.ordinal != 1 or binding.predecessor is not None))
            or (
                previous is not None
                and (
                    binding.ordinal != previous.ordinal + 1
                    or binding.predecessor != previous_digest
                    or binding.invocation_id != invocation_id
                    or binding.grants != previous.grants
                )
            )
            or not intent["sequence"] < checkpoint["sequence"] < response["sequence"] < consume["sequence"]
        ):
            raise ValueError("Completion custody differs from its current runtime, provider, model or task scope")
        AgentCheckpoints._validate_checkpoint(checkpoint, intent)
        captured = BoundaryCheckpoint.decode(payload.snapshot.encode(), payload.checkpoint_digest)
        restore_custody: Data = {}
        if consume["payload"].get("version") != 1:
            if report is None or restored is None:
                raise ValueError("Restored completion custody requires its original restore-ticket proof")
            from firefly_forge.native_terminal_restore_proof import restored_result

            raw, restore_custody = restored_result(restored, intent, checkpoint, response, consume)
        else:
            body = CompletionResponsePayload.model_validate(response["payload"])
            consumed = ConsumePayload.model_validate(consume["payload"])
            raw = body.response.encode()
            if (
                body.request_id != intent["idempotency_key"]
                or body.intent_digest != intent["digest"]
                or body.response_sha256 != hashlib.sha256(raw).hexdigest()
                or body.response_bytes != len(raw)
                or consumed.request_id != body.request_id
                or consumed.intent_digest != intent["digest"]
                or consumed.checkpoint_record_digest != checkpoint["digest"]
                or consumed.response_record_digest != response["digest"]
                or consumed.response_sha256 != body.response_sha256
                or consumed.response_bytes != body.response_bytes
            ):
                raise ValueError("Known completion response and consume custody differ")
        final = ChatCompletion.model_validate_json(raw, strict=True)
        if (
            final.model != original.descriptor.model
            or len(final.choices) != 1
            or final.choices[0].finish_reason not in {"stop", "tool_calls"}
        ):
            raise ValueError("Known completion does not identify one exact model outcome")
        if intent is not grouped[CUSTODY[0]][-1]:
            calls = final.choices[0].message.tool_calls or []
            schemas = {
                item["name"]: item["parameters_json_schema"]
                for item in captured.record.contract["parameters"]["function_tools"]
            }
            if final.choices[0].finish_reason != "tool_calls" or not calls:
                raise ValueError("An earlier completion is not a declared intermediate tool result")
            for call in calls:
                if call.type != "function" or call.function.name not in schemas:
                    raise ValueError("An intermediate completion calls an undeclared function")
                _schema(schemas[call.function.name], _json(call.function.arguments))
        closures.append(
            {
                "request_id": intent["idempotency_key"],
                "intent_digest": intent["digest"],
                "checkpoint_record_digest": checkpoint["digest"],
                "response_record_digest": response["digest"],
                "consume_record_digest": consume["digest"],
                **restore_custody,
            }
        )
        previous, previous_digest, invocation_id = binding, payload.checkpoint_digest, binding.invocation_id
    assert final is not None and captured is not None
    if report is not None and _final_report(final, captured) != report:
        raise ValueError("Final model output differs from exact normalized RuntimeReport")
    if report is None and _final_feedback(final, captured) != selected:
        raise ValueError("Final model output differs from exact typed worker feedback")
    active_feedbacks = [
        entry
        for entry in entries
        if entry["kind"] == "worker_feedback" and entry["sequence"] > grouped[CUSTODY[0]][0]["sequence"]
    ]
    feedbacks = [entry for entry in active_feedbacks if entry["payload"].get("id") == selected.get("id")]
    if (
        len(active_feedbacks) != len(feedbacks)
        or len(feedbacks) > 1
        or (
            feedbacks
            and (
                feedbacks[0]["payload"] != selected or feedbacks[0]["sequence"] <= grouped[CUSTODY[-1]][-1]["sequence"]
            )
        )
        or (report is None and feedback is None and not feedbacks)
    ):
        raise ValueError("Known worker feedback has no unique consume-before-feedback custody")
    prepared = PreparedWorkerCompletion(
        len(entries),
        digest({"entries": entries}),
        _scope(run, task),
        copy.deepcopy(selected),
        tuple(closures),
        hashlib.sha256(raw).hexdigest(),
        contract,
        feedbacks[0]["digest"] if feedbacks else None,
        "",
        copy.deepcopy(report),
    )
    from dataclasses import replace

    return replace(prepared, seal=_prepared_digest(prepared))


def _prepared_digest(prepared: PreparedWorkerCompletion) -> str:
    from dataclasses import asdict

    return digest({k: v for k, v in asdict(prepared).items() if k != "seal"})


def validate_worker_completion(prepared: PreparedWorkerCompletion, run: Data, task: Data, entries: list[Data]) -> Data:
    """Database-only final predicate; no filesystem, network, decode or result exposure."""
    if _prepared_digest(prepared) != prepared.seal:
        raise ValueError("Prepared completion evidence changed")
    _eligible(run, task, prepared.feedback)
    if (
        not Journal.verify_chain(entries)
        or _scope(run, task) != prepared.scope_digest
        or digest({"entries": entries[: prepared.prefix_count]}) != prepared.prefix_digest
    ):
        raise ValueError("Known completion scope or immutable journal cut changed")
    feedbacks = [
        entry
        for entry in entries
        if entry["kind"] == "worker_feedback" and entry["payload"].get("id") == prepared.feedback["id"]
    ]
    if (
        len(feedbacks) != 1
        or feedbacks[0]["payload"] != prepared.feedback
        or run.get("worker_feedback") != prepared.feedback
        or (
            prepared.existing_feedback_digest is not None
            and feedbacks[0]["digest"] != prepared.existing_feedback_digest
        )
    ):
        raise ValueError("Known completion requires exact stored worker feedback projection")
    if len(entries) != prepared.prefix_count + (prepared.existing_feedback_digest is None) or (
        prepared.existing_feedback_digest is None and entries[-1] != feedbacks[0]
    ):
        raise ValueError("Additional native effects changed the prepared completion cut")
    proof = {
        "version": 1,
        "run_id": run["id"],
        "task_id": task["id"],
        "task_attempt": task["attempt"],
        "task_key": task["idempotency_key"],
        "runtime_contract": prepared.runtime_contract,
        "completion_count": len(prepared.completions),
        "completions": list(prepared.completions),
        "feedback_record_digest": feedbacks[0]["digest"],
        "feedback_sha256": digest(prepared.feedback),
        "final_response_sha256": prepared.output_sha256,
        "observed_hold_digest": digest(run["bridge_completion_hold"]),
        "verification": "worker_output_known_only",
    }
    proof["digest"] = digest(proof)
    return proof
