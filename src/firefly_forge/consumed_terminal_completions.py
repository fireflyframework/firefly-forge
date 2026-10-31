# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact already-consumed terminal output; never replacement provider or tool authority."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from importlib.metadata import version
from pathlib import Path
from typing import Any

from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
from firefly_forge.agentic_checkpoint_invocation import runtime_contract, workspace_cut, workspace_source_capture
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.checkpoint_mode_handoff_custody import validate_completed_handoff
from firefly_forge.checkpoint_mode_runtime import mode_execution_scope
from firefly_forge.checkpoint_source_identity import SelectedSourceFence
from firefly_forge.checkpoint_work_duration import checkpoint_payload
from firefly_forge.completed_final_epoch import validated_completed_final
from firefly_forge.final_outcome_continuations import FinalOutcomeExecution
from firefly_forge.journal import digest
from firefly_forge.known_final_outcome_proof import prepare_final_outcome
from firefly_forge.known_worker_completion_models import (
    ConsumedTerminalCandidateV2,
    ConsumedTerminalInputV2,
    ConsumedTerminalProofV2,
    ConsumedTerminalReceiptV2,
)
from firefly_forge.known_worker_completion_proof import (
    CUSTODY,
    PreparedWorkerCompletion,
    _prepared_digest,
    _scope,
    _unsettled,
)
from firefly_forge.known_worker_completions import _actor, _entries, _task, policy_digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.sandbox_command_slots import _canonical
from firefly_forge.source_snapshot import GitIndexIdentityV1, SourceDiffSnapshotV1, git_index_identity
from firefly_forge.store import identifier, now, prepared_credential_rejection
from firefly_forge.verification_source_preparation import PreparedVerificationSource

Data = dict[str, Any]
KIND = "consumed_terminal_recovery"
CLAIM = "consumed_terminal_claim"
RESULT = "consumed_terminal_result"
RECORDED_V30_RUNTIME = "5fb9491fffb90356aa9a76abe3f4072457a41c2b0fc0da2a7677af9367b7b7a6"
RECORDED_V30_PROFILE = "528f7809c473f03ac038b5b0688796583292bccb4a0b6a9c0ca8c932efb1b2cf"
RECORDED_V30_QUALIFICATION = "a4aa3cdd08b7a08b34f81a2c1d2cf5e18fb93d6a687a9a49565881810a9f731c"


@dataclass(frozen=True)
class PreparedConsumedTerminal:
    candidate: Data
    run: Data
    task: Data
    entries: list[Data]
    final: Data
    usage: Data
    workspace: Path
    retained_source: Any
    ownership: object


@dataclass(frozen=True)
class PreparedTerminalAuthority:
    run_digest: str
    _guard: Callable[[], Awaitable[None]]

    async def current(self, expected: Data) -> None:
        if expected.get("status") != "failed" or digest(expected) != self.run_digest:
            raise ValueError("Read-only consumed terminal authority belongs to another failed cut")
        await self._guard()


def _same(left: Any, right: Any) -> bool:
    return digest({"value": left}) == digest({"value": right})


def _runtime(recorded: str, checkpoint: BoundaryCheckpoint, current: str) -> None:
    if recorded == current:
        return
    from firefly_forge.source_edit_qualification import _profiles, profile_contract_digest, profile_payload

    profiles = [
        p
        for p in _profiles()
        if p.version == 29
        and p.loader_profile == "codex-exec-restricted-source/v30"
        and p.proof.artifact_sha256 == RECORDED_V30_QUALIFICATION
        and p.proof.contract_digest == profile_contract_digest(p)
        and digest(profile_payload(p)) == RECORDED_V30_PROFILE
    ]
    compatibility = json.loads(checkpoint.canonical_payload)["compatibility"]
    dependencies = {
        "pyfly": "26.10.7",
        "fireflyframework-agentic": "26.10.1",
        "pydantic-ai-slim": "2.55.0",
        "openai": "3.29.0",
    }
    if (
        recorded != RECORDED_V30_RUNTIME
        or len(profiles) != 1
        or compatibility != {name: dependencies[name] for name in ("fireflyframework-agentic", "pydantic-ai-slim")}
        or any(version(name) != expected for name, expected in dependencies.items())
    ):
        raise ValueError("Consumed terminal runtime has no registered read-only compatibility")


