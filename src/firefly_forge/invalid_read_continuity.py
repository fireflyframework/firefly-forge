# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline custody proof for one already-consumed, statically rejected evidence read."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2, ConsumePayload
from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
from firefly_forge.agentic_checkpoint_invocation import STATIC_TOOLS, invocation_cut
from firefly_forge.bridge_completion_models import CompletionIntentPayload, CompletionResponsePayload
from firefly_forge.bridge_completions import _cut, _outside_unit
from firefly_forge.checkpoint_mode_runtime import effective_model_selection, effective_public_config, effective_revision
from firefly_forge.invalid_read_continuity_models import PROOF
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completion_proof import CUSTODY, _schema, _unsettled
from firefly_forge.run_evidence import EvidenceQuery

Data = dict[str, Any]
# Approved historical validator and pre-callback call site; neither is loaded from an operator's request.
LEGACY_QUERY_SHA256 = "a259f91531882f1370cd2eab5aee98c5e1e76b3cca4084e3abfa05da77d77344"
# The reviewed reader successor keeps EvidenceQuery byte-identical. Historical proof identity stays unchanged.
APPROVED_QUERY_VALIDATORS = dict.fromkeys(
    (
        "a259f91531882f1370cd2eab5aee98c5e1e76b3cca4084e3abfa05da77d77344",
        "2712e480fb4b6a21f0ff999d5b8fa233d5087278d6b755f12a90e90889649e30",
    ),
    "a259f91531882f1370cd2eab5aee98c5e1e76b3cca4084e3abfa05da77d77344",
)
LEGACY_ENGINE_SHA256 = "e32e3d199ab8147017e781e757aa48b5857472a51ac93ff3f49116e13da716ac"
LEGACY_RUNTIME_CONTRACT = "f3cc412b382858ee8b4e9c9534a1dba20fcc582936d07e96a620c9f7a0c247f6"
# Same frozen 55 adapters and prompt manifest; retained 2.58.0/3.35.1 SDK launch epoch.
RETAINED_SDK_RUNTIME_CONTRACT = "0b48cf3f13632a7f182edb904ae97060f585f8907a737b695422eb8f4582786d"


def require_legacy_query_validator() -> None:
    validator_path = Path(__file__).with_name("run_evidence.py")
    observed = hashlib.sha256(validator_path.read_bytes()).hexdigest()
    if APPROVED_QUERY_VALIDATORS.get(observed) != LEGACY_QUERY_SHA256:
        raise ValueError("The pinned historical input validator is unavailable")


def registered_legacy_runtime(entries: list[Data], run_id: str, *, primary: str = LEGACY_RUNTIME_CONTRACT) -> str:
    _outside_unit()
    if not Journal.verify_chain(entries) or any(e["run_id"] != run_id for e in entries):
        raise ValueError("Historical runtime requires intact native custody")
    contracts = {
        CheckpointPayloadV2.model_validate(e["payload"]).invocation.contract
        for e in _unsettled(entries, run_id)[CUSTODY[1]]
    }
    if len(contracts) != 1 or not contracts <= {primary, RETAINED_SDK_RUNTIME_CONTRACT}:
        raise ValueError("No uniform registered historical runtime exists")
    return next(iter(contracts))


def historical_checkpoint(payload: CheckpointPayloadV2) -> BoundaryCheckpoint:
    raw = payload.snapshot.encode()
    if payload.invocation.contract == RETAINED_SDK_RUNTIME_CONTRACT:
        derived = BoundaryCheckpoint.migrate_public_compatibility(raw, payload.checkpoint_digest)
        if not {definition["name"] for definition in derived.record.contract["parameters"]["function_tools"]} <= set(
            payload.invocation.grants
        ):
            raise ValueError("Historical SDK tools exceed their original grants")
        return derived
    return BoundaryCheckpoint.decode(raw, payload.checkpoint_digest)


