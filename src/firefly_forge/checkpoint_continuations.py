# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Append-only ownership handoffs for an exact unconsumed native checkpoint."""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import text

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2, InvocationBinding, RestoreInput
from firefly_forge.agent_restores import RestoreExecution
from firefly_forge.agentic_checkpoint_invocation import invocation_cut, runtime_contract, workspace_cut
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.checkpoint_continuation_models import CheckpointContinuationInputV1
from firefly_forge.checkpoint_continuation_proof import ADMITTED, HANDOFF, pending_checkpoint
from firefly_forge.checkpoint_mode_runtime import effective_revision, mode_execution_scope
from firefly_forge.invalid_read_continuity import _bytes
from firefly_forge.invalid_read_epochs import epoch_scope, epoch_snapshot
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, lease_window, policy_snapshot
from firefly_forge.invalid_read_recoveries import MUTABLE
from firefly_forge.invalid_read_tickets import protected_scope, verify_ticket
from firefly_forge.journal import Journal, digest
from firefly_forge.store import identifier, now

Data = dict[str, Any]
_FIELDS = {
    "version",
    "request_id",
    "action_id",
    "actor",
    "administrator",
    "request",
    "candidate",
    "failed_ticket",
    "events",
    "run",
    "task",
    "selection",
    "binding",
    "lease",
    "lease_policy",
    "policy_epoch",
    "digest",
}


def validate_witness(entry: Data, entries: list[Data]) -> None:
    p = entry["payload"]
    if (
        not Journal.verify_chain(entries)
        or entry not in entries
        or type(p) is not dict
        or set(p) != _FIELDS
        or type(p["version"]) is not int
        or p["version"] != 1
        or entry["kind"] != HANDOFF
        or entry["idempotency_key"] != "checkpoint-handoff:" + p["action_id"]
        or entry["run_id"] != p["run"]["id"]
        or entry["task_id"] != "implement"
        or digest({k: v for k, v in p.items() if k != "digest"}) != p["digest"]
    ):
        raise ValueError("Checkpoint handoff witness custody changed")
    body = CheckpointContinuationInputV1.model_validate(p["request"])
    c = p["candidate"]
    ticket = p["failed_ticket"]
    verify_ticket(ticket)
    if (
        body.candidate_digest != c["digest"]
        or digest({k: v for k, v in c.items() if k != "digest"}) != c["digest"]
        or c["admission"] != "READY"
        or c["holds"] != []
        or c["execution_authorized"] is not False
        or ticket["status"] != "needs_reconciliation"
        or c["run_id"] != p["run"]["id"]
        or c["request_id"] != ticket["id"]
        or type(c["attempt"]) is not int
        or c["attempt"] != p["task"]["attempt"]
        or p["request_id"] != ticket["id"]
        or c["original_admission_seal"] != ticket["admission_seal"]
        or c["run_digest"] != digest(p["run"])
        or c["task_digest"] != digest(p["task"])
        or c["ticket_digest"] != digest(ticket)
        or c["events_digest"] != digest({"events": p["events"]})
        or c["journal_digest"] != digest({"entries": entries[: entry["sequence"] - 1]})
        or c["policy_digest"] != p["lease_policy"]["digest"]
        or p["actor"] != ticket["request"]["actor"]
    ):
        raise ValueError("Checkpoint handoff candidate differs from its original custody")
    prefix = entries[: entry["sequence"] - 1]
    if p["action_id"] == ticket["id"] or any(
        e["payload"].get("recovery_id") == p["action_id"]
        or (e["kind"] == HANDOFF and e["payload"].get("action_id") == p["action_id"])
        for e in prefix
    ):
        raise ValueError("Checkpoint witness reused a spent native action")
    policy = ContinuityLeasePolicyV1.model_validate(p["lease_policy"]["settings"])
    if not policy.allow_checkpoint_continuation:
        raise ValueError("Checkpoint continuation was not explicitly enabled")
    expected = lease_window(
        policy,
        frozen_timeout_seconds=c["frozen_timeout_seconds"],
        valid_for_seconds=body.valid_for_seconds,
        now_ms=p["lease"]["issued_at_ms"],
        original_deadline_ms=c["original_deadline_ms"],
        active_deadline_ms=p["run"].get("active_execution_deadline_ms"),
    )
    cp = next(
        e for e in entries[: entry["sequence"] - 1] if e["digest"] == p["selection"]["expected_checkpoint_digest"]
    )
    binding = {
        **cp["payload"]["invocation"],
        "deadline_ms": expected["expires_at_ms"],
        "contract": c["current_runtime_contract"],
    }
    expected_selection = RestoreInput.model_validate(
        dict(
            request_id=ticket["id"],
            expected_checkpoint_digest=cp["digest"],
            expected_intent_digest=cp["payload"]["intent_digest"],
            source={"kind": "http_response", "record_digest": c["response_record_digest"]},
        )
    ).model_dump(mode="json")
    if _bytes(p["selection"]) != _bytes(expected_selection):
        raise ValueError("Checkpoint handoff changed its exact retained completion selection")
    if _bytes(p["binding"]) != _bytes(binding) or _bytes(p["lease"]) != _bytes(expected):
        raise ValueError("Checkpoint handoff changed more than finite ownership and current runtime")


