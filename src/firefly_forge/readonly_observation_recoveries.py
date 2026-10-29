# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One separately reviewed unavailable read on an existing consumed continuity ticket."""

from __future__ import annotations

import copy
import re
import time
import uuid
from typing import Any, cast

from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import text

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2, InvocationBinding
from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
from firefly_forge.agentic_checkpoint_invocation import invocation_cut, runtime_contract, workspace_cut
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.checkpoint_mode_runtime import effective_revision, mode_execution_scope
from firefly_forge.invalid_read_continuity import _bytes
from firefly_forge.invalid_read_epochs import epoch_scope, epoch_snapshot
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, lease_window, policy_snapshot
from firefly_forge.invalid_read_recoveries import MUTABLE
from firefly_forge.invalid_read_recovery_events import read_recovery_events
from firefly_forge.invalid_read_settlements import SETTLEMENT, settled_requests
from firefly_forge.invalid_read_tickets import InvalidReadExecution, protected_scope, verify_ticket
from firefly_forge.journal import Journal, digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.provider_rejection_recoveries import _metadata_suffix, validate_rejection_recovery_record
from firefly_forge.readonly_observation_models import ReadonlyObservationRecoveryInputV1
from firefly_forge.readonly_observation_reconstruction import (
    observation_projection,
    prepare_observation,
    reconstruct_observation,
)
from firefly_forge.store import identifier, now

Data = dict[str, Any]
OBSERVATION_RECOVERY = "readonly_observation_recovery"
OBSERVATION_ADMITTED = "request_recovered_after_readonly_observation"
_WITNESS_FIELDS = {
    "version",
    "kind",
    "run_id",
    "request_id",
    "recovery_id",
    "actor",
    "administrator",
    "request",
    "candidate",
    "failed_ticket",
    "events",
    "binding",
    "lease",
    "lease_policy",
    "policy_epoch",
    "checkpoint",
    "checkpoint_digest",
    "read_proof_digest",
    "source_run",
    "source_task",
    "digest",
}


def _run_authority(run: Data) -> str:
    return digest({k: v for k, v in run.items() if k not in {"updated_at", "verification_refusal"}})


def _boundary(ticket: Data, entries: list[Data], events: list[Data]) -> tuple[Data, Data]:
    if len(events) != 8 or ticket["status"] != "needs_reconciliation":
        raise ValueError("Readonly observation recovery requires the complete claimed predecessor protocol")
    prior = validate_rejection_recovery_record(ticket, entries, events)
    if prior is None or [e["kind"] for e in events[-2:]] != ["worker_claim", "worker_needs_reconciliation"]:
        raise ValueError("The original claimed predecessor has no exact native receipt")
    request_id = next(e["payload"]["request_id"] for e in reversed(entries) if e["kind"] == "agent_consume")
    rows = [e for e in entries if e["idempotency_key"] == request_id]
    intent = next(e for e in rows if e["kind"] == "bridge_request_intent")
    checkpoint = next(e for e in rows if e["kind"] == "agent_checkpoint")
    if checkpoint["payload"]["invocation"] != prior["payload"]["binding"]:
        raise ValueError("Consumed read does not belong to the exact predecessor ownership epoch")
    return intent, checkpoint


