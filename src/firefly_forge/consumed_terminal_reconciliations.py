# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated continuation of one uncharged consumed-terminal claim."""

from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import dataclass
from typing import Any

from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
from firefly_forge.agentic_checkpoint_invocation import runtime_contract, workspace_source_capture
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.checkpoint_mode_runtime import mode_execution_scope
from firefly_forge.checkpoint_work_duration import checkpoint_payload
from firefly_forge.consumed_terminal_completions import (
    CLAIM,
    ConsumedTerminalExecution,
    PreparedConsumedReconciliationAuthority,
    _runtime,
    find_witness,
    validate_projection,
)
from firefly_forge.invalid_read_continuities import current_profile_seal
from firefly_forge.journal import digest
from firefly_forge.known_final_outcome_proof import prepare_final_outcome
from firefly_forge.known_worker_completion_models import (
    ConsumedTerminalReconciliationCandidateV1,
    ConsumedTerminalReconciliationInputV1,
    ConsumedTerminalReconciliationReceiptV1,
)
from firefly_forge.known_worker_completions import _actor, _entries, _task, policy_digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.source_snapshot import git_index_identity
from firefly_forge.store import identifier, prepared_credential_rejection

Data = dict[str, Any]
KIND = "consumed_terminal_reconciliation"


def _owner_digest(store: Any, generation: object) -> str:
    return digest({"owner": store.execution_ownership_identity, "configuration": id(generation)})


def _ids(run_id: str, request_id: str) -> tuple[str, str]:
    if identifier(run_id).hex != run_id or identifier(request_id).hex != request_id:
        raise ValueError("Use exact lowercase native reconciliation identities")
    return run_id, request_id


def _record(entries: list[Data], request_id: str) -> Data | None:
    found = [
        e for e in entries if e["kind"] == KIND and e["idempotency_key"] == "consumed-terminal-reconcile:" + request_id
    ]
    if len(found) > 1:
        raise ValueError("Consumed terminal reconciliation identity is not unique")
    return found[0] if found else None


def _stopped(run: Data, task: Data, entries: list[Data], witness: Data) -> Data:
    p = witness["payload"]
    marker = run.get("consumed_terminal_recovery") or {}
    validate_projection(witness, entries, marker)
    suffix = entries[witness["sequence"] :]
    if (
        marker.get("state") != "claimed"
        or run.get("status") != "paused"
        or len(suffix) not in {1, 2}
        or suffix[0]["kind"] != CLAIM
        or suffix[0]["digest"] != marker.get("claim_digest")
        or run.get("usage") != p["run"].get("usage")
    ):
        raise ValueError("Reconciliation requires the inactive uncharged original claim")
    original, current = p["run"].get("events"), run.get("events")
    if (
        type(original) is not list
        or type(current) is not list
        or current[:-1] != original
        or len(current) != len(original) + 1
    ):
        raise ValueError("Reconciliation requires the exact retained activity and single native runner event")
    event = current[-1]
    if (
        type(event) is not dict
        or set(event) != {"id", "stage", "message", "created_at"}
        or type(event["id"]) is not str
        or re.fullmatch(r"[a-f0-9]{32}", event["id"]) is None
        or event["stage"] != "runner_attestation"
        or event["message"] != "Execution worker attested frozen runner and tool grants"
        or type(event["created_at"]) is not str
    ):
        raise ValueError("Reconciliation runner event is not the exact native attestation")
    expected_task = copy.deepcopy(p["task"])
    expected_handoff = p["run"].get("verification_handoff")
    if len(suffix) == 2:
        if type(expected_handoff) is not dict or expected_handoff.get("status") != "completed":
            raise ValueError("Reconciliation cannot replace an uncompleted verification handoff")
        expected_handoff = {
            **expected_handoff,
            "status": "held",
            "error": "Verification needs explicit reconciliation or renewed authority before continuation.",
        }
        hold = suffix[1]
        if (
            hold["kind"] != "verification_handoff_outcome"
            or hold["task_id"] != "implement"
            or hold["idempotency_key"] != expected_handoff["id"] + ":held"
            or hold["payload"] != expected_handoff
            or run.get("display_status") != "paused_worker_request"
        ):
            raise ValueError("Reconciliation refuses foreign post-claim journal activity")
        expected_task.pop("verification_handoff", None)
        expected_task["status"] = "paused"
    if task != expected_task or run.get("verification_handoff") != expected_handoff:
        raise ValueError("Reconciliation task or obsolete handoff changed")
    mutable = {
        "status",
        "display_status",
        "error",
        "updated_at",
        "events",
        "consumed_terminal_recovery",
        "verification_handoff",
    }
    if {k: v for k, v in run.items() if k not in mutable} != {k: v for k, v in p["run"].items() if k not in mutable}:
        raise ValueError("Reconciliation original scope, grants or spent progress changed")
    return suffix[0]


