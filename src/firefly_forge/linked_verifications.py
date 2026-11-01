# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Independent fresh QA of an accepted parent; never implementation dispatch."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from pyfly.data.transaction.synchronization import register_synchronization

from firefly_forge.agentic_checkpoint_invocation import (
    _runtime_material_digest,
    runtime_contract,
    workspace_source_capture,
)
from firefly_forge.bridge_completions import BridgeCompletions, _outside_unit
from firefly_forge.checkpoint_mode_runtime import mode_execution_scope
from firefly_forge.checkpoint_source_identity import SelectedSourceFence, _revalidate, _stamp
from firefly_forge.configuration import redact_secrets
from firefly_forge.consumed_terminal_reconciliations import ReconciliationCommitFence
from firefly_forge.execution_environment import build_execution_environment
from firefly_forge.execution_request_persistence import ForgeExecutionRequestEntity, ForgeExecutionRequestEventEntity
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.execution_requests import digest as request_digest
from firefly_forge.final_outcome_models import RetainedVerifiedSourceV1
from firefly_forge.identity import Principal
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completions import _entries, _task
from firefly_forge.linked_verification_models import (
    KIND,
    LinkedVerificationCandidateV1,
    LinkedVerificationHoldDiagnosticV1,
    LinkedVerificationInputV1,
    LinkedVerificationReceiptV1,
    VerificationHoldPhase,
    verification_progress,
)
from firefly_forge.native_terminal_completion_models import validate_native_receipt
from firefly_forge.process import ProcessResult, checked
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.sandbox import _directory_fd
from firefly_forge.sandbox_models import SandboxInput, validate_sandbox_snapshot
from firefly_forge.scm_linked_verification_models import (
    PreparedSCMLinkedVerificationAdmission,
    SCMLinkedVerificationAdmissionV1,
    SCMLinkedVerificationAuthority,
    SCMLinkedVerificationScopeV1,
)
from firefly_forge.source_snapshot import git_index_identity
from firefly_forge.store import Store, identifier, now
from firefly_forge.verification_scope_attestation import PreparedBuildScope, prepare_build_scope
from firefly_forge.verification_source_preparation import prepare_verification_source, verification_catalog_root
from firefly_forge.work_duration_models import effective_work_timeout
from firefly_forge.workspace_tools import _descriptor_path

Data = dict[str, Any]


def _hold_diagnostic(phase: VerificationHoldPhase, error: Exception) -> LinkedVerificationHoldDiagnosticV1:
    causes = {
        "prepare": "preparation_refused",
        "admission": "admission_refused",
        "command_inputs": "input_scope_changed",
        "dispatch": "transport_error",
        "result_retention": "result_retention_refused",
        "post_result_inputs": "input_scope_changed",
        "post_result_source": "selected_source_changed",
        "post_result_recapture": "source_capture_refused",
        "post_result_authority": "authority_refused",
        "settlement": "settlement_refused",
    }
    allowed = {
        "ValueError",
        "RuntimeError",
        "TimeoutError",
        "OSError",
        "PermissionError",
        "FileNotFoundError",
        "TypeError",
        "AssertionError",
        "AccessDenied",
        "Exception",
    }
    exception_type = next((c.__name__ for c in type(error).__mro__ if c.__name__ in allowed), "Exception")
    return LinkedVerificationHoldDiagnosticV1.model_validate(
        {
            "phase": phase,
            "cause_code": "authority_refused" if isinstance(error, AccessDenied) else causes[phase],
            "exception_type": exception_type,
        }
    )


class _PostResultGuardFailure(ValueError):
    def __init__(self, phase: VerificationHoldPhase, error: Exception) -> None:
        self.diagnostic = _hold_diagnostic(phase, error)
        super().__init__("Verification post-result source or input guard changed")


def actor_identity(actor: Principal) -> Data:
    return {"issuer": actor.issuer, "subject": actor.subject}


def actor_digest(actor: Principal) -> str:
    return digest({"actor": ProjectAccess.actor_snapshot(actor), "identity": actor_identity(actor)})


def final_commands(run: Data, workspace: Path) -> list[Data]:
    from firefly_forge.engine import final_verification_commands

    return [command.model_dump(mode="json") for command in final_verification_commands(run, workspace)]


def _scm_scope(actor: Data, run: Data | None = None) -> SCMLinkedVerificationScopeV1:
    scope = SCMLinkedVerificationScopeV1.model_validate(actor.get("scm_delegation"))
    if (
        set(actor) != {"user_id", "authentication_method", "global_admin", "api_audience", "scm_delegation"}
        or actor["authentication_method"] != "scm_mapping"
        or actor["global_admin"] is not False
        or actor["api_audience"] is not None
        or scope.native_user_id != actor["user_id"]
        or run is not None
        and any(getattr(scope, key) != run.get(key) for key in ("project_id", "repository_id", "component_id"))
        or run is not None
        and scope.run_id != run["id"]
    ):
        raise AccessDenied("SCM verification requires its exact nonprivileged proposal identity")
    return scope


def _scm_binding(
    value: Data, actor: Data, candidate: LinkedVerificationCandidateV1, request: LinkedVerificationInputV1, run: Data
) -> SCMLinkedVerificationAdmissionV1:
    seal = SCMLinkedVerificationAdmissionV1.model_validate(value)
    if (
        seal.scope != _scm_scope(actor, run)
        or seal.candidate_digest != candidate.digest
        or seal.request_id != request.request_id
        or seal.request_digest != request_digest(request.model_dump(mode="json"))
    ):
        raise ValueError("SCM verification grant changed its exact reviewed candidate or request")
    return seal


def effective_command_timeout(command: Data, config: SandboxInput) -> int | None:
    timeout = command.get("timeout")
    if config.work_duration is None:
        if timeout is None:
            raise ValueError("A legacy runner requires its finite command timeout")
        return effective_work_timeout(timeout, config.timeout)
    return effective_work_timeout(timeout, config.work_duration.timeout_seconds)