def settled_observation(entry: Data, entries: list[Data]) -> list[str]:
    """Close only the rejected request; never return or manufacture a model/tool result."""
    p = entry["payload"]
    if (
        not Journal.verify_chain(entries)
        or entry not in entries
        or type(p) is not dict
        or set(p) != _WITNESS_FIELDS
        or type(p["version"]) is not int
        or p["version"] != 1
        or p["kind"] != "reviewed_readonly_observation_continuation"
        or entry["kind"] != OBSERVATION_RECOVERY
        or entry["task_id"] != "implement"
        or entry["run_id"] != p["run_id"]
        or entry["idempotency_key"] != "readonly-observation:" + p["request_id"]
        or digest({k: v for k, v in p.items() if k != "digest"}) != p["digest"]
    ):
        raise ValueError("Readonly-observation settlement has invalid native custody")
    ticket = p["failed_ticket"]
    prefix = [e for e in entries if e["sequence"] < entry["sequence"]]
    intent, checkpoint_record = _boundary(ticket, prefix, p["events"])
    body = ReadonlyObservationRecoveryInputV1.model_validate(p["request"])
    prepared = prepare_observation(
        cast(Data, p["source_run"]),
        cast(Data, p["source_task"]),
        prefix,
        accepted_runtime=checkpoint_record["payload"]["invocation"]["contract"],
    )
    original_read = prepared.checkpoint
    derived = BoundaryCheckpoint.decode(p["checkpoint"].encode(), p["checkpoint_digest"])
    expected_history, expected_usage = observation_projection(prepared)
    full_history = [*original_read.record.history, *expected_history]
    if (
        digest(prepared.data) != p["read_proof_digest"]
        or _bytes(derived.record.history) != _bytes(full_history)
        or _bytes(derived.record.request_history) != _bytes(full_history)
        or _bytes(derived.record.usage) != _bytes(expected_usage)
        or _bytes(derived.record.contract) != _bytes(original_read.record.contract)
        or _bytes(derived.state) != _bytes(original_read.state)
        or _bytes(derived.record.limits) != _bytes(original_read.record.limits)
        or len(derived.history) != len(original_read.history) + 2
        or derived.usage.requests != original_read.usage.requests + 1
        or derived.usage.tool_calls != original_read.usage.tool_calls + len(prepared.data["outcomes"])
    ):
        raise ValueError("Readonly observation derivation changed original history, usage or limits")
    from openai.types.chat import ChatCompletion
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart

    response = ChatCompletion.model_validate_json(prepared.data["response"], strict=True)
    original_calls = response.choices[0].message.tool_calls
    model_response = derived.history[-2]
    if (
        not isinstance(model_response, ModelResponse)
        or original_calls is None
        or len(model_response.parts) != len(original_calls)
        or any(
            not isinstance(part, ToolCallPart)
            or call.type != "function"
            or part.tool_name != call.function.name
            or part.tool_call_id != call.id
            or part.args != call.function.arguments
            for part, call in zip(model_response.parts, original_calls, strict=True)
        )
        or response.usage is None
        or derived.usage.input_tokens != original_read.usage.input_tokens + response.usage.prompt_tokens
        or derived.usage.output_tokens != original_read.usage.output_tokens + response.usage.completion_tokens
    ):
        raise ValueError("Readonly observation reconstruction changed the original response or token accounting")
    outcomes = prepared.data["outcomes"]
    parts = derived.history[-1].parts
    returns = [part for part in parts if isinstance(part, ToolReturnPart)]
    if (
        len(returns) != len(parts)
        or len(returns) != len(outcomes)
        or len({part.tool_call_id for part in returns}) != len(outcomes)
        or any(
            part.tool_call_id not in outcomes
            or part.tool_name != outcomes[part.tool_call_id]["name"]
            or _bytes(part.content) != _bytes(outcomes[part.tool_call_id]["result"])
            for part in returns
        )
    ):
        raise ValueError("Readonly observation history does not preserve the exact known and unavailable results")
    c = p["candidate"]
    policy = p["lease_policy"]
    configured = ContinuityLeasePolicyV1.model_validate(policy["settings"])
    original = next(
        e["payload"] for e in prefix if e["kind"] == SETTLEMENT and e["digest"] == ticket["migration_digest"]
    )
    original_record = next(e for e in prefix if e["kind"] == SETTLEMENT and e["digest"] == ticket["migration_digest"])
    settled_requests(original_record, prefix)
    expected_lease = lease_window(
        configured,
        frozen_timeout_seconds=original["frozen_timeout_seconds"],
        valid_for_seconds=body.valid_for_seconds,
        now_ms=p["lease"]["issued_at_ms"],
        original_deadline_ms=original["proof"]["deadline_ms"],
        active_deadline_ms=p["lease"]["active_deadline_ms"],
    )
    binding = InvocationBinding.model_validate(p["binding"]).model_dump()
    pending = CheckpointPayloadV2.model_validate(checkpoint_record["payload"])
    expected_binding = {
        **pending.invocation.model_dump(),
        "invocation_id": binding["invocation_id"],
        "contract": c["current_runtime_contract"],
        "deadline_ms": expected_lease["expires_at_ms"],
    }
    checkpoint = derived
    if (
        p["run_id"] != ticket["run_id"]
        or p["request_id"] != ticket["id"]
        or p["actor"] != ticket["request"]["actor"]
        or set(p["administrator"]) != set(p["actor"]) | {"identity_digest"}
        or any(p["administrator"].get(k) != v for k, v in p["actor"].items())
        or c["version"] != 1
        or type(c["version"]) is not int
        or c["kind"] != "readonly_observation_recovery_candidate"
        or digest(p["source_run"]) != c["run_digest"]
        or digest(p["source_task"]) != c["task_digest"]
        or c["run_id"] != ticket["run_id"]
        or c["request_id"] != ticket["id"]
        or c["execution_authorized"] is not False
        or c["admission"] != "READY"
        or c["holds"] != []
        or c["review_basis"] != "administrator_reviewed_unavailable_read"
        or digest({k: v for k, v in c.items() if k != "digest"}) != c["digest"]
        or c["digest"] != body.candidate_digest
        or c["original_admission_seal"] != ticket["admission_seal"]
        or c["failed_event_digest"] != p["events"][-1]["digest"]
        or c["events_digest"] != digest({"events": p["events"]})
        or c["journal_digest"] != prefix[-1]["digest"]
        or entry["previous_hash"] != prefix[-1]["digest"]
        or c["intent_digest"] != intent["digest"]
        or c["checkpoint_record_digest"] != checkpoint_record["digest"]
        or c["checkpoint_digest"] != original_read.digest
        or c["read_proof_digest"] != p["read_proof_digest"]
        or c["source_digest"] != digest(ticket["binding"]["workspace"])
        or c["policy_digest"] != policy["digest"]
        or digest({k: v for k, v in policy.items() if k != "digest"}) != policy["digest"]
        or not policy["revision"]
        or not configured.enabled
        or not configured.allow_readonly_observation_recovery
        or original["proof"]["version"] == 2
        and not configured.allow_unavailable_source_observations
        or c["frozen_timeout_seconds"] != original["frozen_timeout_seconds"]
        or not 0 < body.valid_for_seconds <= c["maximum_window_seconds"]
        or c["usage"]["requests"] != checkpoint.usage.requests
        or c["usage"]["tool_calls"] != checkpoint.usage.tool_calls
        or c["usage"]["request_limit"] != checkpoint.record.limits["request_limit"]
        or c["usage"]["tool_calls_limit"] != checkpoint.record.limits["tool_calls_limit"]
        or p["lease"] != expected_lease
        or binding != expected_binding
        or binding["invocation_id"] == pending.invocation.invocation_id
    ):
        raise ValueError("Readonly-observation witness changed its reviewed scope, budgets or native evidence")
    old_epoch = original["policy_epoch"]
    epoch = p["policy_epoch"]
    if old_epoch is None:
        if epoch is not None:
            raise ValueError("A readonly-observation continuation cannot invent a legacy policy epoch")
    elif (
        epoch is None
        or any(
            epoch.get(k) != v
            for k, v in old_epoch.items()
            if k not in {"current", "current_profile", "lease_policy_digest", "digest"}
        )
        or epoch["current_profile"] != c["current_profile"]
        or epoch["lease_policy_digest"] != policy["digest"]
        or digest({k: v for k, v in epoch.items() if k != "digest"}) != epoch["digest"]
    ):
        raise ValueError("The readonly-observation legacy epoch has no exact current authority")
    return [intent["idempotency_key"]]