@dataclass
class PreparedReconciliation:
    candidate: Data
    run: Data
    task: Data
    entries: list[Data]
    witness: Data
    claim: Data
    final: Data
    source: Any
    ownership: object
    generation: object
    actor: Any


async def _prepare(service: Any, run_id: str, request_id: str) -> PreparedReconciliation:
    _outside_unit()
    _ids(run_id, request_id)
    actor = _actor()
    run = await service.completions._reader(run_id, actor, execute=True)
    await service.completions._execution(run, actor)
    await service.store.assert_ownership()
    ownership, generation = service.store.execution_ownership_token, service.store.configuration.write_generation
    if service.engine is None or any(
        not task.done() and (key == run_id or service.engine.active_projects.get(key) == run["repository_id"])
        for key, task in service.engine.active.items()
    ):
        raise ValueError("Reconciliation requires an inactive original worker and repository slot")
    async with service.store._unit():
        entries = _entries(await service.journal.records.find_for_run(identifier(run_id)), run_id)
        task = _task(await service.journal._task(run_id, "implement"))
    witness = find_witness(entries, request_id)
    if witness is None:
        raise KeyError("Consumed terminal action not found")
    p = witness["payload"]
    if digest(ProjectAccess.actor_snapshot(actor)) != p["candidate"]["actor_digest"]:
        raise AccessDenied("Reconciliation belongs to the original authenticated operator")
    claim = _stopped(run, task, entries, witness)
    final = (await prepare_final_outcome(p["run"], p["task"], entries[: witness["sequence"] - 1])).data
    proof = p["candidate"]["proof"]
    if digest(final["report"]) != proof["terminal_report_sha256"]:
        raise ValueError("Reconciliation consumed report changed")
    checkpoint = checkpoint_payload(final["checkpoint"]["payload"])
    _runtime(
        checkpoint.invocation.contract,
        BoundaryCheckpoint.decode(checkpoint.snapshot.encode(), checkpoint.checkpoint_digest),
        runtime_contract(),
    )

    async def current() -> None:
        await service.store.assert_ownership()
        await service.completions._execution(run, actor)
        if any(
            not worker.done() and (key == run_id or service.engine.active_projects.get(key) == run["repository_id"])
            for key, worker in service.engine.active.items()
        ):
            raise ValueError("Reconciliation original worker or repository became active")
        if (
            CURRENT_PRINCIPAL.get() != actor
            or service.store.execution_ownership_token is not ownership
            or service.store.configuration.write_generation is not generation
        ):
            raise AccessDenied("Reconciliation actor, owner or configuration changed")
        async with service.store._unit():
            if (
                await service.store.get_run(run_id) != run
                or _task(await service.journal._task(run_id, "implement")) != task
                or _entries(await service.journal.records.find_for_run(identifier(run_id)), run_id) != entries
            ):
                raise ValueError("Reconciliation current native cut changed")

    source = await workspace_source_capture(service.store, service.engine.workspaces / run_id, assert_current=current)
    try:
        from firefly_forge.native_location_projection import source_workspace_matches

        if (
            source.fence is None
            or digest(source.cut) != proof["source_digest"]
            or not await source_workspace_matches(service.store, run_id, source.cut, checkpoint.invocation.workspace)
        ):
            raise ValueError("Reconciliation source, HEAD, private Git metadata or index changed")
        value = dict(
            version=1,
            run_id=run_id,
            request_id=request_id,
            actor_digest=p["candidate"]["actor_digest"],
            owner_digest=_owner_digest(service.store, generation),
            policy_digest=policy_digest(),
            runtime_contract=runtime_contract(),
            profile_digest=digest(current_profile_seal()),
            run_digest=digest(run),
            task_digest=digest(task),
            journal_digest=digest({"entries": entries}),
            recovery_digest=witness["digest"],
            claim_digest=claim["digest"],
            source_digest=digest(source.cut),
            index_digest=digest(source.index.model_dump(mode="json")),
            same_task_attempt=p["task"]["attempt"],
        )
        value["digest"] = digest(value)
        candidate = ConsumedTerminalReconciliationCandidateV1.model_validate(value).model_dump(mode="json")
        await service.store.admit_credentials(candidate)
        await current()
        source.fence.revalidate()
        if git_index_identity(service.engine.workspaces / run_id) != source.index:
            raise ValueError("Reconciliation staged index changed")
        return PreparedReconciliation(
            candidate, run, task, entries, witness, claim, final, source, ownership, generation, actor
        )
    except BaseException:
        if source.fence is not None:
            source.fence.close()
        raise


