# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Database plans and write-ahead intents: an unknown external effect is never replayed implicitly."""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from collections.abc import Callable
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
from firefly_forge.sandbox_persistence import ForgeSandboxRepository
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
        sandboxes: ForgeSandboxRepository | None = None,
    ) -> None:
        self.store = store
        self.plans = plans or ForgePlanRepository()
        self.tasks = tasks or ForgeTaskRepository()
        self.records = records or ForgeJournalRepository()
        self.sandboxes = sandboxes or ForgeSandboxRepository()
        self.credential_registry: Callable[[], dict[str, str]] = dict

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
                tasks: list[Data] = [
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
                if data.get("assurance", {}).get("review_agent"):
                    tasks.insert(
                        -1,
                        {
                            "id": "review",
                            "kind": "remote",
                            "title": "Review the verified source independently",
                            "instructions": (
                                "Use a fresh source-copy context and retain a strict review verdict before delivery."
                            ),
                        },
                    )
                design_reference = None
                if data.get("design_snapshot"):
                    from firefly_forge.product_design import design_files

                    bundle = data["design_snapshot"]
                    root = (data.get("component_snapshot") or {}).get("path", ".")
                    design_reference = {
                        "digest": bundle["digest"],
                        "definition_digest": bundle["definition_digest"],
                        "brand_digest": bundle["brand_digest"],
                        "paths": [path if root == "." else root + "/" + path for path in design_files(bundle)],
                    }
                definition = {
                    "version": 1,
                    "revision": 1,
                    "run_id": run_id,
                    "repository_id": data["repository_id"],
                    "project_id": data.get("project_id"),
                    "component_id": data.get("component_id"),
                    "component_snapshot": data.get("component_snapshot"),
                    "operation": data.get("operation", "build"),
                    "brief": data["brief"],
                    "backend": data["backend"],
                    "frontend": data["frontend"],
                    "repair_budget": data.get("repair_budget", 2),
                    "assurance": data.get("assurance", {}),
                    "review_config_revision": data.get("review_config_revision"),
                    "review_execution_config": data.get("review_execution_config", {}),
                    "model_selection": data.get("model_selection"),
                    "review_model_selection": data.get("review_model_selection"),
                    "agent": data["agent"],
                    "config_revision": data.get("config_revision"),
                    "execution_config": data.get("execution_config", {}),
                    "policy_snapshot": data.get("policy_snapshot", {}),
                    "role_snapshot": data.get("role_snapshot"),
                    "sandbox_snapshot": data.get("sandbox_snapshot"),
                    "tool_policy_snapshot": data.get("tool_policy_snapshot"),
                    "mcp_snapshot": data.get("mcp_snapshot"),
                    "learning_snapshot": data.get("learning_snapshot"),
                    "product_definition": data.get("product_definition"),
                    "branding_snapshot": data.get("branding_snapshot"),
                    "design_reference": design_reference,
                    "issue_context": data.get("issue_context"),
                    "commands": data.get("commands", []),
                    "tasks": tasks,
                }
                if data.get("role_snapshot"):
                    from firefly_forge.agent_roles import roles_for_stage

                    for task in tasks:
                        task["roles"] = [role["id"] for role in roles_for_stage(data["role_snapshot"], task["id"])]
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

    def _worker_payload(self, run: ForgeRunEntity, payload: str, *, reject_credentials: bool = False) -> str:
        import os

        from firefly_forge.configuration import redact_secrets
        from firefly_forge.secrets import redact_environment, sensitive_name

        settings: dict[str, str] = dict(getattr(self, "credential_registry", dict)())
        for name in ("config_revision", "review_config_revision"):
            revision = run.data.get(name)
            if revision:
                settings.update(self.store.configuration.load_snapshot(revision))
        settings.update({name: value for name, value in os.environ.items() if sensitive_name(name) and value})
        escaped = {
            name + "_ESCAPED": json.dumps(value)[1:-1] for name, value in settings.items() if sensitive_name(name)
        }
        settings.update(escaped)
        safe = redact_environment(redact_secrets(payload, settings))
        if reject_credentials and safe != payload:
            raise ValueError("Worker response contains a configured credential; provide decisions without secrets")
        return safe

    async def worker_messages(self, run_id: str) -> list[Data]:
        entries = await self.entries(run_id)
        return [entry for entry in entries if entry["kind"] in {"worker_feedback", "worker_response"}]

    async def worker_feedback(self, run_id: str, feedback: Any) -> Data:
        from firefly_forge.agent_roles import BINDINGS, WorkerFeedback

        feedback = WorkerFeedback.model_validate(feedback)
        async with self.store._unit():
            run = await self._run(run_id)
            payload = WorkerFeedback.model_validate_json(
                self._worker_payload(run, feedback.model_dump_json())
            ).model_dump(mode="json")
            entries = await self.records.find_for_run(run.id)
            key = "worker-feedback:" + feedback.id
            existing = next(
                (entry for entry in entries if entry.kind == "worker_feedback" and entry.idempotency_key == key), None
            )
            if existing:
                if existing.data["payload"] != payload:
                    raise ValueError("Feedback identifier already contains different content")
                return copy.deepcopy(existing.data)
            if run.status in TERMINAL:
                raise ValueError("A terminal run cannot accept late worker feedback")
            task = await self._task(run_id, feedback.task_id)
            stages = BINDINGS[feedback.role_id][1]
            if feedback.task_id not in stages and not (feedback.role_id == "qa" and feedback.task_id == "review"):
                raise ValueError("Worker role is not bound to this planned stage")
            roles = run.data.get("role_snapshot", {}).get("roles", [])
            if roles and not any(role["id"] == feedback.role_id and role["enabled"] for role in roles):
                raise ValueError("Worker role is disabled in this frozen run")
            if feedback.request:
                if task.status == "done":
                    raise ValueError("A completed stage cannot be paused by late worker feedback")
                prior = [
                    entry
                    for entry in entries
                    if entry.kind == "worker_feedback" and entry.data["payload"].get("request")
                ]
                limit = run.data.get("role_snapshot", {}).get("max_requests", 3)
                if len(prior) >= limit:
                    raise ValueError("Worker request budget exhausted; explicit operator escalation is required")
                if run.data.get("worker_request", {}).get("status") == "pending":
                    raise ValueError("An existing worker request must be resolved first")
                if any(entry.data["payload"]["request"]["id"] == feedback.request.id for entry in prior):
                    raise ValueError("Worker request identifier has already been used")
            record = await self._append(run_id, feedback.task_id, key, "worker_feedback", payload)
            run.data = {**run.data, "worker_feedback": payload, "updated_at": now()}
            if feedback.request:
                task.status = "paused"
                task.data = {**task.data, "worker_feedback": payload, "known_worker_response": True}
                await self.tasks.save(task)
                run.status = "paused"
                run.data = {
                    **run.data,
                    "status": "paused",
                    "display_status": "paused_worker_request",
                    "worker_request": {
                        **payload["request"],
                        "status": "pending",
                        "role_id": feedback.role_id,
                        "task_id": feedback.task_id,
                        "attempt": feedback.attempt,
                    },
                    "pause": {"kind": "worker_request", "message": payload["summary"], "automatic": False},
                }
            await self.store.run_records.save(run)
            return record

    async def worker_response(self, run_id: str, request_id: str, response: Any) -> Data:
        from firefly_forge.agent_roles import WorkerResponse

        response = WorkerResponse.model_validate(response)
        async with self.store._unit():
            run = await self._run(run_id)
            payload = WorkerResponse.model_validate_json(
                self._worker_payload(run, response.model_dump_json(), reject_credentials=True)
            ).model_dump(mode="json")
            payload["request_id"] = request_id
            entries = await self.records.find_for_run(run.id)
            key = "worker-response:" + response.id
            existing = next(
                (entry for entry in entries if entry.kind == "worker_response" and entry.idempotency_key == key), None
            )
            if existing:
                if existing.data["payload"] != payload:
                    raise ValueError("Response identifier already contains different content")
                return copy.deepcopy(existing.data)
            request = run.data.get("worker_request", {})
            if request.get("id") != request_id:
                raise ValueError("Worker request does not belong to this run")
            if request.get("status") != "pending":
                raise ValueError("Worker request is already resolved")
            if run.status != "paused":
                raise ValueError("Only a paused run can receive a worker response")
            record = await self._append(run_id, request["task_id"], key, "worker_response", payload)
            run.data = {
                **run.data,
                "worker_request": {**request, "status": "answered"},
                "worker_response": payload,
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

    async def resume(
        self,
        run_id: str,
        agent: str | None = None,
        *,
        model_profile_id: str | None = None,
        auto: bool = False,
        campaign_action_id: str | None = None,
    ) -> Data:
        current = await self.store.get_run(run_id)
        from firefly_forge.project_access import ProjectAccess

        if auto and (agent is not None or model_profile_id is not None):
            raise ValueError("An automatic retry cannot change the selected provider or model profile")
        if agent is not None and agent not in {"codex", "claude", "bridge", "agentic"}:
            raise ValueError("Unknown handoff coding provider")
        selection = None
        if model_profile_id is not None:
            if current.get("operation") == "test":
                raise ValueError("Test operations check existing source without a coding model")
            if self.store.task_router is None:
                raise ValueError("Named model profiles are not configured")
            selection = await self.store.task_router.resolve("implementation", model_profile_id)
            if selection is None:
                raise ValueError("Choose a configured implementation model profile")
            if agent is not None and agent != selection["provider"]:
                raise ValueError("The chosen agent conflicts with this task's model profile")
            agent = selection["provider"]
        handoff = agent is not None
        ProjectAccess.require_provider_isolation(current, agent or current.get("agent"))
        await ProjectAccess(self.store).authorize_execution(current)
        async with self.store._unit():
            await self.store.assert_campaign_current(current, action_id=campaign_action_id)
            run = await self._run(run_id)
            if run.status not in {"paused", "needs_reconciliation", "interrupted"}:
                raise ValueError("Only a paused or interrupted run can resume")
            if run.data.get("worker_request", {}).get("status") == "pending":
                raise ValueError("Answer the pending worker question before explicitly resuming this run")
            pause = run.data.get("pause", {})
            if auto and (not pause.get("automatic") or (pause.get("retry_at") or float("inf")) > time.time()):
                raise ValueError("This run requires an explicit operator resume")
            if run.data.get("publication_status") == "publishing":
                raise ValueError("Publication outcome is ambiguous; reconcile the remote branch and pull request first")
            ProjectAccess.require_provider_isolation(run.data, agent or run.data.get("agent"))
            await ProjectAccess(self.store).authorize_execution(run.data)
            old_revision = run.data.get("config_revision")
            old_selection = copy.deepcopy(run.data.get("model_selection"))
            old_agent = run.data.get("agent")
            selection_mode = "named_profile" if selection is not None else "legacy_provider" if handoff else "frozen"
            if agent is not None:
                configuration = (
                    {"config_revision": selection["config_revision"], "execution_config": selection["execution_config"]}
                    if selection is not None
                    else self.store.configuration.snapshot(agent)
                )
                run.data = {
                    **run.data,
                    "agent": agent,
                    "model_profile_id": model_profile_id,
                    "model_selection": selection,
                    **configuration,
                }
                previous = await self.plans.find_current(run.id)
                if previous is not None:
                    definition = {
                        **copy.deepcopy(previous.data),
                        "revision": previous.revision + 1,
                        "agent": agent,
                        "config_revision": run.data["config_revision"],
                        "execution_config": run.data["execution_config"],
                        "model_selection": copy.deepcopy(selection),
                        "review_model_selection": copy.deepcopy(run.data.get("review_model_selection")),
                        "selection_transition": {
                            "mode": selection_mode,
                            "previous_agent": old_agent,
                            "agent": agent,
                            "previous_config_revision": old_revision,
                            "config_revision": run.data["config_revision"],
                            "previous_model_selection": old_selection,
                            "model_selection": copy.deepcopy(selection),
                        },
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
                if task.status == "paused" and (task.task_id in LOCAL_TASKS or task.task_id == "review"):
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
                "handoff" if handoff else "resume",
                uuid.uuid4().hex,
                "handoff" if handoff else "resume",
                {
                    "agent": run.data["agent"],
                    "previous_agent": old_agent,
                    "previous_config_revision": old_revision,
                    "config_revision": run.data.get("config_revision"),
                    "previous_model_selection": old_selection,
                    "model_selection": copy.deepcopy(run.data.get("model_selection")),
                    "selection_mode": selection_mode,
                    "automatic": auto,
                },
            )
            return await self.store.get_run(run_id)

    async def ready_retries(self) -> list[str]:
        async with self.store._unit():
            return [row.id.hex for row in await self.store.run_records.find_ready_retries(time.time())]

    async def sandbox_effect_intent(
        self, run_id: str, task_id: str, execution_id: str, *, effect: str, payload: Data
    ) -> Data:
        from firefly_forge.sandbox_persistence import ForgeSandboxEntity

        if task_id not in {"implement", "verify"} or effect not in {"create", "start", "cleanup"}:
            raise ValueError("Sandbox effects must belong to an implementation or verification stage")
        execution = identifier(execution_id)
        async with self.store._unit():
            run = await self._run(run_id)
            task = await self._task(run_id, task_id)
            existing = await self.sandboxes.find_by_id(execution)
            if existing is None:
                if effect != "create" or task.status != "running":
                    raise ValueError("Sandbox creation requires an active stage")
                if payload.get("parent_attempt") != task.attempt or type(payload.get("ordinal")) is not int:
                    raise ValueError("Sandbox invocation does not match its stage attempt")
                if not 1 <= payload["ordinal"] <= 100:
                    raise ValueError("Sandbox invocation ordinal exceeds the bounded stage limit")
                slot = await self.sandboxes.slot(run.id, task_id, task.attempt, payload["ordinal"])
                if slot is not None:
                    raise ValueError("Sandbox invocation slot already belongs to another execution")
                existing = ForgeSandboxEntity(
                    id=execution,
                    run_id=run.id,
                    task_id=task_id,
                    parent_attempt=task.attempt,
                    ordinal=payload["ordinal"],
                    container_name=payload["container_name"],
                    snapshot_digest=payload["snapshot_digest"],
                    status="creating",
                    data={"request": copy.deepcopy(payload), "effects": {}},
                )
            if existing.run_id != run.id or existing.task_id != task_id:
                raise ValueError("Sandbox execution belongs to another stage")
            if existing.data["request"] != payload:
                raise ValueError("Sandbox invocation request changed")
            previous = existing.data["effects"].get(effect)
            if previous:
                return {
                    "disposition": "completed" if previous["status"] == "done" else "needs_reconciliation",
                    "execution": self._sandbox_data(existing),
                }
            if effect == "start" and existing.data["effects"].get("create", {}).get("status") != "done":
                raise ValueError("Sandbox start requires a durable creation receipt")
            if effect == "start" and existing.status in {"removed", "interrupted"}:
                raise ValueError("Interrupted sandbox commands cannot be replayed")
            existing.data = {**existing.data, "effects": {**existing.data["effects"], effect: {"status": "intent"}}}
            existing.status = {"create": "creating", "start": "running", "cleanup": "cleaning"}[effect]
            await self.sandboxes.save(existing)
            await self._append(
                run_id,
                task_id,
                execution.hex + ":" + effect,
                "sandbox_intent",
                {"execution_id": execution.hex, "effect": effect, "request": payload},
            )
            return {"disposition": "execute", "execution": self._sandbox_data(existing)}

    @staticmethod
    def _sandbox_data(row: Any) -> Data:
        return {
            "execution_id": row.id.hex,
            "run_id": row.run_id.hex,
            "task_id": row.task_id,
            "parent_attempt": row.parent_attempt,
            "ordinal": row.ordinal,
            "container_name": row.container_name,
            "status": row.status,
            "snapshot_digest": row.snapshot_digest,
            **copy.deepcopy(row.data),
        }

    async def sandbox_effect_receipt(
        self, run_id: str, task_id: str, execution_id: str, *, effect: str, evidence: Data
    ) -> Data:
        return await self._sandbox_finish(run_id, task_id, execution_id, effect, evidence, reconcile=False)

    async def sandbox_effect_reconcile(
        self, run_id: str, task_id: str, execution_id: str, *, effect: str, evidence: Data, outcome: str
    ) -> Data:
        if outcome not in {"completed", "interrupted"}:
            raise ValueError("Sandbox reconciliation may complete or interrupt an observed effect, never retry it")
        return await self._sandbox_finish(
            run_id, task_id, execution_id, effect, {**evidence, "outcome": outcome}, reconcile=True
        )

    async def _sandbox_finish(
        self, run_id: str, task_id: str, execution_id: str, effect: str, evidence: Data, *, reconcile: bool
    ) -> Data:
        async with self.store._unit():
            run = await self._run(run_id)
            row = await self.sandboxes.find_by_id(identifier(execution_id))
            if row is None or row.run_id != run.id or row.task_id != task_id:
                raise ValueError("Sandbox execution does not belong to this stage")
            previous = row.data["effects"].get(effect)
            if previous is None:
                raise ValueError("Sandbox receipt requires an earlier effect intent")
            if previous["status"] == "done":
                return self._sandbox_data(row)
            row.data = {
                **row.data,
                "effects": {**row.data["effects"], effect: {"status": "done", "evidence": copy.deepcopy(evidence)}},
            }
            row.status = {"create": "created", "start": "finished", "cleanup": "removed"}[effect]
            if evidence.get("outcome") == "interrupted":
                row.status = "interrupted"
            await self.sandboxes.save(row)
            journal_evidence = copy.deepcopy(evidence)
            if isinstance(journal_evidence.get("output"), str):
                output = journal_evidence.pop("output")
                journal_evidence.update(
                    output_sha256=hashlib.sha256(output.encode()).hexdigest(),
                    output_bytes=len(output.encode()),
                    output_excerpt=output[:2000],
                )
            await self._append(
                run_id,
                task_id,
                row.id.hex + ":" + effect,
                "sandbox_reconcile" if reconcile else "sandbox_receipt",
                {"execution_id": row.id.hex, "effect": effect, "evidence": journal_evidence},
            )
            return self._sandbox_data(row)

    async def sandbox_executions(self, run_id: str) -> list[Data]:
        async with self.store._unit():
            return [self._sandbox_data(row) for row in await self.sandboxes.for_run(identifier(run_id))]
