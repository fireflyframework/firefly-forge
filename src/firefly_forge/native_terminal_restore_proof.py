# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Distinct restore-ticket custody for a known version-2 consumption."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.agent_checkpoint_models import ConsumePayloadV2, RestoreInput
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.journal import digest
from firefly_forge.store import identifier

Data = dict[str, Any]


@dataclass(frozen=True)
class PreparedRestoreAcceptance:
    restoration: Any
    ticket: Data
    events: tuple[Data, ...]
    result_record: Data
    body: bytes
    seal: str

    def identity(self) -> Data:
        return {
            "ticket": self.ticket,
            "events": list(self.events),
            "result_record": self.result_record,
            "response_sha256": hashlib.sha256(self.body).hexdigest(),
        }

    async def current(self) -> None:
        service = self.restoration.service
        row = await service.requests.records.find_by_id(identifier(self.ticket["id"]), lock=LockMode.PESSIMISTIC_WRITE)
        events = await service.requests.events(self.ticket["id"])
        if (
            digest(self.identity()) != self.seal
            or row is None
            or row.kind != "agent_restore"
            or row.status != "executing"
            or row.run_id.hex != self.ticket["run_id"]
            or row.request_key != self.ticket["request"]["completion_request_id"]
            or row.data != self.ticket
            or events != list(self.events)
            or not service.requests.verify_chain(events)
            or self.ticket.get("owner_pid") != service.store._ownership_pid
        ):
            raise ValueError("Native terminal restore ticket or original claim changed")
        await service.access(await service.store.get_run(self.ticket["run_id"]), self.ticket["request"]["actor"])


async def prepare_restore_acceptance(restoration: Any) -> PreparedRestoreAcceptance:
    _outside_unit()
    await restoration.guard()
    service = restoration.service
    ticket = await service.requests.get(restoration.ticket["id"])
    events = await service.requests.events(ticket["id"])
    if (
        ticket != restoration.ticket
        or ticket["status"] != "executing"
        or ticket.get("kind") != "agent_restore"
        or ticket.get("run_id") != restoration.run_id
        or ticket["request"]["completion_request_id"] != restoration.request_id
        or digest(ticket["request"]) != ticket["request_digest"]
        or not events
        or not service.requests.verify_chain(events)
        or events[0]["kind"] != "request_admitted"
        or events[0]["evidence"] != {"request_digest": ticket["request_digest"]}
        or events[-1]["kind"] != "worker_claim"
        or events[-1]["evidence"]
        != {
            "request_digest": ticket["request_digest"],
            "generation": ticket["generation"],
            "owner_pid": ticket["owner_pid"],
        }
    ):
        raise ValueError("Native terminal restoration lost its exact admitted ticket")
    request = RestoreInput.model_validate(ticket["request"]["input"])
    bundle = await service.checkpoints.restore_bundle(restoration.run_id, restoration.request_id, request)
    if bundle != (restoration.intent, restoration.record, restoration.result_record, restoration.body):
        raise ValueError("Native terminal restoration differs from its original known result")
    value = PreparedRestoreAcceptance(
        restoration, copy.deepcopy(ticket), tuple(copy.deepcopy(events)), copy.deepcopy(bundle[2]), bundle[3], ""
    )
    from dataclasses import replace

    value = replace(value, seal=digest(value.identity()))
    async with service.store._unit():
        await value.current()
    return value


def restored_result(
    prepared: PreparedRestoreAcceptance, intent: Data, checkpoint: Data, response: Data, consume: Data
) -> tuple[bytes, Data]:
    if digest(prepared.identity()) != prepared.seal:
        raise ValueError("Prepared native restore custody changed")
    value = ConsumePayloadV2.model_validate(consume["payload"])
    ticket = prepared.ticket
    request = RestoreInput.model_validate(ticket["request"]["input"])
    if (
        value.request_id != intent["idempotency_key"]
        or value.intent_digest != intent["digest"]
        or value.checkpoint_record_digest != checkpoint["digest"]
        or value.source != request.source
        or value.source.record_digest != response["digest"]
        or response != prepared.result_record
        or value.restore_request_id != ticket["id"]
        or value.generation != ticket["generation"]
        or ticket["request"]["completion_request_id"] != value.request_id
        or request.expected_intent_digest != intent["digest"]
        or request.expected_checkpoint_digest != checkpoint["digest"]
        or value.response_sha256 != hashlib.sha256(prepared.body).hexdigest()
        or value.response_bytes != len(prepared.body)
        or response["kind"]
        != ("bridge_request_response" if value.source.kind == "http_response" else "bridge_request_observation")
    ):
        raise ValueError("Restored native consume differs from its original ticket and result")
    return prepared.body, {
        "consume_version": 2,
        "source_kind": value.source.kind,
        "restore_request_id": value.restore_request_id,
        "generation": value.generation,
        "restore_ticket_digest": digest(ticket),
        "restore_events_digest": digest({"events": list(prepared.events)}),
    }
