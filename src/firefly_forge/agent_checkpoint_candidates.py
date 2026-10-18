# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read-only, bounded references for the original implementation result."""

from __future__ import annotations

import copy
import time
from typing import TYPE_CHECKING, Any

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2, RestoreInput
from firefly_forge.agent_checkpoints import CHECKPOINT, CONSUME
from firefly_forge.agentic_checkpoint_invocation import invocation_cut
from firefly_forge.bridge_completion_models import validate_remote_snapshot
from firefly_forge.bridge_completions import OBSERVATION, RESPONSE, _outside_unit
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import identifier, prepared_credential_rejection

if TYPE_CHECKING:
    from firefly_forge.agent_restores import AgentRestores

Data = dict[str, Any]


async def candidates(service: AgentRestores, run_id: str) -> Data:
    _outside_unit()
    run_id = identifier(run_id).hex
    reader = CURRENT_PRINCIPAL.get()
    if reader is None:
        raise AccessDenied("Sign in before inspecting original result candidates")
    run = await service.completions._reader(run_id, reader, execute=True)
    actor = ProjectAccess.actor_snapshot(reader)
    await service.access(run, actor)
    page: Data = {
        "version": 1,
        "actor_id": actor["user_id"],
        "run_id": run_id,
        "repository_id": run["repository_id"],
        "project_id": run["project_id"],
        "state": "available",
        "items": [],
    }
    with prepared_credential_rejection(service.store):
        try:
            projected, retained = await _read(service, run, page)
            await service.store.admit_credentials([projected, retained])
            current = await service.completions._reader(run_id, reader, execute=True)
            await service.access(current, actor)
            if invocation_cut(current) != invocation_cut(run):
                raise AccessDenied("Checkpoint candidate scope changed during admission")
            confirmed, evidence = await _read(service, current, page)
            if confirmed != projected or evidence != retained:
                return {**page, "state": "evidence_unavailable", "items": []}
            service.store.reject_credentials([projected, retained])
            final = await service.completions._reader(run_id, reader, execute=True)
            await service.access(final, actor)
            if invocation_cut(final) != invocation_cut(run):
                raise AccessDenied("Checkpoint candidate scope changed before return")
            service.store.reject_credentials([projected, retained])
            return projected
        except (ValueError, KeyError):
            # Even an unavailable projection must pass the current caller/original actor cuts.
            current = await service.completions._reader(run_id, reader, execute=True)
            await service.access(current, actor)
            return {**page, "state": "evidence_unavailable", "items": []}