class ReadonlyObservationExecution(InvalidReadExecution):
    def __init__(self, service: ReadonlyObservationRecoveries, ticket: Data, record: Data) -> None:
        self.service = service
        self.run_id = ticket["run_id"]
        self.request_id, self.record_digest, self.generation = ticket["id"], record["digest"], ticket["generation"]
        self.binding = InvocationBinding.model_validate(record["payload"]["binding"])
        self.checkpoint = BoundaryCheckpoint.decode(
            record["payload"]["checkpoint"].encode(), record["payload"]["checkpoint_digest"]
        )
        self.intent = {"payload": {"task_key": ticket["task_key"], "task_attempt": self.binding.round}}

    async def guard(self) -> None:
        await self.service.guard(self.request_id, self.record_digest, self.generation)


class ReadonlyObservationRecoveries:
    def __init__(self, continuities: Any) -> None:
        self.continuities, self.base = continuities, continuities.recoveries
        self.store, self.journal, self.engine, self.requests = (
            continuities.store,
            continuities.journal,
            continuities.engine,
            continuities.requests,
        )

    async def _events(self, request_id: str) -> list[Data]:
        return await read_recovery_events(self.requests, request_id, maximum=11)

    @staticmethod
    def _matches(ticket: Data, entries: list[Data]) -> list[Data]:
        return [
            e
            for e in entries
            if e["kind"] == OBSERVATION_RECOVERY
            and (
                e["idempotency_key"] == "readonly-observation:" + ticket["id"]
                or e["payload"].get("request_id") == ticket["id"]
            )
        ]

    async def has(self, ticket: Data) -> bool:
        _, _, entries = await self.base._native(ticket["run_id"])
        return bool(self._matches(ticket, entries))

    async def _prepare(self, run_id: str, request_id: str) -> Data:
        _outside_unit()
        ticket = await self.base._ticket(run_id, request_id)
        actor = await self.base._operator(run_id, ticket)
        administrator = await self.base._administrator(actor)
        run, task, entries = await self.base._native(run_id)
        settlement = self.base._admission(ticket, run, task, entries)
        events = await self._events(request_id)
        intent, record = _boundary(ticket, entries, events)
        prepared_read = prepare_observation(
            run, task, entries, accepted_runtime=record["payload"]["invocation"]["contract"]
        )
        if self._matches(ticket, entries):
            raise ValueError("This request already has its one-use readonly-observation continuation")
        active = self.engine.active.get(run_id)
        if (
            self.engine.stopping
            or active is not None
            and not active.done()
            or run["status"] != "needs_reconciliation"
            or task["status"] != "needs_reconciliation"
            or run.get("bridge_completion_hold")
            != {
                "version": 1,
                "reason": "graph_checkpoint_unavailable",
                "request_id": intent["idempotency_key"],
                "intent_digest": intent["digest"],
            }
        ):
            raise ValueError("Readonly-observation recovery requires the exact idle held original task")
        annotations = [
            e for e in entries if e["sequence"] > record["sequence"] and e["kind"] == "verification_handoff_refused"
        ]
        if annotations and (
            run.get("verification_refusal") != annotations[0]["payload"]
            or digest(run.get("worker_request", {})) != annotations[0]["payload"]["request_digest"]
        ):
            raise ValueError("The non-effect refusal annotation has no exact current worker request")
        async with self.store._unit():
            if await self.requests.records.active_for_run(identifier(run_id)):
                raise ValueError("An active native request prevents readonly-observation recovery")
            commands = await self.journal.sandboxes.for_run(identifier(run_id))
        policy = policy_snapshot(self.store.configuration)
        configured = ContinuityLeasePolicyV1.model_validate(policy["settings"])
        settings_generation = self.store.configuration.settings_generation
        from firefly_forge.invalid_read_continuities import current_profile_seal

        holds = []
        if not policy["revision"] or not configured.enabled:
            holds.append("lease_policy_disabled")
        if not configured.allow_readonly_observation_recovery:
            holds.append("readonly_observation_recovery_disabled")
        if settlement["payload"]["proof"]["version"] == 2 and not configured.allow_unavailable_source_observations:
            holds.append("unavailable_observation_policy_disabled")
        try:
            profile = current_profile_seal()
        except ValueError:
            profile = None
            holds.append("current_profile_unqualified")
        runtime = runtime_contract()
        settings = self.store.configuration.load_snapshot(effective_revision(run))
        if (
            digest(settings) != ticket["settings_digest"]
            or digest(self.store.configuration.resolve("bridge")) != ticket["settings_digest"]
        ):
            raise ValueError("The original frozen provider configuration changed")
        selected = settlement["payload"]["proof"]["runtime_contract"]
        epoch = (
            epoch_snapshot(self.store, run, entries, legacy_runtime_contract=selected)
            if profile and configured.enabled
            else None
        )

        async def current() -> None:
            await self.continuities.restores.access(run, actor)
            if await self.base._administrator(actor) != administrator:
                raise AccessDenied("The reviewing administrator changed")
            principal = CURRENT_PRINCIPAL.get()
            if principal is None or ProjectAccess.actor_snapshot(principal) != actor:
                raise AccessDenied("The reviewing operator changed")
            if (
                await self.base._native(run_id) != (run, task, entries)
                or await self.base._ticket(run_id, request_id) != ticket
                or await self._events(request_id) != events
                or self.store.configuration.settings_generation is not settings_generation
                or policy_snapshot(self.store.configuration) != policy
                or digest(self.store.configuration.resolve("bridge")) != ticket["settings_digest"]
                or (
                    current_unit_of_work() is None
                    and (runtime_contract() != runtime or profile is not None and current_profile_seal() != profile)
                )
            ):
                raise ValueError("Readonly-observation native read custody changed")
            async with self.store._unit():
                if await self.requests.records.active_for_run(identifier(run_id)):
                    raise ValueError("Another worker claim prevents readonly-observation recovery")

        with epoch_scope(epoch):
            await self.base._custody(ticket, run, task, entries, settlement)
            source = await workspace_cut(self.store, self.engine.workspaces / run_id, assert_current=current)
            if source != ticket["binding"]["workspace"]:
                raise ValueError("Original source changed before the readonly-observation continuation")
            async with mode_execution_scope(self.journal, run_id):
                checkpoint = await reconstruct_observation(prepared_read)
        await self.store.admit_credentials([ticket, checkpoint.canonical_payload.decode("utf-8")])
        await current()
        self.store.reject_credentials([ticket, checkpoint.canonical_payload.decode("utf-8")])
        checkpoint.resume_kwargs()["usage_limits"].check_before_request(checkpoint.usage)
        frozen_timeout = settlement["payload"]["frozen_timeout_seconds"]
        maximum = min(configured.max_window_seconds, frozen_timeout)
        deadline = run.get("active_execution_deadline_ms")
        if "active_execution_budget" in run or deadline is not None and type(deadline) is not int:
            raise ValueError("Unsupported original active-budget protocol")
        if deadline is not None:
            maximum = min(maximum, max(0, (deadline - int(time.time() * 1000)) // 1000))
            if not maximum:
                holds.append("active_execution_budget_expired")
        candidate = {
            "version": 1,
            "kind": "readonly_observation_recovery_candidate",
            "run_id": run_id,
            "request_id": request_id,
            "admission": "HOLD" if holds else "READY",
            "holds": holds,
            "execution_authorized": False,
            "review_basis": "administrator_reviewed_unavailable_read",
            "original_admission_seal": ticket["admission_seal"],
            "failed_event_digest": events[-1]["digest"],
            "intent_digest": intent["digest"],
            "checkpoint_record_digest": record["digest"],
            "checkpoint_digest": prepared_read.checkpoint.digest,
            "read_proof_digest": digest(prepared_read.data),
            "source_digest": digest(source),
            "current_profile": profile,
            "current_runtime_contract": runtime,
            "policy_digest": policy["digest"],
            "maximum_window_seconds": maximum,
            "frozen_timeout_seconds": frozen_timeout,
            "journal_digest": entries[-1]["digest"],
            "events_digest": digest({"events": events}),
            "run_digest": digest(run),
            "run_authority_digest": _run_authority(run),
            "task_digest": digest(task),
            "usage": {
                "requests": checkpoint.usage.requests,
                "tool_calls": checkpoint.usage.tool_calls,
                "request_limit": checkpoint.record.limits["request_limit"],
                "tool_calls_limit": checkpoint.record.limits["tool_calls_limit"],
                "commands": len(commands),
                "command_limit": (run.get("sandbox_snapshot") or {}).get("configuration", {}).get("command_limit"),
            },
        }
        candidate["digest"] = digest(candidate)
        await self.store.admit_credentials(candidate)
        await current()
        return dict(
            candidate=candidate,
            ticket=ticket,
            run=run,
            task=task,
            entries=entries,
            events=events,
            actor=actor,
            administrator=administrator,
            policy=policy,
            epoch=epoch,
            read_guard=current,
            intent=intent,
            checkpoint=record,
            derived=checkpoint,
            prepared_read=prepared_read,
        )

    async def candidate(self, run_id: str, request_id: str) -> Data:
        value = await self._prepare(identifier(run_id).hex, identifier(request_id).hex)
        return dict(value["candidate"])

    @staticmethod
    def _record(ticket: Data, entries: list[Data], events: list[Data]) -> Data | None:
        matches = ReadonlyObservationRecoveries._matches(ticket, entries)
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("Readonly-observation continuation custody is ambiguous")
        record = matches[0]
        settled_observation(record, entries)
        p = record["payload"]
        before = p["failed_ticket"]
        verify_ticket(ticket)
        if (
            {k: v for k, v in ticket.items() if k not in MUTABLE}
            != {k: v for k, v in before.items() if k not in MUTABLE}
            or events[: len(p["events"])] != p["events"]
            or len(events) < len(p["events"]) + 1
            or events[len(p["events"])]["kind"] != OBSERVATION_ADMITTED
            or events[len(p["events"])]["evidence"]
            != {
                "recovery_id": p["recovery_id"],
                "recovery_digest": record["digest"],
                "admission_seal": ticket["admission_seal"],
            }
        ):
            raise ValueError("Readonly-observation request differs from its original immutable custody")
        return record

    @staticmethod
    def _status_valid(ticket: Data, events: list[Data], record: Data) -> None:
        p = record["payload"]
        n = len(p["events"])
        before = p["failed_ticket"]
        queued = (
            len(events) == n + 1
            and ticket["status"] == "queued"
            and all(ticket[k] == before[k] for k in ("generation", "owner_pid", "result"))
        )
        claim = (
            len(events) >= n + 2
            and events[n + 1]["kind"] == "worker_claim"
            and events[n + 1]["evidence"]
            == {
                "generation": ticket["generation"],
                "owner_pid": ticket["owner_pid"],
                "request_digest": ticket["request_digest"],
            }
        )
        if claim and (
            type(ticket["generation"]) is not str
            or re.fullmatch(r"[a-f0-9]{32}", ticket["generation"]) is None
            or ticket["generation"] == before["generation"]
            or type(ticket["owner_pid"]) is not int
            or ticket["owner_pid"] <= 0
        ):
            raise ValueError("The readonly-observation worker has no exact fresh ownership generation")
        executing = claim and len(events) == n + 2 and ticket["status"] == "executing" and ticket["result"] is None
        terminal = (
            (
                claim
                and len(events) == n + 3
                and ticket["status"] in {"completed", "needs_reconciliation"}
                or not claim
                and len(events) == n + 2
                and ticket["status"] == "failed"
                and all(ticket[k] == before[k] for k in ("generation", "owner_pid"))
            )
            and events[-1]["kind"] == "worker_" + ticket["status"]
            and events[-1]["evidence"] == ticket["result"]
        )
        if not (queued or executing or terminal):
            raise ValueError("Readonly-observation worker state differs from its complete native event history")

    async def queued_valid(self, ticket: Data) -> bool:
        _, _, entries = await self.base._native(ticket["run_id"])
        events = await self._events(ticket["id"])
        record = self._record(ticket, entries, events)
        if record is None:
            return False
        self._status_valid(ticket, events, record)
        return True

    async def status(self, run_id: str, request_id: str) -> Data:
        _outside_unit()
        ticket = await self.base._ticket(identifier(run_id).hex, identifier(request_id).hex)
        await self.base._operator(run_id, ticket)
        _, _, entries = await self.base._native(run_id)
        events = await self._events(request_id)
        record = self._record(ticket, entries, events)
        if record is not None:
            self._status_valid(ticket, events, record)
        value = {
            "version": 1,
            "kind": "readonly_observation_recovery_status",
            "run_id": run_id,
            "request_id": request_id,
            "status": ticket["status"] if record else "not_recovered",
            "recovery_id": record["payload"]["recovery_id"] if record else None,
            "recovery_record_digest": record["digest"] if record else None,
            "original_admission_seal": ticket["admission_seal"],
            "lease": record["payload"]["lease"] if record else None,
        }
        await self.store.admit_credentials(value)
        await self.base._operator(run_id, ticket)
        self.store.reject_credentials(value)
        return value

    async def request(
        self, run_id: str, request_id: str, recovery_id: str, body: ReadonlyObservationRecoveryInputV1
    ) -> Data:
        _outside_unit()
        if any(identifier(v).hex != v for v in (run_id, request_id, recovery_id)):
            raise ValueError("Use exact lowercase native identities")
        body = ReadonlyObservationRecoveryInputV1.model_validate(body.model_dump())
        ticket = await self.base._ticket(run_id, request_id)
        actor = await self.base._operator(run_id, ticket)

        async def existing() -> bool:
            current = await self.base._ticket(run_id, request_id)
            _, _, entries = await self.base._native(run_id)
            record = self._record(current, entries, await self._events(request_id))
            if record is None:
                return False
            if (
                record["payload"]["recovery_id"] != recovery_id
                or record["payload"]["request"] != body.model_dump()
                or record["payload"]["actor"] != actor
            ):
                raise ValueError("This request already has another readonly-observation recovery action")
            return True

        if await existing():
            return await self.status(run_id, request_id)
        try:
            prepared = await self._prepare(run_id, request_id)
            c = prepared["candidate"]
            if (
                c["digest"] != body.candidate_digest
                or c["admission"] != "READY"
                or body.valid_for_seconds > c["maximum_window_seconds"]
            ):
                raise ValueError("The reviewed readonly-observation candidate changed or is held")
            await self.store.admit_credentials([body.model_dump(), c])
            await prepared["read_guard"]()
        except ValueError:
            if await existing():
                return await self.status(run_id, request_id)
            raise
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:readonly-observation:" + self.store.schema},
            )
            await self.journal._run(run_id)
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Original continuity request not found")
            _, _, entries = await self.base._native(run_id)
            if self._matches(row.data, entries):
                if not await existing():
                    raise ValueError("Concurrent readonly-observation recovery conflicts")
            else:
                await prepared["read_guard"]()
                if row.data != prepared["ticket"] or row.status != "needs_reconciliation":
                    raise ValueError("The claimed original request changed before admission")
                configured = ContinuityLeasePolicyV1.model_validate(prepared["policy"]["settings"])
                if await self.requests.records.pending_count() >= configured.max_pending_tickets:
                    raise ValueError("The existing bounded execution queue is full")
                original = next(
                    e["payload"]
                    for e in entries
                    if e["kind"] == SETTLEMENT and e["digest"] == ticket["migration_digest"]
                )
                lease = lease_window(
                    configured,
                    frozen_timeout_seconds=c["frozen_timeout_seconds"],
                    valid_for_seconds=body.valid_for_seconds,
                    now_ms=int(time.time() * 1000),
                    original_deadline_ms=original["proof"]["deadline_ms"],
                    active_deadline_ms=prepared["run"].get("active_execution_deadline_ms"),
                )
                binding = {
                    **prepared["checkpoint"]["payload"]["invocation"],
                    "invocation_id": uuid.uuid4().hex,
                    "contract": c["current_runtime_contract"],
                    "deadline_ms": lease["expires_at_ms"],
                }
                payload = {
                    "version": 1,
                    "kind": "reviewed_readonly_observation_continuation",
                    "run_id": run_id,
                    "request_id": request_id,
                    "recovery_id": recovery_id,
                    "actor": actor,
                    "administrator": prepared["administrator"],
                    "request": body.model_dump(),
                    "candidate": c,
                    "failed_ticket": copy.deepcopy(row.data),
                    "events": prepared["events"],
                    "binding": binding,
                    "lease": lease,
                    "lease_policy": prepared["policy"],
                    "policy_epoch": prepared["epoch"],
                    "checkpoint": prepared["derived"].payload.decode(),
                    "checkpoint_digest": prepared["derived"].digest,
                    "read_proof_digest": digest(prepared["prepared_read"].data),
                    "source_run": prepared["run"],
                    "source_task": prepared["task"],
                }
                payload["digest"] = digest(payload)
                checkpoint = BoundaryCheckpoint.decode(row.data["checkpoint"].encode(), row.data["checkpoint_digest"])
                credential_payload = [payload, checkpoint.canonical_payload.decode("utf-8")]
                self.store.reject_credentials(credential_payload)
                record = await self.journal._append(
                    run_id, "implement", "readonly-observation:" + request_id, OBSERVATION_RECOVERY, payload
                )
                settled_observation(record, [*entries, record])
                row.status = "queued"
                row.data = {**row.data, "status": "queued", "updated_at": now()}
                await self.requests.records.save(row)
                await self.requests._event(
                    row,
                    OBSERVATION_ADMITTED,
                    {
                        "recovery_id": recovery_id,
                        "recovery_digest": record["digest"],
                        "admission_seal": ticket["admission_seal"],
                    },
                )
                await self.base._administrator(actor)
                await self.continuities.restores.access(await self.store.get_run(run_id), actor)
                if policy_snapshot(self.store.configuration) != prepared["policy"]:
                    raise ValueError("Recovery policy changed before commit")
                self.store.reject_credentials(credential_payload)
        return await self.status(run_id, request_id)

    async def _authority(self, ticket: Data, record: Data, *, executing: bool) -> tuple[Data, Data]:
        p = record["payload"]
        run, task, entries = await self.base._native(ticket["run_id"])
        settlement = self.base._admission(ticket, run, task, entries)
        await self.continuities.restores.access(run, p["actor"])
        if await self.base._administrator(p["actor"]) != p["administrator"]:
            raise AccessDenied("The reviewed recovery administrator changed")
        if (
            int(time.time() * 1000) >= p["lease"]["expires_at_ms"]
            or policy_snapshot(self.store.configuration) != p["lease_policy"]
            or digest(self.store.configuration.load_snapshot(effective_revision(run))) != ticket["settings_digest"]
            or digest(self.store.configuration.resolve("bridge")) != ticket["settings_digest"]
            or run.get("active_execution_deadline_ms") != p["lease"]["active_deadline_ms"]
            or invocation_cut(run) != ticket["binding"]["run_cut"]
            or protected_scope(run) != ticket["protected_scope_digest"]
            or run.get("cancel_request", {}).get("status") == "requested"
        ):
            raise ValueError("Current readonly-observation lease, policy, configuration or source authority changed")
        if current_unit_of_work() is None:
            from firefly_forge.invalid_read_continuities import current_profile_seal

            if (
                runtime_contract() != p["binding"]["contract"]
                or current_profile_seal() != p["candidate"]["current_profile"]
            ):
                raise ValueError("The qualified readonly-observation runtime changed")
            current_epoch = epoch_snapshot(
                self.store, run, entries, legacy_runtime_contract=settlement["payload"]["proof"]["runtime_contract"]
            )
            if current_epoch != p["policy_epoch"]:
                raise ValueError("The readonly-observation historical epoch changed")
        if task["attempt"] != ticket["binding"]["round"] or task["idempotency_key"] != ticket["task_key"]:
            raise ValueError("The original task attempt changed")
        if executing:
            if run["status"] != "developing" or task["status"] != "running":
                raise ValueError("The recovery worker no longer owns the active original task")
        else:
            suffix = [e for e in entries if e["sequence"] > record["sequence"]]
            _metadata_suffix(ticket, entries, suffix)
            refusals = [e for e in suffix if e["kind"] == "verification_handoff_refused"]
            run_matches = digest(run) == p["candidate"]["run_digest"]
            if refusals:
                run_matches = (
                    run.get("verification_refusal") == refusals[0]["payload"]
                    and digest(run.get("worker_request", {})) == refusals[0]["payload"]["request_digest"]
                    and _run_authority(run) == p["candidate"]["run_authority_digest"]
                )
            if not run_matches or digest(task) != p["candidate"]["task_digest"]:
                raise ValueError("Later native activity prevents readonly-observation dispatch")
        with epoch_scope(p["policy_epoch"]):
            await self.base._custody(ticket, run, task, entries, settlement)
        return run, task

    async def guard(self, request_id: str, record_digest: str, generation: str) -> None:
        await self.store.assert_ownership()
        raw = await self.requests.get(request_id)
        ticket = await self.base._ticket(raw["run_id"], request_id)
        if (
            ticket["status"] != "executing"
            or ticket["generation"] != generation
            or ticket["owner_pid"] != self.store._ownership_pid
        ):
            raise ValueError("Readonly-observation execution ownership changed")
        _, _, entries = await self.base._native(ticket["run_id"])
        events = await self._events(request_id)
        record = self._record(ticket, entries, events)
        if record is None or record["digest"] != record_digest:
            raise ValueError("The worker lost its exact readonly-observation witness")
        self._status_valid(ticket, events, record)
        await self._authority(ticket, record, executing=True)

    async def execute(self, ticket: Data) -> None:
        self.requests.runtime.require_execution()
        await self.store.assert_ownership()
        _, _, entries = await self.base._native(ticket["run_id"])
        events = await self._events(ticket["id"])
        record = self._record(ticket, entries, events)
        if record is None:
            raise ValueError("No reviewed readonly-observation continuation exists")
        self._status_valid(ticket, events, record)
        p = record["payload"]
        with epoch_scope(p["policy_epoch"]):
            async with mode_execution_scope(self.journal, ticket["run_id"]):
                run, _ = await self._authority(ticket, record, executing=False)
                if (
                    await workspace_cut(self.store, self.engine.workspaces / ticket["run_id"])
                    != p["binding"]["workspace"]
                ):
                    raise ValueError("Original source changed before the readonly-observation worker claim")
                async with self.store._unit():
                    locked = await self.journal._run(ticket["run_id"])
                    row = await self.requests.records.find_by_id(
                        identifier(ticket["id"]), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if row is None or row.status != "queued":
                        return
                    if row.data != ticket:
                        raise ValueError("The queued readonly-observation request changed")
                    await self.continuities.restores.completions._writer(run)
                    await self._authority(ticket, record, executing=False)
                    task = await self.journal._effect_task(ticket["run_id"], ticket["task_key"])
                    task.status = "running"
                    await self.journal.tasks.save(task)
                    locked.status = "developing"
                    locked.data = {k: v for k, v in locked.data.items() if k != "bridge_completion_hold"}
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
                execution = ReadonlyObservationExecution(self, ticket, record)
                await execution.guard_source()
                await self.engine.continue_agent_checkpoint(execution)
                run = await self.store.get_run(ticket["run_id"])
                if run.get("bridge_completion_hold") or run["status"] not in {"paused", "succeeded", "validating"}:
                    raise ValueError("Readonly-observation continuation did not reach a known native boundary")
                await self.continuities._finish(
                    ticket["id"], "completed", {"run_status": run["status"], "same_task_attempt": p["binding"]["round"]}
                )


def validate_readonly_observation_record(ticket: Data, entries: list[Data], events: list[Data]) -> Data | None:
    record = ReadonlyObservationRecoveries._record(ticket, entries, events)
    if record is not None:
        ReadonlyObservationRecoveries._status_valid(ticket, events, record)
    return record