async def prepare(service: Any, run_id: str) -> PreparedConsumedTerminal:
    _outside_unit()
    actor = _actor()
    run = await service.completions._reader(run_id, actor, execute=True)
    if (
        run.get("status") != "failed"
        or run.get("active_task") != "implement"
        or run.get("implementation_pending") is not True
        or type(run.get("attempt")) is not int
        or type(run.get("implementation_rounds")) is not int
        or run["attempt"] != run["implementation_rounds"] + 1
        or (run.get("cancel_request") or {}).get("status") == "requested"
        or (run.get("publication_status") == "publishing")
    ):
        raise ValueError(
            "Known worker settlement refuses an ended or cancelled execution without exact consumed terminal custody"
        )
    await service.store.assert_ownership()
    ownership = service.store.execution_ownership_token
    engine = getattr(service, "engine", None)
    if engine is not None and run_id in engine.active and not engine.active[run_id].done():
        raise ValueError("Consumed terminal recovery refuses an active worker")
    await service.completions._execution_authority(run, actor)
    async with mode_execution_scope(service.journal, run_id):
        completed = await validated_completed_final(service.journal, run)
        if completed is None:
            raise ValueError("Consumed terminal recovery requires its accepted native final predecessor")
        parent = completed.record["payload"]
        if (
            parent["candidate"]["outcome"] != "blocked"
            or parent["candidate"]["followup_kind"] != "verification"
            or run["attempt"] != parent["candidate"]["attempt"] + 1
            or not _same(run.get("usage"), parent["run"].get("usage"))
        ):
            raise ValueError("Consumed terminal recovery cannot reset or charge previously accepted progress")
        async with service.store._unit():
            task = _task(await service.journal._task(run_id, "implement"))
            entries = _entries(await service.journal.records.find_for_run(identifier(run_id)), run_id)
            rows = await service.journal.sandboxes.for_run(identifier(run_id))
        values = {row.id.hex: _canonical(row, entries, run_id) for row in rows}
        custody = validate_completed_handoff(run, entries, rows, values)
        if task["status"] != "running" or task["attempt"] != run["attempt"]:
            raise ValueError("Consumed terminal task is not the same unaccepted native attempt")
        final = (await prepare_final_outcome(run, task, entries)).data
        report = final["report"]
        feedback = report.get("worker_feedback") or {}
        qa = report.get("qa_report")
        if (
            feedback.get("disposition") != "completed"
            or feedback.get("request") is not None
            or feedback.get("role_id") != "implementer"
            or feedback.get("attempt") != run["attempt"]
            or feedback.get("task_id") != "implement"
            or qa is not None
            and (
                qa["mode"] != "not_requested"
                or any(qa[field] for field in ("reviewed_states", "reference_sha256", "findings", "not_reviewed"))
            )
            or final["state"]["files_written"]
        ):
            raise ValueError("Consumed terminal recovery requires the exact completed unchanged-source report")
        payload = checkpoint_payload(final["checkpoint"]["payload"])
        captured = BoundaryCheckpoint.decode(payload.snapshot.encode(), payload.checkpoint_digest)
        current_runtime = runtime_contract()
        _runtime(payload.invocation.contract, captured, current_runtime)
        grouped = _unsettled(entries, run_id)
        if any(
            checkpoint_payload(e["payload"]).invocation.contract != payload.invocation.contract
            for e in grouped["agent_checkpoint"]
        ):
            raise ValueError("Consumed terminal invocation mixed runtime contracts")
        closures = [
            {
                "request_id": intent["idempotency_key"],
                "intent_digest": intent["digest"],
                "checkpoint_record_digest": checkpoint["digest"],
                "response_record_digest": response["digest"],
                "consume_record_digest": consume["digest"],
            }
            for intent, checkpoint, response, consume in zip(*(grouped[k] for k in CUSTODY), strict=True)
        ]
        from firefly_forge.native_location_projection import source_workspace_matches

        workspace = Path(payload.invocation.workspace["path"])

        async def current() -> None:
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("Consumed terminal operator changed during preparation")
            await service.store.assert_ownership()
            await service.completions._execution_authority(run, actor)
            if service.store.execution_ownership_token is not ownership or not _same(
                await service.store.get_run(run_id), run
            ):
                raise ValueError("Consumed terminal run or execution owner changed")
            await completed.current()
            async with service.store._unit():
                live = _entries(await service.journal.records.find_for_run(identifier(run_id)), run_id)
                live_task = _task(await service.journal._task(run_id, "implement"))
                if live != entries or live_task != task:
                    raise ValueError("Consumed terminal native columns or frontier changed")

        actual = await workspace_cut(service.store, workspace, assert_current=current)
        if not await source_workspace_matches(service.store, run_id, actual, payload.invocation.workspace):
            raise ValueError("Consumed terminal source, HEAD or private Git metadata changed")
        from firefly_forge.retained_verified_source import prepare_retained_source

        retained_source = await prepare_retained_source(
            service.store,
            service.journal,
            run,
            workspace,
            terminal_recovery=PreparedTerminalAuthority(digest(run), current),
        )
        usage = copy.deepcopy(run["usage"])
        for key in ("requests", "input_tokens", "output_tokens"):
            if any(type(v) is not int or v < 0 for v in (usage.get(key), final["usage"].get(key))):
                raise ValueError("Consumed terminal requires exact nonnegative native spent counters")
            usage[key] += final["usage"][key]
        usage.update(
            runtime_report=copy.deepcopy(report),
            worker_feedback=copy.deepcopy(feedback),
            files_written=[],
            qa_report=None,
            browser_evidence=[],
            retained_source=retained_source.proof.model_dump(mode="json"),
        )
        from firefly_forge.agentic_tool_custody import EFFECTS

        tools = {tool["name"] for tool in captured.record.contract["parameters"]["function_tools"]}
        usage["agentic_tool_custody"] = dict(
            version=1,
            status="captured",
            captured_tools=sorted(tools & EFFECTS.keys()),
            uncaptured_tools=sorted(tools - EFFECTS.keys()),
            reason=None,
        )
        from firefly_forge.invalid_read_continuities import current_profile_seal

        proof = dict(
            version=2,
            run_id=run_id,
            task_id="implement",
            task_attempt=task["attempt"],
            task_key=task["idempotency_key"],
            completion_count=len(closures),
            completions=closures,
            terminal_report_sha256=digest(report),
            final_response_sha256=final["consume"]["payload"]["response_sha256"],
            recorded_runtime_contract=payload.invocation.contract,
            current_runtime_contract=current_runtime,
            current_profile_digest=digest(current_profile_seal()),
            source_digest=digest(actual),
            parent_final_digest=completed.record["digest"],
            handoff_custody_digest=custody["digest"],
            usage_before_digest=digest(run["usage"]),
            usage_after_digest=digest(usage),
            verification="consumed_terminal_output_known_only",
        )
        proof["digest"] = digest(proof)
        proof = ConsumedTerminalProofV2.model_validate(proof).model_dump(mode="json")
        candidate = dict(
            version=2,
            run_id=run_id,
            actor_digest=digest(ProjectAccess.actor_snapshot(actor)),
            policy_digest=policy_digest(),
            run_digest=digest(run),
            journal_digest=digest({"entries": entries}),
            proof=proof,
        )
        candidate["digest"] = digest(candidate)
        candidate = ConsumedTerminalCandidateV2.model_validate(candidate).model_dump(mode="json")
        await service.store.admit_credentials(candidate)
        await current()
        async with service.store._unit():
            after = _entries(await service.journal.records.find_for_run(identifier(run_id)), run_id)
            if after != entries or _task(await service.journal._task(run_id, "implement")) != task:
                raise ValueError("Consumed terminal native columns or immutable frontier changed")
        return PreparedConsumedTerminal(
            candidate, run, task, entries, final, usage, workspace, retained_source, ownership
        )