async def _read(service: AgentRestores, run: Data, page: Data) -> tuple[Data, list[Any]]:
    run_id = run["id"]
    retained: list[Any] = []
    items = []
    async with service.store._unit():
        await service.checkpoints._budget(run_id)
        keys = await service.journal.records.checkpoint_candidates(identifier(run_id))
        if len(keys) > 16 or len(set(keys)) != len(keys):
            raise ValueError("Checkpoint candidate identities exceed their bound")
        tickets = await service.journal.records.checkpoint_restore_tickets(identifier(run_id))
    if len(tickets) > 50 or len({t["completion_id"] for t in tickets}) != len(tickets):
        raise ValueError("Checkpoint tickets exceed their bound or conflict")
    for completion_id in keys:
        async with service.store._unit():
            intent = await service.completions._record(run_id, completion_id)
            checkpoint = await service.checkpoints._record(run_id, completion_id, CHECKPOINT)
            if checkpoint is None or checkpoint["payload"]["intent_digest"] != intent["digest"]:
                raise ValueError("Checkpoint and intent differ")
            if any(intent["payload"][k] != run.get(k) for k in ("project_id", "repository_id")):
                raise AccessDenied("Checkpoint belongs to another project or repository")
            if intent["task_id"] != "implement":
                continue
            consumed = await service.checkpoints._record(run_id, completion_id, CONSUME)
            observations = await service.journal.records.checkpoint_observations(identifier(run_id), completion_id)
            if len(observations) > 1:
                raise ValueError("Checkpoint observation lookup exceeds its bound")
            observation = (
                await service.completions._record(run_id, observations[0], OBSERVATION) if observations else None
            )
            response_row = await service.journal.records.bridge_record(identifier(run_id), completion_id, RESPONSE)
            response = await service.completions._record(run_id, completion_id, RESPONSE) if response_row else None
        service.checkpoints._validate_checkpoint(checkpoint, intent)
        retained.extend([intent, checkpoint, consumed, observation, response])
        invocation = (
            CheckpointPayloadV2.model_validate(checkpoint["payload"]).invocation
            if checkpoint["payload"]["version"] == 2
            else None
        )
        state = "legacy" if invocation is None else "held"
        observation_ref, result = None, None
        if response is not None:
            result = {
                "kind": "http_response",
                "record_digest": response["digest"],
                "observation_id": None,
                "terminal_digest": None,
            }
        if observation is not None:
            payload = observation["payload"]
            if (
                payload["request_id"] != completion_id
                or payload["intent_digest"] != intent["digest"]
                or observation["task_id"] != "implement"
            ):
                raise ValueError("Observation does not belong to original checkpoint")
            snapshot = validate_remote_snapshot(payload["snapshot"], intent["payload"]["descriptor"])
            observation_ref = {"id": observation["idempotency_key"], "state": snapshot["state"]}
            terminal = snapshot["terminal"]
            if (
                result is None
                and snapshot["state"] == "terminal"
                and terminal
                and terminal["status"] == "succeeded"
                and terminal.get("result_json")
            ):
                result = {
                    "kind": "bridge_observation",
                    "record_digest": observation["digest"],
                    "observation_id": observation["idempotency_key"],
                    "terminal_digest": terminal["digest"],
                }
        if invocation is not None:
            if consumed is not None:
                if (
                    consumed["payload"]["intent_digest"] != intent["digest"]
                    or consumed["payload"]["checkpoint_record_digest"] != checkpoint["digest"]
                    or consumed["task_id"] != "implement"
                ):
                    raise ValueError("Consume receipt bindings differ")
                state = "consumed"
            elif invocation.deadline_ms <= int(time.time() * 1000):
                state = "expired"
            else:
                try:
                    async with service.store._unit():
                        await service.validate_db(run, invocation, intent, active=False)
                    state = "ready" if result is not None else "awaiting_result"
                except ValueError:
                    state = "held"
            if result is not None:
                selection = RestoreInput.model_validate(
                    {
                        "request_id": "0" * 32,
                        "expected_checkpoint_digest": checkpoint["digest"],
                        "expected_intent_digest": intent["digest"],
                        "source": result,
                    }
                )
                bundle = await service.checkpoints.restore_bundle(run_id, completion_id, selection)
                retained.append(bundle[:3])
        ticket_ref = None
        selected = next((t for t in tickets if t["completion_id"] == completion_id), None)
        if selected is not None:
            ticket = await service.get(run_id, completion_id, selected["id"])
            if ticket["status"] != selected["status"]:
                raise ValueError("Checkpoint ticket changed during discovery")
            selected_input = RestoreInput.model_validate(ticket["request"]["input"])
            if selected_input.request_id != ticket["id"]:
                raise ValueError("Checkpoint ticket input has a different original request identity")
            ticket_bundle = await service.checkpoints.restore_bundle(run_id, completion_id, selected_input)
            retained.append(ticket_bundle[:3])
            ticket_ref = {"id": ticket["id"], "status": ticket["status"]}
            retained.append(ticket)
            state = "ticket_recorded"
        items.append(
            {
                "completion_id": completion_id,
                "checkpoint_digest": checkpoint["digest"],
                "intent_digest": intent["digest"],
                "task": "implement",
                "attempt": intent["payload"]["task_attempt"],
                "ordinal": invocation.ordinal if invocation else None,
                "deadline_ms": invocation.deadline_ms if invocation else None,
                "created_at": checkpoint["created_at"],
                "state": state,
                "result": result,
                "observation": observation_ref,
                "ticket": ticket_ref,
            }
        )
    return {**copy.deepcopy(page), "items": items, "state": "available" if items else "no_candidate"}, retained