def compatibility_migration(payload: CheckpointPayloadV2) -> Data | None:
    derived = historical_checkpoint(payload)
    if derived.digest == payload.checkpoint_digest:
        return None
    original_contract = BoundaryCheckpoint(payload.snapshot.encode(), payload.checkpoint_digest).record.contract
    provenance = {
        name: original_contract["parameters"][name]
        for name in ("_telemetry_mcp_url_ids", "_telemetry_mcp_url_toolset_ids", "_telemetry_mcp_url_tool_names")
    }
    tool_kinds = [
        {"name": tool["name"], "source_present": "tool_kind" in tool, "source_value": None, "target_value": None}
        for tool in original_contract["parameters"]["function_tools"]
    ]
    native_target = derived.record.compatibility == {"pyfly": "26.10.16", "pydantic-ai-slim": "2.59.0"}
    projection_version = 4 if native_target else 3
    return dict(
        version=projection_version,
        source_checkpoint=payload.checkpoint_digest,
        derived_checkpoint=derived.digest,
        source_runtime=payload.invocation.contract,
        source={"fireflyframework-agentic": "26.10.1", "pydantic-ai-slim": "2.58.0"},
        target=derived.record.compatibility,
        original_contract=digest(original_contract),
        derived_contract=digest(derived.record.contract),
        source_telemetry=provenance,
        source_telemetry_sha256=digest(provenance),
        telemetry_variant={name: "unknown" if value is None else "known_empty" for name, value in provenance.items()},
        source_function_tool_kinds=tool_kinds,
        source_function_tool_kinds_sha256=digest({"tools": tool_kinds}),
        contract_projection={
            "version": projection_version,
            "preserve_exact_provenance" if native_target else "remove_exact_provenance": provenance,
            "function_tool_kinds": tool_kinds,
        },
        preserved=["history", "request_history", "usage", "limits", "state", "wire_sha256", "wire_bytes"],
    )


