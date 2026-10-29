# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Same-ticket acceptance of a final response that already spent its SDK boundary."""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any, cast

from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import text

from firefly_forge.agent_restores import RestoreExecution
from firefly_forge.agentic_checkpoint_invocation import runtime_contract, workspace_cut
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.checkpoint_continuations import _prior
from firefly_forge.checkpoint_mode_runtime import effective_revision, mode_execution_scope
from firefly_forge.checkpoint_work_duration import checkpoint_payload
from firefly_forge.final_outcome_models import (
    FinalOutcomeCandidateV1,
    FinalOutcomeContinuationInputV1,
    FinalOutcomeStatusV1,
)
from firefly_forge.invalid_read_continuity import _bytes
from firefly_forge.invalid_read_epochs import epoch_scope, epoch_snapshot
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, policy_snapshot
from firefly_forge.invalid_read_recoveries import MUTABLE
from firefly_forge.invalid_read_tickets import protected_scope, verify_ticket
from firefly_forge.journal import Journal, digest
from firefly_forge.known_final_outcome_proof import PreparedFinalOutcome, prepare_final_outcome
from firefly_forge.store import identifier, now, prepared_credential_rejection

Data = dict[str, Any]
HANDOFF = "final_outcome_continuation"
ADMITTED = "final_outcome_continuation_admitted"
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
        or set(p) != _FIELDS
        or type(p["version"]) is not int
        or p["version"] != 1
        or entry["kind"] != HANDOFF
        or entry["idempotency_key"] != "final-outcome:" + p["action_id"]
        or entry["run_id"] != p["run"]["id"]
        or entry["task_id"] != "implement"
        or digest({k: v for k, v in p.items() if k != "digest"}) != p["digest"]
    ):
        raise ValueError("Final outcome witness differs from its native custody")
    body = FinalOutcomeContinuationInputV1.model_validate(p["request"])
    c = FinalOutcomeCandidateV1.model_validate(p["candidate"]).model_dump(mode="json")
    before = p["failed_ticket"]
    verify_ticket(before)
    prefix = entries[: entry["sequence"] - 1]
    policy = ContinuityLeasePolicyV1.model_validate(p["lease_policy"]["settings"])
    if (
        c["digest"] != digest({k: v for k, v in c.items() if k != "digest"})
        or body.candidate_digest != c["digest"]
        or c["admission"] != "READY"
        or c["holds"]
        or not policy.enabled
        or not policy.allow_final_outcome_continuation
        or not 1 <= body.valid_for_seconds <= policy.max_window_seconds
        or before["status"] != "needs_reconciliation"
        or before["id"] != p["request_id"]
        or p["actor"] != before["request"]["actor"]
        or c["original_admission_seal"] != before["admission_seal"]
        or c["run_id"] != p["run"]["id"]
        or c["request_id"] != before["id"]
        or c["attempt"] != p["task"]["attempt"]
        or c["run_digest"] != digest(p["run"])
        or c["task_digest"] != digest(p["task"])
        or c["ticket_digest"] != digest(before)
        or c["events_digest"] != digest({"events": p["events"]})
        or c["journal_digest"] != digest({"entries": prefix})
        or c["policy_digest"] != p["lease_policy"]["digest"]
        or p["run"].get("implementation_rounds") != c["attempt"]
        or p["run"].get("implementation_pending") is not False
        or not p["events"]
        or not Journal.verify_chain(prefix)
    ):
        raise ValueError("Final outcome witness changed reviewed result, authority or charged progress")
    from firefly_forge.execution_requests import ExecutionRequests

    if not ExecutionRequests.verify_chain(p["events"]):
        raise ValueError("Final outcome predecessor events are not intact")
    lease = p["lease"]
    if (
        set(lease) != {"purpose", "issued_at_ms", "expires_at_ms", "window_ms"}
        or lease["purpose"] != "claim_only"
        or type(lease["issued_at_ms"]) is not int
        or lease["issued_at_ms"] < 1
        or lease["window_ms"] != body.valid_for_seconds * 1000
        or lease["expires_at_ms"] != lease["issued_at_ms"] + lease["window_ms"]
        or p["selection"]
        != {
            key: c[key]
            for key in (
                "checkpoint_record_digest",
                "response_record_digest",
                "consume_record_digest",
                "final_outcome_digest",
            )
        }
        or not prefix
        or prefix[-1]["digest"] != c["consume_record_digest"]
        or digest((p["run"].get("usage") or {}).get("runtime_report") or {}) != c["report_digest"]
    ):
        raise ValueError("Final outcome selection or finite admission window changed")
    if p["action_id"] == before["id"] or any(
        e["payload"].get("recovery_id") == p["action_id"] or e["payload"].get("action_id") == p["action_id"]
        for e in prefix
    ):
        raise ValueError("A spent action cannot accept another final outcome")


