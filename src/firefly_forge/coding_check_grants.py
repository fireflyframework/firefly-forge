# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit native administrator authority for one scoped coding-check invocation."""

from __future__ import annotations

import copy
import time
from typing import Any

from pyfly.container import service

from firefly_forge.coding_check_models import (
    CodingCheckCandidate,
    CodingCheckCurrentStatus,
    CodingCheckGrantInput,
    CodingCheckGrantReceipt,
    CodingCheckRevocationInput,
    CodingCheckRevocationReceipt,
)
from firefly_forge.journal import digest
from firefly_forge.models import Command
from firefly_forge.policies import policy_commands
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.provider_recoveries import ProviderRecoveries
from firefly_forge.provider_recovery_allowances import allowance_key
from firefly_forge.sandbox_models import validate_sandbox_snapshot
from firefly_forge.store import identifier

Data = dict[str, Any]
PROTOCOL = "native-coding-configured-check-v1"


def clock_ms() -> int:
    return int(time.time() * 1000)


def stable_cut(cut: Data, run: Data) -> Data:
    # Implementation edits and native index preparation are permitted; authority and Git history are not.
    return {
        **{key: value for key, value in cut.items() if key not in {"source_digest", "index_digest"}},
        "sandbox_digest": digest(run.get("sandbox_snapshot") or {}),
    }