async def candidate(service: Any, run_id: str, request_id: str) -> Data:
    with prepared_credential_rejection(service.store):
        async with mode_execution_scope(service.journal, run_id):
            prepared = await _prepare(service, run_id, request_id)
            try:
                return prepared.candidate
            finally:
                prepared.source.fence.close()


def validate_record(record: Data, entries: list[Data]) -> Data:
    p: Data = record["payload"]
    c = ConsumedTerminalReconciliationCandidateV1.model_validate(p["candidate"]).model_dump(mode="json")
    request = ConsumedTerminalReconciliationInputV1.model_validate(p["request"])
    witness = find_witness(entries, c["request_id"])
    if (
        record["kind"] != KIND
        or [e for e in entries if e["digest"] == record["digest"]] != [record]
        or set(p) != {"version", "candidate", "request", "run", "task"}
        or type(p["version"]) is not int
        or p["version"] != 1
        or c["run_id"] != record["run_id"]
        or record["idempotency_key"] != "consumed-terminal-reconcile:" + c["request_id"]
        or record["task_id"] != "implement"
        or c["run_digest"] != digest(p["run"])
        or c["task_digest"] != digest(p["task"])
        or c["journal_digest"] != digest({"entries": entries[: record["sequence"] - 1]})
        or request.candidate_digest != c["digest"]
        or witness is None
        or witness["digest"] != c["recovery_digest"]
        or _stopped(p["run"], p["task"], entries[: record["sequence"] - 1], witness)["digest"] != c["claim_digest"]
    ):
        raise ValueError("Consumed terminal reconciliation lost its exact native authority")
    if (
        c["actor_digest"] != witness["payload"]["candidate"]["actor_digest"]
        or c["same_task_attempt"] != witness["payload"]["task"]["attempt"]
        or c["source_digest"] != witness["payload"]["candidate"]["proof"]["source_digest"]
    ):
        raise ValueError("Reconciliation authority changed its original actor, attempt or source")
    return p


async def readback(
    service: Any, run_id: str, request_id: str, body: ConsumedTerminalReconciliationInputV1 | None = None
) -> Data:
    _outside_unit()
    _ids(run_id, request_id)
    actor = _actor()
    run = await service.completions._reader(run_id, actor, execute=True)
    await service.completions._execution_authority(run, actor)
    async with service.store._unit():
        entries = _entries(await service.journal.records.find_for_run(identifier(run_id)), run_id)
    record = _record(entries, request_id)
    if record is None:
        raise KeyError("Consumed terminal reconciliation not found")
    p = validate_record(record, entries)
    c = p["candidate"]
    request = ConsumedTerminalReconciliationInputV1.model_validate(p["request"])
    if digest(ProjectAccess.actor_snapshot(actor)) != c["actor_digest"] or body is not None and body != request:
        raise AccessDenied("Consumed terminal reconciliation belongs to another operator or request")
    value = dict(
        version=1,
        candidate=c,
        request=request.model_dump(mode="json"),
        record_digest=record["digest"],
        settlement=await service.get(run_id, request_id),
    )
    result = ConsumedTerminalReconciliationReceiptV1.model_validate(value).model_dump(mode="json")
    await service.store.admit_credentials(result)
    await service.completions._reader(run_id, actor, execute=True)
    if CURRENT_PRINCIPAL.get() != actor:
        raise AccessDenied("Reconciliation reader changed during disclosure")
    service.store.reject_credentials(result)
    return result