def final_record(ticket: Data, entries: list[Data], events: list[Data]) -> Data | None:
    records = [e for e in entries if e["kind"] == HANDOFF and e["payload"].get("request_id") == ticket["id"]]
    if not records:
        return None
    record = records[-1]
    validate_witness(record, entries)
    p = record["payload"]
    before = p["failed_ticket"]
    from firefly_forge.execution_requests import ExecutionRequests

    if (
        not ExecutionRequests.verify_chain(events)
        or events[: len(p["events"])] != p["events"]
        or _bytes({k: v for k, v in ticket.items() if k not in MUTABLE})
        != _bytes({k: v for k, v in before.items() if k not in MUTABLE})
    ):
        raise ValueError("Final outcome changed original ticket or predecessor events")
    tail = events[len(p["events"]) :]
    if (
        not tail
        or tail[0]["kind"] != ADMITTED
        or tail[0]["evidence"]
        != {"action_id": p["action_id"], "witness_digest": record["digest"], "admission_seal": ticket["admission_seal"]}
    ):
        raise ValueError("Final outcome lacks its exact admission event")
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
        ticket["generation"] == before["generation"]
        or type(ticket["owner_pid"]) is not int
        or ticket["owner_pid"] <= 0
        or identifier(ticket["generation"]).hex != ticket["generation"]
    ):
        raise ValueError("Final outcome has no fresh native worker ownership")
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
        raise ValueError("Final outcome lifecycle differs from native events")
    return record


class FinalOutcomeExecution(RestoreExecution):
    service: Any

    def __init__(self, service: Any, ticket: Data, witness: Data, prepared: PreparedFinalOutcome) -> None:
        self.service, self.ticket, self.witness, self.prepared = service, copy.deepcopy(ticket), witness, prepared
        data = prepared.data
        self.intent, self.record, self.result_record, self.body = (
            data["intent"],
            data["checkpoint"],
            data["response"],
            data["response"]["payload"]["response"].encode(),
        )
        self.binding = checkpoint_payload(self.record["payload"]).invocation
        self.run_id, self.request_id = ticket["run_id"], self.record["idempotency_key"]

    async def guard(self) -> None:
        await self.service.guard(self.ticket, self.witness)

    async def guard_source(self) -> None:
        await self.guard()
        if await workspace_cut(self.service.store, self.service.workspace(self.run_id)) != self.binding.workspace:
            raise ValueError("Known final source changed")
        await self.guard()

    async def consume(self, *args: Any, **kwargs: Any) -> None:
        raise ValueError("An already-consumed final response cannot be consumed again")

    async def result(self) -> Any:
        from firefly_forge.providers import AgentResult

        await self.guard_source()
        run = await self.service.store.get_run(self.run_id)
        if run["usage"] != self.witness["payload"]["run"]["usage"]:
            raise ValueError("Known final usage projection changed")
        return AgentResult(
            self.prepared.data["report"]["summary"], "known_final_outcome", usage=copy.deepcopy(run["usage"])
        )


