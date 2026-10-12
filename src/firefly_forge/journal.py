# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Database plans and write-ahead intents: an unknown external effect is never replayed implicitly."""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from typing import Any

from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.persistence import (
    ForgeJournalEntity,
    ForgeJournalRepository,
    ForgePlanEntity,
    ForgePlanRepository,
    ForgeRunEntity,
    ForgeTaskEntity,
    ForgeTaskRepository,
)
from firefly_forge.profiles import build_instruction
from firefly_forge.store import TERMINAL, Store, identifier, now

Data = dict[str, Any]
ZERO_HASH = "0" * 64
LOCAL_TASKS = {"prepare", "implement", "verify", "checkpoint"}
FAILURE_ALIASES = {"rate_limited": "rate_limit", "insufficient_credits": "quota", "missing_auth": "authentication"}


def digest(data: Data) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


@service
class Journal:
    phase = 50

    def __init__(
        self,
        store: Store,
        plans: ForgePlanRepository | None = None,
        tasks: ForgeTaskRepository | None = None,
        records: ForgeJournalRepository | None = None,
    ) -> None:
        self.store = store
        self.plans = plans or ForgePlanRepository()
        self.tasks = tasks or ForgeTaskRepository()
        self.records = records or ForgeJournalRepository()

    async def start(self) -> None:
        # Recovery belongs to the engine after it owns the execution lease.
        pass

    async def stop(self) -> None:
        pass

    async def _run(self, run_id: str) -> ForgeRunEntity:
        row = await self.store.run_records.find_by_id(identifier(run_id), lock=LockMode.PESSIMISTIC_WRITE)
        if row is None:
            raise KeyError("Run not found")
        return row

    async def _task(self, run_id: str, task_id: str) -> ForgeTaskEntity:
        found = next(
            (task for task in await self.tasks.find_for_run(identifier(run_id)) if task.task_id == task_id), None
        )
        if found is None:
            raise KeyError("Planned task not found")
        return found

    async def _append(self, run_id: str, task_id: str, key: str, kind: str, payload: Data) -> Data:
        run_id = identifier(run_id).hex
        entries = await self.records.find_for_run(identifier(run_id))
        existing = next((entry for entry in entries if entry.idempotency_key == key and entry.kind == kind), None)
        if existing is not None:
            return copy.deepcopy(existing.data)
        if len(json.dumps(payload)) > 1_000_000:
            raise ValueError("Journal evidence exceeds 1 MB")
        data = {
            "run_id": run_id,
            "sequence": len(entries) + 1,
            "task_id": task_id,
            "idempotency_key": key,
            "kind": kind,
            "payload": copy.deepcopy(payload),
            "created_at": now(),
            "previous_hash": entries[-1].digest if entries else ZERO_HASH,
        }
        data["digest"] = digest(data)
        await self.records.save(
            ForgeJournalEntity(
                id=uuid.uuid4(),
                run_id=identifier(run_id),
                sequence=data["sequence"],
                task_id=task_id,
                idempotency_key=key,
                kind=kind,
                previous_hash=data["previous_hash"],
                digest=data["digest"],
                data=data,
            )
        )
        return data

    async def ensure_plan(self, run_id: str) -> Data:
        async with self.store._unit():
            run = await self._run(run_id)
            existing = await self.plans.find_current(run.id)
            if existing is None:
                data = run.data
                tasks = [
                    {
                        "id": "prepare",
                        "kind": "local",
                        "title": "Prepare the authorized workspace",
                        "instructions": (
                            "Clone the selected repository and record its authorized origin, "
                            "original HEAD and isolated workspace."
                        ),
                    }
                ]
                if data.get("operation", "build") != "test":
                    tasks.append(
                        {
                            "id": "implement",
                            "kind": "local",
                            "title": "Implement the requested change",
                            "instructions": build_instruction(data["brief"], data["backend"], data["frontend"]),
                        }
                    )
                tasks.extend(
                    [
                        {
                            "id": "verify",
                            "kind": "local",
                            "title": "Run independent verification",
                            "instructions": (
                                "Run the configured behavioral tests, build, lint and type checks; "
                                "retain each real exit code and output. Repair within the configured attempt limit."
                            ),
                        },
                        {
                            "id": "checkpoint",
                            "kind": "local",
                            "title": "Retain the verified source checkpoint",
                            "instructions": (
                                "Record the actual source tree, original base revision, current HEAD "
                                "and verification evidence. Record a commit reference only if that commit exists; "
                                "retain artifacts."
                            ),
                        },
                    ]
                )
                definition = {
                    "version": 1,
                    "revision": 1,
                    "run_id": run_id,
                    "repository_id": data["repository_id"],
                    "operation": data.get("operation", "build"),
                    "brief": data["brief"],
                    "backend": data["backend"],
                    "frontend": data["frontend"],
                    "agent": data["agent"],
                    "config_revision": data.get("config_revision"),
                    "execution_config": data.get("execution_config", {}),
                    "policy_snapshot": data.get("policy_snapshot", {}),
                    "commands": data.get("commands", []),
                    "tasks": tasks,
                }
                plan = ForgePlanEntity(
                    id=uuid.uuid4(), run_id=run.id, revision=1, digest=digest(definition), data=definition
                )
                await self.plans.save(plan)
                for task in tasks:
                    await self.tasks.save(
                        ForgeTaskEntity(
                            id=uuid.uuid4(),
                            run_id=run.id,
                            plan_id=plan.id,
                            task_id=task["id"],
                            status="queued",
                            attempt=1,
                            data={},
                        )
                    )
                await self._append(
                    run_id,
                    "plan",
                    uuid.uuid5(uuid.NAMESPACE_URL, run_id + ":plan:1").hex,
                    "plan",
                    {"revision": 1, "digest": plan.digest},
                )
            return await self.plan(run_id)

    async def plan(self, run_id: str) -> Data:
        async with self.store._unit():
            plan = await self.plans.find_current(identifier(run_id))
            if plan is None:
                raise KeyError("Development plan not found")
            states = {task.task_id: task for task in await self.tasks.find_for_run(identifier(run_id))}
            result = copy.deepcopy(plan.data)
            result["digest"] = plan.digest
            result["tasks"] = [
                {
                    **task,
                    "status": states[task["id"]].status,
                    "attempt": states[task["id"]].attempt,
                    **copy.deepcopy(states[task["id"]].data),
                }
                for task in result["tasks"]
            ]
            known = {task["id"] for task in result["tasks"]}
            result["tasks"].extend(
                {
                    "id": task.task_id,
                    "title": task.task_id,
                    "kind": "remote",
                    "instructions": "Record the external effect's actual outcome before retrying it.",
                    "status": task.status,
                    "attempt": task.attempt,
                    **copy.deepcopy(task.data),
                }
                for task in states.values()
                if task.task_id not in known
            )
            run = await self.store.get_run(run_id)
            result["checkpoint"] = run.get("checkpoint", {})
            return result

    async def entries(self, run_id: str) -> list[Data]:
        async with self.store._unit():
            await self.store.get_run(run_id)
            return [
                {**copy.deepcopy(entry.data), "digest": entry.digest}
                for entry in await self.records.find_for_run(identifier(run_id))
            ]

    @staticmethod
    def verify_chain(entries: list[Data]) -> bool:
        previous = ZERO_HASH
        for sequence, entry in enumerate(entries, 1):
            if entry.get("sequence") != sequence or entry.get("previous_hash") != previous:
                return False
            content = {key: value for key, value in entry.items() if key != "digest"}
            if digest(content) != entry.get("digest"):
                return False
            previous = entry["digest"]
        return True

    async def intent(self, run_id: str, task_id: str, attempt: int = 1, payload: Data | None = None) -> Data:
        if attempt < 1 or attempt > 100:
            raise ValueError("Task attempt is outside the supported range")
        await self.ensure_plan(run_id)
        async with self.store._unit():
            run = await self._run(run_id)
            try:
                task = await self._task(run_id, task_id)
            except KeyError:
                # Remote publication/deployment actions get their own task before their effect starts.
                if task_id in LOCAL_TASKS or not task_id.startswith(("publish", "deploy", "rollback", "memory")):
                    raise
                plan = await self.plans.find_current(run.id)
                assert plan is not None
                task = ForgeTaskEntity(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    plan_id=plan.id,
                    task_id=task_id,
                    status="queued",
                    attempt=attempt,
                    data={"kind": "remote"},
                )
                await self.tasks.save(task)
            key = uuid.uuid5(uuid.NAMESPACE_URL, f"forge:{run.id.hex}:{task_id}:{attempt}").hex
            existing = next(
                (
                    entry
                    for entry in await self.records.find_for_run(run.id)
                    if entry.kind == "intent" and entry.idempotency_key == key
                ),
                None,
            )
            if existing is not None:
                receipted = any(
                    entry.kind == "receipt" and entry.idempotency_key == key
                    for entry in await self.records.find_for_run(run.id)
                )
                disposition = "completed" if receipted else "needs_reconciliation"
                return {"idempotency_key": key, "disposition": disposition, "entry": copy.deepcopy(existing.data)}
            repair = (
                task_id in {"implement", "verify"}
                and attempt > task.attempt
                and (payload or {}).get("reason") == "verification_repair"
            )
            if task.status == "done" and repair:
                checks = (payload or {}).get("failed_checks", [])
                if not isinstance(checks, list) or not any(
                    isinstance(check, dict) and isinstance(check.get("exit_code"), int) and check["exit_code"] != 0
                    for check in checks
                ):
                    raise ValueError("Local repair requires failed verification evidence")
                await self._append(run_id, task_id, key, "repair", payload or {})
                task.status = "queued"
            if task.status == "done":
                return {
                    "idempotency_key": task.data.get("idempotency_key", key),
                    "disposition": "completed",
                    "entry": {},
                }
            if task.status in {"running", "needs_reconciliation"} or attempt < task.attempt:
                return {
                    "idempotency_key": task.data.get("idempotency_key", key),
                    "disposition": "needs_reconciliation",
                    "entry": {},
                }
            task.status, task.attempt = "running", attempt
            task.data = {**task.data, "idempotency_key": key, "started_at": now(), "intent": payload or {}}
            await self.tasks.save(task)
            record = await self._append(run_id, task_id, key, "intent", payload or {})
            run.data = {**run.data, "active_task": task_id, "updated_at": now()}
            await self.store.run_records.save(run)
            return {"idempotency_key": key, "disposition": "execute", "entry": record}

    async def _effect_task(self, run_id: str, key: str) -> ForgeTaskEntity:
        found = next(
            (
                task
                for task in await self.tasks.find_for_run(identifier(run_id))
                if task.data.get("idempotency_key") == key
            ),
            None,
        )
        if found is None:
            raise KeyError("Journal effect intent not found")
        return found

    async def receipt(self, run_id: str, key: str, evidence: Data, *, checkpoint: Data | None = None) -> Data:
        async with self.store._unit():
            run = await self._run(run_id)
            existing = next(
                (
                    entry
                    for entry in await self.records.find_for_run(run.id)
                    if entry.kind == "receipt" and entry.idempotency_key == key
                ),
                None,
            )
            if existing is not None:
                return copy.deepcopy(existing.data)
            task = await self._effect_task(run_id, key)
            if task.status == "done":
                return copy.deepcopy(task.data)
            if task.status not in {"running", "needs_reconciliation"}:
                raise ValueError("Only an active intent can receive a completion receipt")
            record = await self._append(run_id, task.task_id, key, "receipt", evidence)
            task.status = "done"
            task.data = {**task.data, "evidence": copy.deepcopy(evidence), "completed_at": now()}
            await self.tasks.save(task)
            run.data = {
                **run.data,
                "checkpoint": {**run.data.get("checkpoint", {}), **(checkpoint or {})},
                "updated_at": now(),
            }
            await self.store.run_records.save(run)
            return record

    async def fail(self, run_id: str, key: str, kind: str, message: str, *, retry_after: float | None = None) -> Data:
        kind = FAILURE_ALIASES.get(kind, kind)
        if kind not in {"rate_limit", "quota", "authentication", "transient", "verification", "source_invalid"}:
            raise ValueError("Unknown execution failure classification")
        async with self.store._unit():
            run = await self._run(run_id)
            task = await self._effect_task(run_id, key)
            if task.status == "done":
                raise ValueError("A completed effect cannot be overwritten by a late error")
            existing = next(
                (
                    entry
                    for entry in await self.records.find_for_run(run.id)
                    if entry.kind == "error" and entry.idempotency_key == key
                ),
                None,
            )
            if existing is not None:
                return copy.deepcopy(existing.data)
            retry_count = int(run.data.get("retry_count", 0))
            automatic = kind in {"rate_limit", "transient"} and retry_count < 3
            if kind == "rate_limit" and retry_after is None:
                automatic = False
            delay = min(max(retry_after if retry_after is not None else 2**retry_count, 1), 86400)
            pause = {
                "kind": kind,
                "message": message[:8000],
                "automatic": automatic,
                "retry_at": time.time() + delay if automatic else None,
                "retry_count": retry_count,
            }
            record = await self._append(run_id, task.task_id, key, "error", pause)
            task.status = "paused" if kind not in {"verification", "source_invalid"} else "failed"
            task.data = {**task.data, "error": pause}
            await self.tasks.save(task)
            if task.task_id in LOCAL_TASKS or run.status not in TERMINAL:
                run.status = "paused" if task.status == "paused" else "failed"
            run.data = {
                **run.data,
                "status": run.status,
                "pause": pause,
                "error": run.data.get("error")
                if run.status in TERMINAL and task.task_id not in LOCAL_TASKS
                else message[:8000],
                "display_status": run.status
                if run.status in TERMINAL
                else "paused_" + kind
                if task.status == "paused"
                else "failed",
                "updated_at": now(),
            }
            await self.store.run_records.save(run)
            return record

    async def recover(self) -> int:
        async with self.store._unit():
            pending = await self.tasks.find_pending_recovery()
            count = 0
            for candidate in pending:
                run = await self._run(candidate.run_id.hex)
                task = await self._task(run.id.hex, candidate.task_id)
                if task.status != "running":
                    continue
                task.status = "needs_reconciliation"
                await self.tasks.save(task)
                if run.status not in TERMINAL:
                    run.status = "needs_reconciliation"
                    run.data = {**run.data, "status": run.status, "resume_existing": True, "updated_at": now()}
                    await self.store.run_records.save(run)
                await self._append(
                    run.id.hex,
                    task.task_id,
                    task.data["idempotency_key"],
                    "recovery",
                    {
                        "reason": (
                            "Worker stopped without a durable receipt; inspect the retained source and external target"
                        )
                    },
                )
                count += 1
            return count

    async def reconcile(self, run_id: str, key: str, *, evidence: Data, outcome: str) -> Data:
        if outcome not in {"retry", "completed"} or not evidence:
            raise ValueError("Reconciliation requires an observed outcome and evidence")
        async with self.store._unit():
            run = await self._run(run_id)
            task = await self._effect_task(run_id, key)
            if task.status != "needs_reconciliation":
                raise ValueError("Task does not need reconciliation")
            if task.task_id not in LOCAL_TASKS and outcome == "retry":
                raise ValueError("An ambiguous remote effect cannot be retried by generic reconciliation")
            record = await self._append(
                run_id, task.task_id, key, "reconciliation", {"outcome": outcome, "evidence": evidence}
            )
            if outcome == "completed":
                await self.receipt(run_id, key, evidence, checkpoint=evidence.get("checkpoint"))
            else:
                task.status, task.attempt = "queued", task.attempt + 1
                task.data = {**task.data, "reconciliation": evidence}
                await self.tasks.save(task)
            run.data = {**run.data, "updated_at": now()}
            await self.store.run_records.save(run)
            return record

    async def resume(self, run_id: str, agent: str | None = None, *, auto: bool = False) -> Data:
        async with self.store._unit():
            run = await self._run(run_id)
            if run.status not in {"paused", "needs_reconciliation", "interrupted"}:
                raise ValueError("Only a paused or interrupted run can resume")
            pause = run.data.get("pause", {})
            if auto and (not pause.get("automatic") or (pause.get("retry_at") or float("inf")) > time.time()):
                raise ValueError("This run requires an explicit operator resume")
            if run.data.get("publication_status") == "publishing":
                raise ValueError("Publication outcome is ambiguous; reconcile the remote branch and pull request first")
            if agent is not None and agent not in {"codex", "claude", "bridge", "agentic"}:
                raise ValueError("Unknown handoff coding provider")
            old_revision = run.data.get("config_revision")
            if agent is not None:
                run.data = {**run.data, "agent": agent, **self.store.configuration.snapshot(agent)}
                previous = await self.plans.find_current(run.id)
                if previous is not None:
                    definition = {
                        **copy.deepcopy(previous.data),
                        "revision": previous.revision + 1,
                        "agent": agent,
                        "config_revision": run.data["config_revision"],
                        "execution_config": run.data["execution_config"],
                    }
                    await self.plans.save(
                        ForgePlanEntity(
                            id=uuid.uuid4(),
                            run_id=run.id,
                            revision=definition["revision"],
                            digest=digest(definition),
                            data=definition,
                        )
                    )
            for task in await self.tasks.find_for_run(run.id):
                if task.status == "paused" and task.task_id in LOCAL_TASKS:
                    task.status, task.attempt = "queued", task.attempt + 1
                    await self.tasks.save(task)
            run.status = "queued"
            run.data = {
                **run.data,
                "status": "queued",
                "resume_existing": True,
                "display_status": "queued",
                "last_pause": pause,
                "pause": {},
                "error": None,
                "retry_count": int(run.data.get("retry_count", 0)) + int(auto),
                "checkpoint": run.data.get("checkpoint", {}),
                "updated_at": now(),
            }
            await self.store.run_records.save(run)
            await self._append(
                run_id,
                "handoff" if agent else "resume",
                uuid.uuid4().hex,
                "handoff" if agent else "resume",
                {
                    "agent": run.data["agent"],
                    "previous_config_revision": old_revision,
                    "config_revision": run.data.get("config_revision"),
                    "automatic": auto,
                },
            )
            return await self.store.get_run(run_id)

    async def ready_retries(self) -> list[str]:
        async with self.store._unit():
            return [row.id.hex for row in await self.store.run_records.find_ready_retries(time.time())]