def _json(raw: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> Data:
        result: Data = {}
        for key, value in items:
            if key in result:
                raise ValueError("Continuity JSON has duplicate object fields")
            result[key] = value
        return result

    return json.loads(
        raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON"))
    )


def _bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


@dataclass(frozen=True)
class PreparedInvalidRead:
    sealed: bytes = field(repr=False)

    @property
    def data(self) -> Data:
        return dict(json.loads(self.sealed))

    @property
    def proof(self) -> Data:
        return dict(self.data["proof"])

    @property
    def checkpoint(self) -> BoundaryCheckpoint:
        value = self.data["checkpoint"]
        return historical_checkpoint(CheckpointPayloadV2.model_validate(value))


PURE_LIST_FILES_HANDLER_SHA256 = "53d9977ebcb050abcda6592baf09cf3de96f26fb6b3ce40fddbec2a2236d2dce"
UNAVAILABLE_OBSERVATION = {"status": "observation_unavailable", "reason": "original_read_result_not_retained"}


def unavailable_source_observations(
    calls: list[Any], binding: Any, captured: BoundaryCheckpoint, response_sha256: str
) -> tuple[Any, list[Data]]:
    unavailable: list[Data] = []
    if any(c.type != "function" for c in calls) or len({c.id for c in calls}) != len(calls):
        raise ValueError("Sibling observations require unique exact function call identities")
    readers = [c for c in calls if c.function.name == "read_run_evidence"]
    if len(readers) != 1:
        raise ValueError("Sibling observations require exactly one statically rejected evidence read")
    schemas = {t["name"]: t["parameters_json_schema"] for t in captured.record.contract["parameters"]["function_tools"]}
    for index, sibling in enumerate(calls):
        if sibling is readers[0]:
            continue
        if (
            sibling.function.name != "list_files"
            or sibling.function.name not in binding.grants
            or sibling.function.name not in schemas
            or len(sibling.function.arguments.encode()) > 64000
            or not 1 <= len(sibling.id) <= 200
        ):
            raise ValueError("Only granted pinned pure list_files observations may be unavailable")
        args = _json(sibling.function.arguments)
        if type(args) is not dict:
            raise ValueError("Source observations require an exact object argument envelope")
        from jsonschema.exceptions import ValidationError as SchemaValidationError  # type: ignore[import-untyped]

        try:
            _schema(schemas["list_files"], args)
        except SchemaValidationError:
            raise ValueError("Source observation arguments differ from the exact frozen schema") from None
        unavailable.append(
            dict(
                version=1,
                index=index,
                tool_call_id=sibling.id,
                tool_name="list_files",
                arguments_digest=digest(args),
                response_sha256=response_sha256,
                source_digest=digest(binding.workspace),
                grants_digest=digest({"grants": binding.grants}),
                handler_sha256=PURE_LIST_FILES_HANDLER_SHA256,
                original_outcome="not_retained",
                outcome=dict(UNAVAILABLE_OBSERVATION),
            )
        )
    return readers[0], unavailable


def native_tool_custody(run_id: str, entries: list[Data], rows: list[Data]) -> str:
    if not Journal.verify_chain(entries) or any(e["run_id"] != run_id for e in entries):
        raise ValueError("Native tool custody requires its intact original journal chain")
    if type(rows) is not list or len(_bytes(rows)) > 16 * 1024 * 1024:
        raise ValueError("Native tool custody exceeds the existing bounded proof window")
    intents = [e for e in entries if e["kind"] == "tool_intent"]
    receipts = [e for e in entries if e["kind"] == "tool_receipt"]
    if any(e["kind"] in {"tool_unknown", "tool_reconcile", "tool_reconciled"} for e in entries):
        raise ValueError("Unknown native tool effects cannot become unavailable source observations")
    if len(intents) != len(rows) or len(receipts) != len(rows):
        raise ValueError("Native tools require complete immutable row and WAL custody")
    identities = set()
    for row in rows:
        if set(row) != {"id", "run_id", "task_id", "tool", "correlation_id", "status", "data"}:
            raise ValueError("Native tool columns differ")
        key = row["correlation_id"]
        if key in identities or row["run_id"] != run_id or row["status"] not in {"succeeded", "rejected"}:
            raise ValueError("Unsettled or ambiguous native tool custody prevents observation abandonment")
        identities.add(key)
        data, admission = row["data"], row["data"]["admission"]
        admitted = [e for e in intents if e["idempotency_key"] == key]
        completed = [e for e in receipts if e["idempotency_key"] == key]
        evidence = {
            k: v
            for k, v in data.items()
            if k not in {"admission", "request_digest", "created_at", "lease_expires_at", "completed_at"}
        }
        if (
            len(admitted) != 1
            or len(completed) != 1
            or admission != admitted[0]["payload"]
            or digest(admission) != data["request_digest"]
            or any(admission[k] != row[k] for k in ("run_id", "task_id", "tool", "correlation_id"))
            or completed[0]["payload"]
            != {"correlation_id": key, "tool": row["tool"], "status": row["status"], **evidence}
            or not admitted[0]["sequence"] < completed[0]["sequence"]
            or any(e["run_id"] != run_id or e["task_id"] != row["task_id"] for e in (admitted[0], completed[0]))
        ):
            raise ValueError("Native tool row differs from its immutable completed receipt")
    return digest({"native_tools": rows})


def prepare_invalid_read(
    run: Data,
    task: Data,
    entries: list[Data],
    *,
    accepted_runtime_contract: str | None = None,
    allow_unavailable_source_observations: bool = False,
    native_tools: list[Data] | None = None,
) -> PreparedInvalidRead:
    """The runtime argument is a trusted policy-catalog pin, never an admission-body field."""
    _outside_unit()
    if (
        len(entries) > 10000
        or len(_bytes([run, task, entries])) > 16 * 1024 * 1024
        or not entries
        or not Journal.verify_chain(entries)
        or run.get("agent") != "bridge"
        or run.get("status") != "failed"
        or run.get("active_task") != "implement"
        or not run.get("implementation_pending")
        or task.get("id") != "implement"
        or task.get("attempt") != run.get("attempt")
        or task.get("status") not in {"running", "needs_reconciliation"}
    ):
        raise ValueError("Continuity requires an intact, failed original implementation attempt")
    require_legacy_query_validator()
    if accepted_runtime_contract is None:
        accepted_runtime_contract = registered_legacy_runtime(entries, run["id"])
    policy = digest(
        {
            "version": 1,
            "runtime": accepted_runtime_contract,
            "validator": LEGACY_QUERY_SHA256,
            "pre_callback_adapter": LEGACY_ENGINE_SHA256,
            "tool": "read_run_evidence",
        }
    )
    grouped = _unsettled(entries, run["id"])
    keys = [e["idempotency_key"] for e in grouped[CUSTODY[0]]]
    if len(keys) > 100 or run.get("bridge_completion_hold") != {
        "version": 1,
        "reason": "graph_checkpoint_unavailable",
        "request_id": keys[0],
        "intent_digest": grouped[CUSTODY[0]][0]["digest"],
    }:
        raise ValueError("Continuity hold does not name the original unsettled request")
    previous = None
    previous_checkpoint = None
    closures = []
    selection = effective_model_selection(run) or {}
    config = effective_public_config(run)
    endpoint = config.get("endpoint", "")
    endpoint = endpoint if endpoint.endswith("/v1") else endpoint + "/v1"
    for intent, checkpoint, response, consume in zip(*(grouped[k] for k in CUSTODY), strict=True):
        original = CompletionIntentPayload.model_validate(intent["payload"])
        payload = CheckpointPayloadV2.model_validate(checkpoint["payload"])
        body = CompletionResponsePayload.model_validate(response["payload"])
        spent = ConsumePayload.model_validate(consume["payload"])
        binding = payload.invocation
        if (
            any(
                e["run_id"] != run["id"] or e["task_id"] != "implement" for e in (intent, checkpoint, response, consume)
            )
            or original.run_id != run["id"]
            or original.task_id != "implement"
            or original.project_id != run.get("project_id")
            or original.repository_id != run.get("repository_id")
            or original.task_key != task["idempotency_key"]
            or original.task_attempt != task["attempt"]
            or original.profile_id != selection.get("profile_id")
            or original.config_revision != effective_revision(run)
            or original.scope_digest != _cut(run, "config_revision")
            or original.descriptor.endpoint != endpoint
            or original.descriptor.model != config.get("model", "bridge/default")
            or original.descriptor.request_id != intent["idempotency_key"]
            or payload.request_id != original.descriptor.request_id
            or payload.intent_digest != intent["digest"]
            or binding.contract != accepted_runtime_contract
            or binding.run_cut != invocation_cut(run)
            or binding.round != task["attempt"]
            or not binding.grants
            or not set(binding.grants) <= STATIC_TOOLS
            or len(set(binding.grants)) != len(binding.grants)
            or (previous is None and (binding.ordinal != 1 or binding.predecessor is not None))
            or (
                previous is not None
                and (
                    binding.ordinal != previous.ordinal + 1
                    or binding.invocation_id != previous.invocation_id
                    or binding.predecessor != previous_checkpoint
                    or binding.grants != previous.grants
                    or binding.deadline_ms != previous.deadline_ms
                )
            )
            or not intent["sequence"] < checkpoint["sequence"] < response["sequence"] < consume["sequence"]
            or body.request_id != original.descriptor.request_id
            or body.intent_digest != intent["digest"]
            or spent.request_id != body.request_id
            or spent.intent_digest != intent["digest"]
            or spent.checkpoint_record_digest != checkpoint["digest"]
            or spent.response_record_digest != response["digest"]
            or spent.response_sha256 != body.response_sha256
            or spent.response_bytes != body.response_bytes
        ):
            raise ValueError("Continuity custody differs from its original frozen scope or consume receipt")
        if binding.contract == RETAINED_SDK_RUNTIME_CONTRACT:
            captured = historical_checkpoint(payload)
            if (
                captured.wire_sha256 != original.descriptor.wire_sha256
                or captured.record.wire_bytes != original.descriptor.wire_bytes
            ):
                raise ValueError("Retained checkpoint wire differs from its intent")
        else:
            AgentCheckpoints._validate_checkpoint(checkpoint, intent)
        _json(body.response)
        completion = ChatCompletion.model_validate_json(body.response, strict=True)
        if len(completion.choices) != 1 or completion.model != original.descriptor.model or completion.usage is None:
            raise ValueError("Continuity requires one exact original model response")
        previous, previous_checkpoint = binding, payload.checkpoint_digest
        closures.append(
            {
                "request_id": body.request_id,
                "intent_digest": intent["digest"],
                "checkpoint_record_digest": checkpoint["digest"],
                "response_record_digest": response["digest"],
                "consume_record_digest": consume["digest"],
            }
        )
    captured = historical_checkpoint(payload)
    choice = completion.choices[0]
    calls = choice.message.tool_calls or []
    if (
        choice.finish_reason != "tool_calls"
        or not calls
        or choice.message.content not in {None, ""}
        or choice.message.refusal
        or choice.message.audio is not None
    ):
        raise ValueError("Continuity supports exactly one rejected tool call without a final report or sibling effects")
    unavailable: list[Data] = []
    if len(calls) > 1:
        if allow_unavailable_source_observations is not True or native_tools is None:
            raise ValueError("Unavailable source observations require explicit policy and native custody")
        call, unavailable = unavailable_source_observations(calls, binding, captured, body.response_sha256)
        tools_digest = native_tool_custody(run["id"], entries, native_tools)
    else:
        call = calls[0]
    if call.type != "function" or call.function.name != "read_run_evidence" or call.function.name not in binding.grants:
        raise ValueError("Continuity supports only the granted evidence reader")
    schemas = {t["name"]: t["parameters_json_schema"] for t in captured.record.contract["parameters"]["function_tools"]}
    if len(call.function.arguments.encode()) > 64000 or call.function.name not in schemas:
        raise ValueError("Continuity arguments exceed their declared frozen schema")
    arguments = _json(call.function.arguments)
    if not isinstance(arguments, dict) or set(arguments) != {"query"} or not isinstance(arguments["query"], dict):
        raise ValueError("Continuity requires the original object query envelope")
    from jsonschema.exceptions import ValidationError as SchemaValidationError

    try:
        _schema(schemas[call.function.name], arguments)
    except SchemaValidationError:
        raise ValueError("Original query was not admitted by its frozen tool schema") from None
    try:
        EvidenceQuery.model_validate(arguments["query"])
    except ValidationError:
        pass
    else:
        raise ValueError("The original evidence query was valid; callback effects cannot be excluded")
    if any(e["kind"] != "recovery" for e in entries if e["sequence"] > consume["sequence"]):
        raise ValueError("Later native effects prevent isolated validation settlement")
    holds = ["continuation_lease_policy_unavailable"]
    if binding.deadline_ms <= int(time.time() * 1000):
        holds.append("original_deadline_expired")
    proof = dict(
        version=1,
        kind="consumed_invalid_read_continuity",
        run_id=run["id"],
        task_key=task["idempotency_key"],
        attempt=task["attempt"],
        hold_request_id=keys[0],
        completion_request_id=original.descriptor.request_id,
        intent_digest=intent["digest"],
        checkpoint_record_digest=checkpoint["digest"],
        checkpoint_digest=payload.checkpoint_digest,
        response_record_digest=response["digest"],
        consume_record_digest=consume["digest"],
        completions=closures,
        journal_digest=digest({"entries": entries}),
        run_digest=digest(run),
        task_digest=digest(task),
        policy_digest=policy,
        runtime_contract=binding.contract,
        tool_call_id=call.id,
        tool_arguments_digest=digest(arguments),
        deadline_ms=binding.deadline_ms,
        result={"status": "invalid_query"},
        admission="HOLD",
        holds=holds,
    )
    if unavailable:
        proof.update(version=2, unavailable_observations=unavailable, native_tool_custody_digest=tools_digest)
    proof["digest"] = digest(proof)
    proof = PROOF.validate_python(proof).model_dump(mode="json")
    return PreparedInvalidRead(
        _bytes(
            {
                "proof": proof,
                "checkpoint": payload.model_dump(mode="json"),
                "response": body.response,
                "consume_created_at": consume["created_at"],
                "endpoint": endpoint,
                "model": original.descriptor.model,
                "source_run": run,
            }
        )
    )


async def reconstruct_and_migrate(prepared: PreparedInvalidRead) -> Any:
    from firefly_forge.invalid_read_reconstruction import reconstruct_and_migrate as reconstruct

    return await reconstruct(prepared)
