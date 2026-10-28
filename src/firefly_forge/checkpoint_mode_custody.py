# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Validate closed historical HTTP custody without restoring graphs or granting execution."""

import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from openai.types.chat import ChatCompletion

from firefly_forge.agent_checkpoint_models import ConsumePayload, ConsumePayloadV2
from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.bridge_completion_models import CompletionIntentPayload, CompletionResponsePayload
from firefly_forge.checkpoint_work_duration import checkpoint_payload
from firefly_forge.configuration import validate_endpoint
from firefly_forge.journal import Journal, digest

Data = dict[str, Any]
KINDS = ("bridge_request_intent", "agent_checkpoint", "bridge_request_response", "agent_consume")


def _validate_closed_custody(
    entries: list[Data],
    run_id: str,
    load_snapshot: Callable[[str], dict[str, str]],
    *,
    project_id: str | None,
    repository_id: str,
) -> list[dict[str, str]]:
    if (
        len(entries) > 10000
        or len(json.dumps(entries).encode()) > 16 * 1024 * 1024
        or not Journal.verify_chain(entries)
    ):
        raise ValueError("Historical Bridge custody requires its bounded intact native journal")
    if any(e.get("run_id") != run_id for e in entries):
        raise ValueError("Historical Bridge custody belongs to another run")
    groups: dict[str, dict[str, list[Data]]] = {k: {} for k in KINDS}
    for e in entries:
        if e["kind"] in groups:
            groups[e["kind"]].setdefault(e["idempotency_key"], []).append(e)
    keys = set(groups[KINDS[0]])
    if any(set(groups[k]) != keys for k in KINDS):
        raise ValueError("Historical Bridge closure has orphan or missing records")
    result = []
    invocations: dict[str, tuple[int, str, dict[str, Any]]] = {}
    for key in sorted(keys, key=lambda k: groups[KINDS[0]][k][0]["sequence"]):
        if any(len(groups[k][key]) != 1 for k in KINDS):
            raise ValueError("Historical Bridge custody is duplicated")
        a, b, c, d = (groups[k][key][0] for k in KINDS)
        if not a["sequence"] < b["sequence"] < c["sequence"] < d["sequence"] or any(
            e["task_id"] != "implement" for e in (a, b, c, d)
        ):
            raise ValueError("Historical Bridge closure order or task changed")
        i = CompletionIntentPayload.model_validate(a["payload"])
        cp = checkpoint_payload(b["payload"])
        r = CompletionResponsePayload.model_validate(c["payload"])
        consume = (ConsumePayload if d["payload"].get("version") == 1 else ConsumePayloadV2).model_validate(
            d["payload"]
        )
        expected = uuid5(NAMESPACE_URL, f"forge:{run_id}:implement:{i.task_attempt}").hex
        originals = [
            e
            for e in entries
            if e["kind"] == "intent" and e["task_id"] == "implement" and e["idempotency_key"] == i.task_key
        ]
        if (
            i.run_id != run_id
            or i.project_id != project_id
            or i.repository_id != repository_id
            or i.task_id != "implement"
            or i.task_key != expected
            or len(originals) != 1
            or originals[0]["sequence"] >= a["sequence"]
            or originals[0]["payload"].get("config_revision") != i.config_revision
            or i.config_field != "config_revision"
            or i.descriptor.request_id != key
        ):
            raise ValueError("Historical Bridge original native task or configuration identity changed")
        settings = load_snapshot(i.config_revision)
        endpoint = validate_endpoint(settings.get("FORGE_BRIDGE_URL", ""))
        endpoint = endpoint if endpoint.endswith("/v1") else endpoint + "/v1"
        if i.descriptor.endpoint != endpoint or i.descriptor.model != settings.get(
            "FORGE_BRIDGE_MODEL", "bridge/default"
        ):
            raise ValueError("Historical Bridge descriptor differs from its original frozen configuration")
        raw = r.response.encode("utf-8")
        body_sha = hashlib.sha256(raw).hexdigest()
        response_digest = (
            consume.response_record_digest if isinstance(consume, ConsumePayload) else consume.source.record_digest
        )
        if (
            isinstance(consume, ConsumePayloadV2)
            and consume.source.kind != "http_response"
            or cp.request_id != key
            or cp.intent_digest != a["digest"]
            or cp.invocation.round != i.task_attempt
            or r.request_id != key
            or r.intent_digest != a["digest"]
            or r.response_sha256 != body_sha
            or r.response_bytes != len(raw)
            or consume.request_id != key
            or consume.intent_digest != a["digest"]
            or consume.checkpoint_record_digest != b["digest"]
            or response_digest != c["digest"]
            or consume.response_sha256 != body_sha
            or consume.response_bytes != len(raw)
        ):
            raise ValueError("Historical Bridge response/checkpoint/consume pointers or bytes changed")
        bound = cp.invocation
        previous = invocations.get(bound.invocation_id)
        fixed = bound.model_dump(exclude={"ordinal", "predecessor", "workspace"})
        if previous is None:
            if bound.ordinal != 1 or bound.predecessor is not None:
                raise ValueError("Historical Bridge invocation starts without its original predecessor")
        elif bound.ordinal != previous[0] + 1 or bound.predecessor != previous[1] or fixed != previous[2]:
            raise ValueError("Historical Bridge invocation closure is discontinuous")
        invocations[bound.invocation_id] = (bound.ordinal, cp.checkpoint_digest, fixed)
        AgentCheckpoints._validate_checkpoint(b, a)
        response = ChatCompletion.model_validate_json(raw, strict=True)
        if (
            response.model != i.descriptor.model
            or len(response.choices) != 1
            or response.choices[0].finish_reason not in {"stop", "tool_calls"}
        ):
            raise ValueError("Historical Bridge response is not one exact known completion")
        result.append(
            {
                "intent_digest": a["digest"],
                "checkpoint_record_digest": b["digest"],
                "response_record_digest": c["digest"],
                "consume_record_digest": d["digest"],
                "settings_digest": digest(settings),
            }
        )
    return result


def validate_closed_custody(
    entries: list[Data],
    run_id: str,
    load_snapshot: Callable[[str], dict[str, str]],
    *,
    project_id: str | None,
    repository_id: str,
) -> list[dict[str, str]]:
    try:
        return _validate_closed_custody(
            entries, run_id, load_snapshot, project_id=project_id, repository_id=repository_id
        )
    except (ValueError, KeyError, TypeError, OSError):
        raise ValueError("Closed historical Bridge custody is incomplete or inconsistent") from None
