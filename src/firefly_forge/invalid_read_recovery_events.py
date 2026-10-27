# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Complete bounded native event custody; lifecycle eligibility belongs to recovery."""

from __future__ import annotations

import copy
from typing import Any

from firefly_forge.bridge_completions import MAX_BYTES
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.store import identifier

MAX_EVENTS = 5


def _budget(value: Any) -> tuple[int, int]:
    if (
        type(value) is not tuple
        or len(value) != 2
        or any(type(item) is not int for item in value)
        or not 1 <= value[0] <= MAX_EVENTS
        or not 0 < value[1] <= MAX_BYTES
    ):
        raise ValueError("Recovery requires a complete bounded native request-event history")
    return value[0], value[1]


async def read_recovery_events(requests: Any, request_id: str) -> list[dict[str, Any]]:
    try:
        key = identifier(request_id)
    except KeyError as error:
        raise ValueError("Recovery requires an exact lowercase native request identity") from error
    if key.hex != request_id:
        raise ValueError("Recovery requires an exact lowercase native request identity")
    async with requests.store._unit():
        before = _budget(await requests.journal.custody_budget(key))
        rows = await requests.journal.events(key)
        if len(rows) != before[0]:
            raise ValueError("Recovery native request-event history is incomplete")
        entries = []
        for sequence, row in enumerate(rows, 1):
            value = copy.deepcopy(row.data)
            if (
                row.request_id != key
                or type(row.sequence) is not int
                or row.sequence != sequence
                or type(value) is not dict
                or value.get("request_id") != request_id
                or type(value.get("sequence")) is not int
                or value["sequence"] != sequence
            ):
                raise ValueError("Recovery request-event native columns differ from their retained data")
            entries.append(value)
        try:
            valid = ExecutionRequests.verify_chain(entries)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("Recovery native request-event chain changed")
        if _budget(await requests.journal.custody_budget(key)) != before:
            raise ValueError("Recovery native request-event custody changed during inspection")
    return entries
