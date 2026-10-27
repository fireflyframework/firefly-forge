# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One-use native migration tickets; consumed responses are never exposed again."""

from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import text

from firefly_forge.agent_checkpoint_models import InvocationBinding
from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
from firefly_forge.agentic_checkpoint_invocation import invocation_cut, runtime_contract, workspace_cut
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.execution_request_persistence import ForgeExecutionRequestEntity
from firefly_forge.invalid_read_continuity_models import InvalidReadAdmissionInputV1
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, lease_window, policy_snapshot
from firefly_forge.invalid_read_settlements import CLAIM, KIND, SETTLEMENT, settled_requests
from firefly_forge.journal import digest
from firefly_forge.known_worker_completions import _entries, _task
from firefly_forge.project_access import ProjectAccess
from firefly_forge.store import identifier, now

Data = dict[str, Any]


def protected_scope(run: Data) -> str:
    from firefly_forge.unknown_command_proof import continuation_scope

    return digest(
        {
            "grants": continuation_scope(run),
            **{
                name: run.get(name)
                for name in (
                    "attempt",
                    "repair_attempts",
                    "verification_handoff",
                )
            },
        }
    )


def ticket_seal(ticket: Data) -> str:
    return digest(
        {
            key: value
            for key, value in ticket.items()
            if key
            not in {
                "status",
                "generation",
                "owner_pid",
                "result",
                "updated_at",
                "admission_seal",
            }
        }
    )


def verify_ticket(ticket: Data) -> None:
    if ticket_seal(ticket) != ticket.get("admission_seal"):
        raise ValueError("Continuity admission seal changed")


class InvalidReadExecution:
    """A new ownership epoch for the same task, not an OriginalCompletion replay."""

    resume_without_original_response = True

    def __init__(self, service: Any, ticket: Data) -> None:
        self.service, self.ticket = service, copy.deepcopy(ticket)
        self.run_id = ticket["run_id"]
        self.binding = InvocationBinding.model_validate(ticket["binding"])
        self.checkpoint = BoundaryCheckpoint.decode(ticket["checkpoint"].encode(), ticket["checkpoint_digest"])
        self.intent = {"payload": {"task_key": ticket["task_key"], "task_attempt": self.binding.round}}

    async def guard(self) -> None:
        await self.service.guard(self.ticket)

    async def guard_source(self) -> None:
        _outside_unit()
        await self.guard()
        if (
            await workspace_cut(self.service.store, self.service.engine.workspaces / self.run_id)
            != self.binding.workspace
        ):
            raise ValueError("Continuity source changed before the next request")
        await self.guard()

    async def receipt(self, engine: Any, key: str, evidence: Data, checkpoint: Data) -> None:
        await self.guard()
        await engine._receipt(self.run_id, key, evidence, checkpoint=checkpoint)


