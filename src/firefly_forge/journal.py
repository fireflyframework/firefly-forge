# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Database plans and write-ahead intents: an unknown external effect is never replayed implicitly."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

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
from firefly_forge.prompts import prompt_text
from firefly_forge.sandbox_persistence import ForgeSandboxRepository
from firefly_forge.store import TERMINAL, Store, identifier, now, prepared_credential_rejection

if TYPE_CHECKING:
    from firefly_forge.retained_verified_source import PreparedVerifiedResume
    from firefly_forge.scm_source_edit_models import SealedSourceEditAdmission


Data = dict[str, Any]
ZERO_HASH = "0" * 64
LOCAL_TASKS = {"prepare", "implement", "verify", "checkpoint"}
FAILURE_ALIASES = {"rate_limited": "rate_limit", "insufficient_credits": "quota", "missing_auth": "authentication"}
RECOVERY_REASON = "Worker stopped without a durable receipt; inspect the retained source and external target"
_RECOVERY_CLAIMS = {
    "invalid_read_claim": "invalid_read_claim_digest",
    "checkpoint_mode_claim": "checkpoint_mode_claim_digest",
    "original_selection_claim": "original_selection_claim_digest",
}


def digest(data: Data) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _worker_message_key(kind: str, message_id: str) -> str:
    key = f"{kind}:{message_id}"
    return key if len(key) <= 64 else digest({"kind": kind, "id": message_id})


def _recovery_generation(entries: list[Data], task_id: str, key: str) -> Data | None:
    generations = [
        entry
        for entry in entries
        if entry["task_id"] == task_id
        and (
            (entry["kind"] == "intent" and entry["idempotency_key"] == key)
            or (
                entry["kind"] in _RECOVERY_CLAIMS
                and isinstance(entry["payload"], dict)
                and entry["payload"].get("task_key") == key
            )
        )
    ]
    if any(
        entry["kind"] in _RECOVERY_CLAIMS
        and (type(entry["payload"].get("version")) is not int or entry["payload"].get("version") != 1)
        for entry in generations
    ):
        raise ValueError("Recovery requires a native integer claim version")
    return generations[-1] if generations else None


def _recovery_history(entries: list[Data], task_id: str, key: str) -> list[Data]:
    return [
        entry
        for entry in entries
        if entry["kind"] == "recovery"
        and entry["task_id"] == task_id
        and (
            entry["idempotency_key"] == key
            or (isinstance(entry["payload"], dict) and entry["payload"].get("task_key") == key)
        )
    ]


def _recovery_key(run_id: str, task_id: str, key: str, generation: Data) -> str:
    return digest(
        {
            "version": 1,
            "kind": "recovery",
            "run_id": run_id,
            "task_id": task_id,
            "task_key": key,
            "generation_digest": generation["digest"],
        }
    )