def _completed_checks(operation: Data, events: list[Data]) -> list[Data]:
    return [
        {
            "argv": operation["commands"][event["evidence"]["ordinal"] - 1]["argv"],
            "exit_code": event["evidence"]["exit_code"],
            "output": event["evidence"]["output"],
            "created_at": event["created_at"],
            "attempt": operation["parent_run"]["attempt"],
            "execution": "linked_verification",
            "operation_id": operation["id"],
            "operation_result_digest": event["digest"],
        }
        for event in events
        if event["kind"] == "linked_verification_command_result"
    ]


def policy_digest() -> str:
    _outside_unit()
    return _policy_material_digest()


def _policy_material_digest() -> str:
    root = Path(__file__).parent
    return digest(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "linked_verifications.py",
                "linked_verification_models.py",
                "linked_verification_controllers.py",
                "scm_linked_verification.py",
                "scm_linked_verification_models.py",
                "scm_linked_verification_records.py",
                "execution_requests.py",
                "execution_request_persistence.py",
                "runner_backends.py",
                "journal.py",
                "verification_scope_attestation.py",
                "verification_dependencies.py",
                "verification_source_preparation.py",
            )
        }
    )


def profile_digest() -> str:
    from firefly_forge.invalid_read_continuities import current_profile_seal

    return digest(current_profile_seal())


@dataclass
class PreparedLinkedVerification:
    candidate: LinkedVerificationCandidateV1
    run: Data
    entries: list[Data]
    verify_task: Data
    workspace: Path
    source: Any
    inputs: PreparedBuildScope
    actor: Principal
    reader: Any
    ownership: object
    generation: object
    service: LinkedVerifications
    slot: Any
    scm: PreparedSCMLinkedVerificationAdmission | None = None
    scm_seal: SCMLinkedVerificationAdmissionV1 | None = None
    scm_digest: str | None = None

    def bind_scm(self, prepared: PreparedSCMLinkedVerificationAdmission, request: LinkedVerificationInputV1) -> None:
        # Capture the validated seal before the authority's first awaited revalidation.
        seal = _scm_binding(
            prepared.seal.model_dump(mode="json"),
            ProjectAccess.actor_snapshot(self.actor),
            self.candidate,
            request,
            self.run,
        )
        self.scm, self.scm_seal, self.scm_digest = prepared, seal, seal.digest
        self.fence()

    def _scm_view(self) -> None:
        if self.scm is not None:
            seal = SCMLinkedVerificationAdmissionV1.model_validate(self.scm.seal.model_dump(mode="json"))
            if seal != self.scm_seal or seal.digest != self.scm_digest:
                raise ValueError("SCM verification grant view changed after preparation")

    def _authority_fence(self) -> None:
        store = self.service.store
        self._scm_view()
        if self.scm is not None:
            self.scm.fence()
        if (
            store.execution_ownership_token is not self.ownership
            or store.configuration.write_generation is not self.generation
            or CURRENT_PRINCIPAL.get() != self.reader
            or actor_digest(self.actor) != self.candidate.actor_digest
            or self.service.engine.active.get(self.run["id"]) is not self.slot
            or (self.slot is not None and self.slot.done())
            or (
                self.slot is not None
                and self.service.engine.active_projects.get(self.run["id"]) != self.run["repository_id"]
            )
            or digest({"owner": store.execution_ownership_identity, "generation": id(self.generation)})
            != self.candidate.owner_digest
        ):
            raise AccessDenied("Linked verification lost its captured owner, identity or configuration")
        if (
            digest(self.run) != self.candidate.run_digest
            or digest({"entries": self.entries}) != self.candidate.journal_digest
            or digest(self.inputs.witness.model_dump(mode="json")) != self.candidate.input_scope_digest
            or digest(self.source.cut) != self.candidate.source_digest
        ):
            raise ValueError("Linked verification prepared view changed")
        if (
            _runtime_material_digest() != self.candidate.runtime_contract
            or profile_digest() != self.candidate.profile_digest
            or _policy_material_digest() != self.candidate.policy_digest
        ):
            raise ValueError("Linked verification qualified runtime changed")
        if digest(git_index_identity(self.workspace).model_dump(mode="json")) != self.candidate_index:
            raise ValueError("Linked verification staged source changed")

    def fence(self) -> None:
        self._authority_fence()
        self.source.fence.revalidate()

    @property
    def candidate_index(self) -> str:
        return digest(self.source.index.model_dump(mode="json"))

    async def _authority_current(self) -> None:
        if self.scm is not None:
            self._scm_view()
            await self.scm.current()
            self._authority_fence()
        await self.service.store.assert_ownership()
        await self.service._authority(self.run, self.actor)
        async with self.service.store._unit():
            if (
                await self.service.store.get_run(self.run["id"]) != self.run
                or _entries(await self.service.journal.records.find_for_run(identifier(self.run["id"])), self.run["id"])
                != self.entries
                or _task(await self.service.journal._task(self.run["id"], "verify")) != self.verify_task
            ):
                raise ValueError("Linked verification parent frontier changed")
        self._authority_fence()

    async def current(self) -> None:
        await self._authority_current()
        self.source.fence.revalidate()

    async def revalidate(self) -> None:
        await self.inputs.fence()
        await self.current()

    def _result_payload_current(self) -> None:
        self._authority_fence()
        fence = self.source.fence
        if not isinstance(fence, SelectedSourceFence):
            raise ValueError("Known QA outputs require their original typed source witness")
        root = _directory_fd(self.workspace)
        try:
            if _descriptor_path(root) != fence.workspace or tuple(_stamp(os.fstat(root))[:4]) != fence.root_identity:
                raise ValueError("Known QA outputs changed the original source root")
            deadline = time.monotonic() + fence.timeout_seconds
            for stream in fence.streams[:2]:
                if stream.closed:
                    raise ValueError("Known QA outputs lost the original source witness")
                # Existing capture permits directory timestamps only while canonical selection is rechecked.
                _revalidate(root, stream, deadline)
            _revalidate(root, fence.streams[2], deadline, strict_directories=True)
        finally:
            os.close(root)

    async def after_result(self) -> None:
        """Rebind ignored output namespaces only after a known successful native result."""
        phase: VerificationHoldPhase = "post_result_inputs"

        def record_phase(value: VerificationHoldPhase) -> None:
            nonlocal phase
            phase = value

        try:
            await self._after_result(record_phase)
        except Exception as error:
            raise _PostResultGuardFailure(phase, error) from None

    async def _after_result(self, phase_changed: Callable[[VerificationHoldPhase], None]) -> None:
        before = self.source
        await self.inputs.fence()
        phase_changed("post_result_source")
        self._result_payload_current()
        phase_changed("post_result_recapture")
        after = await workspace_source_capture(
            self.service.store, self.workspace, assert_current=self._authority_current
        )
        try:
            phase_changed("post_result_source")
            self._result_payload_current()
            if (
                after.cut != before.cut
                or after.index != before.index
                or after.physical_digest is None
                or after.physical_digest != before.physical_digest
                or after.fence is None
            ):
                raise ValueError("Known QA outputs changed selected source, identity or index")
            phase_changed("post_result_authority")
            await self._authority_current()
            after.fence.revalidate()
            self.source = after
        except BaseException:
            if after.fence is not None:
                after.fence.close()
            raise
        before.fence.close()
        phase_changed("post_result_inputs")
        await self.inputs.fence()
        phase_changed("post_result_authority")
        await self.current()

    def close(self) -> None:
        self.source.fence.close()