class FinalOutcomeContinuations:
    def __init__(self, continuities: Any) -> None:
        self.continuities, self.base = continuities, continuities.recoveries
        self.store, self.journal, self.engine, self.requests = (
            continuities.store,
            continuities.journal,
            continuities.engine,
            continuities.requests,
        )
        self.completions = continuities.restores.completions
        self.operator, self.access, self.workspace = (
            continuities.restores.operator,
            continuities.restores.access,
            continuities.restores.workspace,
        )

    async def _events(self, request_id: str) -> list[Data]:
        return cast(list[Data], await self.continuities.checkpoints._events(request_id))

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
            or run["status"] not in {"failed", "needs_reconciliation"}
            or task["status"] not in {"running", "needs_reconciliation"}
            or self.engine.stopping
            or run.get("implementation_pending") is not False
            or run.get("implementation_rounds") != task["attempt"]
            or (run_id in self.engine.active and not self.engine.active[run_id].done())
        ):
            raise ValueError("Known final continuation requires its idle already-charged original task")
        legacy = (ticket.get("policy_epoch") or {}).get("legacy_runtime_contract", ticket["binding"]["contract"])
        epoch = epoch_snapshot(self.store, run, entries, legacy_runtime_contract=legacy)
        with epoch_scope(epoch):
            settlement = self.base._admission(ticket, run, task, entries)
            await self.base._custody(ticket, run, task, entries, settlement)
            prepared = await prepare_final_outcome(run, task, entries)
        data = prepared.data
        usage = run.get("usage") or {}
        if (
            usage.get("runtime_report") != data["report"]
            or usage.get("requests") != data["usage"]["requests"]
            or usage.get("input_tokens") != data["usage"]["input_tokens"]
            or usage.get("output_tokens") != data["usage"]["output_tokens"]
            or sorted(usage.get("files_written", [])) != sorted(data["state"]["files_written"])
        ):
            raise ValueError("Known final outcome differs from the already-charged native run projection")
        policy = policy_snapshot(self.store.configuration)
        settings = ContinuityLeasePolicyV1.model_validate(policy["settings"])
        holds = []
        if not policy["revision"] or not settings.enabled:
            holds.append("lease_policy_disabled")
        if not settings.allow_final_outcome_continuation:
            holds.append("final_outcome_continuation_disabled")
        from firefly_forge.invalid_read_continuities import current_profile_seal

        try:
            profile = current_profile_seal()
        except ValueError:
            profile = None
            holds.append("current_profile_unqualified")
        deadline = run.get("active_execution_deadline_ms")
        if "active_execution_budget" in run:
            raise ValueError("Unsupported active execution budget protocol")
        if deadline is not None and (type(deadline) is not int or deadline <= int(time.time() * 1000)):
            holds.append("active_execution_budget_expired")
        if self.store.configuration.resolve("bridge") != self.store.configuration.load_snapshot(
            effective_revision(run)
        ):
            raise ValueError("Frozen provider configuration changed")
        guard = await self.continuities.restores.source_read_guard(
            run,
            await self.continuities._actor(run_id),
            task_attempt=task["attempt"],
            invalid_read=run["status"] == "failed",
        )
        cp = checkpoint_payload(data["checkpoint"]["payload"])
        if await workspace_cut(self.store, self.workspace(run_id), assert_current=guard) != cp.invocation.workspace:
            raise ValueError("Known final source differs from its original checkpoint")
        feedback = data["report"]["worker_feedback"]
        outcome = (feedback or {}).get("disposition", "completed")
        c = dict(
            version=1,
            kind="final_outcome_continuation_candidate",
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
            checkpoint_record_digest=data["checkpoint"]["digest"],
            response_record_digest=data["response"]["digest"],
            consume_record_digest=data["consume"]["digest"],
            report_digest=digest(data["report"]),
            final_outcome_digest=prepared.digest,
            source_digest=digest(cp.invocation.workspace),
            current_runtime_contract=runtime_contract(),
            current_profile=profile,
            policy_digest=policy["digest"],
            maximum_window_seconds=settings.max_window_seconds,
            admission_window_purpose="claim_only",
            outcome="complete" if outcome == "completed" else outcome,
            followup_kind=((feedback or {}).get("request") or {}).get("kind"),
            usage=data["usage"],
            limits=data["limits"],
        )
        c["digest"] = digest(c)
        c = FinalOutcomeCandidateV1.model_validate(c).model_dump(mode="json")
        await self.store.admit_credentials([c, data])
        await guard()
        if await self.base._ticket(run_id, request_id) != ticket or await self._events(request_id) != events:
            raise ValueError("Known final request changed during inspection")
        return dict(
            candidate=c,
            ticket=ticket,
            actor=actor,
            administrator=administrator,
            run=run,
            task=task,
            entries=entries,
            events=events,
            policy=policy,
            epoch=epoch,
            prepared=prepared,
        )

    async def candidate(self, run_id: str, request_id: str) -> Data:
        with prepared_credential_rejection(self.store):
            return dict((await self._prepare(identifier(run_id).hex, identifier(request_id).hex))["candidate"])

    async def request(
        self, run_id: str, request_id: str, action_id: str, body: FinalOutcomeContinuationInputV1
    ) -> Data:
        _outside_unit()
        if any(identifier(v).hex != v for v in (run_id, request_id, action_id)):
            raise ValueError("Use exact lowercase native identities")
        body = FinalOutcomeContinuationInputV1.model_validate(body.model_dump())

        async def existing() -> Data | None:
            ticket = await self.base._ticket(run_id, request_id)
            await self.base._operator(run_id, ticket)
            _, _, entries = await self.base._native(run_id)
            matches = [e for e in entries if e["kind"] == HANDOFF and e["payload"].get("action_id") == action_id]
            if not matches:
                return None
            if (
                len(matches) != 1
                or matches[0]["payload"]["request_id"] != request_id
                or matches[0]["payload"]["request"] != body.model_dump(mode="json")
            ):
                raise ValueError("Known final action identity belongs to another input")
            status = await self.status(run_id, request_id)
            if status["action_id"] != action_id:
                raise ValueError("Known final action is spent")
            return status

        with prepared_credential_rejection(self.store):
            saved = await existing()
            if saved is not None:
                return saved
            try:
                return await self._admit(run_id, request_id, action_id, body)
            except ValueError:
                saved = await existing()
                if saved is not None:
                    return saved
                raise

    async def _admit(self, run_id: str, request_id: str, action_id: str, body: FinalOutcomeContinuationInputV1) -> Data:
        prepared = await self._prepare(run_id, request_id)
        c = prepared["candidate"]
        if body.candidate_digest != c["digest"] or c["admission"] != "READY":
            raise ValueError("The reviewed known final candidate changed or is held")
        if not 1 <= body.valid_for_seconds <= c["maximum_window_seconds"]:
            raise ValueError("Known final admission window exceeds current policy")
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:execution-request-admission:" + self.store.schema},
            )
            await self.journal._run(run_id)
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.data != prepared["ticket"] or row.status != "needs_reconciliation":
                raise ValueError("Known final ticket changed before admission")
            await self.base._operator(run_id, prepared["ticket"])
            if (
                await self.base._native(run_id) != (prepared["run"], prepared["task"], prepared["entries"])
                or await self._events(request_id) != prepared["events"]
                or policy_snapshot(self.store.configuration) != prepared["policy"]
            ):
                raise ValueError("Known final custody or current policy changed")
            issued = int(time.time() * 1000)
            p = {key: prepared[key] for key in ("actor", "administrator", "run", "task", "events")}
            p.update(
                version=1,
                request_id=request_id,
                action_id=action_id,
                request=body.model_dump(mode="json"),
                candidate=c,
                failed_ticket=prepared["ticket"],
                lease_policy=prepared["policy"],
                policy_epoch=prepared["epoch"],
                lease=dict(
                    purpose="claim_only",
                    issued_at_ms=issued,
                    expires_at_ms=issued + body.valid_for_seconds * 1000,
                    window_ms=body.valid_for_seconds * 1000,
                ),
                selection={
                    key: c[key]
                    for key in (
                        "checkpoint_record_digest",
                        "response_record_digest",
                        "consume_record_digest",
                        "final_outcome_digest",
                    )
                },
            )
            p["digest"] = digest(p)
            await self.store.admit_credentials(p)
            await self.base._operator(run_id, prepared["ticket"])
            if policy_snapshot(self.store.configuration) != prepared["policy"]:
                raise ValueError("Known final policy changed during admission")
            record = await self.journal._append(run_id, "implement", "final-outcome:" + action_id, HANDOFF, p)
            validate_witness(record, [*prepared["entries"], record])
            row.status = "queued"
            row.data = {**row.data, "status": "queued", "updated_at": now()}
            await self.requests.records.save(row)
            await self.requests._event(
                row,
                ADMITTED,
                dict(action_id=action_id, witness_digest=record["digest"], admission_seal=row.data["admission_seal"]),
            )
            self.store.reject_credentials(p)
        return await self.status(run_id, request_id)

    async def status(self, run_id: str, request_id: str) -> Data:
        ticket = await self.base._ticket(run_id, request_id)
        await self.base._operator(run_id, ticket)
        run, _, entries = await self.base._native(run_id)
        record = final_record(ticket, entries, await self._events(request_id))
        return FinalOutcomeStatusV1.model_validate(
            dict(
                version=1,
                kind="final_outcome_continuation_status",
                run_id=run_id,
                request_id=request_id,
                status=ticket["status"] if record else "not_recovered",
                action_id=record["payload"]["action_id"] if record else None,
                witness_digest=record["digest"] if record else None,
                original_admission_seal=ticket["admission_seal"],
                lease=record["payload"]["lease"] if record else None,
                run_status=run["status"],
            )
        ).model_dump(mode="json")

    async def _authority(self, ticket: Data, record: Data, *, executing: bool) -> None:
        p = record["payload"]
        run, task, entries = await self.base._native(ticket["run_id"])
        await self.access(run, ticket["request"]["actor"])
        await self.base._administrator(p["administrator"])
        if (
            policy_snapshot(self.store.configuration) != p["lease_policy"]
            or protected_scope(run) != ticket["protected_scope_digest"]
            or run.get("implementation_rounds") != p["run"]["implementation_rounds"]
            or run.get("implementation_pending") != p["run"]["implementation_pending"]
            or run.get("usage") != p["run"].get("usage")
            or task["attempt"] != p["task"]["attempt"]
            or task["idempotency_key"] != ticket["task_key"]
            or digest(self.store.configuration.resolve("bridge")) != ticket["settings_digest"]
            or (run.get("cancel_request") or {}).get("status") == "requested"
        ):
            raise ValueError("Known final authority, counters or provider scope changed")
        deadline = run.get("active_execution_deadline_ms")
        if deadline is not None and (type(deadline) is not int or deadline <= int(time.time() * 1000)):
            raise ValueError("Known final explicit execution budget expired")
        if current_unit_of_work() is None:
            from firefly_forge.invalid_read_continuities import current_profile_seal

            if (
                runtime_contract() != p["candidate"]["current_runtime_contract"]
                or current_profile_seal() != p["candidate"]["current_profile"]
            ):
                raise ValueError("Known final qualified runtime changed")
        with epoch_scope(p["policy_epoch"]):
            settlement = self.base._admission(ticket, run, task, entries)
            await self.base._custody(ticket, run, task, entries, settlement)
        if not executing and (
            int(time.time() * 1000) >= p["lease"]["expires_at_ms"]
            or run != p["run"]
            or task != p["task"]
            or len(entries) != record["sequence"]
        ):
            raise ValueError("Known final admission expired or its idle frontier changed")

    async def guard(self, ticket: Data, record: Data) -> None:
        await self.store.assert_ownership()
        current = await self.base._ticket(ticket["run_id"], ticket["id"])
        _, _, entries = await self.base._native(ticket["run_id"])
        if (
            current != ticket
            or current["status"] != "executing"
            or current["owner_pid"] != self.store._ownership_pid
            or final_record(current, entries, await self._events(ticket["id"])) != record
        ):
            raise ValueError("Known final worker lost its exact native ownership")
        await self._authority(ticket, record, executing=True)

    async def acceptance_current(self, execution: FinalOutcomeExecution) -> None:
        from firefly_forge.unknown_command_proof import continuation_scope

        await self.store.assert_ownership()
        ticket = await self.base._ticket(execution.run_id, execution.ticket["id"])
        run, task, entries = await self.base._native(execution.run_id)
        p = execution.witness["payload"]
        if (
            ticket != execution.ticket
            or ticket["status"] != "executing"
            or ticket["owner_pid"] != self.store._ownership_pid
            or final_record(ticket, entries, await self._events(ticket["id"])) != execution.witness
            or continuation_scope(run) != continuation_scope(p["run"])
            or policy_snapshot(self.store.configuration) != p["lease_policy"]
            or any(
                run.get(key) != p["run"].get(key)
                for key in (
                    "attempt",
                    "repair_attempts",
                    "implementation_rounds",
                    "implementation_pending",
                    "usage",
                    "active_execution_deadline_ms",
                )
            )
            or task["attempt"] != p["task"]["attempt"]
            or task["idempotency_key"] != ticket["task_key"]
            or (run.get("cancel_request") or {}).get("status") == "requested"
        ):
            raise ValueError("Known final native acceptance changed owner, grants or spent progress")
        await self.access(run, ticket["request"]["actor"])
        await self.base._administrator(p["administrator"])

    async def execute(self, ticket: Data) -> None:
        self.requests.runtime.require_execution()
        await self.store.assert_ownership()
        _, _, entries = await self.base._native(ticket["run_id"])
        record = final_record(ticket, entries, await self._events(ticket["id"]))
        if record is None:
            raise ValueError("Known final outcome has no admitted witness")
        p = record["payload"]
        with epoch_scope(p["policy_epoch"]):
            async with mode_execution_scope(self.journal, ticket["run_id"]):
                await self._authority(ticket, record, executing=False)
                prepared = await prepare_final_outcome(p["run"], p["task"], entries[: record["sequence"] - 1])
                if prepared.digest != p["candidate"]["final_outcome_digest"]:
                    raise ValueError("Known final immutable projection changed")
                cp = checkpoint_payload(prepared.data["checkpoint"]["payload"])
                if await workspace_cut(self.store, self.workspace(ticket["run_id"])) != cp.invocation.workspace:
                    raise ValueError("Known final source changed before claim")
                async with self.store._unit():
                    locked = await self.journal._run(ticket["run_id"])
                    row = await self.requests.records.find_by_id(
                        identifier(ticket["id"]), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if row is None or row.status != "queued":
                        return
                    if row.data != ticket:
                        raise ValueError("Queued final outcome changed")
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
                        dict(
                            generation=row.data["generation"],
                            owner_pid=row.data["owner_pid"],
                            request_digest=row.data["request_digest"],
                        ),
                    )
                    ticket = copy.deepcopy(row.data)
                execution = FinalOutcomeExecution(self, ticket, record, prepared)
                await self.engine.continue_agent_checkpoint(execution)
                run = await self.store.get_run(ticket["run_id"])
                if run.get("bridge_completion_hold"):
                    raise ValueError("Known final result has no native terminal acceptance")
                await self.continuities._finish(
                    ticket["id"],
                    "completed",
                    dict(
                        run_status=run["status"],
                        same_task_attempt=p["task"]["attempt"],
                        outcome=p["candidate"]["outcome"],
                    ),
                )