class InvalidReadTickets:
    store: Any
    journal: Any
    engine: Any
    requests: Any
    restores: Any

    async def _prepare(self, run_id: str) -> Data:
        raise NotImplementedError

    async def _actor(self, run_id: str) -> Any:
        raise NotImplementedError

    async def _command_cut(self, run: Data, task: Data, entries: list[Data]) -> str:
        raise NotImplementedError

    async def get(self, run_id: str, request_id: str) -> Data:
        _outside_unit()
        await self._actor(run_id)
        ticket = await self.requests.get(request_id)
        verify_ticket(ticket)
        if (
            ticket["kind"] != KIND
            or ticket["run_id"] != identifier(run_id).hex
            or digest(ticket["request"]) != ticket["request_digest"]
        ):
            raise ValueError("Continuity ticket identity differs")
        events = await self.requests.events(request_id)
        if not events or not self.requests.verify_chain(events) or any(e["request_id"] != request_id for e in events):
            raise ValueError("Continuity ticket has no intact native receipt chain")
        if events[0]["kind"] != "request_admitted" or events[0]["evidence"] != {
            "request_digest": ticket["request_digest"],
            "admission_seal": ticket["admission_seal"],
        }:
            raise ValueError("Continuity admission receipt changed")
        last = events[-1]
        if ticket["status"] == "queued":
            valid = last["kind"] == "request_admitted" and ticket["generation"] is None and ticket["result"] is None
        elif ticket["status"] == "executing":
            valid = last["kind"] == "worker_claim" and last["evidence"] == {
                "generation": ticket["generation"],
                "request_digest": ticket["request_digest"],
                "owner_pid": ticket["owner_pid"],
            }
        else:
            valid = last["kind"] == "worker_" + ticket["status"] and last["evidence"] == ticket["result"]
        if not valid:
            raise ValueError("Continuity ticket outcome differs from its immutable receipts")
        await self.store.admit_credentials(ticket)
        await self._actor(run_id)
        self.store.reject_credentials(ticket)
        return copy.deepcopy(ticket)

    async def request(self, run_id: str, request_id: str, body: InvalidReadAdmissionInputV1) -> Data:
        _outside_unit()
        if identifier(request_id).hex != request_id or identifier(run_id).hex != run_id:
            raise ValueError("Use exact lowercase native continuity run and request identities")
        body = InvalidReadAdmissionInputV1.model_validate(body.model_dump())
        actor = await self._actor(run_id)
        incoming = {
            "run_id": run_id,
            "actor": ProjectAccess.actor_snapshot(actor),
            "input": body.model_dump(mode="json"),
        }
        old = await self.requests.get(request_id) if await self._exists(request_id) else None
        if old is not None:
            if old["request"] != incoming or old["kind"] != KIND:
                raise ValueError("Continuity identity belongs to another request or actor")
            return await self.get(run_id, request_id)
        try:
            prepared = await self._prepare(run_id)
        except ValueError:
            retained = await self.requests.get(request_id) if await self._exists(request_id) else None
            if retained is not None and retained["kind"] == KIND and retained["request"] == incoming:
                return await self.get(run_id, request_id)
            raise
        candidate = prepared["candidate"]
        expected = (
            candidate["digest"],
            candidate["run_digest"],
            candidate["journal_digest"],
            candidate["lease_policy"]["digest"],
        )
        if (
            body.candidate_digest,
            body.expected_run_digest,
            body.expected_journal_digest,
            body.expected_policy_digest,
        ) != expected:
            raise ValueError("The reviewed continuity candidate changed")
        if candidate["admission"] != "READY":
            raise ValueError("Continuity lease policy or current qualified profile is unavailable")
        await self.store.admit_credentials([incoming, candidate, prepared["checkpoint"]])
        if await self._actor(run_id) != actor:
            raise ValueError("Continuity operator changed before native admission")
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:execution-request-admission:" + self.store.schema},
            )
            await self.restores.completions._writer(prepared["run"], actor)
            locked = await self.journal._run(run_id)
            old_row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if old_row is not None:
                if old_row.kind != KIND or old_row.data["request"] != incoming:
                    raise ValueError("Concurrent continuity identity conflict")
            else:
                current = await self.store.get_run(run_id)
                task = await self.journal._task(run_id, "implement")
                entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
                if (
                    current != prepared["run"]
                    or _task(task) != prepared["task"]
                    or entries != prepared["entries"]
                    or self.store.configuration.settings_generation is not prepared["generation"]
                    or policy_snapshot(self.store.configuration) != candidate["lease_policy"]
                ):
                    raise ValueError("Continuity admission source, configuration or policy changed")
                await prepared["read_guard"]()
                await self.restores.access(current, incoming["actor"])
                if current.get("cancel_request", {}).get("status") == "requested":
                    raise ValueError("Cancelled run cannot receive a continuity lease")
                if await self.requests.records.for_key(identifier(run_id), KIND, candidate["proof"]["hold_request_id"]):
                    raise ValueError("The original consumed graph already has a migration ticket")
                queue_policy = ContinuityLeasePolicyV1.model_validate(candidate["lease_policy"]["settings"])
                if (
                    await self.requests.records.count_for_run(identifier(run_id), KIND)
                    >= queue_policy.max_tickets_per_run
                    or await self.requests.records.pending_count() >= queue_policy.max_pending_tickets
                ):
                    raise ValueError("The bounded continuity queue is full")
                lease = lease_window(
                    ContinuityLeasePolicyV1.model_validate(candidate["lease_policy"]["settings"]),
                    frozen_timeout_seconds=candidate["frozen_timeout_seconds"],
                    valid_for_seconds=body.valid_for_seconds,
                    now_ms=int(time.time() * 1000),
                    original_deadline_ms=candidate["original_deadline_ms"],
                    active_deadline_ms=candidate["active_deadline_ms"],
                )
                generation = uuid.uuid4().hex
                binding = {
                    **prepared["binding"],
                    "invocation_id": generation,
                    "ordinal": 1,
                    "predecessor": None,
                    "deadline_ms": lease["expires_at_ms"],
                    "contract": candidate["current_runtime_contract"],
                }
                payload = dict(
                    version=1,
                    request_id=request_id,
                    proof=candidate["proof"],
                    policy_epoch=candidate["policy_epoch"],
                    request=body.model_dump(),
                    lease=lease,
                    lease_policy=candidate["lease_policy"],
                    frozen_timeout_seconds=candidate["frozen_timeout_seconds"],
                    candidate_digest=candidate["digest"],
                    reconstruction=candidate["reconstruction"],
                    current_profile=candidate["current_profile"],
                    actor=incoming["actor"],
                    original_binding=prepared["binding"],
                    binding=binding,
                    observed_hold=copy.deepcopy(current["bridge_completion_hold"]),
                    checkpoint_digest=prepared["checkpoint_digest"],
                    command_custody_digest=candidate["command_custody_digest"],
                    settings_digest=prepared["settings_digest"],
                    protected_scope_digest=protected_scope(current),
                )
                payload["digest"] = digest(payload)
                extra = len(json.dumps(payload).encode()) + len(prepared["checkpoint"].encode()) + 16384
                await self.journal._bridge_read_bound(run_id, extra=extra)
                settlement = await self.journal._append(
                    run_id, "implement", "invalid-read:" + request_id, SETTLEMENT, payload
                )
                claim_payload = dict(
                    version=1,
                    request_id=request_id,
                    task_key=task.data["idempotency_key"],
                    attempt=task.attempt,
                    settlement_digest=settlement["digest"],
                    lease_generation=generation,
                    expires_at_ms=lease["expires_at_ms"],
                )
                claim = await self.journal._append(
                    run_id, "implement", "invalid-claim:" + request_id, CLAIM, claim_payload
                )
                task.data = {**task.data, "invalid_read_claim_digest": claim["digest"]}
                await self.journal.tasks.save(task)
                locked.data = {
                    **locked.data,
                    "bridge_completion_hold": {},
                    "invalid_read_continuity": {
                        "request_id": request_id,
                        "settlement_digest": settlement["digest"],
                        "claim_digest": claim["digest"],
                        "lease_generation": generation,
                    },
                }
                await self.store.run_records.save(locked)
                data = dict(
                    id=request_id,
                    run_id=run_id,
                    kind=KIND,
                    status="queued",
                    request=incoming,
                    request_digest=digest(incoming),
                    generation=None,
                    owner_pid=None,
                    result=None,
                    lease=lease,
                    lease_generation=generation,
                    binding=binding,
                    checkpoint=prepared["checkpoint"],
                    checkpoint_digest=prepared["checkpoint_digest"],
                    task_key=prepared["task"]["idempotency_key"],
                    migration_digest=settlement["digest"],
                    claim_digest=claim["digest"],
                    current_profile=candidate["current_profile"],
                    command_custody_digest=candidate["command_custody_digest"],
                    lease_policy=candidate["lease_policy"],
                    settings_digest=prepared["settings_digest"],
                    policy_epoch=candidate["policy_epoch"],
                    protected_scope_digest=protected_scope(current),
                    created_at=now(),
                    updated_at=now(),
                )
                data["admission_seal"] = ticket_seal(data)
                row = ForgeExecutionRequestEntity(
                    id=identifier(request_id),
                    run_id=identifier(run_id),
                    kind=KIND,
                    request_key=candidate["proof"]["hold_request_id"],
                    status="queued",
                    data=data,
                )
                await self.requests.records.save(row)
                await self.requests._event(
                    row,
                    "request_admitted",
                    {
                        "request_digest": data["request_digest"],
                        "admission_seal": data["admission_seal"],
                    },
                )
                if policy_snapshot(self.store.configuration) != candidate["lease_policy"]:
                    raise ValueError("Continuity policy changed before commit")
                await self.restores.access(await self.store.get_run(run_id), incoming["actor"])
                if await self._actor(run_id) != actor:
                    raise ValueError("Continuity operator changed before commit")
                self.store.reject_credentials([payload, data])
        return await self.get(run_id, request_id)

    async def _exists(self, request_id: str) -> bool:
        async with self.store._unit():
            return await self.requests.records.find_by_id(identifier(request_id)) is not None

    async def guard(self, ticket: Data) -> None:
        await self.store.assert_ownership()
        current = await self.requests.get(ticket["id"])
        verify_ticket(current)
        verify_ticket(ticket)
        if (
            current["kind"] != KIND
            or current["status"] != "executing"
            or current["generation"] != ticket["generation"]
            or current.get("owner_pid") != getattr(self.store, "_ownership_pid", None)
            or current["request_digest"] != ticket["request_digest"]
            or digest(current["request"]) != ticket["request_digest"]
            or current["binding"] != ticket["binding"]
            or current["checkpoint_digest"] != ticket["checkpoint_digest"]
            or current["lease"] != ticket["lease"]
            or current["migration_digest"] != ticket["migration_digest"]
            or current["admission_seal"] != ticket["admission_seal"]
        ):
            raise ValueError("Continuity ownership generation changed")
        await self._authority(ticket)
        run = await self.store.get_run(ticket["run_id"])
        verify_ticket(ticket)
        if run["status"] != "developing":
            raise ValueError("Continuity task is no longer executing")
        async with self.store._unit():
            task = await self.journal._effect_task(ticket["run_id"], ticket["task_key"])
            if task.status != "running" or task.attempt != ticket["binding"]["round"]:
                raise ValueError("Continuity original task attempt changed")

    async def _authority(self, ticket: Data) -> None:
        from firefly_forge.checkpoint_mode_runtime import effective_revision
        from firefly_forge.invalid_read_continuities import current_profile_seal

        verify_ticket(ticket)
        events = await self.requests.events(ticket["id"])
        if (
            not events
            or not self.requests.verify_chain(events)
            or events[0]["kind"] != "request_admitted"
            or events[0]["evidence"]
            != {"request_digest": ticket["request_digest"], "admission_seal": ticket["admission_seal"]}
        ):
            raise ValueError("Continuity admission seal lost its native receipt")
        run = await self.store.get_run(ticket["run_id"])
        await self.restores.access(run, ticket["request"]["actor"])
        # File inspection stays outside the native writer; the claim still compares the sealed preflight below.
        if current_unit_of_work() is None and (
            runtime_contract() != ticket["binding"]["contract"] or current_profile_seal() != ticket["current_profile"]
        ):
            raise ValueError("The qualified continuity runtime changed")
        marker = run.get("invalid_read_continuity") or {}
        if (
            marker
            != {
                "request_id": ticket["id"],
                "settlement_digest": ticket["migration_digest"],
                "claim_digest": ticket["claim_digest"],
                "lease_generation": ticket["lease_generation"],
            }
            or run.get("cancel_request", {}).get("status") == "requested"
            or invocation_cut(run) != ticket["binding"]["run_cut"]
            or protected_scope(run) != ticket["protected_scope_digest"]
            or int(time.time() * 1000) >= ticket["lease"]["expires_at_ms"]
            or policy_snapshot(self.store.configuration) != ticket["lease_policy"]
            or digest(self.store.configuration.load_snapshot(effective_revision(run))) != ticket["settings_digest"]
            or digest(self.store.configuration.resolve("bridge")) != ticket["settings_digest"]
        ):
            raise ValueError("Current continuity lease, policy, source scope or capability authority changed")
        async with self.store._unit():
            entries = _entries(await self.journal.records.find_for_run(identifier(run["id"])), run["id"])
            task = _task(await self.journal._task(run["id"], "implement"))
        settlements = [e for e in entries if e["kind"] == SETTLEMENT and e["digest"] == ticket["migration_digest"]]
        if len(settlements) != 1:
            raise ValueError("Continuity migration receipt is missing")
        settled_requests(settlements[0], entries)
        receipt = settlements[0]["payload"]
        for key in (
            "binding",
            "checkpoint_digest",
            "command_custody_digest",
            "settings_digest",
            "protected_scope_digest",
            "lease",
            "lease_policy",
            "current_profile",
            "policy_epoch",
        ):
            if receipt[key] != ticket[key]:
                raise ValueError("Continuity ticket differs from its immutable native settlement")
        if await self._command_cut(run, task, entries) != ticket["command_custody_digest"]:
            raise ValueError("Original command or unknown-effect custody changed")
        if self.engine.tool_broker is not None and await self.engine.tool_broker.unresolved(run["id"]):
            raise ValueError("A native tool requires reconciliation before continuity")

    async def execute(self, request_id: str) -> None:
        self.requests.runtime.require_execution()
        ticket = await self.requests.get(request_id)
        if ticket["kind"] != KIND or ticket["status"] != "queued":
            return
        if not await self.engine.reserve_agent_restore(ticket["run_id"]):
            return
        try:
            await self._execute(ticket)
        except BaseException as error:
            current = await self.requests.get(request_id)
            await asyncio.shield(
                self._finish(
                    request_id,
                    "failed" if current["status"] == "queued" else "needs_reconciliation",
                    {"detail": "Continuity stopped; inspect its one-use lease without replay"},
                )
            )
            if not isinstance(error, Exception):
                raise
        finally:
            self.engine.release_agent_restore(ticket["run_id"])

    async def _execute(self, ticket: Data) -> None:
        from firefly_forge.invalid_read_epochs import epoch_scope, epoch_snapshot

        run = await self.store.get_run(ticket["run_id"])
        async with self.store._unit():
            entries = _entries(await self.journal.records.find_for_run(identifier(run["id"])), run["id"])
        epoch = epoch_snapshot(self.store, run, entries)
        if epoch != ticket["policy_epoch"]:
            raise ValueError("The reviewed historical policy epoch changed before execution")
        with epoch_scope(epoch):
            await self._execute_owned(ticket)

    async def _execute_owned(self, ticket: Data) -> None:
        from firefly_forge.checkpoint_mode_runtime import mode_execution_scope

        async with mode_execution_scope(self.journal, ticket["run_id"]):
            await self._authority(ticket)
            if (
                await workspace_cut(self.store, self.engine.workspaces / ticket["run_id"])
                != ticket["binding"]["workspace"]
            ):
                raise ValueError("Continuity source changed before claim")
            async with self.store._unit():
                locked = await self.journal._run(ticket["run_id"])
                row = await self.requests.records.find_by_id(identifier(ticket["id"]), lock=LockMode.PESSIMISTIC_WRITE)
                if row is None or row.status != "queued":
                    return
                if row.data != ticket:
                    raise ValueError("Queued continuity ticket changed")
                await self.restores.completions._writer(await self.store.get_run(ticket["run_id"]))
                await self._authority(ticket)
                task = await self.journal._effect_task(ticket["run_id"], ticket["task_key"])
                if task.attempt != ticket["binding"]["round"] or task.status not in {"running", "needs_reconciliation"}:
                    raise ValueError("Only the same unfinished task attempt may claim continuity")
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
            execution = InvalidReadExecution(self, ticket)
            await execution.guard_source()
            await self.engine.continue_agent_checkpoint(execution)
            run = await self.store.get_run(ticket["run_id"])
            if run.get("bridge_completion_hold") or run["status"] not in {"paused", "succeeded", "validating"}:
                raise ValueError("Continuity did not reach a known native terminal boundary")
            await self._finish(
                ticket["id"],
                "completed",
                {"run_status": run["status"], "same_task_attempt": ticket["binding"]["round"]},
            )

    async def _finish(self, request_id: str, status: str, result: Data) -> None:
        async with self.store._unit():
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.kind != KIND or row.status not in {"queued", "executing"}:
                return
            if status == "needs_reconciliation":
                run = await self.journal._run(row.run_id.hex)
                task = await self.journal._effect_task(row.run_id.hex, row.data["task_key"])
                if task.status == "running":
                    task.status = "needs_reconciliation"
                    await self.journal.tasks.save(task)
                if run.status not in {"paused", "succeeded", "cancelled", "failed"}:
                    run.status = "needs_reconciliation"
                    run.data = {**run.data, "status": run.status}
                    await self.store.run_records.save(run)
            row.status = status
            row.data = {**row.data, "status": status, "result": result, "updated_at": now()}
            await self.requests.records.save(row)
            await self.requests._event(row, "worker_" + status, result)

    async def recover(self) -> None:
        async with self.store._unit():
            tickets = [copy.deepcopy(row.data) for row in await self.requests.records.state("executing", kind=KIND)]
        for ticket in tickets:
            await self._finish(
                ticket["id"],
                "needs_reconciliation",
                {"detail": "Ownership ended; never requeue a claimed continuity lease"},
            )