def validate_witness(entry: Data, entries: list[Data]) -> Data:
    value: Data = entry["payload"]
    candidate = ConsumedTerminalCandidateV2.model_validate(value["candidate"]).model_dump(mode="json")
    request = ConsumedTerminalInputV2.model_validate(value["request"]).model_dump(mode="json")
    if (
        entry["kind"] != KIND
        or type(value.get("version")) is not int
        or value["version"] != 2
        or set(value) != {"version", "run_id", "request_id", "actor", "candidate", "request", "run", "task", "usage"}
        or value["run_id"] != entry["run_id"]
        or candidate["run_id"] != entry["run_id"]
        or entry["idempotency_key"] != "consumed-terminal:" + value["request_id"]
        or digest(value["actor"]) != candidate["actor_digest"]
        or digest(value["run"]) != candidate["run_digest"]
        or digest(value["usage"]) != candidate["proof"]["usage_after_digest"]
        or digest(value["run"]["usage"]) != candidate["proof"]["usage_before_digest"]
        or digest(value["usage"]["runtime_report"]) != candidate["proof"]["terminal_report_sha256"]
        or digest({"entries": entries[: entry["sequence"] - 1]}) != candidate["journal_digest"]
        or value["task"]["idempotency_key"] != candidate["proof"]["task_key"]
        or value["task"]["attempt"] != candidate["proof"]["task_attempt"]
        or any(
            request[k] != expected
            for k, expected in {
                "candidate_digest": candidate["digest"],
                "policy_digest": candidate["policy_digest"],
                "expected_run_digest": candidate["run_digest"],
                "journal_digest": candidate["journal_digest"],
                "proof_digest": candidate["proof"]["digest"],
            }.items()
        )
    ):
        raise ValueError("Consumed terminal witness differs from its reviewed native cut")
    for closure in candidate["proof"]["completions"]:
        for kind, field in zip(
            CUSTODY,
            ("intent_digest", "checkpoint_record_digest", "response_record_digest", "consume_record_digest"),
            strict=True,
        ):
            found = [
                e
                for e in entries[: entry["sequence"] - 1]
                if e["kind"] == kind and e["idempotency_key"] == closure["request_id"]
            ]
            if len(found) != 1 or found[0]["digest"] != closure[field]:
                raise ValueError("Consumed terminal witness lost its immutable response custody")
    return value


def find_witness(entries: list[Data], request_id: str) -> Data | None:
    matches = [e for e in entries if e["kind"] == KIND and e["idempotency_key"] == "consumed-terminal:" + request_id]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Consumed terminal action identity is not unique")
    validate_witness(matches[0], entries)
    return matches[0]


def validate_projection(witness: Data, entries: list[Data], marker: Data) -> None:
    p = validate_witness(witness, entries)
    proof, request_id = p["candidate"]["proof"], p["request_id"]
    claims = [e for e in entries if e["kind"] == CLAIM and e["payload"].get("recovery_digest") == witness["digest"]]
    results = [e for e in entries if e["kind"] == RESULT and e["payload"].get("recovery_digest") == witness["digest"]]
    base = {"version": 2, "request_id": request_id, "record_digest": witness["digest"]}
    if marker == {**base, "state": "queued"}:
        if claims or results:
            raise ValueError("Queued consumed terminal projection conflicts with its native records")
        return
    if (
        len(claims) != 1
        or claims[0]["sequence"] <= witness["sequence"]
        or type(claims[0]["payload"].get("version")) is not int
        or claims[0]["payload"] != {"version": 2, "recovery_digest": witness["digest"]}
        or marker.get("state") not in {"claimed", "accepted"}
        or marker != {**base, "state": marker.get("state"), "claim_digest": claims[0]["digest"]}
        or len(results) > 1
    ):
        raise ValueError("Consumed terminal projection requires its exact native claim")
    if results and (
        results[0]["sequence"] <= claims[0]["sequence"]
        or results[0]["idempotency_key"] != "consumed-terminal-result:" + request_id
        or results[0]["payload"]
        != {
            "version": 2,
            "recovery_digest": witness["digest"],
            "usage_before_digest": proof["usage_before_digest"],
            "usage_after_digest": proof["usage_after_digest"],
        }
    ):
        raise ValueError("Consumed terminal projection changed its native usage acceptance")
    if marker["state"] != "accepted":
        return
    from firefly_forge.native_terminal_completion_models import validate_native_receipt

    expected = [
        {**c, "consume_version": 1, "custody": "consumed_terminal_v2", "recovery_witness_digest": witness["digest"]}
        for c in proof["completions"]
    ]
    terminals = [
        e
        for e in entries
        if e["kind"] == "terminal_completion_receipt"
        and e["sequence"] > claims[0]["sequence"]
        and any(c.get("recovery_witness_digest") == witness["digest"] for c in e["payload"]["proof"]["completions"])
    ]
    receipts = [
        e
        for e in entries
        if e["kind"] == "receipt"
        and e["idempotency_key"] == proof["task_key"]
        and e["sequence"] > claims[0]["sequence"]
    ]
    if len(results) != 1 or len(terminals) != 1 or len(receipts) != 1:
        raise ValueError("Consumed terminal acceptance requires its complete native receipts")
    terminal = validate_native_receipt(terminals[0], entries).proof
    if (
        terminal.task_key != proof["task_key"]
        or terminal.task_attempt != proof["task_attempt"]
        or [c.model_dump(mode="json") for c in terminal.completions] != expected
        or terminal.acceptance_kind != "worker_feedback"
        or terminal.accepted_payload_sha256 != digest(p["usage"]["worker_feedback"])
        or not results[0]["sequence"] < terminals[0]["sequence"] < receipts[0]["sequence"]
        or receipts[0]["payload"].get("implementation_round") != proof["task_attempt"]
    ):
        raise ValueError("Consumed terminal native receipts belong to another result or attempt")


