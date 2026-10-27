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
from firefly_forge.invalid_read_continuity_models import InvalidReadProofV1
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completion_proof import CUSTODY, _schema, _unsettled
from firefly_forge.run_evidence import EvidenceQuery

Data = dict[str, Any]
# Approved historical validator and pre-callback call site; neither is loaded from an operator's request.
LEGACY_QUERY_SHA256 = "a259f91531882f1370cd2eab5aee98c5e1e76b3cca4084e3abfa05da77d77344"
LEGACY_ENGINE_SHA256 = "e32e3d199ab8147017e781e757aa48b5857472a51ac93ff3f49116e13da716ac"
LEGACY_RUNTIME_CONTRACT = "f3cc412b382858ee8b4e9c9534a1dba20fcc582936d07e96a620c9f7a0c247f6"


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
        return BoundaryCheckpoint.decode(value["snapshot"].encode(), value["checkpoint_digest"])


def prepare_invalid_read(
    run: Data, task: Data, entries: list[Data], *, accepted_runtime_contract: str = LEGACY_RUNTIME_CONTRACT
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
    validator_path = Path(__file__).with_name("run_evidence.py")
    if hashlib.sha256(validator_path.read_bytes()).hexdigest() != LEGACY_QUERY_SHA256:
        raise ValueError("The pinned historical input validator is unavailable")
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
    captured = BoundaryCheckpoint.decode(payload.snapshot.encode(), payload.checkpoint_digest)
    choice = completion.choices[0]
    calls = choice.message.tool_calls or []
    if (
        choice.finish_reason != "tool_calls"
        or len(calls) != 1
        or choice.message.content not in {None, ""}
        or choice.message.refusal
        or choice.message.audio is not None
    ):
        raise ValueError("Continuity supports exactly one rejected tool call without a final report or sibling effects")
    call = calls[0]
    if call.type != "function" or call.function.name != "read_run_evidence" or call.function.name not in binding.grants:
        raise ValueError("Continuity supports only the granted evidence reader")
    schemas = {t["name"]: t["parameters_json_schema"] for t in captured.record.contract["parameters"]["function_tools"]}
    if len(call.function.arguments.encode()) > 64000 or call.function.name not in schemas:
        raise ValueError("Continuity arguments exceed their declared frozen schema")
    arguments = _json(call.function.arguments)
    if not isinstance(arguments, dict) or set(arguments) != {"query"} or not isinstance(arguments["query"], dict):
        raise ValueError("Continuity requires the original object query envelope")
    from jsonschema.exceptions import ValidationError as SchemaValidationError  # type: ignore[import-untyped]

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
        checkpoint_digest=captured.digest,
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
    proof["digest"] = digest(proof)
    proof = InvalidReadProofV1.model_validate(proof).model_dump(mode="json")
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