async def reconcile(service: Any, run_id: str, request_id: str, body: ConsumedTerminalReconciliationInputV1) -> Data:
    _outside_unit()
    _ids(run_id, request_id)
    body = ConsumedTerminalReconciliationInputV1.model_validate(body.model_dump(mode="json"))
    actor = _actor()
    await service.completions._reader(run_id, actor, execute=True)
    async with service.store._unit():
        entries = _entries(await service.journal.records.find_for_run(identifier(run_id)), run_id)
    if _record(entries, request_id) is not None:
        return await readback(service, run_id, request_id, body)
    if service.engine is None:
        raise ValueError("Reconciliation requires its native execution worker")
    if service.store.runtime_roles is not None:
        service.store.runtime_roles.require_execution()
    async with service.engine.publication_lock:
        with prepared_credential_rejection(service.store):
            async with mode_execution_scope(service.journal, run_id):
                prepared = await _prepare(service, run_id, request_id)
                reserved = False
                try:
                    if body.candidate_digest != prepared.candidate["digest"]:
                        raise ValueError("The reviewed reconciliation candidate changed")
                    if not await service.engine.reserve_agent_restore(run_id):
                        raise ValueError("Reconciliation requires the owned native repository slot")
                    reserved = True
                    p = dict(
                        version=1,
                        candidate=prepared.candidate,
                        request=body.model_dump(mode="json"),
                        run=prepared.run,
                        task=prepared.task,
                    )
                    await service.store.admit_credentials(p)
                    if (
                        runtime_contract() != prepared.candidate["runtime_contract"]
                        or digest(current_profile_seal()) != prepared.candidate["profile_digest"]
                        or policy_digest() != prepared.candidate["policy_digest"]
                    ):
                        raise ValueError("Reconciliation current qualified authority changed")
                    async with service.store.source_budget_admission_unit():
                        await service.completions._writer(prepared.run, actor)
                        await service.journal._run(run_id)
                        await service.store.assert_ownership()
                        await service.completions._execution(prepared.run, actor)
                        task = await service.journal._task(run_id, "implement")
                        if (
                            await service.store.get_run(run_id) != prepared.run
                            or _task(task) != prepared.task
                            or _entries(await service.journal.records.find_for_run(identifier(run_id)), run_id)
                            != prepared.entries
                        ):
                            raise ValueError("Reconciliation reviewed authority changed before native write")
                        prepared.source.fence.revalidate()
                        if git_index_identity(service.engine.workspaces / run_id) != prepared.source.index:
                            raise ValueError("Reconciliation index changed before native write")
                        service.store.reject_credentials(p)
                        record = await service.journal._append(
                            run_id, "implement", "consumed-terminal-reconcile:" + request_id, KIND, p
                        )
                        task.status = "running"
                        await service.journal.tasks.save(task)
                        await service.store.assert_ownership()
                        if (
                            service.store.execution_ownership_token is not prepared.ownership
                            or service.store.configuration.write_generation is not prepared.generation
                            or CURRENT_PRINCIPAL.get() != actor
                            or _owner_digest(service.store, prepared.generation) != prepared.candidate["owner_digest"]
                            or service.engine.active.get(run_id) is not asyncio.current_task()
                            or service.engine.active_projects.get(run_id) != prepared.run["repository_id"]
                        ):
                            raise AccessDenied("Reconciliation lost its actor, owner or configuration before commit")
                        prepared.source.fence.revalidate()
                        if git_index_identity(service.engine.workspaces / run_id) != prepared.source.index:
                            raise ValueError("Reconciliation index changed before commit")
                    execution = ConsumedTerminalExecution(
                        service,
                        prepared.witness,
                        [*prepared.entries, record],
                        prepared.claim,
                        prepared.final,
                        reconciliation=PreparedConsumedReconciliationAuthority(record),
                    )
                    await service.engine.continue_agent_checkpoint(execution)
                finally:
                    prepared.source.fence.close()
                    if reserved:
                        service.engine.release_agent_restore(run_id)
    return await readback(service, run_id, request_id, body)
