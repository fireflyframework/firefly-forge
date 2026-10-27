# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit same-request recovery after a proven failure before any worker claim."""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any, cast

from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import text

from firefly_forge.agent_checkpoint_models import InvocationBinding
from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
from firefly_forge.agentic_checkpoint_invocation import invocation_cut, runtime_contract, workspace_cut
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.checkpoint_mode_runtime import effective_revision
from firefly_forge.invalid_read_continuity import native_tool_custody
from firefly_forge.invalid_read_epochs import epoch_scope, epoch_snapshot
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, lease_window, policy_snapshot
from firefly_forge.invalid_read_recovery_models import (
    InvalidReadRecoveryCandidateV1,
    InvalidReadRecoveryInputV1,
    InvalidReadRecoveryStatusV1,
    InvalidReadRecoveryWitnessV1,
)
from firefly_forge.invalid_read_settlements import CLAIM, KIND, SETTLEMENT, settled_requests
from firefly_forge.invalid_read_tickets import InvalidReadExecution, protected_scope, verify_ticket
from firefly_forge.journal import Journal, digest, valid_recovery_annotation
from firefly_forge.known_worker_completions import _entries, _task
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.source_edit_continuations import _assert_source_administrator
from firefly_forge.store import identifier, now

Data = dict[str, Any]
RECOVERY = "invalid_read_recovery"
RECOVERED = "request_recovered_before_claim"
MUTABLE = ("status", "generation", "owner_pid", "result", "updated_at")


class RecoveredInvalidReadExecution(InvalidReadExecution):
    def __init__(self, service: InvalidReadRecoveries, ticket: Data, record: Data) -> None:
        self.service = service
        self.run_id = ticket["run_id"]
        self.original_id, self.recovery_digest = ticket["id"], record["digest"]
        self.owner_generation = ticket["generation"]
        self.binding = InvocationBinding.model_validate(record["payload"]["binding"])
        self.checkpoint = BoundaryCheckpoint.decode(ticket["checkpoint"].encode(), ticket["checkpoint_digest"])
        self.intent = {"payload": {"task_key": ticket["task_key"], "task_attempt": self.binding.round}}

    async def guard(self) -> None:
        await self.service.guard(self.original_id, self.recovery_digest, self.owner_generation)