def _prior(ticket: Data, entries: list[Data], events: list[Data]) -> None:
    from firefly_forge.execution_requests import ExecutionRequests

    while True:
        if (
            not events
            or not ExecutionRequests.verify_chain(events)
            or any(e.get("request_id") != ticket["id"] or type(e.get("sequence")) is not int for e in events)
        ):
            raise ValueError("Checkpoint predecessor request events changed")
        if not any(e["kind"] == ADMITTED for e in events):
            break
        record = checkpoint_record(ticket, entries, events, _prefix_checked=True)
        if record is None:
            raise ValueError("Missing predecessor checkpoint custody")
        p = record["payload"]
        ticket, entries, events = p["failed_ticket"], entries[: record["sequence"] - 1], p["events"]
    if any(e["kind"] == "request_recovered_after_readonly_observation" for e in events):
        from firefly_forge.readonly_observation_recoveries import validate_readonly_observation_record

        if validate_readonly_observation_record(ticket, entries, events) is None:
            raise ValueError("Missing predecessor observation custody")
    elif any(e["kind"] == "request_recovered_after_rejection" for e in events):
        from firefly_forge.provider_rejection_recoveries import validate_rejection_recovery_record

        if validate_rejection_recovery_record(ticket, entries, events) is None:
            raise ValueError("Missing predecessor rejection custody")
    elif any(e["kind"] == "request_recovered_before_claim" for e in events):
        from firefly_forge.invalid_read_recoveries import validate_recovery_record

        if validate_recovery_record(ticket, entries, events) is None:
            raise ValueError("Missing predecessor recovery custody")
    elif (
        [e["kind"] for e in events] != ["request_admitted", "worker_claim", "worker_needs_reconciliation"]
        or events[0]["evidence"]
        != {"request_digest": ticket["request_digest"], "admission_seal": ticket["admission_seal"]}
        or events[1]["evidence"]
        != {
            "generation": ticket["generation"],
            "owner_pid": ticket["owner_pid"],
            "request_digest": ticket["request_digest"],
        }
        or events[-1]["evidence"] != ticket["result"]
    ):
        raise ValueError("The original worker has no complete terminal custody")


