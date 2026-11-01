# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Independent QA must have a confirmed closed outcome before WIP Git effects."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pyfly.data.transaction import current_unit_of_work
from pyfly.data.transaction.synchronization import TransactionSynchronizationAdapter, register_synchronization
from pyfly.data.transaction.unit_of_work import UnitOfWork

from firefly_forge.checkpoint_source_identity import SelectedSourceFence
from firefly_forge.journal import digest
from firefly_forge.known_worker_completions import _entries, _task
from firefly_forge.linked_verification_models import (
    KIND,
    LinkedVerificationCandidateV1,
    LinkedVerificationInputV1,
    LinkedVerificationReceiptV1,
    verification_progress,
)
from firefly_forge.linked_verifications import _completed_checks
from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_linked_verification_models import PreparedSCMLinkedVerificationAdmission
from firefly_forge.source_snapshot import GitIndexIdentityV1, git_index_identity
from firefly_forge.store import identifier
from firefly_forge.unknown_command_proof import continuation_scope
from firefly_forge.verification_scope_attestation import PreparedBuildScope


def _git_phase_binding(run: dict[str, Any]) -> str:
    # Git writes only these progress fields; source, grants, usage and task phase remain frozen.
    mutable = {"checkpoint_revision", "git_delivery_head", "git_delivery_summary", "git_delivery_draft", "updated_at"}
    return digest({key: value for key, value in run.items() if key not in mutable and key != "events"})


@dataclass(frozen=True)
class _PreparedSettlement:
    run_id: str
    receipt_digest: str
    task_digest: str
    ownership: object
    generation: object
    reader: Any
    inputs: PreparedBuildScope
    current: Callable[[], Awaitable[None]]
    fence: Callable[[], None]
    actor_id: str
    phase_digest: str
    scm: PreparedSCMLinkedVerificationAdmission | None
    workspace: Path
    payload: SelectedSourceFence | None = None


_SETTLEMENT: ContextVar[_PreparedSettlement | None] = ContextVar("forge_git_linked_settlement", default=None)


def linked_git_actor_ids(run_id: str) -> frozenset[str]:
    prepared = _SETTLEMENT.get()
    if prepared is None or prepared.run_id != run_id:
        return frozenset()
    return frozenset({prepared.actor_id})


@dataclass
class _GitCommitFence(TransactionSynchronizationAdapter):
    deliveries: Any
    prepared: _PreparedSettlement
    unit: UnitOfWork
    owner_task: asyncio.Task[Any]
    row: Any
    expected: str | None = None
    index: GitIndexIdentityV1 | None = None

    def _row_digest(self) -> str:
        return digest({"id": self.row.id.hex, "status": self.row.status, "data": self.row.data})

    def seal_phase(self) -> None:
        run = {**self.row.data, "id": self.row.id.hex, "status": self.row.status}
        if _git_phase_binding(run) != self.prepared.phase_digest:
            raise ValueError("Git writer changed source, grants or implementation phase")
        self.expected = self._row_digest()
        self.index = git_index_identity(self.prepared.workspace)

    def fence(self) -> None:
        if (
            current_unit_of_work() is not self.unit
            or self.unit.owner_task is not self.owner_task
            or self.expected is None
            or self._row_digest() != self.expected
            or self.index is None
            or git_index_identity(self.prepared.workspace) != self.index
            or self.prepared.payload is None
        ):
            raise AccessDenied("Git commit lost its exact native unit, owner task or sealed phase")
        self.prepared.fence()
        self.prepared.payload.revalidate(payload_only=True)

    async def before_commit(self, read_only: bool) -> None:
        if read_only:
            raise AccessDenied("Git effects require their writable native unit")
        self.fence()
        await assert_linked_git_closed(self.deliveries, self.prepared.run_id, prepared=self.prepared)
        await self.prepared.inputs.fence()
        self.fence()


async def bind_linked_git_writer(deliveries: Any, run_id: str, row: Any) -> _GitCommitFence | None:
    prepared = _SETTLEMENT.get()
    if prepared is None:
        return None
    unit, task = current_unit_of_work(), asyncio.current_task()
    if prepared.run_id != run_id or unit is None or task is None or unit.owner_task is not task:
        raise AccessDenied("Linked Git settlement requires its exact native writer")
    await assert_linked_git_closed(deliveries, run_id, prepared=prepared)
    if current_unit_of_work() is not unit or unit.owner_task is not task:
        raise AccessDenied("Linked Git settlement changed its native writer during preparation")
    cut = _GitCommitFence(deliveries, prepared, unit, task, row)
    register_synchronization(cut)
    return cut


