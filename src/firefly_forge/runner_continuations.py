# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit, one-target Factory admission; an unknown controller call is never replayed."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path, PurePosixPath
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field
from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text

from firefly_forge.execution_request_persistence import ForgeExecutionRequestEntity
from firefly_forge.execution_requests import ExecutionRequests, digest
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_models import component_path
from firefly_forge.runner_factory_models import validate_factory_pool
from firefly_forge.runner_pool_models import FactoryFence, RunnerProjectInvocation, pool_digest
from firefly_forge.runner_pool_persistence import ForgeRunnerInvocationRepository
from firefly_forge.runner_run_liveness import run_stage_active, run_task_key
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.sandbox import pack_workspace
from firefly_forge.sandbox_persistence import ForgeSandboxRepository
from firefly_forge.store import Store, identifier, now

if TYPE_CHECKING:
    from firefly_forge.runner_pool_worker import RunnerPoolWorker

KIND = "runner_continue"
RUN_FIELDS = ("repository_id", "project_id", "execution_actor", "agent", "sandbox_snapshot", "component_snapshot")
CANDIDATE_READ_SECONDS = 3
CANDIDATE_READ_COMMANDS = 16
CANDIDATE_SOURCE_BYTES = 16_777_216
CANDIDATE_READ_CHECKPOINTS = 4096


class _CandidateBudgetExceeded(RuntimeError):
    pass


class _CandidateReadBudget:
    def __init__(self) -> None:
        self.deadline = monotonic() + CANDIDATE_READ_SECONDS
        self.stopping = Event()
        self.remaining = CANDIDATE_READ_CHECKPOINTS

    def checkpoint(self) -> None:
        if self.stopping.is_set() or monotonic() >= self.deadline or self.remaining <= 0:
            raise _CandidateBudgetExceeded("Local candidate inspection budget exhausted")
        self.remaining -= 1


class RunnerContinuationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def continuation_binding(run: dict[str, Any], wal: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    payload, effects = wal["request"], wal["effects"]
    invocation = RunnerProjectInvocation.model_validate(payload["request"])
    if "factory_fence" not in payload:
        raise ValueError("The original command has no retained Factory fence")
    fence = FactoryFence.model_validate(payload["factory_fence"])
    component = run.get("component_snapshot") or {}
    if run.get("project_id") and (
        component.get("project_id") != run["project_id"] or component.get("repository_id") != run["repository_id"]
    ):
        raise ValueError("Original component no longer grants this project and repository")
    configuration = (run.get("sandbox_snapshot") or {}).get("configuration", {})
    legacy = (
        run.get("work_duration") is None
        and configuration.get("work_duration") is None
        and configuration.get("profile", {}).get("work_duration") is None
    )
    if (
        not (
            run_stage_active(run, wal["task_id"])
            or legacy
            and run["status"] == "running"
            and run.get("active_task") == wal["task_id"]
        )
        or run.get("cancel_request", {}).get("status") == "requested"
        or stage["status"] != "running"
        or stage.get("id") != wal["task_id"]
        or wal["task_id"] not in {"implement", "verify"}
        or type(stage["attempt"]) is not int
        or stage["attempt"] < 1
        or type(wal["parent_attempt"]) is not int
        or type(payload["parent_attempt"]) is not int
        or wal["parent_attempt"] != stage["attempt"]
        or payload["parent_attempt"] != stage["attempt"]
        or stage.get("idempotency_key") != run_task_key(run["id"], wal["task_id"], stage["attempt"])
        or wal["status"] != "running"
        or effects.get("start") != {"status": "intent"}
        or "cleanup" in effects
        or effects.get("create", {}).get("status") != "done"
        or effects["create"].get("evidence", {}).get("scope") != "factory_request_prepared"
        or invocation.execution_id != wal["execution_id"]
        or invocation.run_id.hex != run["id"]
        or invocation.repository_id.hex != run["repository_id"]
        or (invocation.project_id.hex if invocation.project_id else None) != run.get("project_id")
        or payload["snapshot_digest"] != run["sandbox_snapshot"]["digest"]
        or payload["request_digest"] != pool_digest(invocation.model_dump(mode="json"))
    ):
        raise ValueError("Only the exact unfinished active Factory command may be continued")
    return {
        "run_id": run["id"],
        "execution_id": wal["execution_id"],
        "task_id": wal["task_id"],
        "attempt": stage["attempt"],
        "wal_digest": pool_digest(payload),
        "invocation_digest": payload["request_digest"],
        "fence": fence.model_dump(mode="json"),
        "run_binding": {key: copy.deepcopy(run.get(key)) for key in RUN_FIELDS},
    }


class RunnerContinuations:
    def __init__(self, store: Store, runtime: RuntimeRoles, worker: RunnerPoolWorker | None) -> None:
        self.store, self.runtime, self.worker = store, runtime, worker
        self.requests = ExecutionRequests(store, runtime)
        self.journal = Journal(store)
        self._candidate_sources = asyncio.Semaphore(1)

    async def _actor(self, run: dict[str, Any]) -> dict[str, Any]:
        principal = CURRENT_PRINCIPAL.get()
        if principal is None:
            raise AccessDenied("Explicit continuation requires an authenticated operator")
        actor = ProjectAccess.actor_snapshot(principal)
        await self._access(run, actor)
        return actor

    async def _access(self, run: dict[str, Any], actor: dict[str, Any]) -> None:
        access = ProjectAccess(self.store)
        await access.authorize_actor(actor, run.get("project_id"), "release" if run.get("publish") else "execute")
        await access.authorize_execution(run)

    async def binding(self, run_id: str, execution_id: str) -> dict[str, Any]:
        run = await self.store.get_run(run_id)
        async with self.store._unit():
            row = await ForgeSandboxRepository().find_by_id(identifier(execution_id))
            if row is None or row.run_id.hex != run["id"]:
                raise KeyError("Runner command not found in this run")
            wal = Journal._sandbox_data(row)
        entries = await self.journal.entries(run_id)
        if not Journal.verify_chain(entries):
            raise RuntimeError("Factory command journal integrity failed")
        starts = [
            entry
            for entry in entries
            if entry["kind"] == "sandbox_intent"
            and entry["payload"].get("execution_id") == execution_id
            and entry["payload"].get("effect") == "start"
        ]
        if len(starts) != 1 or starts[0]["payload"].get("request") != wal["request"]:
            raise RuntimeError("Factory command has no exact original start intent")
        stage = next(task for task in (await self.journal.plan(run_id))["tasks"] if task["id"] == wal["task_id"])
        return continuation_binding(run, wal, stage)

    async def _source_digest(self, workspace: Path, limit: int, budget: _CandidateReadBudget) -> str:
        budget.checkpoint()
        if self._candidate_sources.locked():
            raise _CandidateBudgetExceeded("Local candidate source reader is occupied")
        registry = self.store.credential_registry()
        async with self._candidate_sources:

            def capture() -> str:
                return pack_workspace(
                    workspace, min(limit, CANDIDATE_SOURCE_BYTES), registry=registry, _checkpoint=budget.checkpoint
                )[1]

            reader = asyncio.create_task(asyncio.to_thread(capture))
            try:
                return await asyncio.shield(reader)
            except BaseException:
                budget.stopping.set()
                # Thread cancellation is cooperative. Keep ownership until even a late local syscall returns.
                while not reader.done():
                    try:
                        await asyncio.shield(reader)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                if not reader.cancelled():
                    reader.exception()
                raise

    async def _candidates(
        self, run_id: str, actor: dict[str, Any], budget: _CandidateReadBudget
    ) -> tuple[list[dict[str, Any]], str]:
        run = await self.store.get_run(run_id)
        if (run.get("sandbox_snapshot") or {}).get("mode") != "pool":
            return [], "not_applicable"
        policy = validate_factory_pool(run["sandbox_snapshot"])
        async with self.store._unit():
            previous = await self.requests.records.for_run(identifier(run_id), KIND)
            if len(previous) >= 50 or await self.requests.records.pending_count() >= 500:
                return [], "admission_unavailable"
            requested = {row.request_key for row in previous}
        rows = await self.journal.sandbox_executions(run_id)
        if len(rows) > policy.command_limit:
            return [], "evidence_unavailable"
        rows = [row for row in rows if row["status"] == "running" and row["execution_id"] not in requested]
        if len(rows) > CANDIDATE_READ_COMMANDS:
            raise _CandidateBudgetExceeded("Too many original commands for bounded local inspection")
        source_digests: dict[Path, str] = {}
        commands = []
        state = "request_already_recorded" if requested else "no_candidate"
        families = {
            "python": "Python",
            "python3": "Python",
            "python3.13": "Python",
            "git": "Git",
            "npm": "npm",
            "node": "Node",
            "make": "Make",
            "pytest": "pytest",
            "uv": "uv",
            "java": "Java",
            "mvn": "Maven",
        }
        for row in rows:
            budget.checkpoint()
            try:
                binding = await self.binding(run_id, row["execution_id"])
                async with self.store._unit():
                    if not await ForgeRunnerInvocationRepository().factory_owned(
                        FactoryFence.model_validate(binding["fence"])
                    ):
                        state = "leader_unavailable"
                        continue
                invocation = RunnerProjectInvocation.model_validate(row["request"]["request"])
                await self.store.admit_credentials(invocation.model_dump(mode="json"))
                workspaces = self.store.path.parent / "workspaces"
                root = workspaces / run_id
                workspace = root / component_path((run.get("component_snapshot") or {}).get("path", "."))
                if (
                    workspaces.is_symlink()
                    or root.is_symlink()
                    or workspace.is_symlink()
                    or not root.resolve(strict=True).is_relative_to(workspaces.resolve(strict=True))
                    or not workspace.resolve(strict=True).is_relative_to(root.resolve(strict=True))
                    or row["request"]["command"]["workspace"] != str(workspace)
                ):
                    state = "source_unavailable"
                    continue
                if workspace not in source_digests:
                    source_digests[workspace] = await self._source_digest(workspace, policy.workspace_max_bytes, budget)
                if source_digests[workspace] != invocation.bundle.source_archive_digest:
                    state = "source_changed"
                    continue
                family = families.get(PurePosixPath(invocation.argv[0]).name, "Project")
                commands.append(
                    {
                        "execution_id": row["execution_id"],
                        "task_id": binding["task_id"],
                        "attempt": binding["attempt"],
                        "command_label": family + " command",
                        "argument_count": len(invocation.argv) - 1,
                        "source_digest": invocation.source_digest,
                        "invocation_digest": binding["invocation_digest"],
                        "wal_digest": binding["wal_digest"],
                        "binding_digest": digest({**binding, "actor": actor}),
                    }
                )
            except _CandidateBudgetExceeded:
                raise
            except OSError:
                state = "source_unavailable"
            except (KeyError, ValueError, RuntimeError):
                state = "evidence_unavailable"
        return commands, "requires_leader_confirmation" if commands else state

    async def candidates(self, run_id: str) -> dict[str, Any]:
        run_id = identifier(run_id).hex
        run = await self.store.get_run(run_id)
        actor = await self._actor(run)
        budget = _CandidateReadBudget()
        try:
            async with asyncio.timeout(CANDIDATE_READ_SECONDS):
                return await self._candidate_read(run_id, run, actor, budget)
        except (TimeoutError, _CandidateBudgetExceeded):
            budget.stopping.set()
            await self._actor(await self.store.get_run(run_id))
            result = {
                "version": 1,
                "run_id": run_id,
                "repository_id": run["repository_id"],
                "project_id": run.get("project_id"),
                "actor_id": actor["user_id"],
                "state": "read_budget_exceeded",
                "commands": [],
                "reason": (
                    "Bounded local inspection is unavailable. No continuation is offered; "
                    "exact saved requests remain readable."
                ),
            }
            await self.store.admit_credentials(result)
            await self._actor(await self.store.get_run(run_id))
            self.store.reject_credentials(result)
            return result

    async def _candidate_read(
        self, run_id: str, run: dict[str, Any], actor: dict[str, Any], budget: _CandidateReadBudget
    ) -> dict[str, Any]:
        commands, state = await self._candidates(run_id, actor, budget)
        reasons = {
            "requires_leader_confirmation": (
                "The leader must confirm that the original command is still waiting; "
                "controller checks and quotas apply."
            ),
            "source_unavailable": (
                "This API host cannot verify the shared workspace. Use the authorized host with the shared source; "
                "existing request receipts remain read-only."
            ),
            "leader_unavailable": "The original Factory leader is not currently proven live. No request is offered.",
            "source_changed": "Original source changed. No continuation is offered; inspect the execution evidence.",
            "request_already_recorded": (
                "A continuation identity is already saved for an original command. "
                "Read its exact request; do not submit another identity."
            ),
            "admission_unavailable": (
                "The bounded continuation admission queue is full. "
                "Existing requests remain attached to their original identities."
            ),
            "not_applicable": "This run does not use an original pool command.",
            "no_candidate": "No original active command currently passes local continuation checks.",
            "evidence_unavailable": "Local original-command evidence cannot be verified. No continuation is offered.",
        }
        result = {
            "version": 1,
            "run_id": run_id,
            "repository_id": run["repository_id"],
            "project_id": run.get("project_id"),
            "actor_id": actor["user_id"],
            "state": state,
            "commands": commands,
            "reason": reasons[state],
        }
        await self.store.admit_credentials(result)
        await self._actor(await self.store.get_run(run_id))
        if (commands, state) != await self._candidates(run_id, actor, budget):
            result["commands"] = []
            result["state"] = "evidence_changed"
            result["reason"] = "Local command evidence changed. Refresh it before reviewing a continuation."
        await self._actor(await self.store.get_run(run_id))
        self.store.reject_credentials(result)
        return result

    async def request(self, run_id: str, body: RunnerContinuationInput) -> dict[str, Any]:
        run_id = identifier(run_id).hex
        run = await self.store.get_run(run_id)
        actor = await self._actor(run)
        await self.store.admit_credentials(body.model_dump())
        async with self.store._unit():
            previous = await self.requests.records.find_by_id(identifier(body.request_id))
            retained = self._existing(previous, run_id, body, actor) if previous is not None else None
        if retained is not None:
            return await self.get(run_id, body.request_id)
        admitted = {**await self.binding(run_id, body.execution_id), "actor": actor}
        await self.store.admit_credentials(admitted)
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:execution-request-admission:" + self.store.schema},
            )
            locked = await self.store.run_records.find_by_id(identifier(run_id), lock=LockMode.PESSIMISTIC_WRITE)
            if locked is None:
                raise KeyError("Run not found")
            await self._access(locked.data, actor)
            previous = await self.requests.records.find_by_id(identifier(body.request_id))
            if previous is not None:
                self._existing(previous, run_id, body, actor)
            else:
                others = await self.requests.records.for_run(identifier(run_id), KIND)
                if any(row.request_key == body.execution_id for row in others):
                    raise ValueError("This command already has an explicit continuation identity")
                if len(others) >= 50 or await self.requests.records.pending_count() >= 500:
                    raise ValueError("The bounded explicit continuation queue is full")
                binding = await self.binding(run_id, body.execution_id)
                if admitted != {**binding, "actor": actor}:
                    raise ValueError("Factory admission changed during credential validation")
                await self._access(locked.data, actor)
                data = {
                    "id": body.request_id,
                    "run_id": run_id,
                    "kind": KIND,
                    "status": "queued",
                    "request": admitted,
                    "request_digest": digest(admitted),
                    "result": None,
                    "created_at": now(),
                    "updated_at": now(),
                }
                row = ForgeExecutionRequestEntity(
                    id=identifier(body.request_id),
                    run_id=locked.id,
                    kind=KIND,
                    request_key=body.execution_id,
                    status="queued",
                    data=data,
                )
                await self.requests.records.save(row)
                await self.requests._event(
                    row,
                    "request_admitted",
                    {
                        "request_digest": data["request_digest"],
                        "execution_id": body.execution_id,
                        "task_id": admitted["task_id"],
                        "attempt": admitted["attempt"],
                        "actor_id": actor["user_id"],
                        "factory_fence": admitted["fence"],
                    },
                )
        return await self.get(run_id, body.request_id)

    @staticmethod
    def _existing(
        row: ForgeExecutionRequestEntity, run_id: str, body: RunnerContinuationInput, actor: dict[str, Any]
    ) -> dict[str, Any]:
        if (
            row.kind != KIND
            or row.run_id.hex != run_id
            or row.data["request"].get("execution_id") != body.execution_id
            or row.data["request"].get("actor") != actor
        ):
            raise ValueError("Continuation request identity is already bound")
        return copy.deepcopy(row.data)

    async def get(self, run_id: str, request_id: str) -> dict[str, Any]:
        run = await self.store.get_run(run_id)
        await self._actor(run)
        async with self.store._unit():
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_READ)
            if row is None or row.kind != KIND or row.run_id.hex != run["id"]:
                raise KeyError("Continuation request not found in this run")
            data = copy.deepcopy(row.data)
            events = await self.requests.events(request_id)
            if (
                data.get("id") != row.id.hex
                or data.get("run_id") != row.run_id.hex
                or data.get("kind") != KIND
                or data.get("status") != row.status
                or data["request"].get("run_id") != row.run_id.hex
                or digest(data["request"]) != data["request_digest"]
                or not self.requests.verify_chain(events)
                or not events
                or any(event["request_id"] != row.id.hex for event in events)
                or events[0]["kind"] != "request_admitted"
                or events[0]["evidence"].get("request_digest") != data["request_digest"]
            ):
                raise RuntimeError("Continuation receipt integrity failed")
            latest = events[-1]
            if row.status in {"completed", "failed", "needs_reconciliation"}:
                valid = latest["kind"] == "worker_" + row.status and latest["evidence"] == data["result"]
            elif row.status == "queued":
                valid = latest["kind"] == "request_admitted" and data["result"] is None
            else:
                valid = (
                    row.status == "executing"
                    and latest["kind"] in {"worker_claim", "worker_dispatch"}
                    and data["result"] is None
                )
            if not valid:
                raise RuntimeError("Continuation outcome integrity failed")
        await self.store.admit_credentials(data)
        await self._actor(await self.store.get_run(run_id))
        return data

    async def _finish(self, request_id: str, status: str, result: dict[str, Any]) -> None:
        observed = await self.requests.get(request_id)
        async with self.store._unit():
            await self.store.run_records.find_by_id(identifier(observed["run_id"]), lock=LockMode.PESSIMISTIC_WRITE)
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.kind != KIND or row.status != "executing":
                return
            await self.requests._event(row, "worker_" + status, result)
            row.status, row.data = status, {**row.data, "status": status, "result": result, "updated_at": now()}
            await self.requests.records.save(row)

    async def execute(self, request_id: str) -> None:
        self.runtime.require_execution()
        observed = await self.requests.get(request_id)
        if observed["kind"] != KIND:
            raise ValueError("Not a runner continuation")
        if not await self.requests._claim(request_id):
            return
        dispatched = False
        try:
            request = observed["request"]
            await self.store.admit_credentials(request)
            await self._access(await self.store.get_run(observed["run_id"]), request["actor"])
            binding = await self.binding(observed["run_id"], request["execution_id"])
            if binding != {key: value for key, value in request.items() if key != "actor"}:
                raise ValueError("Original Factory command changed after continuation admission")
            if self.worker is None:
                raise ValueError("The frozen Factory controller worker is unavailable")
            await self.worker.continuation_scope(request)
            await self.requests._observation(
                request_id, "worker_dispatch", {"request_digest": observed["request_digest"]}
            )
            dispatched = True
            result = await self.worker.continue_invocation(request_id, observed["request_digest"], request)
            await self.store.admit_credentials(result)
            await self._finish(request_id, "completed", result)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(
                    request_id,
                    "needs_reconciliation",
                    {"detail": "Worker stopped; observe the original continuation without replay"},
                )
            )
            raise
        except Exception:
            await self._finish(
                request_id,
                "needs_reconciliation" if dispatched else "failed",
                {
                    "detail": "Observe the exact retained continuation; no automatic replay",
                    "before_controller_dispatch": not dispatched,
                },
            )

    async def recover(self) -> None:
        async with self.store._unit():
            rows = await self.requests.records.state("executing", kind=KIND)
        for row in rows:
            await self._finish(row.id.hex, "needs_reconciliation", {"detail": "Factory owner restarted; no replay"})