@service
class LinkedVerifications:
    def __init__(self, store: Store, journal: Journal, requests: ExecutionRequests, engine: Any) -> None:
        self.store, self.journal, self.requests, self.engine = store, journal, requests, engine
        self.completions = BridgeCompletions(store, journal)

    def _scm_authority(self) -> SCMLinkedVerificationAuthority:
        authority = getattr(self.store, "scm_linked_verification_authority", None)
        if authority is None:
            raise AccessDenied("The native SCM verification consent authority is unavailable")
        return cast(SCMLinkedVerificationAuthority, authority)

    async def _authority(self, run: Data, actor: Principal) -> None:
        await self.completions._execution_authority(run, CURRENT_PRINCIPAL.get())
        mapped = actor.authentication_method == "scm_mapping"
        if mapped:
            _scm_scope(ProjectAccess.actor_snapshot(actor), run)
            await self._scm_authority().review(actor, run["id"])
        await ProjectAccess(self.store).authorize_actor(
            ProjectAccess.actor_snapshot(actor), run.get("project_id"), "execute"
        )
        user = await self.completions._identity(actor.id.hex)
        if (user.issuer, user.subject) != (actor.issuer, actor.subject):
            raise AccessDenied("Linked verification consent identity changed")
        if (
            not mapped
            and validate_sandbox_snapshot(run["sandbox_snapshot"]).mode == "host"
            and not await ProjectAccess(self.store).current_administrator(actor)
        ):
            raise AccessDenied("Fresh host verification requires a current factory administrator")
        await self.store.assert_campaign_current(run)

    async def _actor(self, saved: Data | None = None, identity: Data | None = None) -> Principal:
        actor = CURRENT_PRINCIPAL.get()
        if saved is None:
            if actor is not None and actor.authentication_method == "scm_mapping":
                _scm_scope(ProjectAccess.actor_snapshot(actor))
                self._scm_authority()
                return actor
            if actor is None or actor.authentication_method not in {"oidc_session", "oidc_bearer", "local_bootstrap"}:
                raise AccessDenied("A current native operator must consent to fresh verification")
            return actor
        user = await self.completions._identity(saved["user_id"])
        if identity is None or identity != {"issuer": user.issuer, "subject": user.subject}:
            raise AccessDenied("Linked verification saved consent identity changed")
        principal = Principal(
            user.id,
            user.issuer,
            user.subject,
            authentication_method=saved["authentication_method"],
            api_roles=frozenset({"forge-admin"}) if saved.get("global_admin") else frozenset(),
            api_audience=saved.get("api_audience"),
            scm_delegation=copy.deepcopy(saved.get("scm_delegation")),
        )
        if ProjectAccess.actor_snapshot(principal) != saved:
            raise AccessDenied("Linked verification consent metadata changed")
        if principal.authentication_method == "scm_mapping":
            _scm_scope(saved)
            self._scm_authority()
        if saved.get("global_admin") and not await ProjectAccess(self.store).current_administrator(principal):
            raise AccessDenied("Linked verification administrator consent was revoked")
        return principal

    def _transport(self, run: Data) -> Any:
        config = validate_sandbox_snapshot(run["sandbox_snapshot"])
        if config.mode != "host" or config.pool_binding is not None or self.engine.sandbox is None:
            raise ValueError(
                "Linked verification supports its configured host transport; Docker and pools are unsupported"
            )
        return self.engine.sandbox.backends.select(config)

    async def _inputs(
        self, run: Data, entries: list[Data], workspace: Path, current: Any, *, actor: Principal
    ) -> PreparedBuildScope:
        proof = RetainedVerifiedSourceV1.model_validate(run["usage"]["retained_source"])
        source = await prepare_verification_source(
            workspace,
            verification_catalog_root(self.store.path, run["id"]),
            budget=self.journal.source_catalog_budget,
            assert_current=current,
        )
        if source.source_digest != proof.source_digest:
            raise ValueError("Linked verification source differs from the accepted retained source")
        handoff = next(e for e in entries if e["digest"] == proof.handoff_intent_digest)
        inputs = await prepare_build_scope(
            self.store,
            self.journal,
            self.engine,
            run,
            handoff["payload"],
            handoff,
            current,
            scratch_identity=digest(
                {
                    "parent": digest(run),
                    "actor": actor_digest(actor),
                    "commands": final_commands(run, workspace),
                    "runtime": runtime_contract(),
                    "policy": policy_digest(),
                }
            ),
        )
        if inputs is None:
            raise ValueError("Linked verification requires its current qualified dependency input scope")
        return inputs

    async def _prepare(self, run_id: str, actor: Principal) -> PreparedLinkedVerification:
        _outside_unit()
        identifier(run_id)
        run = await self.store.get_run(run_id)
        await self._authority(run, actor)
        await self.store.assert_ownership()
        self._transport(run)
        owner, generation, reader = (
            self.store.execution_ownership_token,
            self.store.configuration.write_generation,
            CURRENT_PRINCIPAL.get(),
        )
        active = self.engine.active.get(run_id)
        if active is not None and not active.done() and active is not asyncio.current_task():
            raise ValueError("A worker still owns the verification source")
        if any(
            not task.done() and key != run_id and self.engine.active_projects.get(key) == run["repository_id"]
            for key, task in self.engine.active.items()
        ):
            raise ValueError("The repository has another active worker")
        async with self.store._unit():
            entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
            implement = _task(await self.journal._task(run_id, "implement"))
            verify = _task(await self.journal._task(run_id, "verify"))
            rows = await self.journal.sandboxes.for_run(identifier(run_id))
        commands = final_commands(run, self.engine.component_directory(run, self.engine.workspaces / run_id))
        config = validate_sandbox_snapshot(run["sandbox_snapshot"])
        for command in commands:
            effective_command_timeout(command, config)
        terminals = [
            e
            for e in entries
            if e["kind"] == "terminal_completion_receipt"
            and e["payload"]["proof"].get("retained_source") == run.get("usage", {}).get("retained_source")
        ]
        receipts = [
            e
            for e in entries
            if e["kind"] == "receipt"
            and e["task_id"] == "implement"
            and e["idempotency_key"] == implement.get("idempotency_key")
        ]
        if (
            run["status"] != "paused"
            or run.get("implementation_pending") is not False
            or implement["status"] != "done"
            or implement["attempt"] != run.get("attempt")
            or verify["status"] != "queued"
            or not commands
            or len(terminals) != 1
            or len(receipts) != 1
            or not run.get("usage", {}).get("retained_source")
            or run.get("bridge_completion_hold")
        ):
            raise ValueError("Fresh verification requires an accepted implementation and untouched verify task")
        terminal = validate_native_receipt(terminals[0], entries)
        tree = receipts[0]["payload"]["source_tree"]
        if (
            terminal.proof.task_key != implement["idempotency_key"]
            or terminal.proof.task_attempt != implement["attempt"]
        ):
            raise ValueError("Linked verification terminal belongs to another implementation")

        from firefly_forge.consumed_terminal_completions import prepare_accepted_reconciled_handoff
        from firefly_forge.verification_models import verification_scope

        proof = RetainedVerifiedSourceV1.model_validate(run["usage"]["retained_source"])
        if (
            proof.run_id != run_id
            or proof.attempt != run["attempt"]
            or proof.task_key != implement["idempotency_key"]
            or proof.commands_digest != digest({"commands": run["commands"]})
            or proof.sandbox_digest != run["sandbox_snapshot"]["digest"]
            or proof.scope_digest != verification_scope(run)
        ):
            raise ValueError("Linked verification changed the accepted implementation scope")
        accepted = await prepare_accepted_reconciled_handoff(self.store, self.journal, run, entries, terminals[0])
        if accepted is not None:
            await accepted.current()

        async def current() -> None:
            await self.store.assert_ownership()
            await self._authority(run, actor)
            if await self.store.get_run(run_id) != run:
                raise ValueError("Linked verification parent changed during input preparation")
            if (
                self.store.execution_ownership_token is not owner
                or self.store.configuration.write_generation is not generation
                or CURRENT_PRINCIPAL.get() != reader
            ):
                raise AccessDenied("Linked verification input owner changed")

        workspace = self.engine.workspaces / run_id
        source = await workspace_source_capture(self.store, workspace, assert_current=current)
        try:
            await checked(
                ["git", "diff", "--cached", "--quiet", "--no-ext-diff", "--no-textconv", tree, "--"], workspace
            )
            inputs = await self._inputs(
                run, entries, self.engine.component_directory(run, workspace), current, actor=actor
            )
            config = validate_sandbox_snapshot(run["sandbox_snapshot"])
            value = dict(
                version=1,
                run_id=run_id,
                actor_digest=actor_digest(actor),
                policy_digest=policy_digest(),
                runtime_contract=runtime_contract(),
                profile_digest=profile_digest(),
                runner_digest=digest(run["sandbox_snapshot"]),
                input_scope_digest=digest(inputs.witness.model_dump(mode="json")),
                source_digest=digest(source.cut),
                source_tree=tree,
                implementation_receipt_digest=receipts[0]["digest"],
                terminal_receipt_digest=terminals[0]["digest"],
                commands_digest=digest({"commands": commands}),
                effect_budget=len(commands),
                original_effect_budget=config.command_limit,
                original_effect_count=len(rows),
                owner_digest=digest({"owner": self.store.execution_ownership_identity, "generation": id(generation)}),
                run_digest=digest(run),
                journal_digest=digest({"entries": entries}),
            )
            value["digest"] = digest(value)
            candidate = LinkedVerificationCandidateV1.model_validate(value)
            prepared = PreparedLinkedVerification(
                candidate,
                run,
                entries,
                verify,
                workspace,
                source,
                inputs,
                actor,
                reader,
                owner,
                generation,
                self,
                active,
            )
            await prepared.revalidate()
            return prepared
        except BaseException:
            if source.fence is not None:
                source.fence.close()
            raise

    async def candidate(self, run_id: str) -> Data:
        actor = await self._actor()
        async with mode_execution_scope(self.journal, run_id):
            prepared = await self._prepare(run_id, actor)
            try:
                value = prepared.candidate.model_dump(mode="json")
                await self.store.admit_credentials(value)
                return value
            finally:
                prepared.close()

    async def _events(self, row: Any) -> list[Data]:
        self._operation(row)
        return [
            copy.deepcopy(event.data)
            for event in await self.requests.journal.linked_verification_events(
                row.id, 2 * row.data["effect_budget"] + 5
            )
        ]

    @staticmethod
    def _operation(row: Any) -> None:
        data = row.data
        candidate = LinkedVerificationCandidateV1.model_validate(data["candidate"])
        body = LinkedVerificationInputV1.model_validate(data["request"])
        if data["actor"].get("authentication_method") == "scm_mapping":
            _scm_binding(data.get("scm_authority"), data["actor"], candidate, body, data["parent_run"])
        elif data.get("scm_authority") is not None:
            raise ValueError("Native verification cannot borrow an SCM grant")
        if (
            row.kind != KIND
            or row.status != data["status"]
            or row.id.hex != data["id"]
            or row.run_id.hex != data["run_id"]
            or data["run_id"] != candidate.run_id
            or body.request_id != data["id"]
            or body.candidate_digest != candidate.digest
            or data["request_digest"] != request_digest(body.model_dump(mode="json"))
            or data["effect_budget"] != candidate.effect_budget
            or digest({"commands": data["commands"]}) != candidate.commands_digest
            or data["commands_digest"] != candidate.commands_digest
            or data["inputs_digest"] != candidate.input_scope_digest
            or digest(data["parent_run"]) != candidate.run_digest
            or digest({"actor": data["actor"], "identity": data["identity"]}) != candidate.actor_digest
        ):
            raise ValueError("Linked verification native projection differs from its sealed consent")

    async def _event(self, row: Any, kind: str, evidence: Data) -> Data:
        events = await self._events(row)
        value = dict(
            request_id=row.id.hex,
            sequence=len(events) + 1,
            previous_hash=events[-1]["digest"] if events else "0" * 64,
            kind=kind,
            evidence=evidence,
            created_at=now(),
        )
        value["digest"] = request_digest(value)
        verification_progress(row.data, [*events, value])
        await self.requests.journal.save(
            ForgeExecutionRequestEventEntity(id=uuid.uuid4(), request_id=row.id, sequence=value["sequence"], data=value)
        )
        return value

    async def get(self, run_id: str, request_id: str) -> Data:
        actor = await self._actor()
        run = await self.store.get_run(run_id)
        await self._authority(run, actor)
        async with self.store._unit():
            row = await self.requests.records.find_by_id(identifier(request_id))
            if row is None or row.kind != KIND or row.run_id != identifier(run_id):
                raise KeyError("Linked verification operation not found")
            data, events = copy.deepcopy(row.data), await self._events(row)
        if actor.authentication_method == "scm_mapping" and (
            actor_digest(actor) != data["candidate"]["actor_digest"]
            or _scm_scope(ProjectAccess.actor_snapshot(actor), run).proposal_id != request_id
        ):
            raise AccessDenied("SCM verification status belongs to another proposal")
        value = dict(
            version=1,
            kind=KIND,
            run_id=run_id,
            request_id=request_id,
            request_digest=data["request_digest"],
            candidate=data["candidate"],
            request=data["request"],
            status=data["status"],
            progress=verification_progress(data, events).model_dump(),
            event_digest=events[-1]["digest"],
            no_coding_grant=True,
            original_budget_preserved=True,
            detail=data.get("detail", ""),
        )
        failures = [
            event
            for event in events
            if event["kind"] == "linked_verification_command_result" and event["evidence"]["exit_code"] != 0
        ]
        if failures:
            event = failures[-1]
            evidence = event["evidence"]
            retained = redact_secrets(evidence["output"], self.engine.credential_registry()).encode()
            preview = retained[-2000:].decode("utf-8", errors="ignore")
            value["last_failure"] = dict(
                ordinal=evidence["ordinal"],
                command_digest=digest(data["commands"][evidence["ordinal"] - 1]),
                exit_code=evidence["exit_code"],
                output_preview=preview,
                output_truncated=len(retained) != len(preview.encode()),
                event_digest=event["digest"],
            )
        if data.get("settlement") is not None:
            value["settlement"] = data["settlement"]
        if events[-1]["kind"] == "linked_verification_held" and "diagnostic" in events[-1]["evidence"]:
            value["hold_diagnostic"] = events[-1]["evidence"]["diagnostic"]
        result = LinkedVerificationReceiptV1.model_validate(value).model_dump(mode="json")
        await self.store.admit_credentials(result)
        return result

    async def request(self, run_id: str, body: LinkedVerificationInputV1) -> Data:
        body = LinkedVerificationInputV1.model_validate(body.model_dump(mode="json"))
        actor = await self._actor()
        async with self.store._unit():
            existing = await self.requests.records.find_by_id(identifier(body.request_id))
        if existing is not None:
            if (
                existing.kind != KIND
                or existing.run_id != identifier(run_id)
                or existing.data["request"] != body.model_dump(mode="json")
                or existing.data["candidate"]["actor_digest"] != actor_digest(actor)
            ):
                raise ValueError("Linked verification identity already belongs to different consent")
            return await self.get(run_id, body.request_id)
        async with self.engine.publication_lock, mode_execution_scope(self.journal, run_id):
            prepared = await self._prepare(run_id, actor)
            try:
                if body.candidate_digest != prepared.candidate.digest:
                    raise ValueError("Reviewed fresh verification inputs changed")
                if actor.authentication_method == "scm_mapping":
                    grant = await self._scm_authority().admit(actor, run_id, prepared.candidate, body)
                    prepared.bind_scm(grant, body)
                    await prepared.current()
                data = dict(
                    id=body.request_id,
                    run_id=run_id,
                    kind=KIND,
                    status="queued",
                    request=body.model_dump(mode="json"),
                    request_digest=request_digest(body.model_dump(mode="json")),
                    candidate=prepared.candidate.model_dump(mode="json"),
                    actor=ProjectAccess.actor_snapshot(actor),
                    identity=actor_identity(actor),
                    parent_run=copy.deepcopy(prepared.run),
                    commands=final_commands(
                        prepared.run, self.engine.component_directory(prepared.run, prepared.workspace)
                    ),
                    commands_digest=prepared.candidate.commands_digest,
                    effect_budget=prepared.candidate.effect_budget,
                    inputs_digest=prepared.candidate.input_scope_digest,
                    created_at=now(),
                    updated_at=now(),
                )
                if prepared.scm_seal is not None:
                    data["scm_authority"] = prepared.scm_seal.model_dump(mode="json")
                await self.store.admit_credentials(data)
                async with self.store.source_budget_admission_unit():
                    unit, task = current_unit_of_work(), asyncio.current_task()
                    if unit is None or task is None:
                        raise AccessDenied("Linked verification admission requires its native owner unit")
                    register_synchronization(ReconciliationCommitFence(unit, task, prepared.fence))
                    await self.completions._writer(prepared.run, actor)
                    await self.journal._run(run_id)
                    if await self.requests.records.count_for_run(identifier(run_id), KIND):
                        raise ValueError("This accepted frontier already has an independent verification operation")
                    await prepared.current()
                    row = ForgeExecutionRequestEntity(
                        id=identifier(body.request_id),
                        run_id=identifier(run_id),
                        kind=KIND,
                        request_key=prepared.candidate.implementation_receipt_digest,
                        status="queued",
                        data=data,
                    )
                    await self.requests.records.save(row)
                    await self._event(row, "linked_verification_admitted", {"request_digest": data["request_digest"]})
            finally:
                prepared.close()
        return await self.get(run_id, body.request_id)

    async def _locked(self, operation_id: str) -> Any:
        observed = await self.requests.get(operation_id)
        await self.journal._run(observed["run_id"])
        row = await self.requests.records.find_by_id(identifier(operation_id), lock=LockMode.PESSIMISTIC_WRITE)
        if (
            row is None
            or row.kind != KIND
            or row.run_id.hex != observed["run_id"]
            or row.data["request_digest"] != request_digest(row.data["request"])
        ):
            raise ValueError("Linked verification operation lost its native request identity")
        verification_progress(row.data, await self._events(row))
        return row

    async def _write(self, prepared: PreparedLinkedVerification, operation_id: str, kind: str, evidence: Data) -> Data:
        await self.store.admit_credentials(evidence)
        async with self.store.source_budget_admission_unit():
            unit, task = current_unit_of_work(), asyncio.current_task()
            if unit is None or task is None:
                raise AccessDenied("Verification effects require their native owned writer")
            register_synchronization(ReconciliationCommitFence(unit, task, prepared.fence))
            await self.completions._writer(prepared.run, prepared.actor)
            row = await self._locked(operation_id)
            await prepared.current()
            if row.status not in {"queued", "executing"}:
                raise ValueError("Closed verification cannot admit another effect")
            event = await self._event(row, kind, evidence)
            if kind == "linked_verification_claimed":
                row.status = "executing"
                row.data = {**row.data, "status": row.status, "updated_at": now()}
                await self.requests.records.save(row)
            return event

    async def _hold(
        self, operation_id: str, reason: str, diagnostic: LinkedVerificationHoldDiagnosticV1 | None = None
    ) -> None:
        # Outcome retention grants no new effect and must survive changed source inputs.
        await self.store.assert_ownership()
        async with self.store.source_budget_admission_unit():
            row = await self._locked(operation_id)
            if row.status not in {"queued", "executing"}:
                return
            evidence = {"reason": reason, **({"diagnostic": diagnostic.model_dump(mode="json")} if diagnostic else {})}
            await self._event(row, "linked_verification_held", evidence)
            row.status = "failed" if reason == "check_failed" else "needs_reconciliation"
            row.data = {**row.data, "status": row.status, "detail": reason, "updated_at": now()}
            await self.requests.records.save(row)

    async def _result(self, operation_id: str, evidence: Data) -> None:
        # A known result remains evidence even if the command changed a protected input.
        owner, generation, reader = (
            self.store.execution_ownership_token,
            self.store.configuration.write_generation,
            CURRENT_PRINCIPAL.get(),
        )
        await self.store.admit_credentials(evidence)

        def current() -> None:
            if (
                self.store.execution_ownership_token is not owner
                or self.store.configuration.write_generation is not generation
                or CURRENT_PRINCIPAL.get() != reader
            ):
                raise AccessDenied("Verification result retention lost its current native owner")

        async with self.store.source_budget_admission_unit():
            unit, task = current_unit_of_work(), asyncio.current_task()
            if unit is None or task is None:
                raise AccessDenied("Verification result requires its native owned transaction")
            register_synchronization(ReconciliationCommitFence(unit, task, current))
            row = await self._locked(operation_id)
            if row.status != "executing":
                raise ValueError("A closed verification cannot receive another command result")
            await self._event(row, "linked_verification_command_result", evidence)

    async def execute(self, operation_id: str) -> None:
        self.requests.runtime.require_execution()
        await self.store.assert_ownership()
        observed = await self.requests.get(operation_id)
        if observed["kind"] != KIND or observed["status"] != "queued":
            return
        run_id, prepared, reserved = observed["run_id"], None, False
        phase: VerificationHoldPhase = "prepare"
        try:
            actor = await self._actor(observed["actor"], observed["identity"])
            async with mode_execution_scope(self.journal, run_id):
                async with self.engine.publication_lock:
                    if not await self.engine.reserve_agent_restore(run_id):
                        raise ValueError("Fresh verification requires its owned inactive repository slot")
                    reserved = True
                prepared = await self._prepare(run_id, actor)
                if prepared.candidate.model_dump(mode="json") != observed["candidate"]:
                    raise ValueError("Fresh verification inputs changed after explicit consent")
                if actor.authentication_method == "scm_mapping":
                    grant = await self._scm_authority().worker(observed["actor"], observed["identity"], observed)
                    prepared.bind_scm(grant, LinkedVerificationInputV1.model_validate(observed["request"]))
                    if prepared.scm_seal is None or prepared.scm_seal.model_dump(mode="json") != observed.get(
                        "scm_authority"
                    ):
                        raise ValueError("SCM verification worker changed its stored grant")
                    await prepared.current()
                async with self.engine.publication_lock:
                    phase = "admission"
                    await self._write(
                        prepared,
                        operation_id,
                        "linked_verification_claimed",
                        {"request_digest": observed["request_digest"]},
                    )
                backend = self._transport(prepared.run)
                config = validate_sandbox_snapshot(prepared.run["sandbox_snapshot"])
                for ordinal, command in enumerate(observed["commands"], 1):
                    phase = "command_inputs"
                    await prepared.revalidate()
                    intent = await self._write(
                        prepared,
                        operation_id,
                        "linked_verification_command_intent",
                        {
                            "ordinal": ordinal,
                            "command_digest": digest(command),
                            "inputs_digest": observed["inputs_digest"],
                        },
                    )
                    from firefly_forge.native_location_projection import runner_location

                    argv, _location, guard = await runner_location(self.store, run_id, "verify", command["argv"])
                    if guard is not None:
                        await guard()
                    await prepared.revalidate()
                    timeout = effective_command_timeout(command, config)
                    selected = next(
                        (check for check in prepared.inputs.witness.checks if check.command_index == ordinal), None
                    )
                    physical = list(selected.wrapped_argv) if selected is not None else argv
                    scratch = Path(prepared.inputs.witness.scratch) / str(ordinal) if selected is not None else None
                    if scratch is not None and any(any((scratch / part).iterdir()) for part in ("home", "tmp")):
                        raise ValueError("A fresh verification check requires its empty owned scratch inputs")
                    with (
                        build_execution_environment(scratch, prepared.inputs.environment())
                        if scratch is not None
                        else nullcontext()
                    ):
                        phase = "dispatch"
                        result = await backend.run(
                            config,
                            physical,
                            self.engine.component_directory(prepared.run, prepared.workspace),
                            timeout,
                            config.max_output_bytes,
                        )
                    if (
                        not isinstance(result, ProcessResult)
                        or result.argv != physical
                        or type(result.exit_code) is not int
                    ):
                        raise ValueError("Verification runner returned no exact native process result")
                    output = (
                        redact_secrets(result.output, self.engine.credential_registry())
                        .encode()[-32000:]
                        .decode("utf-8", errors="ignore")
                    )
                    phase = "result_retention"
                    await self._result(
                        operation_id,
                        {
                            "ordinal": ordinal,
                            "intent_digest": intent["digest"],
                            "exit_code": result.exit_code,
                            "output": output,
                        },
                    )
                    if result.exit_code != 0:
                        await self._hold(operation_id, "check_failed")
                        return
                    await prepared.after_result()
                async with self.engine.publication_lock:
                    phase = "settlement"
                    await self._settle(prepared, operation_id)
        except asyncio.CancelledError:
            await asyncio.shield(self._hold(operation_id, "shutdown"))
            raise
        except Exception as error:
            diagnostic = (
                error.diagnostic if isinstance(error, _PostResultGuardFailure) else _hold_diagnostic(phase, error)
            )
            await self._hold(
                operation_id, "inputs_changed" if isinstance(error, _PostResultGuardFailure) else "unknown", diagnostic
            )
        finally:
            if prepared is not None:
                prepared.close()
            if reserved:
                self.engine.release_agent_restore(run_id)

    async def _settle(self, prepared: PreparedLinkedVerification, operation_id: str) -> None:
        await prepared.revalidate()
        async with self.store.source_budget_admission_unit():
            unit, task = current_unit_of_work(), asyncio.current_task()
            if unit is None or task is None:
                raise AccessDenied("Parent verification settlement requires its owned native writer")
            register_synchronization(ReconciliationCommitFence(unit, task, prepared.fence))
            await self.completions._writer(prepared.run, prepared.actor)
            row = await self._locked(operation_id)
            events = await self._events(row)
            progress = verification_progress(row.data, events)
            if row.status != "executing" or not progress.ready_to_settle:
                raise ValueError("Parent verification requires the complete successful independent ledger")
            await prepared.current()
            complete = await self._event(
                row,
                "linked_verification_completed",
                {"inputs_digest": row.data["inputs_digest"], "completed": row.data["effect_budget"]},
            )
            checks = _completed_checks(row.data, events)
            intent = await self.journal.intent(
                prepared.run["id"],
                "verify",
                attempt=prepared.verify_task["attempt"],
                payload={"linked_verification": operation_id, "source_tree": prepared.candidate.source_tree},
            )
            if intent["disposition"] != "execute":
                raise ValueError("Parent verification task was already claimed")
            receipt = await self.journal._receipt(
                prepared.run["id"],
                intent["idempotency_key"],
                {
                    "checks": checks,
                    "failed_checks": [],
                    "source_tree": prepared.candidate.source_tree,
                    "implementation_round": prepared.run["attempt"],
                    "linked_verification": {"request_id": operation_id, "operation_receipt_digest": complete["digest"]},
                },
            )
            parent = await self.journal._run(prepared.run["id"])
            parent.data = {**parent.data, "source_tree": prepared.candidate.source_tree, "final_verification": checks}
            await self.store.run_records.save(parent)
            row.status = "completed"
            row.data = {
                **row.data,
                "status": row.status,
                "updated_at": now(),
                "detail": (
                    "Fresh verification completed on the configured host transport. "
                    "Docker and pool transports are unsupported."
                ),
                "settlement": {
                    "parent_verify_receipt_digest": receipt["digest"],
                    "operation_receipt_digest": complete["digest"],
                    "source_tree": prepared.candidate.source_tree,
                    "source_digest": prepared.candidate.source_digest,
                    "commands_digest": prepared.candidate.commands_digest,
                    "checks_count": len(checks),
                },
            }
            await self.requests.records.save(row)

    async def recover(self) -> None:
        async with self.store._unit():
            rows = await self.requests.records.state("executing", kind=KIND)
        for row in rows:
            await self._hold(row.id.hex, "shutdown")

    async def validate_settlement(self, run_id: str, tree: str, task: Data) -> None:
        from firefly_forge.unknown_command_proof import continuation_scope

        link = task["evidence"]["linked_verification"]
        async with self.store._unit():
            row = await self.requests.records.find_by_id(identifier(link["request_id"]))
            if row is None:
                raise ValueError("Parent verification lost its linked native operation")
            self._operation(row)
            data, events = copy.deepcopy(row.data), await self._events(row)
            entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
            run = await self.store.get_run(run_id)
        progress = verification_progress(data, events)
        candidate = LinkedVerificationCandidateV1.model_validate(data["candidate"])
        settlement = data.get("settlement") or {}
        receipts = [e for e in entries if e["digest"] == settlement.get("parent_verify_receipt_digest")]
        if (
            row.run_id.hex != run_id
            or row.status != "completed"
            or len(receipts) != 1
            or receipts[0]["kind"] != "receipt"
            or receipts[0]["task_id"] != "verify"
            or receipts[0]["payload"] != task["evidence"]
            or task["status"] != "done"
            or run.get("final_verification") != _completed_checks(data, events)
            or task["evidence"].get("checks") != _completed_checks(data, events)
            or task["evidence"].get("failed_checks") != []
            or settlement.get("source_tree") != candidate.source_tree
            or settlement.get("source_digest") != candidate.source_digest
            or settlement.get("commands_digest") != candidate.commands_digest
            or settlement.get("checks_count") != candidate.effect_budget
            or settlement.get("operation_receipt_digest") != events[-1]["digest"]
            or link.get("operation_receipt_digest") != events[-1]["digest"]
            or events[-1]["kind"] != "linked_verification_completed"
            or progress.completed != candidate.effect_budget
            or progress.unknown
            or progress.failed
            or tree != candidate.source_tree
            or task["evidence"].get("source_tree") != tree
            or continuation_scope(run) != continuation_scope(data["parent_run"])
            or any(
                run.get(key) != data["parent_run"].get(key)
                for key in (
                    "attempt",
                    "usage",
                    "implementation_rounds",
                    "implementation_pending",
                    "verification_handoff",
                    "consumed_terminal_recovery",
                    "agent",
                    "config_revision",
                    "execution_config",
                    "model_selection",
                    "checkpoint_mode_adoption",
                    "original_selection_readmission",
                    "invalid_read_continuity",
                )
            )
        ):
            raise ValueError("Parent verification changed its exact complete linked receipt or original scope")
        expected_commands = final_commands(run, self.engine.component_directory(run, self.engine.workspaces / run_id))
        if (
            data["commands"] != expected_commands
            or candidate.commands_digest != digest({"commands": expected_commands})
            or candidate.effect_budget != len(expected_commands)
        ):
            raise ValueError("Linked verification omitted or changed required final checks")
        actor = await self._actor(data["actor"], data["identity"])
        grant = None
        grant_digest = None
        if actor.authentication_method == "scm_mapping":
            grant = await self._scm_authority().worker(data["actor"], data["identity"], data)
            seal = _scm_binding(
                grant.seal.model_dump(mode="json"),
                data["actor"],
                candidate,
                LinkedVerificationInputV1.model_validate(data["request"]),
                data["parent_run"],
            )
            if seal.model_dump(mode="json") != data.get("scm_authority"):
                raise ValueError("SCM verification settlement changed its stored grant")
            grant_digest = seal.digest
        owner, generation, reader = (
            self.store.execution_ownership_token,
            self.store.configuration.write_generation,
            CURRENT_PRINCIPAL.get(),
        )

        async def current() -> None:
            if grant is not None:
                await grant.current()
                seal = SCMLinkedVerificationAdmissionV1.model_validate(grant.seal.model_dump(mode="json"))
                if seal.digest != grant_digest:
                    raise ValueError("SCM verification settlement grant view changed")
                grant.fence()
            await self.store.assert_ownership()
            await self._authority(run, actor)
            if (
                await self.store.get_run(run_id) != run
                or self.store.execution_ownership_token is not owner
                or self.store.configuration.write_generation is not generation
                or CURRENT_PRINCIPAL.get() != reader
            ):
                raise AccessDenied("Linked verification settlement lost its current source owner")
            if grant is not None:
                if (
                    SCMLinkedVerificationAdmissionV1.model_validate(grant.seal.model_dump(mode="json")).digest
                    != grant_digest
                ):
                    raise ValueError("SCM verification settlement grant view changed")
                grant.fence()

        if (
            runtime_contract() != candidate.runtime_contract
            or profile_digest() != candidate.profile_digest
            or policy_digest() != candidate.policy_digest
        ):
            raise ValueError("Linked verification current qualified authority changed")
        inputs = await self._inputs(
            data["parent_run"],
            entries,
            self.engine.component_directory(run, self.engine.workspaces / run_id),
            current,
            actor=actor,
        )
        if digest(inputs.witness.model_dump(mode="json")) != candidate.input_scope_digest:
            raise ValueError("Linked verification current dependencies or inputs changed after success")
        await inputs.fence()
        await current()