class InvalidReadRecoveries:
    def __init__(self, continuities: Any) -> None:
        self.continuities = continuities
        self.store, self.journal, self.engine, self.requests = (
            continuities.store,
            continuities.journal,
            continuities.engine,
            continuities.requests,
        )

    async def _ticket(self, run_id: str, request_id: str) -> Data:
        async with self.store._unit():
            row = await self.requests.records.find_by_id(identifier(request_id))
            if row is None or row.id.hex != request_id or row.run_id.hex != run_id or row.kind != KIND:
                raise KeyError("Continuity request not found")
            ticket: Data = copy.deepcopy(row.data)
            if row.status != ticket.get("status") or ticket.get("id") != request_id or ticket.get("run_id") != run_id:
                raise ValueError("Continuity request native columns changed")
        verify_ticket(ticket)
        if digest(ticket["request"]) != ticket["request_digest"]:
            raise ValueError("Continuity request custody changed")
        return ticket

    async def _events(self, request_id: str) -> list[Data]:
        from firefly_forge.invalid_read_recovery_events import read_recovery_events

        return await read_recovery_events(self.requests, request_id)

    async def _administrator(self, actor: Data) -> Data:
        principal = await self.continuities.restores.operator(actor)
        current = CURRENT_PRINCIPAL.get()
        if current is not None and (
            current.id != principal.id or current.issuer != principal.issuer or current.subject != principal.subject
        ):
            raise AccessDenied("Recovery administrator identity changed")
        administrator = {**actor, "identity_digest": digest({"issuer": principal.issuer, "subject": principal.subject})}
        await _assert_source_administrator(self.store, administrator)
        return administrator

    async def _operator(self, run_id: str, ticket: Data) -> Data:
        actor = await self.continuities._actor(run_id)
        selected = ProjectAccess.actor_snapshot(actor)
        if selected != ticket["request"]["actor"] or not ProjectAccess.is_admin(actor):
            raise AccessDenied("Recovery requires the original current administrator")
        await self._administrator(selected)
        return selected

    async def _native(self, run_id: str) -> tuple[Data, Data, list[Data]]:
        async with self.store._unit():
            run = await self.store.get_run(run_id)
            task = _task(await self.journal._task(run_id, "implement"))
            entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
        return run, task, entries

    def _admission(self, ticket: Data, run: Data, task: Data, entries: list[Data]) -> Data:
        settlements = [e for e in entries if e["kind"] == SETTLEMENT and e["digest"] == ticket["migration_digest"]]
        claims = [e for e in entries if e["kind"] == CLAIM and e["digest"] == ticket["claim_digest"]]
        if len(settlements) != 1 or len(claims) != 1:
            raise ValueError("Original continuity settlement or reservation is unavailable")
        settlement = settlements[0]
        settled_requests(settlement, entries)
        payload = settlement["payload"]
        expected = {
            "request_id": ticket["id"],
            "settlement_digest": ticket["migration_digest"],
            "claim_digest": ticket["claim_digest"],
            "lease_generation": ticket["lease_generation"],
        }
        if (
            run.get("invalid_read_continuity") != expected
            or claims[0]["idempotency_key"] != "invalid-claim:" + ticket["id"]
            or task.get("invalid_read_claim_digest") != ticket["claim_digest"]
            or task["idempotency_key"] != ticket["task_key"]
            or task["attempt"] != ticket["binding"]["round"]
            or run.get("cancel_request", {}).get("status") == "requested"
            or invocation_cut(run) != ticket["binding"]["run_cut"]
            or protected_scope(run) != ticket["protected_scope_digest"]
            or any(
                payload[key] != ticket[key]
                for key in (
                    "binding",
                    "checkpoint_digest",
                    "lease",
                    "lease_policy",
                    "current_profile",
                    "policy_epoch",
                    "settings_digest",
                    "protected_scope_digest",
                    "command_custody_digest",
                )
            )
        ):
            raise ValueError("Original continuity scope or admission facts changed")
        return settlement

    @staticmethod
    def _original_events(ticket: Data, events: list[Data]) -> None:
        if (
            len(events) != 2
            or [e["kind"] for e in events] != ["request_admitted", "worker_failed"]
            or ticket["status"] != "failed"
            or ticket["generation"] is not None
            or ticket["owner_pid"] is not None
            or events[0]["evidence"]
            != {"request_digest": ticket["request_digest"], "admission_seal": ticket["admission_seal"]}
            or events[1]["evidence"] != ticket["result"]
        ):
            raise ValueError("Only an exactly proven never-claimed failed request can recover")

    async def _custody(self, ticket: Data, run: Data, task: Data, entries: list[Data], settlement: Data) -> None:
        if await self.continuities._command_cut(run, task, entries) != ticket["command_custody_digest"]:
            raise ValueError("Original command custody changed")
        if settlement["payload"]["proof"]["version"] == 2:
            before = settlement["sequence"]
            rows = await self.continuities._native_observation_tools(run["id"], entries, before_sequence=before)
            prefix = [e for e in entries if e["sequence"] < before]
            if (
                native_tool_custody(run["id"], prefix, rows)
                != settlement["payload"]["proof"]["native_tool_custody_digest"]
            ):
                raise ValueError("Original native tool custody changed")
        if self.engine.tool_broker is not None and await self.engine.tool_broker.unresolved(run["id"]):
            raise ValueError("Unknown native tools prevent recovery")

    @staticmethod
    def _matches(ticket: Data, entries: list[Data]) -> list[Data]:
        return [
            e
            for e in entries
            if e["kind"] == RECOVERY
            and (
                e["idempotency_key"] == "invalid-recover:" + ticket["id"]
                or e["payload"].get("request_id") == ticket["id"]
            )
        ]

    @staticmethod
    def _idle_head(ticket: Data, entries: list[Data], allowed: Data | None = None) -> str:
        claim = next((e for e in entries if e["kind"] == CLAIM and e["digest"] == ticket["claim_digest"]), None)
        if claim is None or not Journal.verify_chain(entries):
            raise ValueError("Recovery has no exact original reservation custody")
        tail = [
            e
            for e in entries
            if e["sequence"] > claim["sequence"] and (allowed is None or e["digest"] != allowed["digest"])
        ]
        if len(tail) > 1:
            raise ValueError("Later native activity prevents never-claimed recovery")
        reference = {k: claim[k] for k in ("kind", "idempotency_key", "digest")}
        for annotation in tail:
            payload = annotation["payload"]
            if (
                annotation["kind"] != "recovery"
                or not isinstance(payload, dict)
                or type(payload.get("version")) is not int
                or payload.get("version") != 1
                or payload.get("generation") != reference
                or not valid_recovery_annotation(annotation, entries, "implement", ticket["task_key"])
            ):
                raise ValueError("Only the exact native reservation recovery annotation may follow admission")
        return cast(str, entries[-1]["digest"])

    @staticmethod
    def _record(ticket: Data, entries: list[Data], events: list[Data]) -> Data | None:
        matches = InvalidReadRecoveries._matches(ticket, entries)
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("Continuity recovery custody is ambiguous")
        record = matches[0]
        p = InvalidReadRecoveryWitnessV1.model_validate(record["payload"]).model_dump()
        failed = {**ticket, **p["failed_state"]}
        settlement = next((e for e in entries if e["digest"] == ticket["migration_digest"]), None)
        if settlement is None:
            raise ValueError("Original migration is unavailable")
        claims = [e for e in entries if e["kind"] == CLAIM and e["digest"] == ticket["claim_digest"]]
        if (
            settlement["kind"] != SETTLEMENT
            or len(claims) != 1
            or claims[0]["idempotency_key"] != "invalid-claim:" + ticket["id"]
        ):
            raise ValueError("Original admission has no exact native settlement and reservation")
        settled_requests(settlement, entries)
        original = settlement["payload"]
        if any(
            original[key] != ticket[key]
            for key in (
                "binding",
                "checkpoint_digest",
                "lease",
                "lease_policy",
                "current_profile",
                "policy_epoch",
                "settings_digest",
                "protected_scope_digest",
                "command_custody_digest",
            )
        ):
            raise ValueError("Recovery original settlement differs from the sealed ticket")
        prefix = [e for e in entries if e["sequence"] < record["sequence"]]
        reviewed_head = InvalidReadRecoveries._idle_head(ticket, prefix)
        policy = p["lease_policy"]
        configured = ContinuityLeasePolicyV1.model_validate(policy["settings"])
        expected_lease = lease_window(
            configured,
            frozen_timeout_seconds=original["frozen_timeout_seconds"],
            valid_for_seconds=p["request"]["valid_for_seconds"],
            now_ms=p["lease"]["issued_at_ms"],
            original_deadline_ms=original["proof"]["deadline_ms"],
            active_deadline_ms=p["lease"]["active_deadline_ms"],
        )
        expected_binding = {
            **ticket["binding"],
            "invocation_id": p["binding"]["invocation_id"],
            "deadline_ms": p["lease"]["expires_at_ms"],
            "contract": p["binding"]["contract"],
        }
        if (
            record["run_id"] != ticket["run_id"]
            or record["task_id"] != "implement"
            or record["idempotency_key"] != "invalid-recover:" + ticket["id"]
            or p["run_id"] != ticket["run_id"]
            or p["request_id"] != ticket["id"]
            or digest({k: v for k, v in p.items() if k != "digest"}) != p["digest"]
            or set(p["failed_state"]) != set(MUTABLE)
            or digest(failed) != p["original_ticket_digest"]
            or p["actor"] != ticket["request"]["actor"]
            or p["original_admission_seal"] != ticket["admission_seal"]
            or set(p["administrator"]) != set(p["actor"]) | {"identity_digest"}
            or any(p["administrator"].get(k) != v for k, v in p["actor"].items())
            or p["original_request_digest"] != ticket["request_digest"]
            or p["original_settlement_digest"] != ticket["migration_digest"]
            or p["original_claim_digest"] != ticket["claim_digest"]
            or p["checkpoint_digest"] != ticket["checkpoint_digest"]
            or any(
                p[key] != ticket[key] for key in ("settings_digest", "protected_scope_digest", "command_custody_digest")
            )
            or p["source_digest"] != digest(ticket["binding"]["workspace"])
            or p["post_admission_journal_digest"] != reviewed_head
            or record["previous_hash"] != reviewed_head
            or p["request"]["candidate_digest"] != p["candidate_digest"]
            or p["request"]["expected_admission_seal"] != ticket["admission_seal"]
            or p["request"]["expected_failed_event_digest"] != p["failed_event_digest"]
            or digest({"events": events[:2]}) != p["request_events_digest"]
            or events[1]["digest"] != p["failed_event_digest"]
            or expected_lease != p["lease"]
            or expected_binding != p["binding"]
            or p["binding"]["invocation_id"] == ticket["binding"]["invocation_id"]
            or digest({k: v for k, v in policy.items() if k != "digest"}) != policy["digest"]
            or not policy["revision"]
            or not configured.enabled
            or (original["proof"]["version"] == 2 and not configured.allow_unavailable_source_observations)
        ):
            raise ValueError("Recovery witness differs from its immutable original admission")
        checkpoint = BoundaryCheckpoint.decode(ticket["checkpoint"].encode(), ticket["checkpoint_digest"])
        if (
            digest({"usage": checkpoint.record.usage, "limits": checkpoint.record.limits})
            != p["checkpoint_usage_digest"]
        ):
            raise ValueError("Recovery changed the original spent SDK usage or limits")
        InvalidReadRecoveries._original_events(failed, events[:2])
        if (
            len(events) < 3
            or events[2]["kind"] != RECOVERED
            or events[2]["evidence"]
            != {
                "recovery_id": p["recovery_id"],
                "recovery_digest": record["digest"],
                "admission_seal": ticket["admission_seal"],
            }
        ):
            raise ValueError("Recovery has no exact native request receipt")
        old_epoch = ticket["policy_epoch"]
        new_epoch = p["policy_epoch"]
        if old_epoch is None:
            if new_epoch is not None:
                raise ValueError("Recovery cannot add a historical policy epoch")
        elif new_epoch is None or any(
            new_epoch.get(k) != v
            for k, v in old_epoch.items()
            if k not in {"current", "current_profile", "lease_policy_digest", "digest"}
        ):
            raise ValueError("Recovery changed its original historical policy lineage")
        if new_epoch is not None and (
            new_epoch["current_profile"] != p["current_profile"]
            or new_epoch["lease_policy_digest"] != policy["digest"]
            or digest({k: v for k, v in new_epoch.items() if k != "digest"}) != new_epoch["digest"]
        ):
            raise ValueError("Recovery historical epoch has no exact current authority")
        return record

    @staticmethod
    def _status_valid(ticket: Data, events: list[Data], record: Data) -> None:
        if ticket["status"] == "queued":
            valid = (
                len(events) == 3
                and ticket["generation"] is None
                and ticket["owner_pid"] is None
                and ticket["result"] is None
            )
        else:
            claimed = (
                len(events) >= 4
                and events[3]["kind"] == "worker_claim"
                and events[3]["evidence"]
                == {
                    "generation": ticket["generation"],
                    "request_digest": ticket["request_digest"],
                    "owner_pid": ticket["owner_pid"],
                }
            )
            if ticket["status"] == "executing":
                valid = (
                    claimed
                    and len(events) == 4
                    and ticket["generation"] is not None
                    and ticket["owner_pid"] is not None
                )
            else:
                valid = (
                    len(events) == 5
                    and claimed
                    or len(events) == 4
                    and not claimed
                    and ticket["status"] == "failed"
                    and ticket["generation"] is None
                    and ticket["owner_pid"] is None
                )
                valid = (
                    valid
                    and events[-1]["kind"] == "worker_" + ticket["status"]
                    and events[-1]["evidence"] == ticket["result"]
                )
        if not valid:
            raise ValueError("Recovery worker state differs from its complete native history")

    async def has(self, ticket: Data) -> bool:
        _, _, entries = await self._native(ticket["run_id"])
        return bool(self._matches(ticket, entries))

    async def queued_valid(self, ticket: Data) -> bool:
        _, _, entries = await self._native(ticket["run_id"])
        events = await self._events(ticket["id"])
        record = self._record(ticket, entries, events)
        if record is None:
            return False
        self._status_valid(ticket, events, record)
        return True

    async def status(self, run_id: str, request_id: str) -> Data:
        _outside_unit()
        run_id, request_id = identifier(run_id).hex, identifier(request_id).hex
        ticket = await self._ticket(run_id, request_id)
        await self._operator(run_id, ticket)
        _, _, entries = await self._native(run_id)
        # Receipt readback remains available after legitimate same-attempt work changes current scope.
        events = await self._events(request_id)
        record = self._record(ticket, entries, events)
        if record is not None:
            self._status_valid(ticket, events, record)
        p = record["payload"] if record is not None else None
        value = InvalidReadRecoveryStatusV1(
            version=1,
            kind="unclaimed_invalid_read_recovery_status",
            run_id=run_id,
            request_id=request_id,
            recovery_id=p["recovery_id"] if p else None,
            recovery_record_digest=record["digest"] if record else None,
            status=ticket["status"] if p else "not_recovered",
            original_admission_seal=ticket["admission_seal"],
            lease=p["lease"] if p else None,
            current_profile=p["current_profile"] if p else None,
        ).model_dump()
        await self.store.admit_credentials(value)
        await self._operator(run_id, ticket)
        return value

    async def _prepare(self, run_id: str, request_id: str) -> Data:
        _outside_unit()
        ticket = await self._ticket(run_id, request_id)
        actor = await self._operator(run_id, ticket)
        administrator = await self._administrator(actor)
        run, task, entries = await self._native(run_id)
        settlement = self._admission(ticket, run, task, entries)
        events = await self._events(request_id)
        self._original_events(ticket, events)
        active = self.engine.active.get(run_id)
        if self.engine.stopping or active is not None and not active.done() or run["status"] != "failed":
            raise ValueError("Recovery requires the unchanged idle failed original execution")
        if task["status"] not in {"running", "needs_reconciliation"}:
            raise ValueError("Later native activity or task changes prevent unclaimed recovery")
        self._idle_head(ticket, entries)
        if self._matches(ticket, entries):
            raise ValueError("This request already has its one-use recovery witness")
        async with self.store._unit():
            if await self.requests.records.active_for_run(identifier(run_id)):
                raise ValueError("Another native execution request is active")
        policy = policy_snapshot(self.store.configuration)
        configured = ContinuityLeasePolicyV1.model_validate(policy["settings"])
        generation = self.store.configuration.settings_generation
        settings = self.store.configuration.load_snapshot(effective_revision(run))
        if (
            digest(settings) != ticket["settings_digest"]
            or digest(self.store.configuration.resolve("bridge")) != ticket["settings_digest"]
        ):
            raise ValueError("Frozen execution configuration changed")
        holds = []
        if not policy["revision"] or not configured.enabled:
            holds.append("lease_policy_disabled")
        if settlement["payload"]["proof"]["version"] == 2 and not configured.allow_unavailable_source_observations:
            holds.append("unavailable_observation_policy_disabled")
        from firefly_forge.invalid_read_continuities import current_profile_seal

        try:
            profile = current_profile_seal()
        except ValueError:
            profile = None
            holds.append("current_profile_unqualified")
        runtime = runtime_contract()
        selected = settlement["payload"]["proof"]["runtime_contract"]
        epoch = epoch_snapshot(self.store, run, entries, legacy_runtime_contract=selected) if not holds else None

        async def current() -> None:
            await self.continuities.restores.access(run, actor)
            if await self._administrator(actor) != administrator:
                raise AccessDenied("Recovery administrator identity changed during inspection")
            current_actor = CURRENT_PRINCIPAL.get()
            if current_actor is None or ProjectAccess.actor_snapshot(current_actor) != actor:
                raise AccessDenied("Recovery operator changed during source inspection")
            if (
                await self._native(run_id) != (run, task, entries)
                or await self._ticket(run_id, request_id) != ticket
                or await self._events(request_id) != events
                or self.store.configuration.settings_generation is not generation
                or policy_snapshot(self.store.configuration) != policy
                or digest(self.store.configuration.resolve("bridge")) != ticket["settings_digest"]
                or (
                    current_unit_of_work() is None
                    and (runtime_contract() != runtime or (profile is not None and current_profile_seal() != profile))
                )
            ):
                raise ValueError("Recovery native read custody changed")
            async with self.store._unit():
                if await self.requests.records.active_for_run(identifier(run_id)):
                    raise ValueError("A native worker claim prevents recovery inspection")

        with epoch_scope(epoch):
            await self._custody(ticket, run, task, entries, settlement)
            source = await workspace_cut(self.store, self.engine.workspaces / run_id, assert_current=current)
        if source != ticket["binding"]["workspace"]:
            raise ValueError("Original source differs from the admitted closed checkpoint")
        checkpoint = BoundaryCheckpoint.decode(ticket["checkpoint"].encode(), ticket["checkpoint_digest"])
        checkpoint.resume_kwargs()["usage_limits"].check_before_request(checkpoint.usage)
        async with self.store._unit():
            commands = await self.journal.sandboxes.for_run(identifier(run_id))
        frozen_timeout = settlement["payload"]["frozen_timeout_seconds"]
        maximum = min(configured.max_window_seconds, frozen_timeout)
        active_deadline = run.get("active_execution_deadline_ms")
        if "active_execution_budget" in run or active_deadline is not None and type(active_deadline) is not int:
            raise ValueError("The original active-budget protocol is unsupported")
        if active_deadline is not None:
            maximum = min(maximum, max(0, (active_deadline - int(time.time() * 1000)) // 1000))
            if maximum == 0:
                holds.append("active_execution_budget_expired")
        value = dict(
            version=1,
            kind="unclaimed_invalid_read_recovery_candidate",
            run_id=run_id,
            request_id=request_id,
            admission="HOLD" if holds else "READY",
            holds=holds,
            execution_authorized=False,
            original_admission_seal=ticket["admission_seal"],
            failed_event_digest=events[-1]["digest"],
            original_settlement_digest=ticket["migration_digest"],
            original_claim_digest=ticket["claim_digest"],
            checkpoint_digest=ticket["checkpoint_digest"],
            source_digest=digest(source),
            attempt=ticket["binding"]["round"],
            current_profile=profile,
            current_runtime_contract=runtime,
            policy_digest=policy["digest"],
            maximum_window_seconds=maximum,
            frozen_timeout_seconds=frozen_timeout,
            original_lease=ticket["lease"],
            usage=dict(
                requests=checkpoint.usage.requests,
                tool_calls=checkpoint.usage.tool_calls,
                request_limit=checkpoint.record.limits.get("request_limit"),
                tool_calls_limit=checkpoint.record.limits.get("tool_calls_limit"),
                commands=len(commands),
                command_limit=(run.get("sandbox_snapshot") or {}).get("configuration", {}).get("command_limit"),
            ),
        )
        value["digest"] = digest(value)
        value = InvalidReadRecoveryCandidateV1.model_validate(value).model_dump()
        await self.store.admit_credentials(value)
        await current()
        return dict(
            candidate=value,
            ticket=ticket,
            run=run,
            task=task,
            entries=entries,
            events=events,
            actor=actor,
            policy=policy,
            epoch=epoch,
            generation=generation,
            source=source,
            read_guard=current,
            checkpoint=checkpoint,
            administrator=administrator,
        )

    async def candidate(self, run_id: str, request_id: str) -> Data:
        return cast(Data, (await self._prepare(identifier(run_id).hex, identifier(request_id).hex))["candidate"])

    async def request(self, run_id: str, request_id: str, recovery_id: str, body: InvalidReadRecoveryInputV1) -> Data:
        _outside_unit()
        if any(identifier(value).hex != value for value in (run_id, request_id, recovery_id)):
            raise ValueError("Use exact lowercase native recovery identities")
        body = InvalidReadRecoveryInputV1.model_validate(body.model_dump())
        ticket = await self._ticket(run_id, request_id)
        actor = await self._operator(run_id, ticket)

        async def existing() -> bool:
            current = await self._ticket(run_id, request_id)
            _, _, entries = await self._native(run_id)
            events = await self._events(request_id)
            record = self._record(current, entries, events)
            if record is None:
                return False
            if (
                record["payload"]["recovery_id"] != recovery_id
                or record["payload"]["request"] != body.model_dump()
                or record["payload"]["actor"] != actor
            ):
                raise ValueError("This original request already has another one-use recovery action")
            return True

        if await existing():
            return await self.status(run_id, request_id)
        try:
            prepared = await self._prepare(run_id, request_id)
        except ValueError:
            if await existing():
                return await self.status(run_id, request_id)
            raise
        c = prepared["candidate"]
        if (
            (body.candidate_digest, body.expected_admission_seal, body.expected_failed_event_digest)
            != (
                c["digest"],
                c["original_admission_seal"],
                c["failed_event_digest"],
            )
            or c["admission"] != "READY"
            or body.valid_for_seconds > c["maximum_window_seconds"]
        ):
            raise ValueError("The reviewed unclaimed recovery candidate changed or is unavailable")
        await self.store.admit_credentials([body.model_dump(), c, prepared["ticket"]])
        try:
            await prepared["read_guard"]()
        except ValueError:
            if await existing():
                return await self.status(run_id, request_id)
            raise
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:execution-request-admission:" + self.store.schema},
            )
            await self.continuities.restores.completions._writer(prepared["run"], CURRENT_PRINCIPAL.get())
            await self.journal._run(run_id)
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Continuity request not found")
            _, _, entries = await self._native(run_id)
            records = self._matches(row.data, entries)
            if records:
                record = self._record(row.data, entries, await self._events(request_id))
                if (
                    record is None
                    or record["payload"]["recovery_id"] != recovery_id
                    or record["payload"]["request"] != body.model_dump()
                    or record["payload"]["actor"] != actor
                ):
                    raise ValueError("Concurrent unclaimed recovery action conflicts")
            else:
                await prepared["read_guard"]()
                if row.data != prepared["ticket"] or row.status != "failed":
                    raise ValueError("The original failed request changed before recovery")
                configured = ContinuityLeasePolicyV1.model_validate(prepared["policy"]["settings"])
                if await self.requests.records.pending_count() >= configured.max_pending_tickets:
                    raise ValueError("The existing bounded execution queue is full")
                original = next(e["payload"] for e in entries if e["digest"] == row.data["migration_digest"])
                lease = lease_window(
                    configured,
                    frozen_timeout_seconds=c["frozen_timeout_seconds"],
                    valid_for_seconds=body.valid_for_seconds,
                    now_ms=int(time.time() * 1000),
                    original_deadline_ms=original["proof"]["deadline_ms"],
                    active_deadline_ms=prepared["run"].get("active_execution_deadline_ms"),
                )
                binding = {
                    **row.data["binding"],
                    "invocation_id": uuid.uuid4().hex,
                    "contract": c["current_runtime_contract"],
                    "deadline_ms": lease["expires_at_ms"],
                }
                checkpoint = prepared["checkpoint"]
                payload = dict(
                    version=1,
                    kind="unclaimed_invalid_read_recovery",
                    run_id=run_id,
                    request_id=request_id,
                    recovery_id=recovery_id,
                    request=body.model_dump(),
                    actor=actor,
                    administrator=prepared["administrator"],
                    candidate_digest=c["digest"],
                    original_ticket_digest=digest(row.data),
                    original_admission_seal=row.data["admission_seal"],
                    original_request_digest=row.data["request_digest"],
                    original_settlement_digest=row.data["migration_digest"],
                    original_claim_digest=row.data["claim_digest"],
                    checkpoint_digest=row.data["checkpoint_digest"],
                    checkpoint_usage_digest=digest(
                        {"usage": checkpoint.record.usage, "limits": checkpoint.record.limits}
                    ),
                    failed_state={key: copy.deepcopy(row.data[key]) for key in MUTABLE},
                    failed_event_digest=c["failed_event_digest"],
                    request_events_digest=digest({"events": prepared["events"]}),
                    post_admission_journal_digest=entries[-1]["digest"],
                    run_digest=digest(prepared["run"]),
                    task_digest=digest(prepared["task"]),
                    source_digest=c["source_digest"],
                    binding=binding,
                    lease=lease,
                    lease_policy=prepared["policy"],
                    policy_epoch=prepared["epoch"],
                    current_profile=c["current_profile"],
                    settings_digest=row.data["settings_digest"],
                    protected_scope_digest=row.data["protected_scope_digest"],
                    command_custody_digest=row.data["command_custody_digest"],
                )
                payload["digest"] = digest(payload)
                payload = InvalidReadRecoveryWitnessV1.model_validate(payload).model_dump()
                self.store.reject_credentials(payload)
                import json

                await self.journal._bridge_read_bound(run_id, extra=len(json.dumps(payload).encode()) + 16384)
                record = await self.journal._append(
                    run_id, "implement", "invalid-recover:" + request_id, RECOVERY, payload
                )
                row.status = "queued"
                row.data = {
                    **row.data,
                    "status": "queued",
                    "generation": None,
                    "owner_pid": None,
                    "result": None,
                    "updated_at": now(),
                }
                verify_ticket(row.data)
                await self.requests.records.save(row)
                await self.requests._event(
                    row,
                    RECOVERED,
                    {
                        "recovery_id": recovery_id,
                        "recovery_digest": record["digest"],
                        "admission_seal": row.data["admission_seal"],
                    },
                )
                if policy_snapshot(self.store.configuration) != prepared["policy"]:
                    raise ValueError("Recovery policy changed before commit")
                await _assert_source_administrator(self.store, prepared["administrator"])
        return await self.status(run_id, request_id)

    async def _authority(self, ticket: Data, record: Data, *, executing: bool) -> tuple[Data, Data, list[Data]]:
        p = record["payload"]
        run, task, entries = await self._native(ticket["run_id"])
        settlement = self._admission(ticket, run, task, entries)
        events = await self._events(ticket["id"])
        current = self._record(ticket, entries, events)
        if current != record:
            raise ValueError("Recovery witness changed")
        self._status_valid(ticket, events, record)
        await self.continuities.restores.access(run, p["actor"])
        await _assert_source_administrator(self.store, p["administrator"])
        if (
            int(time.time() * 1000) >= p["lease"]["expires_at_ms"]
            or policy_snapshot(self.store.configuration) != p["lease_policy"]
            or digest(self.store.configuration.load_snapshot(effective_revision(run))) != p["settings_digest"]
            or digest(self.store.configuration.resolve("bridge")) != p["settings_digest"]
            or run.get("active_execution_deadline_ms") != p["lease"]["active_deadline_ms"]
        ):
            raise ValueError("Current recovery lease, policy or configuration changed")
        if current_unit_of_work() is None:
            from firefly_forge.invalid_read_continuities import current_profile_seal

            if runtime_contract() != p["binding"]["contract"] or current_profile_seal() != p["current_profile"]:
                raise ValueError("The qualified recovery runtime changed")
            epoch = epoch_snapshot(
                self.store, run, entries, legacy_runtime_contract=settlement["payload"]["proof"]["runtime_contract"]
            )
            if epoch != p["policy_epoch"]:
                raise ValueError("Current recovery policy epoch changed")
        if executing:
            if run["status"] != "developing" or task["status"] != "running" or task["attempt"] != p["binding"]["round"]:
                raise ValueError("Recovery no longer owns its original active task")
        else:
            self._idle_head(ticket, entries, record)
            task_matches = digest(task) == p["task_digest"]
            after = [e for e in entries if e["sequence"] > record["sequence"]]
            if after and task["status"] == "needs_reconciliation":
                task_matches = task_matches or digest({**task, "status": "running"}) == p["task_digest"]
            if run["status"] != "failed" or digest(run) != p["run_digest"] or not task_matches:
                raise ValueError("Later activity prevents a never-claimed recovery")
        with epoch_scope(p["policy_epoch"]):
            await self._custody(ticket, run, task, entries, settlement)
        return run, task, entries

    async def guard(self, request_id: str, recovery_digest: str, generation: str) -> None:
        await self.store.assert_ownership()
        raw = await self.requests.get(request_id)
        ticket = await self._ticket(raw["run_id"], request_id)
        if (
            ticket["status"] != "executing"
            or ticket["generation"] != generation
            or ticket["owner_pid"] != self.store._ownership_pid
        ):
            raise ValueError("Recovery worker ownership generation changed")
        _, _, entries = await self._native(ticket["run_id"])
        record = self._record(ticket, entries, await self._events(request_id))
        if record is None or record["digest"] != recovery_digest:
            raise ValueError("Recovery worker lost its immutable witness")
        await self._authority(ticket, record, executing=True)

    async def execute(self, ticket: Data) -> None:
        self.requests.runtime.require_execution()
        await self.store.assert_ownership()
        _, _, entries = await self._native(ticket["run_id"])
        record = self._record(ticket, entries, await self._events(ticket["id"]))
        if record is None:
            raise ValueError("No one-use unclaimed recovery exists")
        p = record["payload"]
        from firefly_forge.checkpoint_mode_runtime import mode_execution_scope

        with epoch_scope(p["policy_epoch"]):
            async with mode_execution_scope(self.journal, ticket["run_id"]):
                run, _, _ = await self._authority(ticket, record, executing=False)
                if (
                    await workspace_cut(self.store, self.engine.workspaces / ticket["run_id"])
                    != p["binding"]["workspace"]
                ):
                    raise ValueError("Original source changed before the recovery worker claim")
                async with self.store._unit():
                    locked = await self.journal._run(ticket["run_id"])
                    row = await self.requests.records.find_by_id(
                        identifier(ticket["id"]), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if row is None or row.status != "queued":
                        return
                    if row.data != ticket:
                        raise ValueError("Recovery queued state changed")
                    await self.continuities.restores.completions._writer(await self.store.get_run(ticket["run_id"]))
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
                        "updated_at": now(),
                    }
                    await self.requests.records.save(row)
                    await self.requests._event(
                        row,
                        "worker_claim",
                        {
                            "generation": row.data["generation"],
                            "request_digest": row.data["request_digest"],
                            "owner_pid": row.data["owner_pid"],
                        },
                    )
                    ticket = copy.deepcopy(row.data)
                execution = RecoveredInvalidReadExecution(self, ticket, record)
                await execution.guard_source()
                await self.engine.continue_agent_checkpoint(execution)
                run = await self.store.get_run(ticket["run_id"])
                if run.get("bridge_completion_hold") or run["status"] not in {"paused", "succeeded", "validating"}:
                    raise ValueError("Recovered worker did not reach a known native boundary")
                await self.continuities._finish(
                    ticket["id"], "completed", {"run_status": run["status"], "same_task_attempt": p["binding"]["round"]}
                )


def validate_recovery_record(ticket: Data, entries: list[Data], events: list[Data]) -> Data | None:
    """Validate immutable custody/status only; grants no execution or current authority.

    Callers must obtain native row/event columns with the bounded custody readers.
    A None result means no recovery witness; ordinary ticket closure is separate.
    """
    from firefly_forge.execution_requests import ExecutionRequests

    verify_ticket(ticket)
    if (
        not Journal.verify_chain(entries)
        or any(e["run_id"] != ticket["run_id"] for e in entries)
        or not ExecutionRequests.verify_chain(events)
        or any(e["request_id"] != ticket["id"] for e in events)
    ):
        raise ValueError("Recovery requires exact complete native custody")
    record = InvalidReadRecoveries._record(ticket, entries, events)
    if record is not None:
        InvalidReadRecoveries._status_valid(ticket, events, record)
    return record