def checkpoint_record(
    ticket: Data, entries: list[Data], events: list[Data], *, _prefix_checked: bool = False
) -> Data | None:
    actions = [e for e in entries if e["kind"] == HANDOFF and e["payload"].get("request_id") == ticket["id"]]
    if not actions:
        return None
    record = actions[-1]
    p = record["payload"]
    validate_witness(record, entries)
    before = p["failed_ticket"]
    prefix = entries[: record["sequence"] - 1]
    if not _prefix_checked:
        _prior(before, prefix, p["events"])
    if (
        _bytes({k: v for k, v in before.items() if k not in MUTABLE})
        != _bytes({k: v for k, v in ticket.items() if k not in MUTABLE})
        or events[: len(p["events"])] != p["events"]
    ):
        raise ValueError("Checkpoint handoff changed immutable ticket or predecessor events")
    tail = events[len(p["events"]) :]
    if (
        not tail
        or tail[0]["kind"] != ADMITTED
        or tail[0]["evidence"]
        != {"action_id": p["action_id"], "witness_digest": record["digest"], "admission_seal": ticket["admission_seal"]}
    ):
        raise ValueError("Checkpoint handoff lacks its exact native admission event")
    claim = (
        len(tail) >= 2
        and tail[1]["kind"] == "worker_claim"
        and tail[1]["evidence"]
        == {
            "generation": ticket["generation"],
            "owner_pid": ticket["owner_pid"],
            "request_digest": ticket["request_digest"],
        }
    )
    if claim and (
        type(ticket["generation"]) is not str
        or identifier(ticket["generation"]).hex != ticket["generation"]
        or ticket["generation"] == before["generation"]
        or type(ticket["owner_pid"]) is not int
        or ticket["owner_pid"] <= 0
    ):
        raise ValueError("Checkpoint handoff requires fresh actual worker ownership")
    queued = (
        len(tail) == 1
        and ticket["status"] == "queued"
        and all(ticket[k] == before[k] for k in ("generation", "owner_pid", "result"))
    )
    executing = len(tail) == 2 and claim and ticket["status"] == "executing" and ticket["result"] is None
    terminal = (
        (
            (claim and len(tail) == 3 and ticket["status"] in {"completed", "needs_reconciliation"})
            or (
                not claim
                and len(tail) == 2
                and ticket["status"] == "failed"
                and all(ticket[k] == before[k] for k in ("generation", "owner_pid"))
            )
        )
        and tail[-1]["kind"] == "worker_" + ticket["status"]
        and tail[-1]["evidence"] == ticket["result"]
    )
    if not (queued or executing or terminal):
        raise ValueError("Checkpoint handoff lifecycle differs from complete native events")
    return record


class CheckpointContinuationExecution(RestoreExecution):
    def __init__(self, service: Any, ticket: Data, witness: Data, bundle: Any) -> None:
        self.service: Any = service
        self.ticket = copy.deepcopy(ticket)
        self.witness = witness
        self.intent, self.record, self.result_record, self.body = bundle
        from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint

        p = CheckpointPayloadV2.model_validate(self.record["payload"])
        self.binding = InvocationBinding.model_validate(witness["payload"]["binding"])
        self.checkpoint = BoundaryCheckpoint.decode(p.snapshot.encode(), p.checkpoint_digest)
        self.request = RestoreInput.model_validate(witness["payload"]["selection"])
        self.generation = ticket["generation"]
        self.run_id, self.request_id = ticket["run_id"], p.request_id
        self.reference = {
            "completion_request_id": self.request_id,
            "intent_digest": self.intent["digest"],
            "principal_id": self.intent["payload"]["descriptor"]["principal_id"],
        }

    async def guard(self) -> None:
        await self.service.guard(self.ticket, self.witness)