async def readback(
    service: Any, run_id: str, request_id: str, *, request: ConsumedTerminalInputV2 | None = None
) -> Data:
    actor = _actor()
    await service.completions._reader(run_id, actor, execute=request is not None)
    async with service.store._unit():
        run = await service.store.get_run(run_id)
        entries = _entries(await service.journal.records.find_for_run(identifier(run_id)), run_id)
    witness = find_witness(entries, request_id)
    if witness is None:
        raise KeyError("Consumed terminal action not found")
    p = witness["payload"]
    if request is not None and (
        request.model_dump(mode="json") != p["request"]
        or digest(ProjectAccess.actor_snapshot(actor)) != p["candidate"]["actor_digest"]
    ):
        raise AccessDenied("Consumed terminal action belongs to another input or operator")
    marker = run.get("consumed_terminal_recovery") or {}
    if marker.get("request_id") != request_id or marker.get("record_digest") != witness["digest"]:
        raise ValueError("Consumed terminal action lost its current native projection")
    validate_projection(witness, entries, marker)
    result = {
        "version": 2,
        "kind": KIND,
        "run_id": run_id,
        "request_id": request_id,
        "record_digest": witness["digest"],
        "candidate": p["candidate"],
        "request": p["request"],
        "state": marker.get("state"),
        "run_status": run["status"],
        "same_task_attempt": p["candidate"]["proof"]["task_attempt"],
        "no_execution_grant": True,
    }
    result = ConsumedTerminalReceiptV2.model_validate(result).model_dump(mode="json")
    await service.store.admit_credentials(result)
    await service.completions._reader(run_id, actor, execute=request is not None)
    if CURRENT_PRINCIPAL.get() != actor:
        raise AccessDenied("Consumed terminal reader changed during disclosure")
    service.store.reject_credentials(result)
    return result


async def request(service: Any, run_id: str, request_id: str, body: ConsumedTerminalInputV2) -> Data:
    _outside_unit()
    if identifier(request_id).hex != request_id:
        raise ValueError("Use an exact lowercase consumed terminal action identity")
    run_id = identifier(run_id).hex
    body = ConsumedTerminalInputV2.model_validate(body.model_dump(mode="json"))
    actor = _actor()
    with prepared_credential_rejection(service.store):
        await service.completions._reader(run_id, actor, execute=True)
        async with service.store._unit():
            entries = _entries(await service.journal.records.find_for_run(identifier(run_id)), run_id)
        if find_witness(entries, request_id) is not None:
            return await readback(service, run_id, request_id, request=body)
        prepared = await prepare(service, run_id)
        candidate = prepared.candidate
        if any(
            getattr(body, key) != expected
            for key, expected in {
                "candidate_digest": candidate["digest"],
                "policy_digest": candidate["policy_digest"],
                "expected_run_digest": candidate["run_digest"],
                "journal_digest": candidate["journal_digest"],
                "proof_digest": candidate["proof"]["digest"],
            }.items()
        ):
            raise ValueError("The reviewed consumed terminal candidate changed")
        async with mode_execution_scope(service.journal, run_id):
            await prepared.retained_source.revalidate()
            async with service.store._unit():
                await service.completions._writer(prepared.run, actor)
                row = await service.journal._run(run_id)
                current = await service.store.get_run(run_id)
                await service.completions._execution_authority(current, actor)
                entries = _entries(await service.journal.records.find_for_run(identifier(run_id)), run_id)
                existing = find_witness(entries, request_id)
                if existing is None:
                    if (
                        current != prepared.run
                        or entries != prepared.entries
                        or _task(await service.journal._task(run_id, "implement")) != prepared.task
                    ):
                        raise ValueError("Consumed terminal candidate changed before its native write")
                    if service.store.execution_ownership_token is not prepared.ownership:
                        raise ValueError("Consumed terminal execution owner changed before native write")
                    prepared.retained_source.fence()
                    payload = dict(
                        version=2,
                        run_id=run_id,
                        request_id=request_id,
                        actor=ProjectAccess.actor_snapshot(actor),
                        candidate=candidate,
                        request=body.model_dump(mode="json"),
                        run=prepared.run,
                        task=prepared.task,
                        usage=prepared.usage,
                    )
                    service.store.reject_credentials(payload)
                    witness = await service.journal._append(
                        run_id, "implement", "consumed-terminal:" + request_id, KIND, payload
                    )
                    row.status = "queued"
                    row.data = {
                        **row.data,
                        "status": "queued",
                        "error": None,
                        "updated_at": now(),
                        "consumed_terminal_recovery": {
                            "version": 2,
                            "request_id": request_id,
                            "record_digest": witness["digest"],
                            "state": "queued",
                        },
                    }
                    await service.store.run_records.save(row)
                    await service.store.assert_ownership()
                    if service.store.execution_ownership_token is not prepared.ownership:
                        raise ValueError("Consumed terminal execution owner changed before commit")
                    if CURRENT_PRINCIPAL.get() != actor:
                        raise AccessDenied("Consumed terminal operator changed before commit")
        return await readback(service, run_id, request_id, request=body)