def valid_recovery_annotation(record: Data, entries: list[Data], task_id: str, key: str) -> bool:
    """Validate an observation against its own preceding native execution generation."""
    try:
        if (
            not Journal.verify_chain(entries)
            or record not in entries
            or any(entry["run_id"] != record["run_id"] for entry in entries)
            or record["kind"] != "recovery"
            or record["task_id"] != task_id
            or type(record["sequence"]) is not int
        ):
            return False
        before = [entry for entry in entries if entry["sequence"] < record["sequence"]]
        generation = _recovery_generation(before, task_id, key)
        if record["payload"] == {"reason": RECOVERY_REASON}:
            return bool(record["idempotency_key"] == key)
        payload = record["payload"]
        if (
            generation is None
            or not isinstance(payload, dict)
            or type(payload.get("version")) is not int
            or payload.get("observation") not in {"running", "needs_reconciliation"}
        ):
            return False
        prior = _recovery_history(before, task_id, key)
        previous = prior[-1] if prior else None
        if payload["observation"] == "needs_reconciliation" and (
            previous is None or previous["sequence"] >= generation["sequence"]
        ):
            return False
        return bool(
            record["idempotency_key"] == _recovery_key(record["run_id"], task_id, key, generation)
            and payload
            == {
                "version": 1,
                "reason": RECOVERY_REASON,
                "task_key": key,
                "generation": {name: generation[name] for name in ("kind", "idempotency_key", "digest")},
                "previous_recovery_digest": previous["digest"] if previous else None,
                "observation": payload["observation"],
            }
        )
    except (KeyError, TypeError, ValueError):
        return False


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
        from firefly_forge.source_catalog import SourceCatalogBudget

        self.source_catalog_budget = SourceCatalogBudget()

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
        from firefly_forge.agentic_tool_custody import nested_effect_parent

        parent = nested_effect_parent(kind)
        run_id = identifier(run_id).hex
        current = await self.store.get_run(run_id)
        if current.get("bridge_completion_hold"):
            await self._bridge_read_bound(
                run_id,
                extra=len(json.dumps(payload, ensure_ascii=False).encode()) + 4096,
                consume=kind == "bridge_request_response",
                checkpoint_consume=kind == "agent_consume",
            )
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
        if parent is not None:
            effect = {name: data[name] for name in ("kind", "idempotency_key", "digest")}
            await self._append(
                run_id,
                task_id,
                digest({"parent": parent, "effect": effect}),
                "agentic_tool_effect_link",
                {"version": 1, "custody_id": parent, "effect": effect},
            )
        return data

    async def git_delivery_append(self, run_id: str, kind: str, key: str, payload: Data) -> Data:
        if kind not in {
            "git_delivery_intent",
            "git_delivery_receipt",
            "git_delivery_remote_intent",
            "git_delivery_remote_receipt",
            "git_delivery_observation",
            "git_delivery_draft",
            "git_delivery_hold",
            "git_failed_unit_adoption",
        }:
            raise ValueError("Unsupported factory Git delivery journal event")
        await self.store.assert_ownership()
        async with self.store._unit():
            await self._run(run_id)
            return await self._append(run_id, "checkpoint", key, kind, payload)

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
                        "instructions": prompt_text("tasks/prepare"),
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
                            "instructions": prompt_text("tasks/verify"),
                        },
                        {
                            "id": "checkpoint",
                            "kind": "local",
                            "title": "Retain the verified source checkpoint",
                            "instructions": prompt_text("tasks/checkpoint"),
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
                            "instructions": prompt_text("tasks/review"),
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
                    "instructions": prompt_text("tasks/reconcile"),
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

    async def _bridge_read_bound(
        self, run_id: str, *, extra: int = 0, consume: bool = False, checkpoint_consume: bool = False
    ) -> None:
        budget = await self.records.bridge_budget(identifier(run_id))
        if (
            budget["rows"] > 10000
            or budget["max_intent_bytes"] > 16384
            or budget["bytes"]
            + max(budget["intents"] - budget["responses"] - int(consume), 0) * 532480
            + max(budget.get("checkpoints", 0) - budget.get("checkpoint_consumes", 0) - int(checkpoint_consume), 0)
            * 4096
            + extra
            > 16 * 1024 * 1024
        ):
            raise ValueError("Run completion evidence exceeds its reserved read bound")

    async def entries(self, run_id: str) -> list[Data]:
        from pyfly.data.transaction import Propagation, TransactionTemplate, current_unit_of_work

        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

        reader = CURRENT_PRINCIPAL.get()
        caller = current_unit_of_work()
        async with self.store._unit():
            run = await self.store.get_run(run_id)
            scope = self._worker_scope(run)
            if run.get("bridge_completion_hold"):
                await self._bridge_read_bound(run_id)
            native_records = await self.records.find_for_run(identifier(run_id))
            entries = [{**copy.deepcopy(entry.data), "digest": entry.digest} for entry in native_records]
            protected = [
                entry["payload"]
                for entry in entries
                if entry["kind"]
                in {
                    "diagnostic_observation",
                    "worker_feedback",
                    "worker_response",
                    "bridge_request_intent",
                    "bridge_request_observation",
                    "bridge_request_response",
                    "agent_checkpoint",
                    "agent_consume",
                    "agentic_tool_intent",
                    "agentic_tool_result",
                    "agentic_tool_unknown",
                    "provider_rejection_recovery",
                    "readonly_observation_recovery",
                }
            ]
            if protected:
                await self._diagnostic_reader(run, reader)
        if not protected:
            return entries

        checkpoint_payloads = [entry["payload"] for entry in entries if entry["kind"] == "agent_checkpoint"]
        rejection_entries = [entry for entry in entries if entry["kind"] == "provider_rejection_recovery"]
        observation_entries = [entry for entry in entries if entry["kind"] == "readonly_observation_recovery"]
        if len(observation_entries) > 100:
            raise ValueError("Journal observation custody exceeds its retained request bound")
        if len(checkpoint_payloads) > 100:
            raise ValueError("Journal checkpoint history exceeds its retained bound")
        if len(rejection_entries) > 100:
            raise ValueError("Journal rejection custody exceeds its retained request bound")

        async def checked(value: Any) -> None:
            async with self.store._unit():
                current = await self.store.get_run(run_id)
                if self._worker_scope(current) != scope:
                    raise AccessDenied("Journal run scope changed during credential admission")
                await self._diagnostic_reader(current, reader)
                self.store.reject_credentials(value)

        async def admitted() -> None:
            from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
            from firefly_forge.known_worker_completions import _entries

            try:
                historical_custody = _entries(native_records, run_id) == entries
            except (AttributeError, KeyError, ValueError):
                historical_custody = False

            with prepared_credential_rejection(self.store):
                await self.store.admit_credentials(protected)
                await checked(protected)
                for entry in observation_entries:
                    from firefly_forge.readonly_observation_recoveries import settled_observation

                    if not historical_custody:
                        raise ValueError("Observation credential inspection requires intact native custody")
                    settled_observation(entry, entries)
                    p = entry["payload"]
                    canonical = BoundaryCheckpoint.credential_text(p["checkpoint"].encode(), p["checkpoint_digest"])
                    await self.store.admit_credentials(canonical)
                    await checked(canonical)
                for entry in rejection_entries:
                    from firefly_forge.provider_rejection_recoveries import settled_rejection

                    if not historical_custody:
                        raise ValueError("Rejection credential inspection requires intact native custody")
                    settled_rejection(entry, entries)
                    ticket = entry["payload"]["failed_ticket"]
                    canonical = BoundaryCheckpoint.credential_text(
                        ticket["checkpoint"].encode("utf-8"), ticket["checkpoint_digest"]
                    )
                    await self.store.admit_credentials(canonical)
                    await checked(canonical)
                for payload in checkpoint_payloads:
                    canonical = BoundaryCheckpoint.credential_text(
                        payload["snapshot"].encode("utf-8"),
                        payload["checkpoint_digest"],
                        historical_custody=historical_custody,
                    )
                    await self.store.admit_credentials(canonical)
                    await checked(canonical)

        if caller is None:
            await admitted()
        else:
            # Suspend the caller binding; its physical locks remain held.
            async with TransactionTemplate(caller.manager, propagation=Propagation.NOT_SUPPORTED).transaction():
                await admitted()
        return entries

    async def _diagnostic_reader(self, run: Data, reader: Any) -> None:
        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

        principal = CURRENT_PRINCIPAL.get()
        if principal != reader:
            raise AccessDenied("Diagnostic reader changed during credential admission")
        if principal is not None:
            access = ProjectAccess(self.store)
            if run.get("project_id"):
                await access.require_project(principal, run["project_id"], "read")
            else:
                await access.require_repository(principal, run["repository_id"], "read")
        if CURRENT_PRINCIPAL.get() != reader:
            raise AccessDenied("Journal reader changed during authorization")

    async def php_diagnostic(self, run_id: str, request: Any, observation: Data) -> Data:
        from firefly_forge.php_diagnostics import PHPProfileObservation, PHPProfileRequest
        from firefly_forge.project_access import ProjectAccess

        request = PHPProfileRequest.model_validate(request.model_dump(mode="json"))
        observation = PHPProfileObservation.model_validate(observation, strict=True).model_dump(mode="json")
        if len(json.dumps(observation, ensure_ascii=False).encode()) > 131_072:
            raise ValueError("PHP diagnostic observation exceeds 128 KiB")
        await self.store.assert_ownership()
        async with self.store._unit():
            run = await self.store.get_run(run_id)
            await ProjectAccess(self.store).authorize_execution(run)
            if (run.get("cancel_request") or {}).get("status") == "requested":
                raise ValueError("Cancelled runs cannot retain a new diagnostic observation")
        await self.store.admit_credentials(observation)
        await self.store.assert_ownership()
        async with self.store._unit():
            run_row = await self._run(run_id)
            await self._diagnostic_writer_fence(run_row.data)
            await ProjectAccess(self.store).authorize_execution(run_row.data)
            if (run_row.data.get("cancel_request") or {}).get("status") == "requested":
                raise ValueError("Cancelled runs cannot retain a new diagnostic observation")
            self.store.reject_credentials(observation)
            execution_id = await self._php_diagnostic_binding(run_id, run_row.data, request, observation)
            key = execution_id + ":php_profile"
            existing = next(
                (
                    entry
                    for entry in await self.records.find_for_run(run_row.id)
                    if entry.kind == "diagnostic_observation" and entry.idempotency_key == key
                ),
                None,
            )
            self.store.reject_credentials(observation)
            if existing is not None:
                if existing.task_id != "implement" or existing.data["payload"] != observation:
                    raise ValueError("Retained PHP diagnostic payload changed")
                return {**copy.deepcopy(existing.data), "digest": existing.digest}
            entry = await self._append(run_id, "implement", key, "diagnostic_observation", observation)
            self.store.reject_credentials(observation)
            return entry

    async def _diagnostic_writer_fence(self, run: Data) -> None:
        from sqlalchemy import text

        from firefly_forge.identity_persistence import ForgeIdentityUserRepository

        actor = run.get("execution_actor")
        if actor is None:
            return
        async with self.store._unit() as session:
            if run.get("project_id"):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": self.store.schema + ":members:" + run["project_id"]},
                )
            await ForgeIdentityUserRepository().find_by_id(
                identifier(actor["user_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )

    async def _php_diagnostic_binding(self, run_id: str, run: Data, request: Any, observation: Data) -> str:
        from firefly_forge.agent_roles import stage_allowed_tools
        from firefly_forge.php_diagnostics import build_php_profile_argv, php_profile_observation, profile_configuration
        from firefly_forge.process import ProcessResult

        receipt = observation["receipt"]
        if receipt is None:
            raise ValueError("PHP diagnostic requires retained command receipts")
        execution_id = str(receipt["execution_id"])
        row = await self.sandboxes.find_by_id(identifier(execution_id))
        task = await self._task(run_id, "implement")
        config = profile_configuration(run.get("sandbox_snapshot"))
        grants = stage_allowed_tools(run.get("role_snapshot") or {}, "implement", "agentic")
        if not {"profile_php", "execute_command"}.issubset(grants or ()):
            raise ValueError("PHP diagnostic grants are no longer admitted")
        if (
            row is None
            or row.run_id != identifier(run_id)
            or row.task_id != "implement"
            or row.parent_attempt != task.attempt
            or row.status != "removed"
        ):
            raise ValueError("PHP diagnostic command does not belong to the current completed stage")
        retained = row.data["request"]
        command = retained["request"]
        effects = row.data["effects"]
        start = effects.get("start", {})
        cleanup = effects.get("cleanup", {})
        if (
            start.get("status") != "done"
            or cleanup.get("status") != "done"
            or cleanup.get("evidence", {}).get("container_absent") is not True
            or "exit_code" not in start.get("evidence", {})
            or start["evidence"].get("outcome") == "interrupted"
            or start["evidence"].get("workspace_import") == "requires_reconciliation"
        ):
            raise ValueError("PHP diagnostic requires completed start and cleanup receipts")
        expected_binding = {
            "execution_id": execution_id,
            "request_digest": retained["request_digest"],
            "snapshot_digest": retained["snapshot_digest"],
            "workspace_digest": command.get("workspace_digest"),
            "start_key": execution_id + ":start",
            "cleanup_key": execution_id + ":cleanup",
        }
        if (
            receipt != expected_binding
            or retained["request_digest"] != digest(command)
            or retained["snapshot_digest"] != run["sandbox_snapshot"]["digest"]
            or command["configuration"] != run["sandbox_snapshot"]["configuration"]
            or command["argv"] != build_php_profile_argv(request, observation["source_sha256"])
        ):
            raise ValueError("PHP diagnostic request differs from its frozen command")
        evidence = start["evidence"]
        expected = php_profile_observation(
            request,
            observation["source_sha256"],
            str(config.image),
            ProcessResult(command["argv"], evidence["exit_code"], evidence["output"]),
            expected_binding,
        )
        if observation != expected:
            raise ValueError("PHP diagnostic observation differs from its retained command result")
        records = await self.records.find_for_run(identifier(run_id))
        if not self.verify_chain([{**copy.deepcopy(entry.data), "digest": entry.digest} for entry in records]):
            raise ValueError("PHP diagnostic requires an intact command journal")
        for effect in ("create", "start", "cleanup"):
            if not any(
                entry.kind == "sandbox_intent"
                and entry.task_id == "implement"
                and entry.idempotency_key == execution_id + ":" + effect
                and entry.data["payload"] == {"execution_id": execution_id, "effect": effect, "request": retained}
                for entry in records
            ):
                raise ValueError("PHP diagnostic request differs from its immutable command intent")
        for effect in ("start", "cleanup"):
            journal_evidence = copy.deepcopy(effects[effect]["evidence"])
            if isinstance(journal_evidence.get("output"), str):
                output = journal_evidence.pop("output")
                journal_evidence.update(
                    output_sha256=hashlib.sha256(output.encode()).hexdigest(),
                    output_bytes=len(output.encode()),
                    output_excerpt=output[:2000],
                )
            if not any(
                entry.kind in {"sandbox_receipt", "sandbox_reconcile"}
                and entry.task_id == "implement"
                and entry.idempotency_key == receipt[effect + "_key"]
                and entry.data["payload"].get("execution_id") == execution_id
                and entry.data["payload"].get("effect") == effect
                and entry.data["payload"].get("evidence") == journal_evidence
                for entry in records
            ):
                raise ValueError("PHP diagnostic requires its exact journal command receipts")
        return execution_id

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
                if not receipted:
                    from firefly_forge.checkpoint_mode_runtime import claim_adopted_intent

                    adopted = await claim_adopted_intent(self, run, task, existing)
                    if adopted is not None:
                        return adopted
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
        from firefly_forge.native_terminal_completions import native_terminal_acceptance

        async with native_terminal_acceptance(self, run_id, "receipt", key, evidence, checkpoint) as terminal:
            return await self._receipt(run_id, key, evidence, checkpoint=checkpoint, terminal=terminal)

    async def _receipt(
        self, run_id: str, key: str, evidence: Data, *, checkpoint: Data | None = None, terminal: Any = None
    ) -> Data:
        async with self.store._unit():
            if terminal is not None:
                await terminal.before()
            run = await self._run(run_id)
            if run.data.get("checkpoint_mode_adoption"):
                from firefly_forge.checkpoint_mode_runtime import assert_active_mode

                await assert_active_mode(self, run_id)
            existing = next(
                (
                    entry
                    for entry in await self.records.find_for_run(run.id)
                    if entry.kind == "receipt" and entry.idempotency_key == key
                ),
                None,
            )
            if existing is not None:
                if terminal is not None:
                    await terminal.commit(copy.deepcopy(existing.data))
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
            if terminal is not None:
                await terminal.commit(record)
            if run.data.get("checkpoint_mode_adoption"):
                await assert_active_mode(self, run_id)
            return record

    async def fail(
        self,
        run_id: str,
        key: str,
        kind: str,
        message: str,
        *,
        retry_after: float | None = None,
        observation: Data | None = None,
    ) -> Data:
        kind = FAILURE_ALIASES.get(kind, kind)
        if kind not in {
            "rate_limit",
            "quota",
            "authentication",
            "transient",
            "verification",
            "source_invalid",
            "provider_paused",
            "provider_outcome_unknown",
            "bridge_admission_refused",
        }:
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
                if kind == "bridge_admission_refused" and (
                    existing.data["payload"]["kind"] != kind
                    or observation is not None
                    and existing.data["payload"].get("provider_observation") != observation
                ):
                    raise ValueError("Known Bridge refusal cannot replace a retained failure")
                return copy.deepcopy(existing.data)
            if kind == "bridge_admission_refused" and (
                observation is None
                or set(observation) != {"completion_request_id", "intent_digest", "principal_id", "admission", "code"}
            ):
                raise ValueError("Known Bridge admission refusal requires its original completion identity")
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
            if observation:
                if set(observation) - {
                    "job_id",
                    "retry_after",
                    "completion_request_id",
                    "intent_digest",
                    "principal_id",
                    "admission",
                    "code",
                }:
                    raise ValueError("Unknown provider observation field")
                if "job_id" in observation and (
                    not isinstance(observation["job_id"], str)
                    or len(observation["job_id"]) != 36
                    or str(uuid.UUID(observation["job_id"])) != observation["job_id"]
                ):
                    raise ValueError("Invalid provider job reference")
                delay_observed = observation.get("retry_after")
                if "retry_after" in observation and (
                    not isinstance(delay_observed, (float, int))
                    or isinstance(delay_observed, bool)
                    or not 0 <= delay_observed <= 86400
                    or not math.isfinite(delay_observed)
                ):
                    raise ValueError("Invalid provider retry observation")
                if "completion_request_id" in observation:
                    from pydantic import TypeAdapter

                    from firefly_forge.bridge_completion_models import Digest, RequestID

                    TypeAdapter(RequestID).validate_python(observation["completion_request_id"], strict=True)
                    TypeAdapter(Digest).validate_python(observation.get("intent_digest"), strict=True)
                    principal = observation.get("principal_id")
                    if not isinstance(principal, str) or not 1 <= len(principal) <= 64:
                        raise ValueError("Invalid Bridge principal reference")
                if ("admission" in observation or "code" in observation) and (
                    kind != "bridge_admission_refused"
                    or observation.get("admission") != "admission_refused"
                    or observation.get("code") != "completion_recovery_full"
                    or "completion_request_id" not in observation
                    or "job_id" in observation
                    or "retry_after" in observation
                ):
                    raise ValueError("Invalid known Bridge admission refusal")
                pause["provider_observation"] = copy.deepcopy(observation)
            record = await self._append(run_id, task.task_id, key, "error", pause)
            task.status = "paused" if kind not in {"verification", "source_invalid"} else "failed"
            if kind in {"provider_outcome_unknown", "bridge_admission_refused"}:
                task.status = "needs_reconciliation"
            task.data = {**task.data, "error": pause}
            await self.tasks.save(task)
            if task.task_id in LOCAL_TASKS or run.status not in TERMINAL:
                run.status = task.status if task.status in {"paused", "needs_reconciliation"} else "failed"
            run.data = {
                **run.data,
                "status": run.status,
                "pause": pause,
                "error": run.data.get("error")
                if run.status in TERMINAL and task.task_id not in LOCAL_TASKS
                else message[:8000],
                "display_status": run.status
                if run.status in TERMINAL
                else "needs_reconciliation"
                if kind in {"provider_outcome_unknown", "bridge_admission_refused"}
                else "provider_paused"
                if kind == "provider_paused"
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

    @staticmethod
    def _worker_scope(run: Data) -> tuple[Any, ...]:
        return (run.get("id"), run.get("project_id"), run.get("repository_id"), bool(run.get("publish")))

    async def _worker_authority(self, run: Data, actor: Any, *, writing: bool) -> None:
        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, Permission, ProjectAccess

        if CURRENT_PRINCIPAL.get() != actor or (writing and actor is None):
            raise AccessDenied("The authenticated worker response actor changed or is unavailable")
        if actor is None:
            return
        access = ProjectAccess(self.store)
        if actor.authentication_method == "scm_mapping":
            delegated = actor.scm_delegation or {}
            if (
                (delegated.get("run_id"), delegated.get("repository_id"), delegated.get("component_id"))
                != (
                    run.get("id"),
                    run.get("repository_id"),
                    run.get("component_id"),
                )
                or writing
                and delegated.get("action") != "respond"
            ):
                raise AccessDenied("The SCM delegation does not bind this worker response")
        permission: Permission = "execute" if writing else "read"
        if run.get("project_id"):
            await access.authorize_actor(access.actor_snapshot(actor), run["project_id"], permission)
            if writing and run.get("publish"):
                await access.authorize_actor(access.actor_snapshot(actor), run["project_id"], "release")
        elif writing:
            if not await access.current_administrator(actor):
                raise AccessDenied("An unassigned run requires current factory administrator authority")
        else:
            await access.require_repository(actor, run["repository_id"], "read")
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("The authenticated worker response actor changed during authorization")

    async def worker_messages(self, run_id: str) -> list[Data]:
        entries = await self.entries(run_id)
        return [entry for entry in entries if entry["kind"] in {"worker_feedback", "worker_response"}]

    async def implementation_context(self, run_id: str) -> Data:
        from pyfly.data.transaction import Propagation, TransactionTemplate, current_unit_of_work

        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
        from firefly_forge.worker_context import implementation_context

        reader, caller = CURRENT_PRINCIPAL.get(), current_unit_of_work()

        def scope(run: Data) -> tuple[Any, ...]:
            return (
                self._worker_scope(run),
                run.get("component_id"),
                digest(run.get("role_snapshot") or {}),
                digest(run.get("worker_response") or {}),
                digest(run.get("execution_actor") or {}),
            )

        async with self.store._unit():
            initial = await self.store.get_run(run_id)
            frozen = scope(initial)
            await self._diagnostic_reader(initial, reader)
            await ProjectAccess(self.store).authorize_execution(initial)
            snapshot = await self.records.find_implementation_context(identifier(run_id))
            context = implementation_context(initial, snapshot)

        async def admitted() -> None:
            await self.store.admit_credentials(context)
            async with self.store._unit():
                current = await self.store.get_run(run_id)
                await self._diagnostic_reader(current, reader)
                await ProjectAccess(self.store).authorize_execution(current)
                if (current.get("cancel_request") or {}).get("status") == "requested":
                    raise AccessDenied("Cancelled work cannot receive retained implementation context")
                if scope(current) != frozen:
                    raise AccessDenied("Worker context scope or current answer changed during admission")
                latest = await self.records.find_implementation_context(identifier(run_id))
                if implementation_context(current, latest) != context:
                    raise ValueError("Retained worker context changed during credential admission")
                self.store.reject_credentials(context)

        if caller is None:
            await admitted()
        else:
            async with TransactionTemplate(caller.manager, propagation=Propagation.NOT_SUPPORTED).transaction():
                await admitted()
        return context

    async def worker_feedback(
        self, run_id: str, feedback: Any, *, evidence: Data | None = None, restoration: Any = None
    ) -> Data:
        from firefly_forge.agent_roles import (
            NativeAllowanceWorkerFeedback,
            SourceBudgetWorkerFeedback,
            worker_feedback_model,
        )

        feedback = worker_feedback_model(feedback).model_validate(feedback)
        if isinstance(feedback, NativeAllowanceWorkerFeedback):
            from firefly_forge.provider_recoveries import ProviderRecoveries

            run = await self.store.get_run(run_id)
            actor = (run.get("provider_recovery") or {}).get("actor")
            if not actor:
                raise ValueError("Native final feedback requires its original allowance actor")
            async with ProviderRecoveries(self.store, self, None)._locked(run_id, actor):
                return await self._worker_feedback(run_id, feedback, evidence=evidence, restoration=restoration)
        if isinstance(feedback, SourceBudgetWorkerFeedback):
            from firefly_forge.provider_recoveries import ProviderRecoveries
            from firefly_forge.source_budget_allowances import current_budget_receipt

            run = await self.store.get_run(run_id)
            async with self.store._unit():
                rows = await self.records.find_for_run(identifier(run_id))
                receipt = current_budget_receipt(self.store, run, [{**row.data, "digest": row.digest} for row in rows])
                if receipt is None:
                    raise ValueError("Source budget feedback requires its actual native receipt")
                async with ProviderRecoveries(self.store, self, None)._locked(run_id, receipt["actor"]):
                    return await self._worker_feedback(run_id, feedback, evidence=evidence, restoration=restoration)
        return await self._worker_feedback(run_id, feedback, evidence=evidence, restoration=restoration)

    async def _worker_feedback(
        self, run_id: str, feedback: Any, *, evidence: Data | None = None, restoration: Any = None
    ) -> Data:
        from firefly_forge.native_terminal_completions import native_terminal_acceptance

        payload = feedback.model_dump(mode="json")
        async with native_terminal_acceptance(
            self,
            run_id,
            "worker_feedback",
            _worker_message_key("worker-feedback", feedback.id),
            payload,
            restoration=restoration,
        ) as terminal:
            return await self._accept_worker_feedback(run_id, feedback, evidence=evidence, terminal=terminal)

    async def _accept_worker_feedback(
        self, run_id: str, feedback: Any, *, evidence: Data | None = None, terminal: Any = None
    ) -> Data:
        from firefly_forge.agent_roles import (
            BINDINGS,
            NativeAllowanceWorkerFeedback,
            SourceBudgetWorkerFeedback,
            worker_feedback_model,
        )
        from firefly_forge.worker_evidence import WorkerEvidenceManifest

        model = worker_feedback_model(feedback)
        if evidence is not None:
            manifest = WorkerEvidenceManifest.model_validate(evidence)
            evidence = manifest.model_dump()
            if (
                evidence["run_id"] != run_id
                or evidence["feedback_id"] != feedback.id
                or feedback.request is None
                or evidence["request_id"] != feedback.request.id
                or len({image["id"] for image in evidence["images"]}) != len(evidence["images"])
            ):
                raise ValueError("Worker evidence must identify this exact native feedback and question")
            manifest.check_feedback(feedback.model_dump(mode="json"), feedback.task_id)
        async with self.store._unit():
            if terminal is not None:
                await terminal.before()
            run = await self._run(run_id)
            payload = model.model_validate_json(self._worker_payload(run, feedback.model_dump_json())).model_dump(
                mode="json"
            )
            entries = await self.records.find_for_run(run.id)
            if isinstance(feedback, NativeAllowanceWorkerFeedback):
                from firefly_forge.provider_recovery_allowances import sealed_allowance

                task = await self._task(run_id, "implement")
                native_entries = [{**copy.deepcopy(e.data), "digest": e.digest} for e in entries]
                if not self.verify_chain(native_entries):
                    raise ValueError("Native final feedback requires an intact allowance journal")
                link = sealed_allowance(run.data, {**task.data, "attempt": task.attempt}, native_entries)
                if run.data["provider_recovery"].get("status") not in {"consumed", "completed"} or any(
                    payload[name] != value for name, value in link.items()
                ):
                    raise ValueError("Native final feedback does not match its consumed allowance")
            if isinstance(feedback, SourceBudgetWorkerFeedback):
                from firefly_forge.source_budget_allowances import feedback_binding

                current_task = await self._task(run_id, "implement")
                native_entries = [{**copy.deepcopy(e.data), "digest": e.digest} for e in entries]
                if not self.verify_chain(native_entries):
                    raise ValueError("Source budget feedback requires an intact native journal")
                binding = feedback_binding(
                    run.data,
                    {**current_task.data, "status": current_task.status, "attempt": current_task.attempt},
                    native_entries,
                    self.store,
                )
                if binding is None or any(payload.get(name) != value for name, value in binding.items()):
                    raise ValueError("Worker feedback does not match its exact source budget allowance")
            key = _worker_message_key("worker-feedback", feedback.id)
            existing = next(
                (entry for entry in entries if entry.kind == "worker_feedback" and entry.idempotency_key == key), None
            )
            if existing:
                if existing.data["payload"] != payload:
                    raise ValueError("Feedback identifier already contains different content")
                if evidence is not None:
                    retained = [
                        entry.data["payload"]
                        for entry in entries
                        if entry.kind == "worker_evidence" and entry.data["payload"].get("feedback_id") == feedback.id
                    ]
                    if retained != [evidence]:
                        raise ValueError("Worker evidence replay differs from its immutable receipt")
                if terminal is not None:
                    await terminal.commit(copy.deepcopy(existing.data))
                return copy.deepcopy(existing.data)
            if run.status in TERMINAL:
                raise ValueError("A terminal run cannot accept late worker feedback")
            task = await self._task(run_id, feedback.task_id)
            if isinstance(feedback, (NativeAllowanceWorkerFeedback, SourceBudgetWorkerFeedback)) and (
                run.status != "developing" or task.status != "running"
            ):
                raise ValueError("New final allowance feedback requires its active implementation")
            if (
                run.data.get("provider_recovery")
                and feedback.task_id == "implement"
                and feedback.attempt != task.attempt
            ):
                raise ValueError("Late worker feedback cannot replace the reserved current provider attempt")
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
            if evidence is not None:
                await self._append(
                    run_id,
                    feedback.task_id,
                    _worker_message_key("worker-evidence", feedback.id),
                    "worker_evidence",
                    evidence,
                )
                run.data = {**run.data, "worker_evidence": evidence}
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
                    "verification_handoff": {},
                    "verification_refusal": {},
                    "verification_adoption": {},
                }
            await self.store.run_records.save(run)
            if terminal is not None:
                await terminal.commit(record)
            return record

    async def worker_response(self, run_id: str, request_id: str, response: Any) -> Data:
        from pyfly.data.transaction import current_unit_of_work
        from sqlalchemy import text

        from firefly_forge.agent_roles import WorkerResponse
        from firefly_forge.identity_persistence import ForgeIdentityUserRepository
        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

        actor = CURRENT_PRINCIPAL.get()
        if actor is None:
            raise AccessDenied("An authenticated operator is required to answer a worker request")
        if current_unit_of_work() is not None:
            raise ValueError("Answer worker requests outside an active caller transaction")
        response = WorkerResponse.model_validate(response)
        payload = {**response.model_dump(mode="json"), "request_id": request_id}
        responding_actor = {
            "version": 1,
            "user_id": actor.id.hex,
            "authentication_method": actor.authentication_method,
        }
        if actor.authentication_method == "scm_mapping":
            from firefly_forge.scm_authority_models import SCMRespondingActorV2

            delegation = actor.scm_delegation or {}
            reference = delegation.get("verified_reference", {})
            from firefly_forge.scm_poll_models import SCMRespondingActorV3

            model: type[SCMRespondingActorV2] | type[SCMRespondingActorV3] = SCMRespondingActorV2
            polled = delegation.get("transport") == "github_rest_poll_v1"
            if polled:
                model = SCMRespondingActorV3
            responding_actor = model.model_validate(
                {
                    "version": 3 if polled else 2,
                    "user_id": actor.id.hex,
                    "authentication_method": "scm_mapping",
                    "provenance": {
                        **({"transport": "github_rest_poll_v1"} if polled else {}),
                        "binding_request_id": delegation.get("binding_request_id"),
                        "binding_digest": delegation.get("binding_digest"),
                        "verified_receipt_id": delegation.get("verified_receipt_id"),
                        **{
                            key: reference.get(key)
                            for key in (
                                "provider",
                                "api_origin",
                                "connection_revision",
                                "installation_id",
                                "remote_repository_id",
                                "external_actor_id",
                            )
                        },
                    },
                }
            ).model_dump(mode="json")
        async with self.store._unit():
            initial = await self.store.get_run(run_id)
            scope = self._worker_scope(initial)
            await self._worker_authority(initial, actor, writing=True)
        await self.store.admit_credentials(payload)
        async with self.store._unit() as session:
            if initial.get("project_id"):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": self.store.schema + ":members:" + initial["project_id"]},
                )
            identity = await ForgeIdentityUserRepository().find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
            identity_matches = (
                identity is not None
                and identity.enabled
                and identity.issuer == actor.issuer
                and identity.subject == actor.subject
            )
            run = await self._run(run_id)
            if (
                self._worker_scope(run.data) != scope
                or run.id != identifier(run_id)
                or run.repository_id != identifier(initial["repository_id"])
            ):
                raise AccessDenied("Worker response run scope changed during admission")
            await self._worker_authority(run.data, actor, writing=True)
            self._worker_payload(run, json.dumps(payload), reject_credentials=True)
            self.store.reject_credentials(payload)
            entries = await self.records.find_for_run(run.id)
            key = _worker_message_key("worker-response", response.id)
            existing = next(
                (entry for entry in entries if entry.kind == "worker_response" and entry.idempotency_key == key), None
            )
            if existing:
                retained = copy.deepcopy(existing.data["payload"])
                recorded_actor = retained.pop("responding_actor", None)
                if retained != payload:
                    raise ValueError("Response identifier already contains different content")
                if recorded_actor is not None and (not identity_matches or recorded_actor != responding_actor):
                    raise AccessDenied("Response identity belongs to another authenticated actor")
                record = copy.deepcopy(existing.data)
            else:
                if not identity_matches:
                    raise AccessDenied("The authenticated response identity is unavailable or inconsistent")
                request = run.data.get("worker_request", {})
                if request.get("id") != request_id:
                    raise ValueError("Worker request does not belong to this run")
                if request.get("status") != "pending":
                    raise ValueError("Worker request is already resolved")
                if run.status != "paused" or (run.data.get("cancel_request") or {}).get("status") == "requested":
                    raise ValueError("Only a paused, uncancelled run can receive a new worker response")
                payload["responding_actor"] = responding_actor
                record = await self._append(run_id, request["task_id"], key, "worker_response", payload)
                run.data = {
                    **run.data,
                    "worker_request": {**request, "status": "answered"},
                    "worker_response": payload,
                    "updated_at": now(),
                }
                await self.store.run_records.save(run)
            await self._worker_authority(run.data, actor, writing=True)
            self.store.reject_credentials(record)
            self._worker_payload(run, json.dumps(record), reject_credentials=True)
        # A refusal here does not undo an already committed immutable response.
        await self.store.admit_credentials(record)
        async with self.store._unit():
            current = await self.store.get_run(run_id)
            if self._worker_scope(current) != scope:
                raise AccessDenied("Worker response run scope changed before return")
            await self._worker_authority(current, actor, writing=True)
            self.store.reject_credentials(record)
        return record

    async def verification_adopt(
        self, run_id: str, body: Data, policy: Data, *, check_current: Callable[[Data], None]
    ) -> Data:
        from sqlalchemy import text

        from firefly_forge.identity_persistence import ForgeIdentityUserRepository
        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
        from firefly_forge.verification_models import VerificationAdoption

        body = VerificationAdoption.model_validate(body).model_dump()
        actor = CURRENT_PRINCIPAL.get()
        if actor is None or actor.authentication_method == "scm_mapping":
            raise AccessDenied("An authenticated native operator is required for verification adoption")
        initial = await self.store.get_run(run_id)
        scope = self._worker_scope(initial)
        await self._worker_authority(initial, actor, writing=True)
        async with self.store._unit() as session:
            if initial.get("project_id"):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": self.store.schema + ":members:" + initial["project_id"]},
                )
            identity = await ForgeIdentityUserRepository().find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
            if (
                identity is None
                or not identity.enabled
                or identity.issuer != actor.issuer
                or identity.subject != actor.subject
            ):
                raise AccessDenied("The verification adoption identity is unavailable or suspended")
            run = await self._run(run_id)
            if self._worker_scope(run.data) != scope:
                raise AccessDenied("Verification adoption scope changed during admission")
            await self._worker_authority(run.data, actor, writing=True)
            await ProjectAccess(self.store).authorize_execution(run.data)
            entries = await self.records.find_for_run(run.id)
            key = "verification-adopt:" + body["request_id"]
            existing = next(
                (
                    row.data["payload"]
                    for row in entries
                    if row.kind == "verification_handoff_adoption" and row.idempotency_key == key
                ),
                None,
            )
            if existing:
                if existing["request"] != body or existing["actor"]["user_id"] != actor.id.hex:
                    raise ValueError("Verification adoption identifier already contains different content or owner")
                return copy.deepcopy(existing)
            request = run.data.get("worker_request", {})
            if (
                run.status != "paused"
                or request.get("status") != "pending"
                or request.get("kind") != "verification"
                or request.get("task_id") != "implement"
                or digest(request) != body["pending_request_digest"]
            ):
                raise ValueError("Legacy verification adoption must bind the exact pending native request")
            if body["agent"] in {"codex", "claude"} and not await ProjectAccess(self.store).current_administrator(
                actor
            ):
                raise AccessDenied("Host coding provider selection requires current factory administrator authority")
            ProjectAccess.require_provider_isolation(run.data, body["agent"])
            check_current(run.data)
            payload = {
                "version": 1,
                "request": body,
                "body_digest": digest(body),
                "policy_snapshot": policy,
                "actor": {"user_id": actor.id.hex, "authentication_method": actor.authentication_method},
            }
            await self._append(run_id, "implement", key, "verification_handoff_adoption", payload)
            check_current(run.data)
            run.data = {
                **run.data,
                "verification_adoption": payload,
                "verification_refusal": {},
                "verification_handoff": {},
                "updated_at": now(),
            }
            await self.store.run_records.save(run)
            return copy.deepcopy(payload)

    async def verification_begin(self, run_id: str, intent: Data, *, check_current: Callable[[Data], None]) -> Data:
        from firefly_forge.project_access import ProjectAccess

        async with self.store._unit():
            run = await self._run(run_id)
            if "consumed_terminal_recovery" in run.data:
                raise ValueError("Automatic verification cannot replace consumed terminal postprocessing")
            request = run.data.get("worker_request", {})
            task = await self._task(run_id, "implement")
            retained = run.data.get("verification_handoff", {})
            if (
                run.status not in {"paused", "needs_reconciliation"}
                or request.get("kind") != "verification"
                or request.get("status") != "pending"
                or request.get("task_id") != "implement"
                or request.get("id") != intent["request_id"]
                or digest(request) != intent["request_digest"]
                or digest({"commands": run.data.get("commands", [])}) != intent["commands_digest"]
                or task.attempt != intent["parent_attempt"]
                or request.get("attempt") != task.attempt
                or not 1 <= task.attempt < 4
                or (run.data.get("cancel_request") or {}).get("status") == "requested"
            ):
                raise ValueError("Automatic verification request or stage changed")
            entries = await self.records.find_for_run(run.id)
            previous = next(
                (
                    entry
                    for entry in entries
                    if entry.kind == "verification_handoff_intent" and entry.idempotency_key == intent["id"]
                ),
                None,
            )
            if previous:
                if (
                    previous.data["payload"] != intent
                    or retained.get("id") != intent["id"]
                    or retained.get("status") != "running"
                ):
                    raise ValueError("Automatic verification intent changed or requires explicit attention")
            else:
                used = sum(entry.kind == "verification_handoff_intent" for entry in entries)
                if (
                    task.status != "paused"
                    or used + 1 != intent["continuation"]
                    or type(intent.get("continuation")) is not int
                    or type(intent.get("max_continuations")) is not int
                    or not 1 <= used + 1 <= intent["max_continuations"]
                ):
                    raise ValueError("Automatic verification continuation budget exhausted")
            await ProjectAccess(self.store).authorize_execution(run.data)
            check_current(run.data)
            await self._append(run_id, "implement", intent["id"], "verification_handoff_intent", intent)
            check_current(run.data)
            state = {
                **{
                    key: value
                    for key, value in intent.items()
                    if key
                    in {
                        "version",
                        "id",
                        "request_id",
                        "source_digest",
                        "commands_digest",
                        "policy_digest",
                        "provider_digest",
                        "continuation",
                        "max_continuations",
                        "commands_count",
                    }
                },
                "status": "running",
                "completed_commands": retained.get("completed_commands", 0) if previous else 0,
                "failed_commands": retained.get("failed_commands", 0) if previous else 0,
            }
            task.status = "running"
            task.data = {**task.data, "verification_handoff": intent["id"]}
            await self.tasks.save(task)
            run.data = {
                **run.data,
                "verification_handoff": state,
                "display_status": "automatic_verification",
                "updated_at": now(),
            }
            await self.store.run_records.save(run)
            await self.store.assert_ownership()
            await ProjectAccess(self.store).authorize_execution(run.data)
            check_current(run.data)
            return copy.deepcopy(state)

    async def verification_effect_authority(self, run_id: str, task_id: str, payload: Data) -> None:
        run = await self._run(run_id)
        task = await self._task(run_id, task_id)
        handoff_id = task.data.get("verification_handoff")
        if not handoff_id:
            return
        entries = await self.records.find_for_run(run.id)
        entry = next(
            (
                item
                for item in entries
                if item.kind == "verification_handoff_intent" and item.idempotency_key == handoff_id
            ),
            None,
        )
        if entry is None:
            raise ValueError("Automatic verification slot has no immutable intent")
        intent = entry.data["payload"]
        state = run.data.get("verification_handoff", {})
        if (
            state.get("id") != handoff_id
            or state.get("status") != "running"
            or payload.get("ordinal") not in intent["ordinals"]
        ):
            raise ValueError("Automatic verification slot is outside its admitted handoff")
        index = intent["ordinals"].index(payload["ordinal"])
        from firefly_forge.verification_scope_attestation import mapped_argv

        expected_argv = mapped_argv(run.data, intent, [e.data for e in entries], index)
        request = payload.get("request", {})
        if (
            task_id != "implement"
            or payload.get("parent_attempt") != intent["parent_attempt"]
            or request.get("argv") != expected_argv
            or request.get("workspace") != intent["workspace"]
            or request.get("cwd") != intent["cwd"]
            or request.get("timeout") != intent["timeouts"][index]
            or payload.get("snapshot_digest") != intent["sandbox_digest"]
            or digest(run.data.get("worker_request", {})) != intent["request_digest"]
        ):
            raise ValueError("Command differs from the frozen verification handoff")

    async def verification_command(
        self, run_id: str, intent: Data, index: int, *, check_current: Callable[[Data], None]
    ) -> Data:
        from firefly_forge.project_access import ProjectAccess

        async with self.store._unit():
            run = await self._run(run_id)
            if "consumed_terminal_recovery" in run.data:
                raise ValueError("Automatic verification cannot replace consumed terminal postprocessing")
            state = run.data.get("verification_handoff", {})
            if state.get("id") != intent["id"] or state.get("status") != "running":
                raise ValueError("Automatic verification is not active")
            receipt = next(
                (
                    row
                    for row in await self.sandbox_executions(run_id)
                    if row["task_id"] == "implement"
                    and row["parent_attempt"] == intent["parent_attempt"]
                    and row["ordinal"] == intent["ordinals"][index]
                ),
                None,
            )
            evidence = (receipt or {}).get("effects", {}).get("start", {})
            if evidence.get("status") != "done" or type(evidence.get("evidence", {}).get("exit_code")) is not int:
                raise ValueError("Automatic verification requires a known native command receipt")
            assert receipt is not None
            await ProjectAccess(self.store).authorize_execution(run.data)
            check_current(run.data)
            payload = {
                "version": 1,
                "id": intent["id"],
                "request_id": intent["request_id"],
                "command_index": index + 1,
                "ordinal": intent["ordinals"][index],
                "execution_id": receipt["execution_id"],
                "exit_code": evidence["evidence"]["exit_code"],
                "outcome": "completed",
            }
            await self._append(
                run_id, "implement", intent["id"] + ":" + str(index), "verification_handoff_command", payload
            )
            entries = await self.records.find_for_run(run.id)
            commands = [
                item.data["payload"]
                for item in entries
                if item.kind == "verification_handoff_command" and item.data["payload"].get("id") == intent["id"]
            ]
            check_current(run.data)
            state = {
                **state,
                "completed_commands": len(commands),
                "failed_commands": sum(item["exit_code"] != 0 for item in commands),
            }
            run.data = {**run.data, "verification_handoff": state, "updated_at": now()}
            await self.store.run_records.save(run)
            await self.store.assert_ownership()
            await ProjectAccess(self.store).authorize_execution(run.data)
            check_current(run.data)
            return copy.deepcopy(state)

    async def verification_finish(
        self,
        run_id: str,
        intent: Data,
        *,
        check_current: Callable[[Data], None],
        assert_inputs: Callable[[], Awaitable[None]] | None = None,
    ) -> Data:
        from firefly_forge.project_access import ProjectAccess

        async with self.store._unit():
            run = await self._run(run_id)
            if "consumed_terminal_recovery" in run.data:
                raise ValueError("Automatic verification cannot replace consumed terminal postprocessing")
            if assert_inputs is not None:
                await assert_inputs()
            state = run.data.get("verification_handoff", {})
            if (
                state.get("id") != intent["id"]
                or state.get("status") != "running"
                or state.get("completed_commands") != intent["commands_count"]
            ):
                raise ValueError("All frozen verification commands require known receipts before continuation")
            if digest(run.data.get("worker_request", {})) != intent["request_digest"]:
                raise ValueError("Pending verification request changed")
            receipts = await self.sandbox_executions(run_id)
            outputs: list[str] = []
            excerpt_limit = min(1500, 6000 // max(1, len(intent["ordinals"])))
            for ordinal in intent["ordinals"]:
                receipt = next(
                    (
                        row
                        for row in receipts
                        if row["task_id"] == "implement"
                        and row["parent_attempt"] == intent["parent_attempt"]
                        and row["ordinal"] == ordinal
                    ),
                    None,
                )
                evidence = (receipt or {}).get("effects", {}).get("start", {})
                if evidence.get("status") != "done" or type(evidence.get("evidence", {}).get("exit_code")) is not int:
                    raise ValueError("Native verification receipt is unavailable")
                outputs.append(
                    f"Command {len(outputs) + 1}: exit {evidence['evidence']['exit_code']}\n"
                    + evidence["evidence"].get("output", "")[:excerpt_limit]
                )
            await ProjectAccess(self.store).authorize_execution(run.data)
            check_current(run.data)
            answer = self._worker_payload(
                run,
                prompt_text(
                    "implementation/verification-result",
                    count=str(len(outputs)),
                    failed=str(state["failed_commands"]),
                    results="\n".join(outputs),
                ),
            ).strip()
            from firefly_forge.agent_roles import WorkerResponse

            answer = WorkerResponse(id=intent["id"], actor="Forge verification", answer=answer).answer
            state = {**state, "status": "completed"}
            response = {
                "id": intent["id"],
                "request_id": intent["request_id"],
                "actor": "Forge verification",
                "source": "factory_verification",
                "verification_handoff_id": intent["id"],
                "answer": answer,
                "responding_actor": {
                    "version": 4,
                    "authentication_method": "factory_verification",
                    "handoff_id": intent["id"],
                    **{
                        key: intent[key]
                        for key in ("source_digest", "commands_digest", "policy_digest", "provider_digest")
                    },
                },
            }
            await self._append(
                run_id, "implement", _worker_message_key("worker-response", intent["id"]), "worker_response", response
            )
            await self._append(run_id, "implement", intent["id"] + ":outcome", "verification_handoff_outcome", state)
            task = await self._task(run_id, "implement")
            check_current(run.data)
            task.status = "paused"
            task.data = {key: value for key, value in task.data.items() if key != "verification_handoff"}
            await self.tasks.save(task)
            run.status = "paused"
            run.data = {
                **run.data,
                "status": "paused",
                "verification_handoff": state,
                "worker_request": {
                    **run.data["worker_request"],
                    "status": "answered",
                    "response_source": "factory_verification",
                },
                "worker_response": response,
                "updated_at": now(),
            }
            await self.store.run_records.save(run)
            await self.store.assert_ownership()
            await ProjectAccess(self.store).authorize_execution(run.data)
            check_current(run.data)
            if assert_inputs is not None:
                await assert_inputs()
            return copy.deepcopy(state)

    async def verification_assert_current(self, run_id: str) -> None:
        from firefly_forge.unknown_command_acknowledgements import policy_digest as acknowledgement_policy_digest
        from firefly_forge.verification_handoffs import prepare_journal_verification_source
        from firefly_forge.verification_source_preparation import verification_source_scope

        acknowledgement_policy = acknowledgement_policy_digest()
        prepared = await prepare_journal_verification_source(self, run_id, provider=False)
        if prepared is None:
            return await self._verification_assert_current(run_id, acknowledgement_policy=acknowledgement_policy)
        with verification_source_scope(prepared):
            await self._verification_assert_current(run_id, acknowledgement_policy=acknowledgement_policy)

    async def _verification_assert_current(self, run_id: str, *, acknowledgement_policy: str | None = None) -> None:
        from firefly_forge.project_access import ProjectAccess
        from firefly_forge.verification_models import check_verification_cut

        async with self.store._unit():
            run = await self._run(run_id)
            state = run.data.get("verification_handoff", {})
            if state.get("status") != "running":
                return
            entries = await self.records.find_for_run(run.id)
            intent = next(
                item.data["payload"]
                for item in entries
                if item.kind == "verification_handoff_intent" and item.idempotency_key == state["id"]
            )
            await self.store.assert_ownership()
            await ProjectAccess(self.store).authorize_execution(run.data)
            from firefly_forge.unknown_command_proof import assert_followup_actors

            await assert_followup_actors(self, run.data, entries, intent, acknowledgement_policy)
            check_verification_cut(self.store, run.data, intent)

    async def verification_provider_admission(self, run_id: str) -> None:
        from firefly_forge.verification_handoffs import prepare_journal_verification_source
        from firefly_forge.verification_source_preparation import verification_source_scope

        prepared = await prepare_journal_verification_source(self, run_id, provider=True)
        if prepared is None:
            return await self._verification_provider_admission(run_id)
        with verification_source_scope(prepared):
            await self._verification_provider_admission(run_id)

    async def _verification_provider_admission(self, run_id: str) -> None:
        from firefly_forge.project_access import ProjectAccess
        from firefly_forge.verification_models import check_verification_cut

        async with self.store._unit():
            run = await self._run(run_id)
            response = run.data.get("worker_response", {})
            if response.get("source") != "factory_verification":
                return
            state = run.data.get("verification_handoff", {})
            entries = await self.records.find_for_run(run.id)
            intent = next(
                (
                    item.data["payload"]
                    for item in entries
                    if item.kind == "verification_handoff_intent"
                    and item.idempotency_key == response.get("verification_handoff_id")
                ),
                None,
            )
            if intent is None or state.get("id") != intent["id"] or state.get("status") != "completed":
                raise ValueError("Factory continuation requires completed verification evidence")
            if int(run.data.get("implementation_rounds", 0)) > intent["parent_round"]:
                return
            coding = next(
                (
                    item
                    for item in entries
                    if item.kind == "verification_continuation_intent"
                    and item.idempotency_key == intent["id"] + ":coding"
                ),
                None,
            )
            if coding is not None:
                from firefly_forge.checkpoint_mode_runtime import claimed_continuation

                if claimed_continuation(run.data, entries, coding):
                    from firefly_forge.checkpoint_mode_runtime import assert_mode_actors
                    from firefly_forge.known_worker_completions import _entries

                    await assert_mode_actors(self, run.data, _entries(entries, run_id))
                    check_verification_cut(self.store, run.data, intent)
                    return
                raise ValueError("The original verification continuation outcome requires reconciliation")
            await self.store.assert_ownership()
            await ProjectAccess(self.store).authorize_execution(run.data)
            check_verification_cut(self.store, run.data, intent)
            await self._append(
                run_id,
                "implement",
                intent["id"] + ":coding",
                "verification_continuation_intent",
                {
                    "version": 1,
                    "id": intent["id"],
                    "request_id": intent["request_id"],
                    "parent_round": intent["parent_round"],
                    "next_round": intent["parent_round"] + 1,
                    "provider_digest": intent["provider_digest"],
                },
            )
            await self.store.assert_ownership()
            await ProjectAccess(self.store).authorize_execution(run.data)
            check_verification_cut(self.store, run.data, intent)

    async def verification_hold(self, run_id: str, handoff_id: str) -> None:
        async with self.store._unit():
            run = await self._run(run_id)
            if "consumed_terminal_recovery" in run.data:
                return
            state = run.data.get("verification_handoff", {})
            if run.status in TERMINAL or state.get("id") != handoff_id or state.get("status") == "held":
                return
            state = {
                **state,
                "status": "held",
                "error": "Verification needs explicit reconciliation or renewed authority before continuation.",
            }
            await self._append(run_id, "implement", handoff_id + ":held", "verification_handoff_outcome", state)
            task = await self._task(run_id, "implement")
            task.status = "paused"
            task.data = {key: value for key, value in task.data.items() if key != "verification_handoff"}
            await self.tasks.save(task)
            run.status = "paused"
            run.data = {
                **run.data,
                "status": "paused",
                "display_status": "paused_worker_request",
                "verification_handoff": state,
                "updated_at": now(),
            }
            await self.store.run_records.save(run)

    async def verification_refuse(self, run_id: str, request_digest: str) -> None:
        async with self.store._unit():
            run = await self._run(run_id)
            if "consumed_terminal_recovery" in run.data:
                return
            request = run.data.get("worker_request", {})
            handoff = run.data.get("verification_handoff", {})
            completed = (
                request.get("status") == "answered"
                and request.get("response_source") == "factory_verification"
                and handoff.get("status") == "completed"
                and handoff.get("request_id") == request.get("id")
            )
            if (
                run.status not in {"paused", "needs_reconciliation"}
                or not (request.get("status") == "pending" or completed)
                or digest(request) != request_digest
                or run.data.get("verification_refusal", {}).get("request_digest") == request_digest
            ):
                return
            refusal = {
                "version": 1,
                "request_id": request["id"],
                "request_digest": request_digest,
                "status": "held",
                "reason": "admission_refused",
                "message": "Automatic verification was not admitted. Review current authority, approved commands, "
                "source and policy before an explicit handoff.",
            }
            await self._append(
                run_id, "implement", request_digest[:48] + ":refusal", "verification_handoff_refused", refusal
            )
            run.data = {**run.data, "verification_refusal": refusal, "updated_at": now()}
            await self.store.run_records.save(run)

    async def verification_continuation_allowance(self, run_id: str) -> int:
        async with self.store._unit():
            entries = await self.records.find_for_run(identifier(run_id))
            admitted = {
                item.data["payload"]["id"]: item.data["payload"]
                for item in entries
                if item.kind == "verification_handoff_intent"
            }
            completed = {
                item.data["payload"]["id"]
                for item in entries
                if item.kind == "verification_handoff_outcome" and item.data["payload"].get("status") == "completed"
            }
            responses = {
                item.data["payload"].get("responding_actor", {}).get("handoff_id")
                for item in entries
                if item.kind == "worker_response"
                and item.data["payload"].get("responding_actor", {}).get("version") == 4
                and item.data["payload"].get("responding_actor", {}).get("authentication_method")
                == "factory_verification"
            }
            resumed = {
                item.data["payload"].get("verification_handoff_id")
                for item in entries
                if item.kind in {"resume", "handoff"} and item.data["payload"].get("automatic") is True
            }
            return int(
                min(
                    3,
                    sum(
                        handoff in completed & responses & resumed
                        and type(payload.get("continuation")) is int
                        and type(payload.get("max_continuations")) is int
                        and 1 <= payload["continuation"] <= payload["max_continuations"]
                        for handoff, payload in admitted.items()
                    ),
                )
            )

    async def human_clarification_continuation_allowance(self, run_id: str) -> int:
        from firefly_forge.human_clarifications import continuation_allowance

        async with self.store._unit():
            run = await self._run(run_id)
            entries = [
                {**copy.deepcopy(row.data), "digest": row.digest} for row in await self.records.find_for_run(run.id)
            ]
            if not self.verify_chain(entries):
                raise ValueError("Human clarification requires an intact journal")
            return continuation_allowance(run.data, entries)

    async def recover(self) -> int:
        async with self.store._unit():
            pending = await self.tasks.find_pending_recovery()
            count = 0
            for candidate in pending:
                run = await self._run(candidate.run_id.hex)
                task = await self._task(run.id.hex, candidate.task_id)
                if task.status not in {"running", "needs_reconciliation"}:
                    continue
                entries = [
                    {**copy.deepcopy(row.data), "digest": row.digest} for row in await self.records.find_for_run(run.id)
                ]
                if not self.verify_chain(entries) or any(entry["run_id"] != run.id.hex for entry in entries):
                    raise ValueError("Recovery requires an intact native journal")
                key = task.data["idempotency_key"]
                if (
                    run.status == "failed"
                    and task.task_id == "implement"
                    and any(
                        entry["kind"] == "bridge_request_intent"
                        and entry["task_id"] == task.task_id
                        and isinstance(entry["payload"], dict)
                        and entry["payload"].get("task_key") == key
                        for entry in entries
                    )
                ):
                    # Saved provider custody has its own same-attempt recovery;
                    # pre-dispatch failures still need the ordinary restart witness.
                    continue
                generation = _recovery_generation(entries, task.task_id, key)
                for kind, pointer in _RECOVERY_CLAIMS.items():
                    claims = [
                        entry
                        for entry in entries
                        if entry["kind"] == kind
                        and entry["task_id"] == task.task_id
                        and isinstance(entry["payload"], dict)
                        and entry["payload"].get("task_key") == key
                    ]
                    reference = task.data.get(pointer)
                    if claims:
                        if reference != claims[-1]["digest"]:
                            raise ValueError("Recovery task claim reference changed")
                    elif reference is not None:
                        historical = [
                            entry
                            for entry in entries
                            if entry["digest"] == reference
                            and entry["kind"] == kind
                            and entry["task_id"] == task.task_id
                            and isinstance(entry["payload"], dict)
                        ]
                        prior = historical[0] if len(historical) == 1 else None
                        prior_attempt = prior["payload"].get("attempt") if prior else None
                        if (
                            prior is None
                            or generation is None
                            or type(prior_attempt) is not int
                            or not 0 < prior_attempt < task.attempt
                            or prior["sequence"] >= generation["sequence"]
                            or prior["payload"].get("task_key")
                            != uuid.uuid5(uuid.NAMESPACE_URL, f"forge:{run.id.hex}:{task.task_id}:{prior_attempt}").hex
                            or key
                            != uuid.uuid5(uuid.NAMESPACE_URL, f"forge:{run.id.hex}:{task.task_id}:{task.attempt}").hex
                        ):
                            raise ValueError("Recovery historical claim reference changed")
                history = _recovery_history(entries, task.task_id, key)
                current = [
                    entry
                    for entry in history
                    if (generation is None or entry["sequence"] > generation["sequence"])
                    and valid_recovery_annotation(entry, entries, task.task_id, key)
                ]
                if current:
                    if len(current) != 1:
                        raise ValueError("Recovery generation has duplicate annotations")
                    if task.status == "running":
                        raise ValueError("Recovered task requires a fresh native execution generation")
                    continue
                previous = history[-1] if history else None
                if previous is not None and generation is not None and previous["sequence"] >= generation["sequence"]:
                    raise ValueError("Recovery generation has an invalid prior annotation")
                if task.status == "needs_reconciliation" and (
                    generation is None or previous is None or previous["sequence"] >= generation["sequence"]
                ):
                    continue
                observation = task.status
                payload: Data = {"reason": RECOVERY_REASON}
                event_key = key
                if generation is not None and (generation["kind"] in _RECOVERY_CLAIMS or previous is not None):
                    event_key = _recovery_key(run.id.hex, task.task_id, key, generation)
                    payload = {
                        "version": 1,
                        "reason": RECOVERY_REASON,
                        "task_key": key,
                        "generation": {name: generation[name] for name in ("kind", "idempotency_key", "digest")},
                        "previous_recovery_digest": previous["digest"] if previous else None,
                        "observation": observation,
                    }
                if any(entry["kind"] == "recovery" and entry["idempotency_key"] == event_key for entry in entries):
                    raise ValueError("Recovery generation identity is already occupied")
                if observation == "running":
                    task.status = "needs_reconciliation"
                    await self.tasks.save(task)
                if observation == "running" and run.status not in TERMINAL:
                    run.status = "needs_reconciliation"
                    run.data = {**run.data, "status": run.status, "resume_existing": True, "updated_at": now()}
                    await self.store.run_records.save(run)
                await self._append(
                    run.id.hex,
                    task.task_id,
                    event_key,
                    "recovery",
                    payload,
                )
                count += 1
            return count

    async def reconcile(self, run_id: str, key: str, *, evidence: Data, outcome: str) -> Data:
        if outcome not in {"retry", "completed"} or not evidence:
            raise ValueError("Reconciliation requires an observed outcome and evidence")
        from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed

        await assert_generic_dispatch_allowed(self.store, self, await self.store.get_run(run_id))
        async with self.store._unit():
            run = await self._run(run_id)
            await assert_generic_dispatch_allowed(self.store, self, await self.store.get_run(run_id))
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
        verification_handoff_id: str | None = None,
        expected_provider_digest: str | None = None,
        source_human_readmission: str | None = None,
        source_budget_readmission: str | None = None,
    ) -> Data:
        current = await self.store.get_run(run_id)
        from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed

        await assert_generic_dispatch_allowed(self.store, self, current)
        if current.get("bridge_completion_hold"):
            raise ValueError("Bridge completion history needs a graph checkpoint before resuming or handoff")
        if current.get("pause", {}).get("kind") in {"provider_outcome_unknown", "bridge_admission_refused"}:
            raise ValueError("Bridge outcome requires reconciliation before resuming")
        from firefly_forge.checkpoint_mode_runtime import mode_execution_scope

        async def selected() -> Data:
            verified_resume = None
            if current.get("status") == "failed":
                if (
                    auto
                    or agent is not None
                    or model_profile_id is not None
                    or campaign_action_id is not None
                    or verification_handoff_id is not None
                    or expected_provider_digest is not None
                    or source_human_readmission is not None
                    or source_budget_readmission is not None
                ):
                    raise ValueError("Failed verification admission requires an explicit unchanged-provider resume")
                from firefly_forge.retained_verified_source import prepare_verified_resume

                verified_resume = await prepare_verified_resume(self.store, self, current)
            return await self._resume_selected(
                current,
                run_id,
                agent,
                model_profile_id=model_profile_id,
                auto=auto,
                campaign_action_id=campaign_action_id,
                verification_handoff_id=verification_handoff_id,
                expected_provider_digest=expected_provider_digest,
                source_human_readmission=source_human_readmission,
                source_budget_readmission=source_budget_readmission,
                verified_resume=verified_resume,
            )

        if current.get("checkpoint_mode_adoption"):
            async with mode_execution_scope(self, run_id):
                return await selected()
        return await selected()

    async def _resume_selected(
        self,
        current: Data,
        run_id: str,
        agent: str | None = None,
        *,
        model_profile_id: str | None = None,
        auto: bool = False,
        campaign_action_id: str | None = None,
        verification_handoff_id: str | None = None,
        expected_provider_digest: str | None = None,
        source_human_readmission: str | None = None,
        source_budget_readmission: str | None = None,
        verified_resume: PreparedVerifiedResume | None = None,
    ) -> Data:
        from firefly_forge.project_access import ProjectAccess

        if auto and (agent is not None or model_profile_id is not None):
            raise ValueError("An automatic retry cannot change the selected provider or model profile")
        inherited_mode = current.get("checkpoint_mode_adoption")
        if inherited_mode:
            from firefly_forge.checkpoint_mode_runtime import execution_settings, prepared_mode

            prepared = prepared_mode(current)
            assert prepared is not None
            if agent not in {None, "bridge"} or model_profile_id is not None:
                raise ValueError("Checkpoint mode adoption keeps its original Bridge provider and model profile")
            if (
                expected_provider_digest is not None
                and digest({"agent": "bridge", "settings": execution_settings(self.store.configuration, current)})
                != expected_provider_digest
            ):
                raise ValueError("Explicit verification handoff provider snapshot changed")
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
        frozen_verification = (
            verification_handoff_id is not None and agent in {None, current.get("agent")} and model_profile_id is None
        )
        configuration = None
        selected_settings = None
        if (agent is not None or frozen_verification) and not inherited_mode:
            from firefly_forge.bridge_completions import _outside_unit
            from firefly_forge.verification_models import validate_coding_readiness

            _outside_unit()
            selected_agent = agent or current["agent"]
            selected_settings = (
                self.store.configuration.load_snapshot(current["config_revision"])
                if frozen_verification
                else self.store.configuration.load_snapshot(selection["config_revision"])
                if selection is not None
                else self.store.configuration.resolve(selected_agent)
            )
            validate_coding_readiness(selected_agent, selected_settings)
            if (
                expected_provider_digest is not None
                and digest({"agent": selected_agent, "settings": selected_settings}) != expected_provider_digest
            ):
                raise ValueError("Explicit verification handoff provider snapshot changed")
            if not frozen_verification:
                configuration = (
                    {"config_revision": selection["config_revision"], "execution_config": selection["execution_config"]}
                    if selection is not None
                    else self.store.configuration.snapshot(selected_agent)
                )
        settings_generation = (
            self.store.configuration.settings_generation
            if agent is not None or frozen_verification or inherited_mode
            else None
        )
        ProjectAccess.require_provider_isolation(current, agent or current.get("agent"))
        await ProjectAccess(self.store).authorize_execution(current)
        if current.get("bridge_completion_hold"):
            raise ValueError("Bridge completion history needs a graph checkpoint before resuming or handoff")
        if current.get("pause", {}).get("kind") in {"provider_outcome_unknown", "bridge_admission_refused"}:
            raise ValueError("Bridge outcome requires reconciliation before resuming")
        from firefly_forge.human_clarifications import awaiting_human_resume, continuation_link
        from firefly_forge.identity_persistence import ForgeIdentityUserRepository
        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

        if current.get("source_edit_continuation") and awaiting_human_resume(current):
            if (
                (source_human_readmission is None and source_budget_readmission is None)
                or (source_human_readmission is not None and source_budget_readmission is not None)
                or auto
                or agent is not None
                or model_profile_id is not None
                or verification_handoff_id is not None
            ):
                raise ValueError("A fresh native source-only human admission is required for the next attempt")
        elif source_human_readmission is not None or source_budget_readmission is not None:
            raise ValueError("A source-only human admission requires its answered current question")
        human_candidate = not auto and verification_handoff_id is None and awaiting_human_resume(current)
        actor = CURRENT_PRINCIPAL.get() if human_candidate else None
        if human_candidate and actor is None:
            raise AccessDenied("An authenticated operator must explicitly resume a human clarification")
        async with self.store._unit() as resume_session:
            if human_candidate:
                assert actor is not None
                if actor.authentication_method == "scm_mapping" and (actor.scm_delegation or {}).get("action") not in {
                    "resume",
                    "handoff",
                }:
                    raise AccessDenied("This SCM delegation does not authorize explicit clarification resume")
                if current.get("project_id"):
                    from sqlalchemy import text

                    await resume_session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": self.store.schema + ":members:" + current["project_id"]},
                    )
                identity = await ForgeIdentityUserRepository().find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
                if (
                    identity is None
                    or not identity.enabled
                    or (identity.issuer, identity.subject) != (actor.issuer, actor.subject)
                ):
                    raise AccessDenied("The clarification resume identity is unavailable or inconsistent")
            await self.store.assert_campaign_current(current, action_id=campaign_action_id)
            run = await self._run(run_id)
            from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed

            await assert_generic_dispatch_allowed(self.store, self, await self.store.get_run(run_id))
            if (
                settings_generation is not None
                and self.store.configuration.settings_generation is not settings_generation
            ):
                raise ValueError("The selected provider changed before resume admission")
            if inherited_mode:
                from firefly_forge.checkpoint_mode_runtime import assert_mode_actors

                if run.data.get("checkpoint_mode_adoption") != inherited_mode:
                    raise ValueError("Checkpoint mode adoption changed before resume")
                mode_entries = [
                    {**copy.deepcopy(row.data), "digest": row.digest} for row in await self.records.find_for_run(run.id)
                ]
                await assert_mode_actors(self, run.data, mode_entries)
            if verified_resume is not None:
                if run.status != "failed":
                    raise ValueError("Failed verification admission was already resumed or changed")
                await verified_resume.admit()
            elif run.status not in {"paused", "needs_reconciliation", "interrupted"}:
                raise ValueError("Only a paused or interrupted run can resume")
            if run.data.get("worker_request", {}).get("status") == "pending":
                raise ValueError("Answer the pending worker question before explicitly resuming this run")
            pause = run.data.get("pause", {})
            if run.data.get("bridge_completion_hold"):
                raise ValueError("Bridge completion history needs a graph checkpoint before resuming or handoff")
            if pause.get("kind") in {"provider_outcome_unknown", "bridge_admission_refused"}:
                raise ValueError("Bridge outcome requires reconciliation before resuming")
            if auto and (not pause.get("automatic") or (pause.get("retry_at") or float("inf")) > time.time()):
                raise ValueError("This run requires an explicit operator resume")
            if run.data.get("publication_status") == "publishing":
                raise ValueError("Publication outcome is ambiguous; reconcile the remote branch and pull request first")
            ProjectAccess.require_provider_isolation(run.data, agent or run.data.get("agent"))
            await ProjectAccess(self.store).authorize_execution(run.data)
            if verification_handoff_id is not None:
                from firefly_forge.verification_models import check_verification_cut

                state = run.data.get("verification_handoff", {})
                entries = await self.records.find_for_run(run.id)
                admitted = next(
                    (
                        item.data["payload"]
                        for item in entries
                        if item.kind == "verification_handoff_intent"
                        and item.idempotency_key == verification_handoff_id
                    ),
                    None,
                )
                if (
                    admitted is None
                    or state.get("id") != verification_handoff_id
                    or state.get("status") != "completed"
                    or run.data.get("worker_response", {}).get("responding_actor", {}).get("handoff_id")
                    != verification_handoff_id
                    or agent != admitted.get("agent")
                    or expected_provider_digest != admitted["provider_digest"]
                ):
                    raise ValueError("Automatic continuation requires its completed immutable verification handoff")
                check_verification_cut(self.store, run.data, admitted)
            human_clarification = None
            source_budget_link = None
            if not auto and verification_handoff_id is None and awaiting_human_resume(run.data):
                if not human_candidate or actor is None or self._worker_scope(current) != self._worker_scope(run.data):
                    raise AccessDenied("The human clarification scope changed during resume")
                access = ProjectAccess(self.store)
                snapshot = access.actor_snapshot(actor)
                await access.authorize_actor(snapshot, run.data.get("project_id"), "execute")
                if run.data.get("publish"):
                    await access.authorize_actor(snapshot, run.data.get("project_id"), "release")
                human_entries = [
                    {**copy.deepcopy(row.data), "digest": row.digest} for row in await self.records.find_for_run(run.id)
                ]
                if not self.verify_chain(human_entries):
                    raise ValueError("Human clarification requires an intact journal")
                task = await self._task(run_id, "implement")
                if source_budget_readmission is not None:
                    from firefly_forge.source_budget_allowances import assert_budget_resume

                    source_budget_link = assert_budget_resume(
                        run.data,
                        {
                            **copy.deepcopy(task.data),
                            "id": task.task_id,
                            "status": task.status,
                            "attempt": task.attempt,
                        },
                        human_entries,
                        snapshot,
                        source_budget_readmission,
                        self.store.configuration,
                    )
                else:
                    human_clarification = continuation_link(
                        run.data,
                        {"status": task.status, "attempt": task.attempt},
                        human_entries,
                        {"user_id": actor.id.hex, "authentication_method": actor.authentication_method},
                    )
                    if run.data.get("source_edit_continuation"):
                        from firefly_forge.source_human_readmission import assert_resume_admission

                        assert_resume_admission(
                            run.data,
                            {
                                **copy.deepcopy(task.data),
                                "id": task.task_id,
                                "status": task.status,
                                "attempt": task.attempt,
                            },
                            human_entries,
                            snapshot,
                            human_clarification,
                            source_human_readmission,
                        )
                if CURRENT_PRINCIPAL.get() != actor:
                    raise AccessDenied("The clarification resume operator changed")
            old_revision = run.data.get("config_revision")
            old_selection = copy.deepcopy(run.data.get("model_selection"))
            old_agent = run.data.get("agent")
            selection_mode = (
                "frozen"
                if inherited_mode or frozen_verification
                else "named_profile"
                if selection is not None
                else "legacy_provider"
                if handoff
                else "frozen"
            )
            if configuration is not None:
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
                    if source_budget_link is not None and task.task_id == "implement":
                        from uuid import NAMESPACE_URL, uuid5

                        task.data = {
                            **task.data,
                            "idempotency_key": uuid5(NAMESPACE_URL, f"forge:{run_id}:implement:{task.attempt}").hex,
                        }
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
                **(
                    {"retry_count": int(run.data.get("retry_count", 0)) + int(auto)}
                    if auto or "retry_count" in run.data
                    else {}
                ),
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
                    **({"checkpoint_mode_adoption_digest": inherited_mode["record_digest"]} if inherited_mode else {}),
                    "automatic": auto or verification_handoff_id is not None,
                    **({"verification_handoff_id": verification_handoff_id} if verification_handoff_id else {}),
                    **({"verified_pre_dispatch_resume": verified_resume.proof} if verified_resume else {}),
                    **({"human_clarification": human_clarification} if human_clarification else {}),
                    **({"source_budget_continuation": source_budget_link} if source_budget_link else {}),
                },
            )
            if verified_resume is not None:
                await verified_resume.source_current()
            result = await self.store.get_run(run_id)
            if verification_handoff_id is not None:
                assert admitted is not None
                await self.store.assert_ownership()
                await ProjectAccess(self.store).authorize_execution(run.data)
                check_verification_cut(self.store, run.data, admitted)
            return result

    async def queue_environment_reconciled(
        self, run_id: str, request_id: str, *, scm_source_admission: SealedSourceEditAdmission | None = None
    ) -> Data:
        """Queue the existing task only after its explicit environment effect has a sealed receipt."""
        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
        from firefly_forge.workspace_reconciliation_models import WorkspaceReconciliationInput

        identifier(request_id)
        actor = CURRENT_PRINCIPAL.get()
        access = ProjectAccess(self.store)
        if scm_source_admission is None:
            if (
                actor is None
                or not await access.current_administrator(actor)
                or actor.authentication_method == "scm_mapping"
            ):
                raise AccessDenied("Environment reconciliation requires a current native administrator")
            snapshot = {
                **access.actor_snapshot(actor),
                "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
            }
        else:
            from firefly_forge.scm_source_edit_models import SealedSourceEditAdmission
            from firefly_forge.source_edit_continuations import _assert_source_administrator

            if (
                not isinstance(scm_source_admission, SealedSourceEditAdmission)
                or actor != scm_source_admission.caller
                or actor is None
                or actor.authentication_method != "scm_mapping"
                or scm_source_admission.run_id != run_id
                or scm_source_admission.request.request_id != request_id
            ):
                raise AccessDenied("The exact sealed SCM source trigger is required")
            await scm_source_admission.verify()
            snapshot = copy.deepcopy(scm_source_admission.native_actor)
            await _assert_source_administrator(self.store, snapshot)
        await self.store.assert_ownership()
        async with self.store._unit():
            run = await self._run(run_id)
            await access.authorize_execution(run.data)
            entries = [copy.deepcopy(row.data) for row in await self.records.find_for_run(run.id)]
            if not self.verify_chain(entries):
                raise ValueError("Environment reconciliation requires an intact native journal")
            receipt = next(
                (
                    row
                    for row in entries
                    if row["kind"] == "workspace_reconciliation_receipt" and row["idempotency_key"] == request_id
                ),
                None,
            )
            if receipt is None:
                raise ValueError("Environment reconciliation has no durable completion receipt")
            value = receipt["payload"]
            source_edit = candidate_source = value["candidate"].get("source_edit")
            body: WorkspaceReconciliationInput
            if source_edit is not None:
                from firefly_forge.source_edit_models import PROTOCOL, SourceEditContinuationInput

                if candidate_source.get("protocol") != PROTOCOL:
                    raise ValueError("The source-edit capability version is unavailable")
                body = SourceEditContinuationInput.model_validate(value["request"])
            else:
                body = WorkspaceReconciliationInput.model_validate(value["request"])
            if scm_source_admission is not None and (
                source_edit is None
                or value.get("scm_source_activation") != scm_source_admission.evidence
                or value["request"] != scm_source_admission.request.model_dump()
            ):
                raise AccessDenied("The native receipt does not bind the exact SCM source trigger")
            candidate = value["candidate"]
            intent = next(
                (
                    row
                    for row in entries
                    if row["kind"] == "workspace_reconciliation_intent" and row["idempotency_key"] == request_id
                ),
                None,
            )
            if (
                body.request_id != request_id
                or value["actor"] != snapshot
                or candidate["digest"] != digest({k: v for k, v in candidate.items() if k != "digest"})
                or body.candidate_digest != candidate["digest"]
                or intent is None
                or value.get("intent_digest") != intent["digest"]
                or any(
                    value[key] != intent["payload"][key] for key in ("request", "actor", "candidate", "previous_task")
                )
            ):
                raise AccessDenied("The completed environment transition is not this operator's native intent")
            queued = next(
                (
                    row
                    for row in entries
                    if row["kind"] == "workspace_reconciliation_queued" and row["idempotency_key"] == request_id
                ),
                None,
            )
            if queued is not None:
                return await self.store.get_run(run_id)
            task = await self._task(run_id, "implement")
            task_view = {**copy.deepcopy(task.data), "id": task.task_id, "status": task.status, "attempt": task.attempt}
            if (
                run.status != "failed"
                or digest(run.data) != candidate["previous_run_digest"]
                or task_view != value["previous_task"]
                or task.status != "queued"
                or type(task.attempt) is not int
                or not 2 <= task.attempt <= 4
                or run.data.get("git_metadata_digest") != value["effect"]["metadata_before"]
            ):
                raise ValueError("The original failed run or already queued implementation task changed")
            await self._append(
                run_id,
                "implement",
                request_id,
                "workspace_reconciliation_queued",
                {
                    "version": 1,
                    "receipt_digest": receipt["digest"],
                    "existing_attempt": task.attempt,
                },
            )
            run.status = "queued"
            run.data = {
                **run.data,
                "status": "queued",
                "display_status": "queued",
                "error": None,
                "resume_existing": True,
                "updated_at": now(),
                "git_metadata_digest": value["effect"]["metadata_after"],
                "workspace_reconciliation": {"request_id": request_id, "receipt_digest": receipt["digest"]},
                **(
                    {"source_edit_continuation": {"request_id": request_id, "receipt_digest": receipt["digest"]}}
                    if source_edit
                    else {}
                ),
            }
            await self.store.run_records.save(run)
            await self.store.assert_ownership()
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The environment reconciliation operator changed before commit")
            if scm_source_admission is None:
                if not await access.current_administrator(actor):
                    raise AccessDenied("The environment reconciliation operator changed before commit")
            else:
                await scm_source_admission.verify()
                await _assert_source_administrator(self.store, snapshot)
                if CURRENT_PRINCIPAL.get() != actor:
                    raise AccessDenied("The SCM source trigger caller changed before commit")
            return await self.store.get_run(run_id)

    async def ready_retries(self) -> list[str]:
        async with self.store._unit():
            return [row.id.hex for row in await self.store.run_records.find_ready_retries(time.time())]

    async def sandbox_command_intent(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
        snapshot: Data,
        payload: Callable[[int, str, Data | None], Data],
        *,
        ordinal: int | None = None,
    ) -> Data:
        from firefly_forge.sandbox_command_slots import command_intent

        return await command_intent(self, run_id, task_id, attempt, snapshot, payload, ordinal=ordinal)

    async def sandbox_command_replay(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
        snapshot: Data,
        payload: Callable[[int, str, Data | None], Data],
        *,
        ordinal: int,
    ) -> Data:
        from firefly_forge.sandbox_command_slots import command_intent

        return await command_intent(
            self, run_id, task_id, attempt, snapshot, payload, ordinal=ordinal, replay_only=True
        )

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
            await self.verification_effect_authority(run_id, task_id, payload)
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
