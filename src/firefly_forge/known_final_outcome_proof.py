# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Inspect an already consumed final response through its sealed public SDK."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

from openai.types.chat import ChatCompletion
from pydantic_ai.usage import RequestUsage

from firefly_forge.agent_checkpoint_models import ConsumePayload
from firefly_forge.agentic_checkpoint_boundary import USAGE, BoundaryCheckpoint
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.checkpoint_continuation_proof import pending_checkpoint
from firefly_forge.invalid_read_continuity import _bytes
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completion_proof import _eligible, _final_report
from firefly_forge.run_evidence import LIMITS, MAX_BYTES

Data = dict[str, Any]


@dataclass(frozen=True)
class PreparedFinalOutcome:
    encoded: bytes

    @property
    def data(self) -> Data:
        return cast(Data, json.loads(self.encoded))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.encoded).hexdigest()


async def prepare_final_outcome(run: Data, task: Data, entries: list[Data]) -> PreparedFinalOutcome:
    _outside_unit()
    if (
        not entries
        or len(entries) > LIMITS["journal"]
        or len(_bytes(entries)) > MAX_BYTES
        or not Journal.verify_chain(entries)
        or entries[-1]["kind"] != "agent_consume"
    ):
        raise ValueError("Known final outcome requires an intact consumed frontier")
    consume = entries[-1]
    consumed = ConsumePayload.model_validate(consume["payload"])
    # This is the real immutable prefix before consumption, not a rewritten journal.
    frontier = pending_checkpoint(run, task, entries[:-1])
    intent, record, response = (frontier[k] for k in ("intent", "checkpoint", "response"))
    rp = response["payload"]
    raw = rp["response"].encode()
    if (
        consume["run_id"] != run["id"]
        or consume["task_id"] != task["id"]
        or consume["sequence"] != response["sequence"] + 1
        or consumed.request_id != intent["idempotency_key"]
        or consumed.intent_digest != intent["digest"]
        or consumed.checkpoint_record_digest != record["digest"]
        or consumed.response_record_digest != response["digest"]
        or consumed.response_sha256 != hashlib.sha256(raw).hexdigest()
        or consumed.response_bytes != len(raw)
    ):
        raise ValueError("Known final outcome differs from its existing consume custody")
    cp = record["payload"]
    checkpoint = BoundaryCheckpoint.decode(cp["snapshot"].encode(), cp["checkpoint_digest"])
    completion = ChatCompletion.model_validate_json(raw, strict=True)
    if (
        completion.choices[0].message.refusal is not None
        or completion.usage is None
        or checkpoint.record.contract["parameters"].get("output_mode") != "prompted"
    ):
        raise ValueError("Known final outcome requires one non-refusal prompted result")
    report = _final_report(completion, checkpoint)
    _eligible(run, task, report["worker_feedback"], accepted=True)
    # Use the SDK's public usage parser; no client, request, graph or callback is constructed.
    raw_usage = completion.usage.model_dump(exclude_none=True)
    details = {
        key: value
        for key, value in raw_usage.items()
        if key not in {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens"}
        and isinstance(value, int)
    }
    if completion.usage.completion_tokens_details is not None:
        details.update(completion.usage.completion_tokens_details.model_dump(exclude_none=True))
    response_usage = RequestUsage.extract(
        {"model": completion.model, "usage": raw_usage},
        provider="openai",
        provider_url=intent["payload"]["descriptor"]["endpoint"].rstrip("/") + "/",
        provider_fallback="openai",
        api_flavor="chat",
        details=details,
    )
    input_details = raw_usage.get("prompt_tokens_details")
    if isinstance(input_details, dict) and isinstance(input_details.get("cache_write_tokens"), int):
        response_usage.cache_write_tokens = input_details["cache_write_tokens"]
    projected = checkpoint.usage
    projected.incr(response_usage)
    projected.requests += 1
    usage = cast(Data, USAGE.dump_python(projected, mode="json"))
    if usage["requests"] != checkpoint.usage.requests + 1 or usage["tool_calls"] != checkpoint.usage.tool_calls:
        raise ValueError("Known final projection changed spent counters")
    data = {
        "version": 1,
        "kind": "already_consumed_final_outcome",
        "run_id": run["id"],
        "task_key": task["idempotency_key"],
        "attempt": task["attempt"],
        "journal_digest": digest({"entries": entries}),
        "intent": intent,
        "checkpoint": record,
        "response": response,
        "consume": consume,
        "report": report,
        "usage": usage,
        "limits": checkpoint.record.limits,
        "state": checkpoint.state,
        "closed_requests": frontier["closed_requests"],
    }
    return PreparedFinalOutcome(_bytes(data))
