# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native administrator reservation for one final, fifth provider dispatch."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pyfly.container import service

from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.provider_recoveries import ProviderRecoveries
from firefly_forge.provider_recovery_models import ProviderProcessOutcome, dispatch_keys
from firefly_forge.store import identifier, now

Data = dict[str, Any]
PROTOCOL = "native-provider-recovery-allowance-v1"


class ProviderRecoveryAllowanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    native_process_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    agent: Literal["codex", "claude"]
    reason: str = Field(min_length=1, max_length=2000)
    mitigation_evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    adopt_current_executable: Literal[True]

    @field_validator("adopt_current_executable", mode="before")
    @classmethod
    def explicit_executable(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("The final allowance requires explicit adoption of the reviewed current CLI")
        return value

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Explain the reviewed mitigation before reserving the final allowance")
        return value


def allowance_key(run_id: str, request_id: str) -> str:
    return digest({"protocol": PROTOCOL, "run": run_id, "request": request_id})[:32]


def sealed_allowance(run: Data, task: Data, entries: list[Data]) -> Data:
    """Read the actual native WAL link; run JSON alone never grants a fifth dispatch."""
    try:
        return _sealed_allowance(run, task, entries)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("The final provider allowance record is incomplete") from exc


def _sealed_allowance(run: Data, task: Data, entries: list[Data]) -> Data:
    state = run.get("provider_recovery") or {}
    grants = [entry for entry in entries if entry["kind"] == "provider_recovery_allowance"]
    if len(grants) != 1:
        raise ValueError("The final provider allowance requires exactly one native reservation")
    event = grants[0]
    receipt = event["payload"]
    body = ProviderRecoveryAllowanceInput.model_validate(receipt["request"])
    candidate = receipt["candidate"]
    key = allowance_key(run["id"], body.request_id)
    admission = [
        e
        for e in entries
        if e["kind"] == "provider_recovery_admission" and e["payload"]["request"]["request_id"] == body.request_id
    ]
    failures = [
        e for e in entries if e["digest"] == body.native_process_digest and e["kind"] == "provider_failure_observation"
    ]
    candidate_body = {k: v for k, v in candidate.items() if k != "digest"}
    intents = [
        e for e in entries if e["kind"] == "intent" and e["task_id"] == "implement" and e["idempotency_key"] == key
    ]
    original = [
        e
        for e in entries
        if e["kind"] == "intent"
        and e["task_id"] == "implement"
        and e["idempotency_key"] == candidate["failed_task_key"]
        and e["digest"] == candidate["failed_intent_digest"]
    ]
    frozen_cut = {k: v for k, v in candidate["cut"].items() if k != "index_digest"}
    state_cut = {k: v for k, v in state["cut"].items() if k != "index_digest"}
    if (
        receipt.get("protocol") != PROTOCOL
        or receipt.get("version") != 2
        or event["idempotency_key"] != body.request_id
        or event["task_id"] != "implement"
        or receipt.get("task_key") != key
        or candidate.get("protocol") != PROTOCOL
        or candidate.get("digest") != digest(candidate_body)
        or candidate.get("operator_digest") != digest(receipt["actor"])
        or state_cut != frozen_cut
        or len(original) != 1
        or len(intents) != 1
        or intents[0]["payload"]
        != {
            "reason": "explicit_final_provider_allowance",
            "admission_id": body.request_id,
            "allowance_digest": event["digest"],
        }
        or body.candidate_digest != candidate["digest"]
        or candidate.get("run_id") != run["id"]
        or candidate.get("agent") != body.agent
        or body.agent != run["agent"]
        or candidate.get("next_attempt") != 5
        or candidate.get("dispatches_used") != 4
        or candidate.get("journal_digest") != event["previous_hash"]
        or candidate.get("native_process_digest") != body.native_process_digest
        or candidate.get("process_evidence") != "native_cli_process_receipt"
        or receipt.get("previous_task", {}).get("attempt") != 4
        or receipt.get("previous_task", {}).get("status") != "failed"
        or receipt.get("previous_task", {}).get("idempotency_key") != candidate.get("failed_task_key")
        or len(failures) != 1
        or failures[0]["payload"].get("provenance") != "native_cli_process_observation"
        or failures[0]["payload"].get("task_key") != candidate.get("failed_task_key")
        or not ProviderProcessOutcome.model_validate(failures[0]["payload"]["process"]).known_failure
        or len(admission) != 1
        or admission[0]["payload"] != {**receipt, "allowance_digest": event["digest"]}
        or state.get("request_id") != body.request_id
        or state.get("allowance_id") != body.request_id
        or state.get("allowance_digest") != event["digest"]
        or state.get("actor") != receipt.get("actor")
        or state.get("task_key") != key
        or state.get("attempt") != 5
        or task.get("attempt") != 5
        or task.get("idempotency_key") != key
        or task.get("provider_recovery_id") != body.request_id
        or state.get("executable_adoption") != candidate.get("executable_adoption")
        or len(dispatch_keys(entries)) != 5
    ):
        raise ValueError("The final provider allowance linkage changed")
    return {"allowance_id": body.request_id, "allowance_digest": event["digest"], "protocol": PROTOCOL}


@service
class ProviderRecoveryAllowances:
    def __init__(self, recoveries: ProviderRecoveries) -> None:
        self.recoveries = recoveries

    async def candidate(self, run_id: str, agent: str, *, adopt_current_executable: bool = False) -> Data:
        identifier(run_id)
        if adopt_current_executable is not True:
            raise ValueError("The final allowance requires explicit current CLI adoption")
        r = self.recoveries
        await r._operator(run_id)
        async with r.store._unit():
            value = await r._candidate(
                await r.store.get_run(run_id),
                agent,
                adopt_current_executable=adopt_current_executable,
                final_allowance=True,
            )
        await r._operator(run_id)
        r.store.reject_credentials(value)
        return value

    async def get(self, run_id: str, request_id: str) -> Data:
        identifier(request_id)
        r = self.recoveries
        actor = await r._operator(run_id)
        snapshot = {
            **ProjectAccess.actor_snapshot(actor),
            "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
        }
        async with r._locked(run_id, snapshot):
            entries = await r._entries(run_id)
            found = next(
                (
                    e["payload"]
                    for e in entries
                    if e["kind"] == "provider_recovery_allowance" and e["idempotency_key"] == request_id
                ),
                None,
            )
            if found is None:
                raise KeyError("The original final provider allowance was not found")
            if found["actor"] != snapshot:
                raise AccessDenied("Only the original current operator can read this allowance receipt")
            r.store.reject_credentials(found)
            return copy.deepcopy(found)

    async def reserve(self, run_id: str, body: ProviderRecoveryAllowanceInput) -> Data:
        r = self.recoveries
        actor = await r._operator(run_id)
        snapshot = {
            **ProjectAccess.actor_snapshot(actor),
            "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
        }
        request = body.model_dump()
        r.store.reject_credentials(request)
        async with r._locked(run_id, snapshot) as row:
            entries = await r._entries(run_id)
            grants = [e for e in entries if e["kind"] == "provider_recovery_allowance"]
            if grants:
                prior = grants[0]["payload"]
                if len(grants) != 1 or prior["request"] != request or prior["actor"] != snapshot:
                    raise ValueError("The one lifetime final allowance is already reserved")
                return copy.deepcopy(prior)
            candidate = await r._candidate(
                row.data, body.agent, adopt_current_executable=body.adopt_current_executable, final_allowance=True
            )
            if (
                candidate["digest"] != body.candidate_digest
                or candidate["native_process_digest"] != body.native_process_digest
            ):
                raise ValueError("The reviewed final allowance candidate changed")
            task = await r.journal._task(run_id, "implement")
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The final allowance operator changed before reservation")
            prospective = {"status": "reserved", "executable_adoption": candidate.get("executable_adoption")}
            if r._cut({**row.data, "provider_recovery": prospective}) != candidate["cut"]:
                raise ValueError("The final allowance execution cut changed")
            key = allowance_key(run_id, body.request_id)
            receipt = dict(
                version=2,
                protocol=PROTOCOL,
                request=request,
                candidate=candidate,
                actor=snapshot,
                task_key=key,
                previous_task=r._task_view(task),
                process_evidence={"provenance": "native_cli_process_receipt", "digest": body.native_process_digest},
                mitigation_evidence={
                    "provenance": "administrator_attestation",
                    "digest": body.mitigation_evidence_digest,
                },
            )
            r.store.reject_credentials(receipt)
            event = await r.journal._append(
                run_id, "implement", body.request_id, "provider_recovery_allowance", receipt
            )
            seal = event["digest"]
            await r.journal._append(
                run_id,
                "implement",
                body.request_id,
                "provider_recovery_admission",
                {**receipt, "allowance_digest": seal},
            )
            await r.journal._append(
                run_id,
                "implement",
                candidate["failed_task_key"],
                "provider_failure_closed",
                {
                    "provenance": "native_cli_process_receipt",
                    "admission_id": body.request_id,
                    "failed_intent_digest": candidate["failed_intent_digest"],
                    "allowance_digest": seal,
                },
            )
            await r.journal._append(
                run_id,
                "implement",
                key,
                "intent",
                {
                    "reason": "explicit_final_provider_allowance",
                    "admission_id": body.request_id,
                    "allowance_digest": seal,
                },
            )
            task.status, task.attempt = "queued", 5
            task.data = {"idempotency_key": key, "provider_recovery_id": body.request_id}
            await r.journal.tasks.save(task)
            row.status = "queued"
            row.data = {
                **row.data,
                "status": "queued",
                "resume_existing": True,
                "updated_at": now(),
                "provider_recovery": dict(
                    request_id=body.request_id,
                    task_key=key,
                    status="reserved",
                    attempt=5,
                    cut=candidate["cut"],
                    actor=snapshot,
                    allowance_id=body.request_id,
                    allowance_digest=seal,
                    **(
                        {"executable_adoption": candidate["executable_adoption"]}
                        if body.adopt_current_executable
                        else {}
                    ),
                ),
            }
            await r.store.run_records.save(row)
            if CURRENT_PRINCIPAL.get() != actor or r._cut(row.data) != candidate["cut"]:
                raise ValueError("The final allowance authority or execution cut changed before commit")
            sealed_allowance(row.data, r._task_view(task), await r._entries(run_id))
            return copy.deepcopy(receipt)
