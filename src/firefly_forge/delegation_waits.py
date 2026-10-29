# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Validate durable parent wait outcomes against the native child frontier."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import Field

from firefly_forge.delegation_lifecycle import Digest, child_lifecycle
from firefly_forge.delegation_models import DelegationWaitInput, Identity, StrictRecord
from firefly_forge.journal import digest

Data = dict[str, Any]


class WaitStarted(StrictRecord):
    parent_run_id: Identity
    parent_task_key: Identity
    request: DelegationWaitInput
    generation: Identity
    owner_pid: int = Field(gt=0)


class WaitResult(StrictRecord):
    wait_digest: Digest
    outcome: Literal["satisfied", "timed_out", "cancelled", "unavailable"]
    observation: dict[str, Any]


class WaitResumed(StrictRecord):
    wait_digest: Digest
    result_digest: Digest
    generation: Identity


def wait_history(events: list[Data]) -> dict[tuple[str, str], Data]:
    waits: dict[tuple[str, str], Data] = {}
    by_digest: dict[str, Data] = {}
    try:
        for index, event in enumerate(events):
            kind, payload = event["kind"], event["payload"]
            if kind == "wait_started":
                start = WaitStarted.model_validate(payload)
                key = start.parent_run_id, start.request.request_id
                children = child_lifecycle(events[:index])
                if key in waits or start.request.after_sequence > index:
                    raise ValueError("Wait nonce or cursor differs from its original admission")
                if any(
                    identity not in children
                    or children[identity]["parent_run_id"] != start.parent_run_id
                    or children[identity]["parent_task_key"] != start.parent_task_key
                    for identity in start.request.delegation_ids
                ):
                    raise ValueError("Wait includes foreign child custody")
                value: Data = {
                    "start": start.model_dump(mode="json"),
                    "digest": event["digest"],
                    "result": None,
                    "resumed": False,
                }
                waits[key] = by_digest[event["digest"]] = value
            elif kind == "wait_result":
                result = WaitResult.model_validate(payload)
                value = by_digest[result.wait_digest]
                request = value["start"]["request"]
                children = child_lifecycle(events[:index])
                selected = [children[identity] for identity in request["delegation_ids"]]
                expected = {
                    "version": 1,
                    "family_id": event["family_id"],
                    "after_sequence": index,
                    "children": [
                        {
                            name: child[name]
                            for name in (
                                "version",
                                "delegation_id",
                                "task_id",
                                "state",
                                "admission_digest",
                                "claim_digest",
                                "receipt_digest",
                                "summary",
                                "changes",
                                "delivery_verified",
                            )
                        }
                        for child in selected
                    ],
                }
                if value["result"] is not None or digest(result.observation) != digest(expected):
                    raise ValueError("Wait result differs from its exact native child frontier")
                if result.outcome == "satisfied" and any(
                    child["state"] not in {"completed", "failed", "cancelled"} for child in selected
                ):
                    raise ValueError("An unfinished or unknown child cannot satisfy a wait")
                value.update(result=result.model_dump(mode="json"), result_digest=event["digest"])
            elif kind == "wait_resumed":
                resumed = WaitResumed.model_validate(payload)
                value = by_digest[resumed.wait_digest]
                if (
                    value["result"] is None
                    or value["resumed"]
                    or resumed.result_digest != value["result_digest"]
                    or resumed.generation != value["start"]["generation"]
                ):
                    raise ValueError("Parent compute resumes once under its original wait owner")
                value["resumed"] = True
    except (KeyError, TypeError, ValueError):
        raise ValueError("Native parent wait history does not verify") from None
    return copy.deepcopy(waits)