class CheckpointContinuations:
    def __init__(self, continuities: Any) -> None:
        self.continuities = continuities
        self.base = continuities.recoveries
        self.store, self.journal, self.engine, self.requests = (
            continuities.store,
            continuities.journal,
            continuities.engine,
            continuities.requests,
        )
        self.checkpoints = continuities.restores.checkpoints
        self.completions = continuities.restores.completions
        self.workspace = continuities.restores.workspace
        self.operator = continuities.restores.operator
        self.access = continuities.restores.access

    async def _events(self, request_id: str) -> list[Data]:
        from firefly_forge.invalid_read_recovery_events import read_recovery_events
        from firefly_forge.run_evidence import LIMITS

        return await read_recovery_events(self.requests, request_id, maximum=LIMITS["journal"])

    async def has(self, ticket: Data) -> bool:
        _, _, entries = await self.base._native(ticket["run_id"])
        return any(e["kind"] == HANDOFF and e["payload"].get("request_id") == ticket["id"] for e in entries)

    async def _prepare(self, run_id: str, request_id: str) -> Data:
        _outside_unit()
        ticket = await self.base._ticket(run_id, request_id)
        actor = await self.base._operator(run_id, ticket)
        administrator = await self.base._administrator(actor)
        run, task, entries = await self.base._native(run_id)
        events = await self._events(request_id)
        _prior(ticket, entries, events)
        if (
            ticket["status"] != "needs_reconciliation"
            or run["status"] != "needs_reconciliation"
            or task["status"] != "needs_reconciliation"
            or self.engine.stopping
            or (run_id in self.engine.active and not self.engine.active[run_id].done())
        ):
            raise ValueError("Checkpoint continuation requires its idle held original task")
        selected_runtime = (ticket.get("policy_epoch") or {}).get(
            "legacy_runtime_contract", ticket["binding"]["contract"]
        )
        epoch = epoch_snapshot(self.store, run, entries, legacy_runtime_contract=selected_runtime)
        with epoch_scope(epoch):
            settlement = self.base._admission(ticket, run, task, entries)
            await self.base._custody(ticket, run, task, entries, settlement)
            frontier = pending_checkpoint(run, task, entries)
        cp = CheckpointPayloadV2.model_validate(frontier["checkpoint"]["payload"])
        selection = RestoreInput.model_validate(
            dict(
                request_id=request_id,
                expected_checkpoint_digest=frontier["checkpoint"]["digest"],
                expected_intent_digest=frontier["intent"]["digest"],
                source={"kind": "http_response", "record_digest": frontier["response"]["digest"]},
            )
        )
        bundle = await self.checkpoints.restore_bundle(run_id, cp.request_id, selection)
        policy = policy_snapshot(self.store.configuration)
        configured = ContinuityLeasePolicyV1.model_validate(policy["settings"])
        from firefly_forge.invalid_read_continuities import current_profile_seal

        holds = []
        if not policy["revision"] or not configured.enabled:
            holds.append("lease_policy_disabled")
        if not configured.allow_checkpoint_continuation:
            holds.append("checkpoint_continuation_disabled")
        try:
            profile = current_profile_seal()
        except ValueError:
            profile = None
            holds.append("current_profile_unqualified")
        if "active_execution_budget" in run:
            raise ValueError("Unsupported active execution budget protocol")
        deadline = run.get("active_execution_deadline_ms")
        if deadline is not None and (type(deadline) is not int or deadline <= int(time.time() * 1000)):
            holds.append("active_execution_budget_expired")
        settings = self.store.configuration.load_snapshot(effective_revision(run))
        if self.store.configuration.resolve("bridge") != settings:
            raise ValueError("Frozen provider configuration changed")
        timeout = int(settings.get("FORGE_PROVIDER_TIMEOUT", "1800"))
        if not 1 <= timeout <= 7200:
            raise ValueError("Frozen provider timeout is unsupported")
        guard = await self.continuities.restores.source_read_guard(
            run, await self.continuities._actor(run_id), task_attempt=task["attempt"]
        )
        if (
            await workspace_cut(self.store, self.engine.workspaces / run_id, assert_current=guard)
            != cp.invocation.workspace
        ):
            raise ValueError("Current source differs from the selected checkpoint")
        from firefly_forge.bridge_completion_models import CompletionDescriptor
        from firefly_forge.pending_checkpoint_reconstruction import reconstruct_pending_request

        with epoch_scope(epoch):
            async with mode_execution_scope(self.journal, run_id):
                await reconstruct_pending_request(
                    cp, CompletionDescriptor.model_validate(frontier["intent"]["payload"]["descriptor"]), run
                )
        value = dict(
            version=1,
            kind="checkpoint_continuation_candidate",
            run_id=run_id,
            request_id=request_id,
            admission="HOLD" if holds else "READY",
            holds=holds,
            execution_authorized=False,
            original_admission_seal=ticket["admission_seal"],
            attempt=task["attempt"],
            ticket_digest=digest(ticket),
            events_digest=digest({"events": events}),
            journal_digest=digest({"entries": entries}),
            run_digest=digest(run),
            task_digest=digest(task),
            checkpoint_record_digest=frontier["checkpoint"]["digest"],
            response_record_digest=frontier["response"]["digest"],
            source_digest=digest(cp.invocation.workspace),
            current_runtime_contract=runtime_contract(),
            current_profile=profile,
            policy_digest=policy["digest"],
            frozen_timeout_seconds=timeout,
            maximum_window_seconds=min(configured.max_window_seconds, timeout),
            original_deadline_ms=cp.invocation.deadline_ms,
            usage=frontier["usage"],
            limits=frontier["limits"],
        )
        value["digest"] = digest(value)
        await guard()
        if (
            await self.base._ticket(run_id, request_id) != ticket
            or await self._events(request_id) != events
            or policy_snapshot(self.store.configuration) != policy
        ):
            raise ValueError("Checkpoint custody changed during inspection")
        await self.store.admit_credentials([value, bundle[:3]])
        return dict(
            candidate=value,
            ticket=ticket,
            actor=actor,
            administrator=administrator,
            run=run,
            task=task,
            entries=entries,
            events=events,
            selection=selection.model_dump(mode="json"),
            policy=policy,
            epoch=epoch,
        )

    async def candidate(self, run_id: str, request_id: str) -> Data:
        return dict((await self._prepare(run_id, request_id))["candidate"])

    async def request(self, run_id: str, request_id: str, action_id: str, body: CheckpointContinuationInputV1) -> Data:
        _outside_unit()
        if any(identifier(v).hex != v for v in (run_id, request_id, action_id)):
            raise ValueError("Use exact lowercase native identities")
        body = CheckpointContinuationInputV1.model_validate(body.model_dump())

        async def existing() -> Data | None:
            ticket = await self.base._ticket(run_id, request_id)
            await self.base._operator(run_id, ticket)
            _, _, entries = await self.base._native(run_id)
            if action_id == request_id or any(
                e["kind"] != HANDOFF and e["payload"].get("recovery_id") == action_id for e in entries
            ):
                raise ValueError("A prior native action identity is spent")
            matches = [e for e in entries if e["kind"] == HANDOFF and e["payload"].get("action_id") == action_id]
            if not matches:
                return None
            if (
                len(matches) != 1
                or matches[0]["payload"]["request_id"] != request_id
                or _bytes(matches[0]["payload"]["request"]) != _bytes(body.model_dump(mode="json"))
            ):
                raise ValueError("Checkpoint action identity belongs to another input")
            status = await self.status(run_id, request_id)
            if status["action_id"] != action_id:
                raise ValueError("This checkpoint action is spent; inspect current status")
            return status

        old = await existing()
        if old is not None:
            return old
        try:
            return await self._admit(run_id, request_id, action_id, body)
        except ValueError:
            retained = await existing()
            if retained is not None:
                return retained
            raise

    async def _admit(self, run_id: str, request_id: str, action_id: str, body: CheckpointContinuationInputV1) -> Data:
        prepared = await self._prepare(run_id, request_id)
        c = prepared["candidate"]
        if c["digest"] != body.candidate_digest or c["admission"] != "READY":
            raise ValueError("The reviewed checkpoint continuation changed or is held")
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:execution-request-admission:" + self.store.schema},
            )
            await self.journal._run(run_id)
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.data != prepared["ticket"] or row.status != "needs_reconciliation":
                raise ValueError("Checkpoint request changed before admission")
            await self.base._operator(run_id, prepared["ticket"])
            if (
                await self.base._native(run_id) != (prepared["run"], prepared["task"], prepared["entries"])
                or await self._events(request_id) != prepared["events"]
                or policy_snapshot(self.store.configuration) != prepared["policy"]
            ):
                raise ValueError("Checkpoint frontier changed before admission")
            lease = lease_window(
                ContinuityLeasePolicyV1.model_validate(prepared["policy"]["settings"]),
                frozen_timeout_seconds=c["frozen_timeout_seconds"],
                valid_for_seconds=body.valid_for_seconds,
                now_ms=int(time.time() * 1000),
                original_deadline_ms=c["original_deadline_ms"],
                active_deadline_ms=prepared["run"].get("active_execution_deadline_ms"),
            )
            cp = next(e for e in prepared["entries"] if e["digest"] == c["checkpoint_record_digest"])
            payload = dict(
                version=1,
                request_id=request_id,
                action_id=action_id,
                actor=prepared["actor"],
                administrator=prepared["administrator"],
                request=body.model_dump(mode="json"),
                candidate=c,
                failed_ticket=prepared["ticket"],
                events=prepared["events"],
                run=prepared["run"],
                task=prepared["task"],
                selection=prepared["selection"],
                binding={
                    **cp["payload"]["invocation"],
                    "deadline_ms": lease["expires_at_ms"],
                    "contract": c["current_runtime_contract"],
                },
                lease=lease,
                lease_policy=prepared["policy"],
                policy_epoch=prepared["epoch"],
            )
            payload["digest"] = digest(payload)
            await self.store.admit_credentials(payload)
            record = await self.journal._append(
                run_id, "implement", "checkpoint-handoff:" + action_id, HANDOFF, payload
            )
            validate_witness(record, [*prepared["entries"], record])
            row.status = "queued"
            row.data = {**row.data, "status": "queued", "updated_at": now()}
            await self.requests.records.save(row)
            await self.requests._event(
                row,
                ADMITTED,
                {
                    "action_id": action_id,
                    "witness_digest": record["digest"],
                    "admission_seal": row.data["admission_seal"],
                },
            )
            await self.base._operator(run_id, prepared["ticket"])
            if policy_snapshot(self.store.configuration) != prepared["policy"]:
                raise ValueError("Checkpoint policy changed before commit")
            self.store.reject_credentials(payload)
        return await self.status(run_id, request_id)

    async def status(self, run_id: str, request_id: str) -> Data:
        ticket = await self.base._ticket(run_id, request_id)
        await self.base._operator(run_id, ticket)
        _, _, entries = await self.base._native(run_id)
        record = checkpoint_record(ticket, entries, await self._events(request_id))
        return dict(
            version=1,
            kind="checkpoint_continuation_status",
            run_id=run_id,
            request_id=request_id,
            status=ticket["status"] if record else "not_recovered",
            action_id=record["payload"]["action_id"] if record else None,
            witness_digest=record["digest"] if record else None,
            original_admission_seal=ticket["admission_seal"],
            lease=record["payload"]["lease"] if record else None,
        )

    async def _authority(self, ticket: Data, record: Data, *, executing: bool) -> Data:
        p = record["payload"]
        run, task, entries = await self.base._native(ticket["run_id"])
        await self.access(run, ticket["request"]["actor"])
        await self.base._administrator(p["administrator"])
        if (
            int(time.time() * 1000) >= p["lease"]["expires_at_ms"]
            or policy_snapshot(self.store.configuration) != p["lease_policy"]
            or protected_scope(run) != ticket["protected_scope_digest"]
            or invocation_cut(run) != ticket["binding"]["run_cut"]
            or run.get("active_execution_deadline_ms") != p["lease"]["active_deadline_ms"]
            or digest(self.store.configuration.load_snapshot(effective_revision(run))) != ticket["settings_digest"]
            or digest(self.store.configuration.resolve("bridge")) != ticket["settings_digest"]
            or task["attempt"] != ticket["binding"]["round"]
            or task["idempotency_key"] != ticket["task_key"]
        ):
            raise ValueError("Checkpoint finite lease or original authority changed")
        if current_unit_of_work() is None:
            from firefly_forge.invalid_read_continuities import current_profile_seal

            if (
                runtime_contract() != p["binding"]["contract"]
                or current_profile_seal() != p["candidate"]["current_profile"]
            ):
                raise ValueError("Checkpoint qualified runtime changed")
        with epoch_scope(p["policy_epoch"]):
            settlement = self.base._admission(ticket, run, task, entries)
            await self.base._custody(ticket, run, task, entries, settlement)
        if executing:
            if run["status"] != "developing" or task["status"] != "running":
                raise ValueError("Checkpoint worker no longer owns its task")
        elif (
            run != p["run"]
            or task != p["task"]
            or entries[: record["sequence"] - 1] != [e for e in entries if e["sequence"] < record["sequence"]]
            or len(entries) != record["sequence"]
        ):
            raise ValueError("Later activity prevents checkpoint claim")
        return dict(run)

    async def guard(self, ticket: Data, record: Data) -> None:
        await self.store.assert_ownership()
        current = await self.base._ticket(ticket["run_id"], ticket["id"])
        if current != ticket or current["status"] != "executing" or current["owner_pid"] != self.store._ownership_pid:
            raise ValueError("Checkpoint worker ownership changed")
        _, _, entries = await self.base._native(ticket["run_id"])
        if checkpoint_record(current, entries, await self._events(ticket["id"])) != record:
            raise ValueError("Checkpoint witness changed")
        await self._authority(ticket, record, executing=True)

    async def acceptance_current(self, restoration: CheckpointContinuationExecution) -> None:
        await self.store.assert_ownership()
        ticket = await self.base._ticket(restoration.run_id, restoration.ticket["id"])
        run, task, entries = await self.base._native(restoration.run_id)
        record = checkpoint_record(ticket, entries, await self._events(ticket["id"]))
        if (
            ticket != restoration.ticket
            or record != restoration.witness
            or ticket["status"] != "executing"
            or ticket["owner_pid"] != self.store._ownership_pid
        ):
            raise ValueError("Checkpoint terminal custody lost its original ownership")
        p = restoration.witness["payload"]
        from firefly_forge.unknown_command_proof import continuation_scope

        if (
            int(time.time() * 1000) >= p["lease"]["expires_at_ms"]
            or policy_snapshot(self.store.configuration) != p["lease_policy"]
            or continuation_scope(run) != continuation_scope(p["run"])
            or invocation_cut(run) != p["binding"]["run_cut"]
            or any(
                run.get(k) != p["run"].get(k) for k in ("attempt", "repair_attempts", "active_execution_deadline_ms")
            )
            or task["attempt"] != p["task"]["attempt"]
            or task["idempotency_key"] != ticket["task_key"]
            or (run.get("cancel_request") or {}).get("status") == "requested"
        ):
            raise ValueError("Checkpoint terminal finite lease or execution grants changed")
        await self.access(run, ticket["request"]["actor"])
        await self.base._administrator(p["administrator"])

    async def execute(self, ticket: Data) -> None:
        self.requests.runtime.require_execution()
        await self.store.assert_ownership()
        _, _, entries = await self.base._native(ticket["run_id"])
        record = checkpoint_record(ticket, entries, await self._events(ticket["id"]))
        if record is None:
            raise ValueError("No checkpoint handoff witness")
        p = record["payload"]
        with epoch_scope(p["policy_epoch"]):
            async with mode_execution_scope(self.journal, ticket["run_id"]):
                run = await self._authority(ticket, record, executing=False)
                if (
                    await workspace_cut(self.store, self.engine.workspaces / ticket["run_id"])
                    != p["binding"]["workspace"]
                ):
                    raise ValueError("Checkpoint source changed before claim")
                async with self.store._unit():
                    locked = await self.journal._run(ticket["run_id"])
                    row = await self.requests.records.find_by_id(
                        identifier(ticket["id"]), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if row is None or row.status != "queued":
                        return
                    if row.data != ticket:
                        raise ValueError("Queued checkpoint request changed")
                    await self._authority(ticket, record, executing=False)
                    task = await self.journal._effect_task(ticket["run_id"], ticket["task_key"])
                    task.status = "running"
                    await self.journal.tasks.save(task)
                    locked.status = "developing"
                    locked.data = {**locked.data, "status": "developing"}
                    await self.store.run_records.save(locked)
                    row.status = "executing"
                    row.data = {
                        **row.data,
                        "status": "executing",
                        "generation": uuid.uuid4().hex,
                        "owner_pid": self.store._ownership_pid,
                        "result": None,
                        "updated_at": now(),
                    }
                    await self.requests.records.save(row)
                    await self.requests._event(
                        row,
                        "worker_claim",
                        {
                            "generation": row.data["generation"],
                            "owner_pid": row.data["owner_pid"],
                            "request_digest": row.data["request_digest"],
                        },
                    )
                    ticket = copy.deepcopy(row.data)
                bundle = await self.checkpoints.restore_bundle(
                    ticket["run_id"],
                    next(
                        e["payload"]["request_id"]
                        for e in entries
                        if e["digest"] == p["selection"]["expected_checkpoint_digest"]
                    ),
                    RestoreInput.model_validate(p["selection"]),
                )
                execution = CheckpointContinuationExecution(self, ticket, record, bundle)
                await execution.guard_source()
                await self.engine.continue_agent_checkpoint(execution)
                run = await self.store.get_run(ticket["run_id"])
                if run.get("bridge_completion_hold") or run["status"] not in {"paused", "succeeded", "validating"}:
                    raise ValueError("Checkpoint continuation did not reach a known native boundary")
                await self.continuities._finish(
                    ticket["id"], "completed", {"run_status": run["status"], "same_task_attempt": p["binding"]["round"]}
                )
