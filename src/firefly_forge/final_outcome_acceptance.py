# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bind existing consumed custody to the ordinary native terminal writer."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import Any

from firefly_forge.agent_checkpoint_models import ConsumePayloadV2
from firefly_forge.journal import digest
from firefly_forge.known_worker_completion_proof import (
    CUSTODY,
    PreparedWorkerCompletion,
    _prepared_digest,
    _scope,
    _unsettled,
)
from firefly_forge.known_worker_completions import _entries, _task
from firefly_forge.store import identifier


@dataclass(frozen=True)
class PreparedFinalAcceptance:
    restoration: Any
    prepared: PreparedWorkerCompletion

    @property
    def ticket(self) -> dict[str, Any]:
        return dict(self.restoration.ticket)

    async def current(self) -> None:
        await self.restoration.service.acceptance_current(self.restoration)


async def prepare_final_acceptance(execution: Any) -> PreparedFinalAcceptance:
    await execution.guard_source()
    service = execution.service
    p = execution.witness["payload"]
    async with service.store._unit():
        run = await service.store.get_run(execution.run_id)
        task = _task(await service.journal._task(execution.run_id, "implement"))
        entries = _entries(await service.journal.records.find_for_run(identifier(execution.run_id)), execution.run_id)
    original = entries[: execution.witness["sequence"] - 1]
    if digest({"entries": original}) != p["candidate"]["journal_digest"]:
        raise ValueError("Known final acceptance lost its original immutable prefix")
    grouped = _unsettled(original, execution.run_id, restored=True)
    closures = []
    for intent, checkpoint, response, consume in zip(*(grouped[kind] for kind in CUSTODY), strict=True):
        fields = dict(
            request_id=intent["idempotency_key"],
            intent_digest=intent["digest"],
            checkpoint_record_digest=checkpoint["digest"],
            response_record_digest=response["digest"],
            consume_record_digest=consume["digest"],
        )
        if consume["payload"].get("version") == 2:
            spent = ConsumePayloadV2.model_validate(consume["payload"])
            if (
                spent.restore_request_id != execution.ticket["id"]
                or spent.generation != p["failed_ticket"]["generation"]
            ):
                raise ValueError("Earlier consumed handoff has a different retained native claim")
            fields.update(
                consume_version=2,
                custody="known_final_outcome_v1",
                source_kind=spent.source.kind,
                restore_request_id=spent.restore_request_id,
                generation=spent.generation,
                final_outcome_witness_digest=execution.witness["digest"],
                original_ticket_digest=digest(p["failed_ticket"]),
                original_events_digest=digest({"events": p["events"]}),
            )
        closures.append(fields)
    data = execution.prepared.data
    prepared = PreparedWorkerCompletion(
        len(entries),
        digest({"entries": entries}),
        _scope(run, task),
        copy.deepcopy(data["report"]["worker_feedback"] or {}),
        tuple(closures),
        data["consume"]["payload"]["response_sha256"],
        p["candidate"]["current_runtime_contract"],
        None,
        "",
        copy.deepcopy(data["report"]),
    )
    prepared = replace(prepared, seal=_prepared_digest(prepared))
    return PreparedFinalAcceptance(execution, prepared)
