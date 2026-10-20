# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One explicitly admitted replacement dispatch from a retained failed CLI workspace."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text

from firefly_forge.agentic_checkpoint_invocation import invocation_cut
from firefly_forge.git_workspace import metadata_digest
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal, digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.provider_executable_adoption import executable_identity
from firefly_forge.provider_recovery_models import (
    ProviderAttemptClosed,
    ProviderProcessOutcome,
    ProviderRecoveryAdmission,
    dispatch_keys,
    failed_implementation,
)
from firefly_forge.store import Store, identifier, now
from firefly_forge.tool_broker import tool_policy_snapshot
from firefly_forge.verification_models import verification_scope, verification_source

Data = dict[str, Any]
RESULT_CUT_FIELDS = (
    "scope_digest",
    "metadata_digest",
    "git_digest",
    "invocation_digest",
    "commands_digest",
    "policy_digest",
    "provider_digest",
    "retained_state_digest",
)


@service
class ProviderRecoveries:
    def __init__(self, store: Store, journal: Journal, engine: Any) -> None:
        self.store, self.journal, self.engine = store, journal, engine

    async def _operator(self, run_id: str) -> Any:
        actor = CURRENT_PRINCIPAL.get()
        access = ProjectAccess(self.store)
        if (
            actor is None
            or actor.authentication_method == "scm_mapping"
            or not await access.current_administrator(actor)
        ):
            raise AccessDenied("Failed-provider recovery requires a current native factory administrator")
        await access.authorize_execution(await self.store.get_run(run_id))
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("The recovery operator changed")
        return actor

    @asynccontextmanager
    async def _locked(self, run_id: str, actor: Data) -> Any:
        initial = await self.store.get_run(run_id)
        async with self.store._unit() as session:
            if initial.get("project_id"):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": self.store.schema + ":members:" + initial["project_id"]},
                )
            users = {actor["user_id"]}
            if initial.get("execution_actor"):
                users.add(initial["execution_actor"]["user_id"])
            for user_id in sorted(users):
                user = await ForgeIdentityUserRepository().find_by_id(
                    identifier(user_id), lock=LockMode.PESSIMISTIC_WRITE
                )
                if user is None or not user.enabled:
                    raise AccessDenied("The recovery identity is unavailable or suspended")
                if (
                    user_id == actor["user_id"]
                    and digest({"issuer": user.issuer, "subject": user.subject}) != actor["identity_digest"]
                ):
                    raise AccessDenied("The recovery identity changed")
            administrator = await ProjectAccess(self.store).authorize_actor(actor, None, "members")
            if not administrator or actor.get("authentication_method") == "scm_mapping":
                raise AccessDenied("Current global administrator authority is required")
            row = await self.journal._run(run_id)
            if verification_scope(row.data) != verification_scope(initial):
                raise ValueError("Recovery run scope changed during admission")
            await ProjectAccess(self.store).authorize_execution(row.data)
            await self.store.assert_campaign_current(row.data)
            yield row

    async def _entries(self, run_id: str) -> list[Data]:
        rows = await self.journal.records.find_for_run(identifier(run_id))
        entries = [{**copy.deepcopy(row.data), "digest": row.digest} for row in rows]
        if len(entries) > 10000 or len(json.dumps(entries)) > 16 * 1024 * 1024 or not Journal.verify_chain(entries):
            raise ValueError("Recovery requires an intact bounded native journal")
        return entries

    @staticmethod
    def _task_view(task: Any) -> Data:
        return {**copy.deepcopy(task.data), "id": task.task_id, "status": task.status, "attempt": task.attempt}

    def _policy_digest(self) -> str:
        return str(tool_policy_snapshot(self.store.configuration)["digest"])

    def _provider_digest(self, run: Data) -> str:
        value = {
            "agent": run["agent"],
            "retained": self.store.configuration.load_snapshot(run["config_revision"]),
            "current": self.store.configuration.resolve(run["agent"]),
        }
        state = run.get("provider_recovery") or {}
        adoption = state.get("executable_adoption")
        if adoption and state.get("status") in {"reserved", "consumed"}:
            observed = self._current_executable(run)
            if observed != adoption:
                raise ValueError("The adopted CLI executable changed")
            value["executable_adoption"] = observed
        return digest(value)

    def _current_executable(self, run: Data) -> Data:
        key = "FORGE_" + run["agent"].upper() + "_EXECUTABLE"
        return executable_identity(self.store.configuration.resolve(run["agent"]).get(key, ""), self.engine.workspaces)

    def execution_settings(self, run: Data, settings: dict[str, str], task_key: str) -> dict[str, str]:
        state = run.get("provider_recovery") or {}
        adoption = state.get("executable_adoption")
        if not adoption or state.get("task_key") != task_key or state.get("status") not in {"reserved", "consumed"}:
            return dict(settings)
        if self._current_executable(run) != adoption:
            raise ValueError("The adopted CLI executable changed before selection")
        return {**settings, "FORGE_" + run["agent"].upper() + "_EXECUTABLE": adoption["path"]}

    def _cut(self, run: Data) -> Data:
        workspace = self.engine.workspaces / run["id"]
        metadata = metadata_digest(workspace)
        if metadata != run.get("git_metadata_digest"):
            raise ValueError("Retained Git metadata changed")
        git = workspace / ".git"
        files = [git / name for name in ("HEAD", "index", "packed-refs") if (git / name).exists()]
        if (git / "refs").exists():
            files.extend(path for path in (git / "refs").rglob("*") if path.is_file())
        identities, size = {}, 0
        index_digest = None
        for path in sorted(files):
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                value = stream.read(1_000_001)
            size += len(value)
            if size > 1_000_000:
                raise ValueError("Retained Git identity exceeds the recovery bound")
            if path.name == "index" and path.parent == git:
                index_digest = hashlib.sha256(value).hexdigest()
            else:
                identities[path.relative_to(git).as_posix()] = hashlib.sha256(value).hexdigest()
        return {
            "source_digest": verification_source(workspace),
            "git_digest": digest(identities),
            "index_digest": index_digest,
            "metadata_digest": metadata,
            "scope_digest": verification_scope(run),
            "invocation_digest": invocation_cut(run),
            "commands_digest": digest(run.get("commands", [])),
            "policy_digest": self._policy_digest(),
            "provider_digest": self._provider_digest(run),
            "retained_state_digest": digest(
                {
                    key: run.get(key)
                    for key in (
                        "base_revision",
                        "checkpoint_revision",
                        "repair_budget",
                        "implementation_rounds",
                        "tool_policy_snapshot",
                        "verification_handoff",
                        "verification_adoption",
                        "worker_response",
                    )
                }
            ),
        }

    async def _settled(self, run: Data, *, idle: bool) -> None:
        active = self.engine.active.get(run["id"])
        if idle and active is not None and not active.done():
            raise ValueError("The original run still has active execution")
        if (run.get("cancel_request") or {}).get("status") == "requested" or run.get("bridge_completion_hold"):
            raise ValueError("Cancellation or an unknown external effect holds recovery")
        broker = self.engine.tool_broker
        if broker is not None and await broker.unresolved(run["id"]):
            raise ValueError("Unknown native tools require reconciliation")
        for execution in await self.journal.sandbox_executions(run["id"]):
            effects = execution.get("effects", {})
            if not effects or any(
                effect.get("status") != "done"
                or effect.get("evidence", {}).get("outcome") == "interrupted"
                or effect.get("evidence", {}).get("workspace_import") == "requires_reconciliation"
                for effect in effects.values()
            ):
                raise ValueError("Unknown sandbox effects require reconciliation")
        requests = getattr(self.store, "execution_requests", None)
        if requests is not None and any(
            row["status"] in {"queued", "running", "needs_reconciliation"} for row in await requests.for_run(run["id"])
        ):
            raise ValueError("An external delivery operation remains unsettled")
        entries = await self._entries(run["id"])
        receipts = {e["idempotency_key"] for e in entries if e["kind"] == "receipt"}
        if any(
            e["kind"] == "intent"
            and e["task_id"].startswith(("publish", "deploy", "rollback", "memory"))
            and e["idempotency_key"] not in receipts
            for e in entries
        ):
            raise ValueError("An unknown remote effect requires reconciliation")

    async def _candidate(self, run: Data, agent: str, *, adopt_current_executable: bool = False) -> Data:
        operator = CURRENT_PRINCIPAL.get()
        if operator is None:
            raise AccessDenied("A current native recovery operator is required")
        operator_digest = digest(
            {
                **ProjectAccess.actor_snapshot(operator),
                "identity_digest": digest({"issuer": operator.issuer, "subject": operator.subject}),
            }
        )
        task = self._task_view(await self.journal._task(run["id"], "implement"))
        entries = await self._entries(run["id"])
        intent = failed_implementation(run, task, entries)
        if agent != run["agent"]:
            raise ValueError("Recovery must explicitly retain the original official provider")
        used = len(dispatch_keys(entries))
        attempt = max(used, task["attempt"])
        if type(attempt) is not int or not 1 <= attempt < 4 or used >= 4:
            raise ValueError("The four-dispatch provider recovery budget is exhausted")
        if any(
            e["kind"] == "provider_recovery_admission"
            and e["payload"]["candidate"]["failed_task_key"] == task["idempotency_key"]
            for e in entries
        ):
            raise ValueError("The original failed provider intent already has a recovery")
        observed = next(
            (
                e
                for e in reversed(entries)
                if e["kind"] == "provider_failure_observation" and e["payload"]["task_key"] == task["idempotency_key"]
            ),
            None,
        )
        if not observed and (
            task.get("provider_recovery_id")
            or any(
                e["kind"] == "provider_dispatch_intent" and e["payload"].get("task_key") == task["idempotency_key"]
                for e in entries
            )
        ):
            raise ValueError("The native provider dispatch has an unknown outcome and needs reconciliation")
        if observed:
            raw = observed["payload"].get("process")
            if raw is None or not ProviderProcessOutcome.model_validate(raw).known_failure:
                raise ValueError("The native provider outcome is unknown and needs reconciliation")
        await self._settled(run, idle=True)
        if CURRENT_PRINCIPAL.get() != operator:
            raise AccessDenied("The reviewing recovery operator changed")
        adoption = self._current_executable(run) if adopt_current_executable else None
        prospective = {"status": "reserved", "executable_adoption": adoption} if adoption else {}
        payload = {
            "version": 1,
            "operator_digest": operator_digest,
            "run_id": run["id"],
            "agent": agent,
            "config_revision": run["config_revision"],
            "failed_task_key": task["idempotency_key"],
            "failed_intent_digest": intent["digest"],
            "failure_digest": digest(
                {
                    "run_error": run.get("error"),
                    "task": task,
                    "run_attempt": run.get("attempt"),
                    "intent": intent["digest"],
                }
            ),
            "journal_digest": entries[-1]["digest"],
            "dispatches_used": used,
            "next_attempt": attempt + 1,
            "process_evidence": "native_cli_process_receipt" if observed else "legacy_operator_attestation_required",
            "native_process_digest": observed["digest"] if observed else None,
            "cut": self._cut({**run, "provider_recovery": prospective}),
        }
        if adoption:
            payload["executable_adoption"] = adoption
        return {**payload, "digest": digest(payload)}

    async def candidate(self, run_id: str, agent: str, *, adopt_current_executable: bool = False) -> Data:
        identifier(run_id)
        if type(adopt_current_executable) is not bool:
            raise ValueError("CLI adoption requires an explicit boolean choice")
        await self._operator(run_id)
        async with self.store._unit():
            value = await self._candidate(
                await self.store.get_run(run_id), agent, adopt_current_executable=adopt_current_executable
            )
        await self._operator(run_id)
        self.store.reject_credentials(value)
        return value

    async def get(self, run_id: str, request_id: str) -> Data:
        identifier(request_id)
        await self._operator(run_id)
        async with self.store._unit():
            entries = await self._entries(run_id)
        found = next(
            (
                e["payload"]
                for e in entries
                if e["kind"] == "provider_recovery_admission" and e["payload"]["request"]["request_id"] == request_id
            ),
            None,
        )
        if found is None:
            raise KeyError("The original provider recovery receipt was not found")
        await self._operator(run_id)
        self.store.reject_credentials(found)
        return copy.deepcopy(found)

    async def admit(self, run_id: str, body: ProviderRecoveryAdmission) -> Data:
        actor = await self._operator(run_id)
        snapshot = {
            **ProjectAccess.actor_snapshot(actor),
            "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
        }
        self.store.reject_credentials(body.model_dump())
        async with self._locked(run_id, snapshot) as row:
            entries = await self._entries(run_id)
            prior = next(
                (
                    e["payload"]
                    for e in entries
                    if e["kind"] == "provider_recovery_admission"
                    and e["payload"]["request"]["request_id"] == body.request_id
                ),
                None,
            )
            if prior is not None:
                if prior["request"] != body.request_payload() or prior["actor"] != snapshot:
                    raise ValueError("The recovery identity already binds different content or actor")
                return copy.deepcopy(prior)
            candidate = await self._candidate(
                row.data, body.agent, adopt_current_executable=body.adopt_current_executable
            )
            if candidate["digest"] != body.candidate_digest:
                raise ValueError("The reviewed recovery candidate changed")
            if candidate["native_process_digest"]:
                if (
                    body.native_process_digest != candidate["native_process_digest"]
                    or body.legacy_attestation is not None
                ):
                    raise ValueError("The recovery must use its actual native process receipt")
                evidence = {"provenance": "native_cli_process_receipt", "digest": body.native_process_digest}
            else:
                if body.legacy_attestation is None:
                    raise ValueError("Legacy process absence requires explicit trusted operator attestation")
                evidence = body.legacy_attestation.model_dump()
            task = await self.journal._task(run_id, "implement")
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The recovery operator changed before admission")
            prospective = {"status": "reserved", "executable_adoption": candidate.get("executable_adoption")}
            if self._cut({**row.data, "provider_recovery": prospective}) != candidate["cut"]:
                raise ValueError("The recovery source or configuration cut changed")
            receipt = {
                "version": 1,
                "request": body.request_payload(),
                "candidate": candidate,
                "actor": snapshot,
                "process_evidence": evidence,
                "previous_task": self._task_view(task),
            }
            self.store.reject_credentials(receipt)
            await self.journal._append(run_id, "implement", body.request_id, "provider_recovery_admission", receipt)
            key = digest({"protocol": "provider-recovery-v1", "run": run_id, "request": body.request_id})[:32]
            await self.journal._append(
                run_id,
                "implement",
                candidate["failed_task_key"],
                "provider_failure_closed",
                {
                    "provenance": evidence["provenance"],
                    "admission_id": body.request_id,
                    "failed_intent_digest": candidate["failed_intent_digest"],
                },
            )
            await self.journal._append(
                run_id,
                "implement",
                key,
                "intent",
                {"reason": "explicit_failed_provider_recovery", "admission_id": body.request_id},
            )
            task.status, task.attempt = "queued", candidate["next_attempt"]
            task.data = {"idempotency_key": key, "provider_recovery_id": body.request_id}
            await self.journal.tasks.save(task)
            row.status = "queued"
            row.data = {
                **row.data,
                "status": "queued",
                "resume_existing": True,
                "updated_at": now(),
                "provider_recovery": {
                    "request_id": body.request_id,
                    "task_key": key,
                    "status": "reserved",
                    "attempt": candidate["next_attempt"],
                    "cut": candidate["cut"],
                    "actor": snapshot,
                    **(
                        {"executable_adoption": candidate["executable_adoption"]}
                        if body.adopt_current_executable
                        else {}
                    ),
                },
            }
            await self.store.run_records.save(row)
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The recovery operator changed before commit")
            if self._cut(row.data) != candidate["cut"]:
                raise ValueError("The recovery cut changed before commit")
            return copy.deepcopy(receipt)

    async def begin(self, run_id: str) -> Data | None:
        run = await self.store.get_run(run_id)
        state = run.get("provider_recovery") or {}
        if not state or state.get("status") == "completed":
            return None
        if state.get("status") != "reserved":
            raise ValueError("The consumed provider dispatch requires reconciliation")
        return {"idempotency_key": state["task_key"], "disposition": "execute", "attempt": state["attempt"]}

    async def prepare_workspace(self, run_id: str, inspect: Any) -> Data:
        initial = await self.store.get_run(run_id)
        state = initial.get("provider_recovery") or {}
        if state.get("status") != "reserved" or self._cut(initial) != state["cut"]:
            raise ValueError("The reserved preparation cut changed")
        observed: Data = await inspect()
        async with self._locked(run_id, state["actor"]) as row:
            current = row.data.get("provider_recovery") or {}
            if current != state or current.get("workspace_prepared"):
                raise ValueError("The original reserved preparation is unavailable")
            after = self._cut(row.data)
            if {k: v for k, v in after.items() if k != "index_digest"} != {
                k: v for k, v in state["cut"].items() if k != "index_digest"
            }:
                raise ValueError("Source or Git history changed during native preparation")
            await self.journal._append(
                run_id,
                "implement",
                state["task_key"],
                "provider_workspace_prepared",
                {
                    "version": 1,
                    "before_index": state["cut"]["index_digest"],
                    "after_index": after["index_digest"],
                    "source_tree": observed["source_tree"],
                    "source_digest": after["source_digest"],
                },
            )
            row.data = {**row.data, "provider_recovery": {**state, "workspace_prepared": True, "cut": after}}
            await self.store.run_records.save(row)
            if self._cut(row.data) != after:
                raise ValueError("The native preparation cut changed before commit")
        return observed

    async def dispatch(self, run_id: str, key: str) -> None:
        initial = await self.store.get_run(run_id)
        state = initial.get("provider_recovery") or {}
        async with self._locked(run_id, state["actor"]) as row:
            current = row.data.get("provider_recovery") or {}
            if current.get("status") != "reserved" or current.get("task_key") != key:
                raise ValueError("The provider dispatch reservation was consumed or changed")
            task = await self.journal._task(run_id, "implement")
            if task.data.get("idempotency_key") != key or task.attempt != current["attempt"]:
                raise ValueError("The provider dispatch task changed")
            await self._settled(row.data, idle=False)
            if self._cut(row.data) != current["cut"]:
                raise ValueError("The reserved source/configuration cut changed")
            await self.journal._append(
                run_id,
                "implement",
                key + ":provider",
                "provider_dispatch_intent",
                {
                    "version": 1,
                    "task_key": key,
                    "recovery_id": current["request_id"],
                    "attempt": current["attempt"],
                    **(
                        {"executable_adoption": current["executable_adoption"]}
                        if current.get("executable_adoption")
                        else {}
                    ),
                },
            )
            task.status = "running"
            await self.journal.tasks.save(task)
            row.data = {**row.data, "provider_recovery": {**current, "status": "consumed"}}
            await self.store.run_records.save(row)
            if self._cut(row.data) != current["cut"]:
                raise ValueError("The source cut changed before dispatch admission commit")

    async def assert_dispatch(self, run_id: str, key: str) -> None:
        run = await self.store.get_run(run_id)
        state = run.get("provider_recovery") or {}
        async with self._locked(run_id, state["actor"]) as row:
            current = row.data.get("provider_recovery") or {}
            if current.get("status") != "consumed" or current.get("task_key") != key:
                raise ValueError("The original provider dispatch is unavailable")
            if row.status in {"failed", "cancelled", "succeeded", "needs_reconciliation"}:
                raise ValueError("The provider dispatch is no longer active")
            await self._settled(row.data, idle=False)
            if self._cut(row.data) != current["cut"]:
                raise ValueError("The source/provider/policy cut changed before process launch")
        if self._cut(row.data) != current["cut"]:
            raise ValueError("The local launch cut changed during native commit")

    @asynccontextmanager
    async def _normal_lock(self, run_id: str) -> Any:
        initial = await self.store.get_run(run_id)
        actor = (initial.get("provider_dispatch") or {}).get("actor") or initial.get("execution_actor")
        if actor:
            async with self.store._unit():
                identity = await ForgeIdentityUserRepository().find_by_id(identifier(actor["user_id"]))
                if identity is None or not identity.enabled:
                    raise AccessDenied("The provider execution identity is unavailable")
                actor = {
                    **actor,
                    "identity_digest": actor.get("identity_digest")
                    or digest({"issuer": identity.issuer, "subject": identity.subject}),
                }
            async with self._locked(run_id, actor) as row:
                yield row, actor
        else:
            async with self.store._unit():
                row = await self.journal._run(run_id)
                await ProjectAccess(self.store).authorize_execution(row.data)
                await self.store.assert_campaign_current(row.data)
                yield row, None

    async def normal_dispatch(self, run_id: str, key: str) -> None:
        async with self._normal_lock(run_id) as (row, actor):
            task = await self.journal._task(run_id, "implement")
            if row.status != "developing" or task.status != "running" or task.data.get("idempotency_key") != key:
                raise ValueError("The provider dispatch is not the current implementation")
            entries = await self._entries(run_id)
            if (
                key not in dispatch_keys(entries)
                or len(dispatch_keys(entries)) > 4
                or any(e["kind"] == "provider_dispatch_intent" and e["payload"]["task_key"] == key for e in entries)
            ):
                raise ValueError("The provider dispatch was consumed or its finite budget is exhausted")
            await self._settled(row.data, idle=False)
            cut = self._cut(row.data)
            await self.journal._append(
                run_id,
                "implement",
                key + ":provider",
                "provider_dispatch_intent",
                {"version": 1, "task_key": key, "attempt": task.attempt},
            )
            row.data = {**row.data, "provider_dispatch": {"task_key": key, "cut": cut, "actor": actor}}
            await self.store.run_records.save(row)
            if self._cut(row.data) != cut:
                raise ValueError("The provider source cut changed during dispatch admission")

    async def assert_launch(self, run_id: str, key: str) -> None:
        initial = await self.store.get_run(run_id)
        if (initial.get("provider_recovery") or {}).get("task_key") == key:
            await self.assert_dispatch(run_id, key)
            return
        async with self._normal_lock(run_id) as (row, _actor):
            task = await self.journal._task(run_id, "implement")
            state = row.data.get("provider_dispatch") or {}
            if (
                row.status != "developing"
                or task.status != "running"
                or state.get("task_key") != key
                or task.data.get("idempotency_key") != key
            ):
                raise ValueError("The original provider dispatch is no longer active")
            await self._settled(row.data, idle=False)
            if self._cut(row.data) != state["cut"]:
                raise ValueError("The source/provider/policy cut changed before process launch")
        if self._cut(row.data) != state["cut"]:
            raise ValueError("The local launch cut changed during native commit")

    async def assert_result(self, run_id: str, key: str) -> None:
        initial = await self.store.get_run(run_id)
        recovery = initial.get("provider_recovery") or {}
        if recovery.get("task_key") == key:
            actor = recovery["actor"]
            async with self._locked(run_id, actor) as row:
                await self._result_cut(row, key, recovery)
            expected = recovery["cut"]
        else:
            async with self._normal_lock(run_id) as (row, _actor):
                state = row.data.get("provider_dispatch") or {}
                await self._result_cut(row, key, state)
            expected = state["cut"]
        after = self._cut(row.data)
        if any(after[name] != expected[name] for name in RESULT_CUT_FIELDS):
            raise ValueError("The local result context changed during native commit")

    async def _result_cut(self, row: Any, key: str, state: Data) -> None:
        task = await self.journal._task(row.data["id"], "implement")
        if task.data.get("idempotency_key") != key:
            raise ProviderAttemptClosed("The provider result belongs to a closed old attempt")
        if (
            row.status != "developing"
            or task.status != "running"
            or task.data.get("idempotency_key") != key
            or state.get("task_key") != key
            or state.get("status", "consumed") != "consumed"
        ):
            raise ValueError("The provider result is not the current active attempt")
        await self._settled(row.data, idle=False)
        current = self._cut(row.data)
        # Source and Git index changes are the worker's output, not a renewed authority grant.
        for name in RESULT_CUT_FIELDS:
            if current[name] != state["cut"][name]:
                raise ValueError("The provider result's frozen execution context changed")

    async def complete(self, run_id: str, key: str) -> None:
        async with self.store._unit():
            row = await self.journal._run(run_id)
            state = row.data.get("provider_recovery") or {}
            if state.get("task_key") != key:
                return
            task = await self.journal._task(run_id, "implement")
            entries = await self._entries(run_id)
            if task.status != "done" or not any(
                e["kind"] == "receipt" and e["idempotency_key"] == key for e in entries
            ):
                raise ValueError("The recovery needs its actual completed implementation receipt")
            row.data = {**row.data, "provider_recovery": {**state, "status": "completed"}}
            await self.store.run_records.save(row)

    async def record_process(self, run_id: str, key: str, outcome: ProviderProcessOutcome | None) -> bool:
        if outcome is not None and not isinstance(outcome, ProviderProcessOutcome):
            raise TypeError("Only trusted typed process metadata can be retained")
        async with self.store._unit():
            row = await self.journal._run(run_id)
            task = await self.journal._task(run_id, "implement")
            if task.data.get("idempotency_key") != key:
                raise ProviderAttemptClosed("A closed old provider attempt cannot replace current evidence")
            payload = {
                "version": 1,
                "provenance": "native_cli_process_observation",
                "task_key": key,
                "process": outcome.model_dump() if outcome is not None else None,
            }
            entries = await self._entries(run_id)
            existing = next(
                (
                    e["payload"]
                    for e in entries
                    if e["kind"] == "provider_failure_observation" and e["payload"]["task_key"] == key
                ),
                None,
            )
            if existing is not None:
                if existing != payload:
                    raise ValueError("Native process failure evidence is immutable")
                return outcome is not None and outcome.known_failure
            if task.status == "done" or any(e["kind"] == "receipt" and e["idempotency_key"] == key for e in entries):
                raise ProviderAttemptClosed("A completed provider result cannot be replaced by a late failure")
            await self.journal._append(run_id, "implement", key, "provider_failure_observation", payload)
            known = outcome is not None and outcome.known_failure
            task.status = "failed" if known else "needs_reconciliation"
            await self.journal.tasks.save(task)
            if not known:
                row.status = "needs_reconciliation"
                row.data = {**row.data, "status": row.status, "updated_at": now()}
                await self.store.run_records.save(row)
            return known