@asynccontextmanager
async def linked_git_scope(deliveries: Any, run_id: str) -> AsyncIterator[None]:
    prepared = await assert_linked_git_closed(deliveries, run_id)
    payload = None
    token = None
    try:
        if prepared is not None:
            from firefly_forge.agentic_checkpoint_invocation import workspace_source_capture

            source = await workspace_source_capture(
                deliveries.store, prepared.workspace, assert_current=prepared.current
            )
            payload = source.fence
            if payload is None:
                raise ValueError("Git settlement requires its retained selected source fence")
            prepared = replace(prepared, payload=payload)
        token = _SETTLEMENT.set(prepared)
        yield
    finally:
        if token is not None:
            _SETTLEMENT.reset(token)
        if payload is not None:
            payload.close()


async def assert_linked_git_closed(
    deliveries: Any, run_id: str, *, prepared: _PreparedSettlement | None = None
) -> _PreparedSettlement | None:
    store = deliveries.store
    requests = getattr(store, "execution_requests", None)
    if requests is None:
        if getattr(deliveries.engine, "linked_verifications", None) is not None:
            raise ValueError("Git delivery lost its linked verification request repository")
        return None
    async with store._unit():
        rows = await requests.records.for_run(identifier(run_id), KIND)
        if not rows:
            return None
        service = getattr(deliveries.engine, "linked_verifications", None)
        if len(rows) != 1 or service is None or service.store is not store or service.requests is not requests:
            raise ValueError("Git delivery requires the exact linked verification receipt consumer")
        row = rows[0]
        if row.run_id != identifier(run_id):
            raise ValueError("Linked verification belongs to another Git source")
        events = await service._events(row)
        data = copy.deepcopy(row.data)
        run = await store.get_run(run_id)
        if continuation_scope(run) != continuation_scope(data["parent_run"]) or any(
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
        ):
            raise ValueError("Linked verification changed its original implementation scope")
        value = dict(
            version=1,
            kind=KIND,
            run_id=run_id,
            request_id=row.id.hex,
            request_digest=data["request_digest"],
            candidate=data["candidate"],
            request=data["request"],
            status=data["status"],
            progress=verification_progress(data, events).model_dump(),
            event_digest=events[-1]["digest"],
            no_coding_grant=True,
            original_budget_preserved=True,
        )
        if data.get("settlement") is not None:
            value["settlement"] = data["settlement"]
        receipt = LinkedVerificationReceiptV1.model_validate(value)
        if receipt.status not in {"failed", "completed"} or receipt.progress.unknown:
            raise ValueError("Unsettled linked verification blocks Git delivery; read its exact receipt")
        if receipt.status == "failed":
            if (
                events[-1]["kind"] != "linked_verification_held"
                or events[-1]["evidence"] != {"reason": "check_failed"}
                or not receipt.progress.failed
                or receipt.progress.started != receipt.progress.completed
            ):
                raise ValueError("Git delivery requires a confirmed closed failed verification")
        else:
            settlement = receipt.settlement
            assert settlement is not None
            entries = _entries(await deliveries.journal.records.find_for_run(identifier(run_id)), run_id)
            parent = [entry for entry in entries if entry["digest"] == settlement.parent_verify_receipt_digest]
            if (
                events[-1]["kind"] != "linked_verification_completed"
                or settlement.operation_receipt_digest != events[-1]["digest"]
                or len(parent) != 1
                or parent[0]["kind"] != "receipt"
                or parent[0]["task_id"] != "verify"
                or parent[0]["payload"].get("checks") != _completed_checks(data, events)
                or parent[0]["payload"].get("failed_checks") != []
                or parent[0]["payload"].get("source_tree") != receipt.candidate.source_tree
                or parent[0]["payload"].get("linked_verification")
                != {
                    "request_id": row.id.hex,
                    "operation_receipt_digest": events[-1]["digest"],
                }
            ):
                raise ValueError("Git delivery requires its complete native verification settlement")
            task = _task(await deliveries.journal._task(run_id, "verify"))
            if task["status"] != "done" or task.get("evidence") != parent[0]["payload"]:
                raise ValueError("Git delivery lost its settled native verification task")
    if receipt.status != "completed":
        return None
    from firefly_forge.agentic_checkpoint_invocation import _runtime_material_digest
    from firefly_forge.linked_verifications import _policy_material_digest, profile_digest
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    if current_unit_of_work() is None:
        from firefly_forge.checkpoint_mode_runtime import mode_execution_scope
        from firefly_forge.linked_verifications import _scm_binding

        owner, generation, reader = (
            store.execution_ownership_token,
            store.configuration.write_generation,
            CURRENT_PRINCIPAL.get(),
        )
        phase_digest = _git_phase_binding(run)
        grant: PreparedSCMLinkedVerificationAdmission | None = None
        scm_seal: dict[str, Any] | None = None
        inputs: PreparedBuildScope | None = None

        def scm_view() -> None:
            if grant is not None and grant.seal.model_dump(mode="json") != scm_seal:
                raise ValueError("Git settlement changed its exact prepared SCM consent")

        def fence() -> None:
            if (
                store.execution_ownership_token is not owner
                or store.configuration.write_generation is not generation
                or CURRENT_PRINCIPAL.get() != reader
                or _runtime_material_digest() != receipt.candidate.runtime_contract
                or _policy_material_digest() != receipt.candidate.policy_digest
                or profile_digest() != receipt.candidate.profile_digest
                or inputs is not None
                and digest(inputs.witness.model_dump(mode="json")) != receipt.candidate.input_scope_digest
            ):
                raise ValueError("Git settlement preparation lost its captured authority")
            scm_view()
            if grant is not None:
                grant.fence()

        async def current() -> None:
            scm_view()
            if grant is not None:
                await grant.current()
                scm_view()
                grant.fence()
            await store.assert_ownership()
            await service._authority(run, actor)
            if _git_phase_binding(await store.get_run(run_id)) != phase_digest:
                raise ValueError("Git settlement preparation lost its captured authority")
            fence()

        async with mode_execution_scope(deliveries.journal, run_id):
            await service.validate_settlement(run_id, receipt.candidate.source_tree, task)
            actor = await service._actor(data["actor"], data["identity"])
            if actor.authentication_method == "scm_mapping":
                grant = await service._scm_authority().worker(data["actor"], data["identity"], data)
                seal = _scm_binding(
                    grant.seal.model_dump(mode="json"),
                    data["actor"],
                    LinkedVerificationCandidateV1.model_validate(data["candidate"]),
                    LinkedVerificationInputV1.model_validate(data["request"]),
                    data["parent_run"],
                )
                scm_seal = seal.model_dump(mode="json")
                if scm_seal != data.get("scm_authority"):
                    raise ValueError("Git settlement changed its recorded SCM consent")
            inputs = await service._inputs(
                data["parent_run"],
                entries,
                service.engine.component_directory(run, service.engine.workspaces / run_id),
                current,
                actor=actor,
            )
            if digest(inputs.witness.model_dump(mode="json")) != receipt.candidate.input_scope_digest:
                raise ValueError("Git settlement build inputs differ from their completed verification")
            await inputs.fence()
            await current()
        return _PreparedSettlement(
            run_id,
            digest({"data": data, "events": events}),
            digest(task),
            owner,
            generation,
            reader,
            inputs,
            current,
            fence,
            actor.id.hex,
            phase_digest,
            grant,
            deliveries.workspace(run),
        )
    captured = prepared or _SETTLEMENT.get()
    if (
        captured is None
        or captured.run_id != run_id
        or captured.receipt_digest != digest({"data": data, "events": events})
        or captured.task_digest != digest(task)
        or store.execution_ownership_token is not captured.ownership
        or store.configuration.write_generation is not captured.generation
        or CURRENT_PRINCIPAL.get() != captured.reader
        or digest(captured.inputs.witness.model_dump(mode="json")) != receipt.candidate.input_scope_digest
        or _runtime_material_digest() != receipt.candidate.runtime_contract
        or _policy_material_digest() != receipt.candidate.policy_digest
        or profile_digest() != receipt.candidate.profile_digest
    ):
        raise ValueError("Prepare the exact current linked settlement before its native Git writer")
    await captured.inputs.fence()
    await captured.current()
    if digest(captured.inputs.witness.model_dump(mode="json")) != receipt.candidate.input_scope_digest:
        raise ValueError("Git settlement prepared build inputs changed")
    captured.fence()
    return captured