@service
class CodingCheckGrants:
    def __init__(self, recoveries: ProviderRecoveries) -> None:
        self.recoveries = recoveries
        recoveries.coding_checks = self

    @staticmethod
    def actor_snapshot(actor: Any) -> Data:
        return {
            **ProjectAccess.actor_snapshot(actor),
            "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
        }

    async def _candidate(self, run: Data, recovery_request_id: str, bind_port: int = 0) -> Data:
        r = self.recoveries
        recovery = await r._candidate(run, run["agent"], adopt_current_executable=True, final_allowance=True)
        if r.engine.sandbox is None:
            raise ValueError("Configured coding checks require the native sandbox executor")
        commands = [Command.model_validate(value).model_dump() for value in run.get("commands", [])]
        if not commands:
            raise ValueError("Configured coding checks require retained verification commands")
        configuration = validate_sandbox_snapshot(run["sandbox_snapshot"])
        used = len(await r.journal.sandbox_executions(run["id"]))
        remaining = max(0, configuration.command_limit - used)
        final_count = len(commands) + len(policy_commands(run.get("policy_snapshot", {}), run["backend"]))
        if final_count > 20 or remaining < final_count:
            raise ValueError("Remaining shared command budget cannot cover final configured verification")
        state = run.get("coding_check_grant") or {}
        value = dict(
            version=1,
            protocol=PROTOCOL,
            run_id=run["id"],
            agent=run["agent"],
            recovery_request_id=recovery_request_id,
            task_key=allowance_key(run["id"], recovery_request_id),
            task_attempt=recovery["next_attempt"],
            revision=state.get("revision", 0),
            actor_digest=recovery["operator_digest"],
            cut=stable_cut(recovery["cut"], run),
            source_digest=recovery["cut"]["source_digest"],
            command_indices=list(range(len(commands))),
            command_count=len(commands),
            commands=[{"command_index": index, **command} for index, command in enumerate(commands)],
            runner=copy.deepcopy(run["sandbox_snapshot"]),
            channel={"transport": "streamable_http", "host": "127.0.0.1", "bind_port": bind_port},
            independent_final_check_count=final_count,
            shared_command_limit=configuration.command_limit,
            shared_commands_used=used,
            shared_commands_remaining=remaining,
            interactive_commands_remaining=max(0, remaining - final_count),
            journal_digest=recovery["journal_digest"],
        )
        return CodingCheckCandidate.model_validate({**value, "digest": digest(value)}).model_dump()

    async def candidate(self, run_id: str, recovery_request_id: str, bind_port: int = 0) -> Data:
        identifier(recovery_request_id)
        if type(bind_port) is not int or not (bind_port == 0 or 1024 <= bind_port <= 65535):
            raise ValueError("Use an explicitly reviewed unprivileged loopback port or zero")
        r = self.recoveries
        actor = await r._operator(run_id)
        async with r._locked(run_id, self.actor_snapshot(actor)) as row:
            value = await self._candidate(row.data, recovery_request_id, bind_port)
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The configured-check operator changed")
            r.store.reject_credentials(value)
            return value

    async def _find(self, run_id: str, request_id: str, *, kind: str | None = None) -> Data:
        entries = await self.recoveries._entries(run_id)
        found = [
            entry
            for entry in entries
            if entry["idempotency_key"] == request_id
            and entry["kind"] in ({kind} if kind else {"coding_check_grant", "coding_check_revocation"})
        ]
        if len(found) != 1 or found[0]["payload"].get("protocol") != PROTOCOL:
            raise ValueError("The original native configured-check receipt is unavailable")
        return found[0]

    async def get(self, run_id: str, request_id: str) -> Data:
        identifier(request_id)
        r = self.recoveries
        actor = await r._operator(run_id)
        snapshot = self.actor_snapshot(actor)
        async with r._locked(run_id, snapshot):
            event = await self._find(run_id, request_id)
            if event["payload"]["actor"] != snapshot:
                raise AccessDenied("Only the original current administrator can read this grant receipt")
            return copy.deepcopy(event["payload"])

    async def grant(self, run_id: str, body: CodingCheckGrantInput) -> Data:
        body = CodingCheckGrantInput.model_validate(body.model_dump())
        if body.request_id == body.recovery_request_id:
            raise ValueError("The grant identity must be distinct from its future recovery request identity")
        r = self.recoveries
        actor = await r._operator(run_id)
        snapshot, request = self.actor_snapshot(actor), body.model_dump()
        await r.store.admit_credentials(request)
        async with r._locked(run_id, snapshot) as row:
            entries = await r._entries(run_id)
            if any(e["kind"] == "coding_check_revocation" and e["idempotency_key"] == body.request_id for e in entries):
                raise ValueError("The configured-check request identity already belongs to a revocation")
            prior = [e for e in entries if e["kind"] == "coding_check_grant"]
            repeated = next((e for e in prior if e["idempotency_key"] == body.request_id), None)
            if repeated:
                receipt = repeated["payload"]
                if receipt["request"] != request or receipt["actor"] != snapshot:
                    raise ValueError("The original configured-check grant request changed")
                return copy.deepcopy(receipt)
            if len(prior) >= 20:
                raise ValueError("Configured-check grant history is full")
            state = row.data.get("coding_check_grant") or {}
            if state.get("status") == "active":
                raise ValueError("Revoke the current configured-check grant before replacing it")
            candidate = await self._candidate(row.data, body.recovery_request_id, body.bind_port)
            if body.expected_revision != candidate["revision"] or body.candidate_digest != candidate["digest"]:
                raise ValueError("The reviewed configured-check candidate or revision changed")
            if not set(body.command_indices) <= set(candidate["command_indices"]):
                raise ValueError("Requested checks exceed the retained configured scope")
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The configured-check operator changed before admission")
            receipt = dict(
                version=1,
                protocol=PROTOCOL,
                request=request,
                actor=snapshot,
                candidate=candidate,
                revision=candidate["revision"] + 1,
                call_limit=min(body.call_limit, candidate["interactive_commands_remaining"]),
                expires_at_ms=clock_ms() + body.valid_seconds * 1000,
            )
            if receipt["call_limit"] < 1:
                raise ValueError("No interactive command capacity remains before independent final verification")
            receipt["receipt_digest"] = digest(receipt)
            receipt = CodingCheckGrantReceipt.model_validate(receipt).model_dump()
            r.store.reject_credentials(receipt)
            event = await r.journal._append(run_id, "implement", body.request_id, "coding_check_grant", receipt)
            row.data = {
                **row.data,
                "coding_check_grant": {
                    "grant_id": body.request_id,
                    "revision": receipt["revision"],
                    "status": "active",
                    "event_digest": event["digest"],
                },
            }
            await r.store.run_records.save(row)
            return copy.deepcopy(receipt)

    async def revoke(self, run_id: str, body: CodingCheckRevocationInput) -> Data:
        body = CodingCheckRevocationInput.model_validate(body.model_dump())
        r = self.recoveries
        actor = await r._operator(run_id)
        snapshot, request = self.actor_snapshot(actor), body.model_dump()
        await r.store.admit_credentials(request)
        async with r._locked(run_id, snapshot) as row:
            entries = await r._entries(run_id)
            if any(e["kind"] == "coding_check_grant" and e["idempotency_key"] == body.request_id for e in entries):
                raise ValueError("The configured-check request identity already belongs to a grant")
            repeated = next(
                (
                    e
                    for e in entries
                    if e["kind"] == "coding_check_revocation" and e["idempotency_key"] == body.request_id
                ),
                None,
            )
            if repeated:
                if repeated["payload"]["request"] != request or repeated["payload"]["actor"] != snapshot:
                    raise ValueError("The original configured-check revocation changed")
                return copy.deepcopy(repeated["payload"])
            state = row.data.get("coding_check_grant") or {}
            if (
                state.get("grant_id") != body.grant_id
                or state.get("revision") != body.expected_revision
                or state.get("status") != "active"
            ):
                raise ValueError("The current configured-check grant revision changed")
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The configured-check revocation operator changed")
            receipt = dict(
                version=1, protocol=PROTOCOL, request=request, actor=snapshot, revision=body.expected_revision + 1
            )
            receipt = CodingCheckRevocationReceipt.model_validate(receipt).model_dump()
            await r.journal._append(run_id, "implement", body.request_id, "coding_check_revocation", receipt)
            row.data = {
                **row.data,
                "coding_check_grant": {**state, "status": "revoked", "revision": receipt["revision"]},
            }
            await r.store.run_records.save(row)
            return receipt

    async def status(self, run_id: str) -> Data:
        r = self.recoveries
        actor = await r._operator(run_id)
        async with r._locked(run_id, self.actor_snapshot(actor)) as row:
            state = row.data.get("coding_check_grant") or {}
            grant_id = state.get("grant_id")
            event = await self._find(run_id, grant_id, kind="coding_check_grant") if grant_id else None
            if event and event["payload"]["actor"] != self.actor_snapshot(actor):
                raise AccessDenied("Only the original current administrator can read this grant authority")
            expires = event["payload"]["expires_at_ms"] if event else None
            status = state.get("status", "none")
            if status == "active" and expires is not None and expires <= clock_ms():
                status = "expired"
            return CodingCheckCurrentStatus.model_validate(
                dict(
                    protocol=PROTOCOL,
                    run_id=run_id,
                    grant_id=grant_id,
                    revision=state.get("revision", 0),
                    status=status,
                    receipt_digest=event["payload"]["receipt_digest"] if event else None,
                    expires_at_ms=expires,
                )
            ).model_dump()

    async def current(self, run: Data, grant_id: str) -> Data:
        state = run.get("coding_check_grant") or {}
        event = await self._find(run["id"], grant_id, kind="coding_check_grant")
        receipt = event["payload"]
        if (
            receipt.get("receipt_digest")
            != digest({key: value for key, value in receipt.items() if key != "receipt_digest"})
            or state.get("grant_id") != grant_id
            or state.get("status") != "active"
            or state.get("revision") != receipt["revision"]
            or state.get("event_digest") != event["digest"]
            or receipt["expires_at_ms"] <= clock_ms()
        ):
            raise ValueError("The configured-check grant was revoked, replaced or expired")
        return copy.deepcopy(receipt)

    async def bind_allowance(self, run: Data, body: Any, candidate: Data) -> None:
        receipt = await self.current(run, body.coding_check_grant_id)
        if (
            receipt["request"]["recovery_request_id"] != body.request_id
            or receipt["candidate"]["task_key"] != allowance_key(run["id"], body.request_id)
            or receipt["candidate"]["actor_digest"] != candidate["operator_digest"]
            or receipt["candidate"]["cut"] != stable_cut(candidate["cut"], run)
        ):
            raise ValueError("The configured-check grant does not bind this allowance authority")

    async def assert_reserved(self, run_id: str, task_key: str) -> Data | None:
        r = self.recoveries
        initial = await r.store.get_run(run_id)
        state = initial.get("provider_recovery") or {}
        grant_id = state.get("coding_check_grant_id")
        if not grant_id:
            return None
        async with r._locked(run_id, state["actor"]) as row:
            return await self.validate_invocation(row.data, task_key)

    async def validate_invocation(self, run: Data, task_key: str) -> Data:
        state = run.get("provider_recovery") or {}
        receipt = await self.current(run, state["coding_check_grant_id"])
        if (
            state.get("task_key") != task_key
            or state.get("request_id") != receipt["request"]["recovery_request_id"]
            or state.get("attempt") != receipt["candidate"]["task_attempt"]
            or state.get("actor") != receipt["actor"]
            or stable_cut(self.recoveries._cut(run), run) != receipt["candidate"]["cut"]
        ):
            raise ValueError("The configured-check invocation authority changed")
        return receipt