@dataclass(frozen=True)
class CapturedConsumedSnapshot:
    before_digest: str
    after: bytes
    index: GitIndexIdentityV1
    source_fence: SelectedSourceFence
    git_directory: tuple[int, ...]
    engine: Any
    base: str
    files: tuple[str, ...]
    source: SourceDiffSnapshotV1
    tree: str

    def evidence(self, attempt: int) -> Data:
        return {
            "source_tree": self.tree,
            "changed_files": list(self.files),
            "implementation_round": attempt,
            "changed_file_count": self.source.artifact.file_count,
            "changed_files_preview_truncated": len(self.files) < self.source.artifact.file_count,
            "diff_artifact": self.source.artifact.model_dump(mode="json"),
        }


class ConsumedTerminalExecution(FinalOutcomeExecution):
    """Reuse the existing final-only Engine branch, without restoring a model graph."""

    def __init__(self, service: Any, witness: Data, entries: list[Data], claim: Data, final: Data) -> None:
        self.service, self.witness, self.claim = service, witness, claim
        self.run_id = witness["run_id"]
        self.request_id = witness["payload"]["request_id"]
        self.final = final
        self.intent = final["intent"]
        self.record = final["checkpoint"]
        self.binding = checkpoint_payload(self.record["payload"]).invocation
        self.workspace = Path(self.binding.workspace["path"])
        self.actor = copy.deepcopy(witness["payload"]["actor"])
        self.reader = CURRENT_PRINCIPAL.get()
        self.ownership = service.store.execution_ownership_token
        self.generation = service.store.configuration.write_generation
        self.prefix = copy.deepcopy(entries)
        self._catalog: PreparedVerificationSource | None = None
        self._snapshot: CapturedConsumedSnapshot | None = None
        self._snapshot_started = False

    async def operator(self) -> Any:
        from firefly_forge.identity import Principal

        user = await self.service.completions._identity(self.actor["user_id"])
        principal = Principal(
            user.id, user.issuer, user.subject, authentication_method=self.actor["authentication_method"]
        )
        run = await self.service.store.get_run(self.run_id)
        access = ProjectAccess(self.service.store)
        administrator = await access.authorize_actor(self.actor, run.get("project_id"), "execute")
        if not administrator:
            await access.require_repository(principal, run["repository_id"], "execute")
        if run.get("publish"):
            await access.authorize_actor(self.actor, run.get("project_id"), "release")
        return principal

    async def guard(self) -> None:
        store, journal = self.service.store, self.service.journal
        await store.assert_ownership()
        if (
            store.execution_ownership_token is not self.ownership
            or CURRENT_PRINCIPAL.get() != self.reader
            or store.configuration.write_generation is not self.generation
        ):
            raise AccessDenied("Consumed terminal worker identity, ownership or configuration changed")
        run = await store.get_run(self.run_id)
        await self.operator()
        await self.service.completions._execution(run, self.reader)
        async with store._unit():
            entries = _entries(await journal.records.find_for_run(identifier(self.run_id)), self.run_id)
            task = _task(await journal._task(self.run_id, "implement"))
        p = validate_witness(self.witness, entries)
        marker = run.get("consumed_terminal_recovery") or {}
        if (
            marker
            != {
                "version": 2,
                "request_id": self.request_id,
                "record_digest": self.witness["digest"],
                "state": "claimed",
                "claim_digest": self.claim["digest"],
            }
            or entries[: len(self.prefix)] != self.prefix
            or run.get("attempt") != p["task"]["attempt"]
            or task["attempt"] != p["task"]["attempt"]
            or task["idempotency_key"] != p["task"]["idempotency_key"]
            or task["status"] != "running"
        ):
            raise ValueError("Consumed terminal claim or original task changed")
        mutable = {
            "status",
            "display_status",
            "updated_at",
            "error",
            "consumed_terminal_recovery",
            "usage",
            "summary",
            "implementation_rounds",
            "implementation_pending",
            "worker_feedback",
            "bridge_completion_hold",
            "bridge_completion_settlement",
        }
        if {k: v for k, v in run.items() if k not in mutable} != {
            k: v for k, v in p["run"].items() if k not in mutable
        }:
            raise ValueError("Consumed terminal original scope or invocation changed")
        projected = _same(run.get("usage"), p["usage"])
        if not (projected or _same(run.get("usage"), p["run"]["usage"])):
            raise ValueError("Consumed terminal spent usage changed")
        expected_round = p["task"]["attempt"] if projected else p["run"]["implementation_rounds"]
        if (
            type(run.get("implementation_rounds")) is not int
            or run["implementation_rounds"] != expected_round
            or run.get("implementation_pending") is not (not projected)
        ):
            raise ValueError("Consumed terminal round accounting changed")
        suffix = entries[len(self.prefix) :]
        for entry in suffix:
            if entry["kind"] == RESULT:
                if entry["idempotency_key"] != "consumed-terminal-result:" + self.request_id:
                    raise ValueError("Foreign consumed terminal result")
            elif entry["kind"] == "worker_feedback":
                if entry["payload"] != self.final["report"]["worker_feedback"]:
                    raise ValueError("Consumed terminal feedback changed")
            elif entry["kind"] == "terminal_completion_receipt":
                from firefly_forge.native_terminal_completion_models import validate_native_receipt

                receipt = validate_native_receipt(entry, entries)
                if receipt.proof.terminal_report_sha256 != p["candidate"]["proof"]["terminal_report_sha256"]:
                    raise ValueError("Consumed terminal acceptance belongs to another report")
            else:
                raise ValueError("Unexpected activity after consumed terminal claim")
        results = [e for e in suffix if e["kind"] == RESULT]
        if len(results) != int(projected) or (
            results
            and results[0]["payload"]
            != {
                "version": 2,
                "recovery_digest": self.witness["digest"],
                "usage_before_digest": p["candidate"]["proof"]["usage_before_digest"],
                "usage_after_digest": p["candidate"]["proof"]["usage_after_digest"],
            }
        ):
            raise ValueError("Consumed terminal usage has no unique native acceptance")
        feedbacks = [e for e in suffix if e["kind"] == "worker_feedback"]
        terminals = [e for e in suffix if e["kind"] == "terminal_completion_receipt"]
        if (
            len(feedbacks) > 1
            or len(terminals) > 1
            or run.get("worker_feedback")
            != (self.final["report"]["worker_feedback"] if feedbacks else p["run"].get("worker_feedback"))
        ):
            raise ValueError("Consumed terminal current feedback projection changed")
        if terminals:
            if (
                run.get("bridge_completion_hold") != {}
                or (run.get("bridge_completion_settlement") or {}).get("record_digest") != terminals[0]["digest"]
            ):
                raise ValueError("Consumed terminal settlement projection changed")
        elif run.get("bridge_completion_hold") != p["run"].get("bridge_completion_hold"):
            raise ValueError("Consumed terminal pending hold changed")

    async def qualified(self) -> None:
        _outside_unit()
        from firefly_forge.invalid_read_continuities import current_profile_seal

        await self.guard()
        proof = self.witness["payload"]["candidate"]["proof"]
        if (
            runtime_contract() != proof["current_runtime_contract"]
            or digest(current_profile_seal()) != proof["current_profile_digest"]
            or policy_digest() != self.witness["payload"]["candidate"]["policy_digest"]
        ):
            raise ValueError("Consumed terminal current qualified runtime or profile changed")

    async def source_current(self, actual: dict[str, str]) -> None:
        from firefly_forge.native_location_projection import source_workspace_matches

        proof = self.witness["payload"]["candidate"]["proof"]
        if self._snapshot is not None:
            if self._snapshot.before_digest != proof["source_digest"] or actual != json.loads(self._snapshot.after):
                raise ValueError("Consumed terminal source changed after its native snapshot")
            if git_index_identity(self.workspace) != self._snapshot.index:
                raise ValueError("Consumed terminal staged index changed after its native snapshot")
        elif digest(actual) != proof["source_digest"] or not await source_workspace_matches(
            self.service.store, self.run_id, actual, self.binding.workspace
        ):
            raise ValueError("Consumed terminal source, HEAD or private Git metadata changed")
        await self.guard()

    async def guard_source(self) -> None:
        await self.qualified()
        actual = await workspace_cut(self.service.store, self.workspace, assert_current=self.guard)
        await self.source_current(actual)

    async def snapshot(
        self, engine: Any, base: str, *, secrets: dict[str, str], expected_head: str | None
    ) -> tuple[list[str], SourceDiffSnapshotV1, str]:
        """A preexisting tombstone removed by staging remains a held selection change."""
        if self._snapshot_started or self._catalog is None or engine is not self.service.engine:
            raise ValueError("Consumed terminal snapshot requires its unique native Engine and prepared source")
        await self.qualified()
        before = await workspace_source_capture(self.service.store, self.workspace, assert_current=self.qualified)
        after = None
        try:
            await self.source_current(before.cut)
            await self._catalog.revalidate(assert_current=self.qualified)
            self._snapshot_started = True
            files, source, tree = await engine._snapshot(
                self.workspace, base, secrets=secrets, expected_head=expected_head
            )
            admitted_index = git_index_identity(self.workspace)
            if before.fence is None:
                raise ValueError("Consumed terminal snapshot has no complete selected-source witnesses")
            before.fence.revalidate(payload_only=True)
            if (
                not isinstance(source, SourceDiffSnapshotV1)
                or re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", tree) is None
            ):
                raise ValueError("Consumed terminal snapshot lacks its actual typed native artifact and tree")
            await self._catalog.revalidate(assert_current=self.qualified)
            after = await workspace_source_capture(self.service.store, self.workspace, assert_current=self.qualified)
            if (
                before.physical_digest is None
                or before.physical_digest != after.physical_digest
                or {k: v for k, v in before.cut.items() if k != "source"}
                != {k: v for k, v in after.cut.items() if k != "source"}
                or after.index != admitted_index
                or after.fence is None
            ):
                raise ValueError("Consumed terminal native snapshot changed selected source, identity or index")
            from firefly_forge.git_workspace import git_environment, trusted_git
            from firefly_forge.process import checked

            await checked(
                trusted_git(["git", "diff", "--cached", "--quiet", "--no-ext-diff", "--no-textconv", tree, "--"]),
                self.workspace,
                env=git_environment(),
            )
            await self.qualified()
            if git_index_identity(self.workspace) != admitted_index:
                raise ValueError("Consumed terminal snapshot tree lost its exact admitted index")
            git = (self.workspace / ".git").lstat()
            if not stat.S_ISDIR(git.st_mode):
                raise ValueError("Consumed terminal Git directory changed")
            self._snapshot = CapturedConsumedSnapshot(
                digest(before.cut),
                json.dumps(after.cut, sort_keys=True).encode(),
                admitted_index,
                after.fence,
                (git.st_dev, git.st_ino, git.st_uid, git.st_mode),
                engine,
                base,
                tuple(files),
                source,
                tree,
            )
            await self.guard_source()
            return files, source, tree
        except BaseException:
            if after is not None and after.fence is not None:
                after.fence.close()
            raise
        finally:
            if before.fence is not None:
                before.fence.close()

    def close_snapshot(self) -> None:
        if self._snapshot is not None:
            self._snapshot.source_fence.close()

    def snapshot_fence(self) -> None:
        from firefly_forge.sandbox import _directory_fd

        snapshot = self._snapshot
        if snapshot is None or self._catalog is None:
            raise ValueError("Consumed terminal receipt has no native snapshot")
        self._catalog.fence()
        snapshot.source_fence.revalidate()
        root = git = index = None
        try:
            root = _directory_fd(self.workspace)
            git = _directory_fd(".git", parent=root)
            directory = os.fstat(git)
            if (directory.st_dev, directory.st_ino, directory.st_uid, directory.st_mode) != snapshot.git_directory:
                raise ValueError("Consumed terminal Git directory changed before receipt")
            index = os.open("index", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=git)
            info = os.fstat(index)
            expected = snapshot.index
            observed = (
                info.st_dev,
                info.st_ino,
                info.st_uid,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            if (
                info.st_nlink != 1
                or observed
                != (
                    expected.device,
                    expected.inode,
                    expected.uid,
                    expected.mode,
                    expected.byte_count,
                    expected.mtime_ns,
                    expected.ctime_ns,
                )
                or tuple(
                    getattr(os.stat("index", dir_fd=git, follow_symlinks=False), "st_" + k)
                    for k in ("dev", "ino", "uid", "mode", "size", "mtime_ns", "ctime_ns")
                )
                != observed
            ):
                raise ValueError("Consumed terminal staged index changed before receipt")
        finally:
            for fd in (index, git, root):
                if fd is not None:
                    os.close(fd)

    async def consume(self, *args: Any, **kwargs: Any) -> None:
        raise ValueError("An already-consumed terminal response cannot be consumed again")

    async def result(self) -> Any:
        from firefly_forge.providers import AgentResult
        from firefly_forge.retained_verified_source import prepare_retained_source

        await self.guard_source()
        store = self.service.store
        run = await store.get_run(self.run_id)
        p = self.witness["payload"]
        retained = await prepare_retained_source(store, self.service.journal, run, self.workspace)
        if not _same(retained.proof.model_dump(mode="json"), p["usage"]["retained_source"]):
            raise ValueError("Consumed terminal retained-source authority changed")
        self._catalog = retained.source
        await store.admit_credentials(p["usage"])
        async with store._unit():
            await self.service.completions._writer(run, await self.operator())
            row = await self.service.journal._run(self.run_id)
            await self.guard()
            retained.fence()
            if not _same(row.data.get("usage"), p["usage"]):
                await self.service.journal._append(
                    self.run_id,
                    "implement",
                    "consumed-terminal-result:" + self.request_id,
                    RESULT,
                    dict(
                        version=2,
                        recovery_digest=self.witness["digest"],
                        usage_before_digest=p["candidate"]["proof"]["usage_before_digest"],
                        usage_after_digest=p["candidate"]["proof"]["usage_after_digest"],
                    ),
                )
                row.data = {
                    **row.data,
                    "usage": copy.deepcopy(p["usage"]),
                    "summary": self.final["report"]["summary"],
                    "implementation_pending": False,
                    "implementation_rounds": p["task"]["attempt"],
                }
                await store.run_records.save(row)

                await store.assert_ownership()
        return AgentResult(self.final["report"]["summary"], "consumed_terminal_result", usage=copy.deepcopy(p["usage"]))

    async def receipt(self, engine: Any, key: str, evidence: Data, checkpoint: Data) -> None:
        await self.guard_source()
        snapshot = self._snapshot
        if (
            snapshot is None
            or engine is not snapshot.engine
            or key != self.witness["payload"]["task"]["idempotency_key"]
            or evidence != snapshot.evidence(self.witness["payload"]["task"]["attempt"])
            or checkpoint != {"base_revision": snapshot.base, "source_tree": snapshot.tree}
        ):
            raise ValueError("Consumed terminal receipt differs from its exact native snapshot")
        store = self.service.store
        with prepared_credential_rejection(store):
            await store.admit_credentials(evidence)
            await self.guard_source()
            async with store._unit():
                run = await store.get_run(self.run_id)
                await self.service.completions._writer(run, await self.operator())
                row = await self.service.journal._run(self.run_id)
                await self.guard()
                self.snapshot_fence()
                await engine._receipt(self.run_id, key, evidence, checkpoint=checkpoint, prepared=True)
                self.snapshot_fence()
                row.data = {
                    **row.data,
                    "consumed_terminal_recovery": {
                        **row.data["consumed_terminal_recovery"],
                        "state": "accepted",
                    },
                    "status": "paused",
                    "pause": {
                        "kind": "consumed_terminal_result",
                        "automatic": False,
                        "message": "The consumed worker result is accepted; review remaining native stages separately.",
                    },
                }
                row.status = "paused"
                await store.run_records.save(row)
                self.snapshot_fence()
                await store.assert_ownership()
                if (
                    store.execution_ownership_token is not self.ownership
                    or CURRENT_PRINCIPAL.get() != self.reader
                    or store.configuration.write_generation is not self.generation
                ):
                    raise AccessDenied("Consumed terminal receipt lost its current owner or configuration")
                self.snapshot_fence()


@dataclass(frozen=True)
class PreparedConsumedAcceptance:
    restoration: ConsumedTerminalExecution
    prepared: PreparedWorkerCompletion

    async def operator(self) -> Any:
        return await self.restoration.operator()

    async def current(self) -> None:
        await self.restoration.guard()


async def prepare_acceptance(execution: ConsumedTerminalExecution) -> PreparedConsumedAcceptance:
    await execution.guard_source()
    service, p = execution.service, execution.witness["payload"]
    async with service.store._unit():
        run = await service.store.get_run(execution.run_id)
        task = _task(await service.journal._task(execution.run_id, "implement"))
        entries = _entries(await service.journal.records.find_for_run(identifier(execution.run_id)), execution.run_id)
    if not _same(run.get("usage"), p["usage"]):
        raise ValueError("Consumed terminal acceptance lost its exactly charged result")
    proof = p["candidate"]["proof"]
    closures = tuple(
        {
            **c,
            "consume_version": 1,
            "custody": "consumed_terminal_v2",
            "recovery_witness_digest": execution.witness["digest"],
        }
        for c in proof["completions"]
    )
    prepared = PreparedWorkerCompletion(
        len(entries),
        digest({"entries": entries}),
        _scope(run, task),
        copy.deepcopy(execution.final["report"]["worker_feedback"]),
        closures,
        proof["final_response_sha256"],
        proof["current_runtime_contract"],
        None,
        "",
        copy.deepcopy(execution.final["report"]),
    )
    prepared = replace(prepared, seal=_prepared_digest(prepared))
    return PreparedConsumedAcceptance(execution, prepared)


async def claimed_execution(engine: Any, run_id: str) -> ConsumedTerminalExecution | None:
    from firefly_forge.known_worker_completions import KnownWorkerCompletions

    service = KnownWorkerCompletions(engine.store, engine.journal, engine)
    run = await engine.store.get_run(run_id)
    marker = run.get("consumed_terminal_recovery")
    if marker is None:
        return None
    if isinstance(marker, dict) and marker.get("state") == "accepted":
        if run.get("status") != "preparing":
            raise ValueError("Accepted consumed terminal requires ordinary native resume admission")
        async with engine.store._unit():
            entries = _entries(await engine.journal.records.find_for_run(identifier(run_id)), run_id)
        witness = find_witness(entries, marker.get("request_id", ""))
        if witness is None:
            raise ValueError("Accepted consumed terminal lost its native witness")
        validate_projection(witness, entries, marker)
        return None
    if (
        type(marker) is not dict
        or type(marker.get("version")) is not int
        or marker.get("version") != 2
        or marker.get("state") != "queued"
        or run.get("status") != "preparing"
    ):
        raise ValueError("Consumed terminal action cannot be claimed or automatically retried")
    async with mode_execution_scope(engine.journal, run_id):
        async with engine.store._unit():
            entries = _entries(await engine.journal.records.find_for_run(identifier(run_id)), run_id)
            task = _task(await engine.journal._task(run_id, "implement"))
        witness = find_witness(entries, marker["request_id"])
        if witness is None or witness["digest"] != marker["record_digest"] or entries[-1] != witness:
            raise ValueError("Consumed terminal queued witness changed before claim")
        p = witness["payload"]
        if task != p["task"] or not _same(run.get("usage"), p["run"]["usage"]):
            raise ValueError("Consumed terminal queued task or usage changed")
        final = (await prepare_final_outcome(p["run"], p["task"], entries[:-1])).data
        if digest(final["report"]) != p["candidate"]["proof"]["terminal_report_sha256"]:
            raise ValueError("Consumed terminal report changed before local claim")
        from firefly_forge.invalid_read_continuities import current_profile_seal
        from firefly_forge.retained_verified_source import prepare_retained_source

        proof = p["candidate"]["proof"]
        if (
            runtime_contract() != proof["current_runtime_contract"]
            or digest(current_profile_seal()) != proof["current_profile_digest"]
            or policy_digest() != p["candidate"]["policy_digest"]
        ):
            raise ValueError("Consumed terminal qualified runtime changed before local claim")
        ownership = engine.store.execution_ownership_token
        preview = ConsumedTerminalExecution(service, witness, entries, {"digest": ""}, final)
        operator = await preview.operator()
        await service.completions._execution(run, CURRENT_PRINCIPAL.get())
        retained = await prepare_retained_source(engine.store, engine.journal, run, preview.workspace)
        if not _same(retained.proof.model_dump(mode="json"), p["usage"]["retained_source"]):
            raise ValueError("Consumed terminal source authority changed before local claim")
        async with engine.store._unit():
            await service.completions._writer(run, operator)
            row = await engine.journal._run(run_id)
            latest = _entries(await engine.journal.records.find_for_run(identifier(run_id)), run_id)
            if (
                latest != entries
                or await engine.store.get_run(run_id) != run
                or _task(await engine.journal._task(run_id, "implement")) != task
            ):
                raise ValueError("Consumed terminal queued custody changed during decoding")
            if engine.store.execution_ownership_token is not ownership:
                raise ValueError("Consumed terminal execution owner changed before claim")
            retained.fence()
            claim = await engine.journal._append(
                run_id,
                "implement",
                "consumed-terminal-claim:" + uuid.uuid4().hex,
                CLAIM,
                {"version": 2, "recovery_digest": witness["digest"]},
            )
            row.data = {
                **row.data,
                "consumed_terminal_recovery": {**marker, "state": "claimed", "claim_digest": claim["digest"]},
            }
            await engine.store.run_records.save(row)
            await engine.store.assert_ownership()
            if engine.store.execution_ownership_token is not ownership:
                raise ValueError("Consumed terminal execution owner changed before claim commit")
        entries = [*entries, claim]
        execution = ConsumedTerminalExecution(service, witness, entries, claim, final)
        await execution.guard_source()
        return execution