async def assert_git_continuation_current(deliveries: Any, run_id: str) -> None:
    from firefly_forge.unknown_command_proof import _POLICY, ACKNOWLEDGEMENT, assert_continuation_current

    await assert_linked_git_closed(deliveries, run_id)
    run = await deliveries.store.get_run(run_id)
    if run.get("verification_handoff", {}).get("status") != "held" or not run.get("consumed_terminal_recovery"):
        await assert_continuation_current(deliveries.journal, run_id)
        return
    async with deliveries.store._unit():
        entries = _entries(await deliveries.journal.records.find_for_run(identifier(run_id)), run_id)
    if not any(entry["kind"] == ACKNOWLEDGEMENT for entry in entries):
        await assert_continuation_current(deliveries.journal, run_id)
        return
    policy = _POLICY.get()
    if policy is None or policy.run_id != run_id:
        from pyfly.data.transaction import current_unit_of_work

        from firefly_forge.checkpoint_mode_runtime import mode_execution_scope
        from firefly_forge.unknown_command_proof import continuation_policy_scope

        if current_unit_of_work() is not None:
            raise ValueError("Prepare accepted Git custody before entering its native writer")
        with continuation_policy_scope(run_id):
            async with mode_execution_scope(deliveries.journal, run_id):
                await assert_git_continuation_current(deliveries, run_id)
        return
    from firefly_forge.checkpoint_mode_handoff_custody import validate_completed_handoff
    from firefly_forge.consumed_terminal_completions import prepare_accepted_reconciled_handoff
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.sandbox_command_slots import _canonical, _complete, _recovered
    from firefly_forge.unknown_command_proof import _acknowledged_followup
    from firefly_forge.verification_models import check_verification_authority, verification_provider

    if run.get("checkpoint_mode_adoption"):
        from firefly_forge.checkpoint_mode_runtime import assert_mode_actors

        await assert_mode_actors(deliveries.journal, run, entries)

    terminals = [
        entry
        for entry in entries
        if entry["kind"] == "terminal_completion_receipt"
        and entry["payload"]["proof"].get("retained_source") == run.get("usage", {}).get("retained_source")
    ]
    if len(terminals) != 1:
        raise ValueError("Accepted Git custody requires its unique native terminal receipt")
    view = await prepare_accepted_reconciled_handoff(deliveries.store, deliveries.journal, run, entries, terminals[0])
    if view is None:
        raise ValueError("Git custody has no accepted historical handoff")
    async with deliveries.store._unit():
        rows = await deliveries.journal.sandboxes.for_run(identifier(run_id))
        tasks = await deliveries.journal.tasks.find_for_run(identifier(run_id))
        values = {row.id.hex: _canonical(row, entries, run_id) for row in rows}
    await view.current()
    custody = validate_completed_handoff(view.run, view.entries, rows, values)
    intent = next(entry["payload"] for entry in view.entries if entry["digest"] == custody["handoff_intent_digest"])
    acknowledged = {entry["payload"].get("execution_id") for entry in view.entries if entry["kind"] == ACKNOWLEDGEMENT}
    if not acknowledged <= values.keys():
        raise ValueError("Accepted Git custody lost an acknowledged native execution")
    for row in rows:
        if row.id.hex not in acknowledged:
            if not _complete(row, values[row.id.hex]) and not _recovered(row, values[row.id.hex], entries, tasks):
                raise ValueError("An unacknowledged command outcome blocks Git delivery")
            continue
        actor = _acknowledged_followup(
            row, values[row.id.hex], view.entries, view.run, intent["ordinals"][0], policy.digest, completed=True
        )
        if actor is None:
            raise ValueError("Accepted historical acknowledgement does not authorize Git custody")
        check_verification_authority(deliveries.store, view.run, intent)
        if (
            not run.get("checkpoint_mode_adoption")
            and verification_provider(deliveries.store, view.run) != intent["provider_digest"]
        ):
            raise ValueError("The acknowledged Git continuation provider grant changed")
        await ProjectAccess(deliveries.store).authorize_actor(actor, run.get("project_id"), "execute")
    await view.current()
