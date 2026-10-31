# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Persist API intentions; only the single execution owner dispatches their effects."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.kernel.lifecycle import CONSUMER_PHASE
from sqlalchemy import text

from firefly_forge.execution_request_persistence import (
    ForgeExecutionRequestEntity,
    ForgeExecutionRequestEventEntity,
    ForgeExecutionRequestEventRepository,
    ForgeExecutionRequestRepository,
)
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.store import TERMINAL, Store, identifier, now

if TYPE_CHECKING:
    from firefly_forge.runner_continuations import RunnerContinuations

Data = dict[str, Any]


class ExecutionRequestReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reconciliation_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@service
class ExecutionRequests:
    phase = CONSUMER_PHASE + 5

    def __init__(
        self,
        store: Store,
        runtime: RuntimeRoles,
        records: ForgeExecutionRequestRepository | None = None,
        journal: ForgeExecutionRequestEventRepository | None = None,
    ) -> None:
        self.store, self.runtime = store, runtime
        self.records = records or ForgeExecutionRequestRepository()
        self.journal = journal or ForgeExecutionRequestEventRepository()
        self.runner_continuations: RunnerContinuations | None = None
        self.agent_restores: Any = None
        self.invalid_read_continuities: Any = None
        self.git_deliveries: Any = None
        self.github_projects: Any = None
        self.publish_effect: Callable[[str, Data | None], Awaitable[str]] | None = None
        self.cancel_effect: Callable[[str], Awaitable[Data]] | None = None
        self.poller: asyncio.Task[None] | None = None
        self.active: dict[str, asyncio.Task[None]] = {}

    async def get(self, request_id: str) -> Data:
        async with self.store._unit():
            row = await self.records.find_by_id(identifier(request_id))
            if row is None:
                raise KeyError("Execution request not found")
            return copy.deepcopy(row.data)

    async def for_run(self, run_id: str) -> list[Data]:
        await self.store.get_run(run_id)
        async with self.store._unit():
            rows = await self.records.for_run(identifier(run_id), "publish")
            rows += await self.records.for_run(identifier(run_id), "cancel")
            return [{**copy.deepcopy(row.data), "events": await self.events(row.id.hex)} for row in rows]

    async def events(self, request_id: str) -> list[Data]:
        async with self.store._unit():
            return [copy.deepcopy(row.data) for row in await self.journal.events(identifier(request_id))]

    @staticmethod
    def verify_chain(entries: list[Data]) -> bool:
        previous = "0" * 64
        for sequence, entry in enumerate(entries, 1):
            if entry["sequence"] != sequence or entry["previous_hash"] != previous:
                return False
            if digest({key: value for key, value in entry.items() if key != "digest"}) != entry["digest"]:
                return False
            previous = entry["digest"]
        return True

    async def _event(self, row: ForgeExecutionRequestEntity, kind: str, evidence: Data) -> None:
        entries = await self.journal.events(row.id)
        if not self.verify_chain([entry.data for entry in entries]):
            raise RuntimeError("Execution request receipt integrity failed")
        value = {
            "request_id": row.id.hex,
            "sequence": len(entries) + 1,
            "previous_hash": entries[-1].data["digest"] if entries else "0" * 64,
            "kind": kind,
            "evidence": evidence,
            "created_at": now(),
        }
        value["digest"] = digest(value)
        await self.journal.save(
            ForgeExecutionRequestEventEntity(
                id=uuid.uuid4(),
                request_id=row.id,
                sequence=value["sequence"],
                data=value,
            )
        )

    @staticmethod
    def _actor(run: Data) -> Data | None:
        principal = CURRENT_PRINCIPAL.get()
        return ProjectAccess.actor_snapshot(principal) if principal else run.get("execution_actor")

    @staticmethod
    def _eligible(run: Data) -> None:
        if (
            run["status"] != "succeeded"
            or not run.get("final_verification")
            or any(check["exit_code"] != 0 for check in run["final_verification"])
            or not run.get("source_tree")
            or not run.get("base_revision")
        ):
            raise ValueError("Publication requires retained passing verification and a source baseline")
        if run.get("operation") == "test":
            raise ValueError("Test operations have no implementation branch to publish")

    async def request_publish(self, run_id: str) -> Data:
        return await self._admit(run_id, "publish")

    async def request_cancel(self, run_id: str) -> Data:
        return await self._admit(run_id, "cancel")

    async def _admit(self, run_id: str, kind: str) -> Data:
        run = await self.store.get_run(run_id)
        actor = self._actor(run)
        permission: Literal["release", "execute"] = "release" if kind == "publish" or run.get("publish") else "execute"
        await ProjectAccess(self.store).authorize_actor(actor, run.get("project_id"), permission)
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:execution-request-admission:" + self.store.schema},
            )
            locked = await self.store.run_records.find_by_id(identifier(run_id), lock=LockMode.PESSIMISTIC_WRITE)
            if locked is None:
                raise KeyError("Run not found")
            run = copy.deepcopy(locked.data)
            await ProjectAccess(self.store).authorize_actor(actor, run.get("project_id"), permission)
            previous = await self.records.for_run(locked.id, kind)
            if previous and previous[0].status != "failed":
                return copy.deepcopy(previous[0].data)
            if previous:
                receipts = await self.events(previous[0].id.hex)
                if not self._pre_effect_failure(receipts, previous[0].data["request_digest"]):
                    raise ValueError("The failed request has no trusted pre-effect proof; reconcile its outcome")
                if kind == "publish" and await self._publication_intent(run_id):
                    raise ValueError(
                        "A retained publication intent requires outcome reconciliation before another request"
                    )
            if len(previous) >= 3:
                raise ValueError("This run exhausted its three explicit delivery request admissions")
            if kind == "publish":
                self._eligible(run)
                if run.get("publication_status") == "publishing":
                    raise ValueError("Publication outcome must be reconciled before another request")
            if await self.records.pending_count() >= 500:
                raise ValueError("The bounded worker request queue is full; wait before requesting more work")
            request = {
                "run_id": run_id,
                "kind": kind,
                "actor": actor,
                "project_id": run.get("project_id"),
                "source_tree": run.get("source_tree"),
                "checkpoint_revision": run.get("checkpoint_revision"),
                "branch": run.get("branch"),
                "attempt": len(previous) + 1,
            }
            value: Data = {
                "id": uuid.uuid4().hex,
                "run_id": run_id,
                "kind": kind,
                "status": "queued",
                "request": request,
                "request_digest": digest(request),
                "result": None,
                "created_at": now(),
                "updated_at": now(),
            }
            row = ForgeExecutionRequestEntity(
                id=identifier(value["id"]),
                run_id=locked.id,
                kind=kind,
                request_key=value["request_digest"],
                status="queued",
                data=value,
            )
            await self.records.save(row)
            await self._event(row, "request_admitted", {"request_digest": value["request_digest"], "request": request})
            field = "publication_request" if kind == "publish" else "cancel_request"
            locked.data = {
                **locked.data,
                field: {
                    "id": value["id"],
                    "status": "queued" if kind == "publish" else "requested",
                    "actor": actor,
                    "requested_at": value["created_at"],
                },
            }
            if kind == "cancel" and locked.status in TERMINAL:
                row.status = "completed"
                row.data = {
                    **row.data,
                    "status": "completed",
                    "result": {"no_op": True, "terminal_status": locked.status},
                }
                await self.records.save(row)
                locked.data[field]["status"] = "acknowledged"
                await self._event(row, "terminal_noop", row.data["result"])
            elif kind == "cancel":
                locked.data = {**locked.data, "display_status": "cancel_requested"}
            await self.store.run_records.save(locked)
            return copy.deepcopy(row.data)

    async def _claim(self, request_id: str) -> bool:
        self.runtime.require_execution()
        await self.store.assert_ownership()
        observed = await self.get(request_id)
        async with self.store._unit():
            await self.store.run_records.find_by_id(identifier(observed["run_id"]), lock=LockMode.PESSIMISTIC_WRITE)
            row = await self.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.status != "queued":
                return False
            if digest(row.data["request"]) != row.data["request_digest"]:
                raise RuntimeError("The admitted execution request changed")
            row.status = "executing"
            row.data = {**row.data, "status": row.status, "updated_at": now()}
            await self.records.save(row)
            await self._event(row, "worker_claim", {"request_digest": row.data["request_digest"]})
            return True

    async def _observation(self, request_id: str, kind: str, evidence: Data) -> None:
        await self.store.assert_ownership()
        await self.store.admit_credentials(evidence)
        observed = await self.get(request_id)
        async with self.store._unit():
            await self.store.run_records.find_by_id(identifier(observed["run_id"]), lock=LockMode.PESSIMISTIC_WRITE)
            row = await self.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.status != "executing":
                raise RuntimeError("This execution request no longer owns its observed outcome")
            await self._event(row, kind, evidence)

    async def _effect_receipt(self, request_id: str, result: Data) -> None:
        data = await self.get(request_id)
        run = await self.store.get_run(data["run_id"])
        await self._observation(
            request_id,
            "worker_effect_receipt",
            {
                "request_digest": data["request_digest"],
                "result": result,
                "run_status": run["status"],
                "source_tree": run.get("source_tree"),
                "checkpoint_revision": run.get("checkpoint_revision"),
                "branch": run.get("branch"),
            },
        )

    @classmethod
    def _pre_effect_failure(cls, entries: list[Data], request_digest: str) -> bool:
        return (
            cls.verify_chain(entries)
            and not any(entry["kind"] == "worker_dispatch" for entry in entries)
            and any(
                entry["kind"] == "worker_pre_effect_failure"
                and entry["evidence"].get("request_digest") == request_digest
                and entry["evidence"].get("before_delivery_dispatch") is True
                for entry in entries
            )
        )

    async def _publication_intent(self, run_id: str) -> bool:
        from firefly_forge.journal import Journal

        run = await self.store.get_run(run_id)
        if run.get("publication_status") in {"publishing", "published"} or run.get("pr_url"):
            return True
        try:
            plan = await Journal(self.store).plan(run_id)
        except KeyError:
            return False
        return any(task["id"] == "publish" for task in plan["tasks"])

    @staticmethod
    def _publication_receipt(entries: list[Data], admitted: Data, current: Data) -> Data | None:
        from firefly_forge.journal import Journal

        run_id = admitted["run_id"]
        if not Journal.verify_chain(entries) or any(entry.get("run_id") != run_id for entry in entries):
            return None
        receipts = [entry for entry in entries if entry.get("task_id") == "publish" and entry.get("kind") == "receipt"]
        if len(receipts) != 1:
            return None
        receipt = receipts[0]
        key = receipt.get("idempotency_key")
        attempts = {
            uuid.uuid5(uuid.NAMESPACE_URL, f"forge:{run_id}:publish:{attempt}").hex: attempt
            for attempt in range(1, 101)
        }
        if not isinstance(key, str) or key not in attempts:
            return None
        intents = [
            entry
            for entry in entries
            if entry.get("task_id") == "publish"
            and entry.get("kind") == "intent"
            and entry.get("idempotency_key") == key
            and entry["sequence"] < receipt["sequence"]
        ]
        if len(intents) != 1:
            return None
        intent = intents[0]
        evidence, planned = receipt.get("payload"), intent.get("payload")
        if not isinstance(evidence, dict) or not isinstance(planned, dict):
            return None
        for field in ("source_tree", "branch"):
            value = evidence.get(field)
            if not isinstance(value, str) or not value or value != planned.get(field) or value != current.get(field):
                return None
            if admitted.get(field) is not None and value != admitted[field]:
                return None
        url = evidence.get("pr_url")
        if (
            not isinstance(url, str)
            or not url
            or url != current.get("pr_url")
            or current.get("publication_status") != "published"
        ):
            return None
        return {
            "source": "publication_journal",
            "intent_digest": intent["digest"],
            "receipt_digest": receipt["digest"],
            "idempotency_key": key,
            "attempt": attempts[key],
            "result": {"pr_url": url},
            "source_tree": evidence["source_tree"],
            "branch": evidence["branch"],
            "checkpoint_revision": current.get("checkpoint_revision"),
        }

    async def _finish(self, request_id: str, status: str, evidence: Data) -> None:
        observed = await self.get(request_id)
        async with self.store._unit():
            run = await self.store.run_records.find_by_id(
                identifier(observed["run_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            row = await self.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.status != "executing":
                return
            row.status = status
            row.data = {**row.data, "status": status, "result": evidence, "updated_at": now()}
            await self.records.save(row)
            await self._event(row, "worker_" + status, evidence)
            if run is not None:
                field = "publication_request" if row.kind == "publish" else "cancel_request"
                view = {
                    **run.data.get(field, {}),
                    "status": "acknowledged" if row.kind == "cancel" and status == "completed" else status,
                }
                run.data = {**run.data, field: view}
                await self.store.run_records.save(run)

    async def execute(self, request_id: str) -> None:
        if not await self._claim(request_id):
            return
        data = await self.get(request_id)
        request = data["request"]
        dispatched = False
        try:
            run = await self.store.get_run(data["run_id"])
            permission: Literal["release", "execute"] = (
                "release" if data["kind"] == "publish" or run.get("publish") else "execute"
            )
            await ProjectAccess(self.store).authorize_actor(request["actor"], request["project_id"], permission)
            await self.store.assert_ownership()
            if data["kind"] == "publish":
                self._eligible(run)
                if (
                    run.get("source_tree") != request["source_tree"]
                    or run.get("checkpoint_revision") != request["checkpoint_revision"]
                ):
                    raise ValueError("The verified source changed after publication admission")
                if self.publish_effect is None:
                    raise ValueError("The publication worker is not configured")
                await self._observation(request_id, "worker_dispatch", {"request_digest": data["request_digest"]})
                dispatched = True
                result = {"pr_url": await self.publish_effect(data["run_id"], request["actor"])}
            else:
                if self.cancel_effect is None:
                    raise ValueError("The cancellation worker is not configured")
                await self._observation(request_id, "worker_dispatch", {"request_digest": data["request_digest"]})
                dispatched = True
                result = await self.cancel_effect(data["run_id"])
            await self._effect_receipt(request_id, result)
            await self._finish(request_id, "completed", result)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(
                    request_id,
                    "needs_reconciliation",
                    {"detail": "Worker stopped without a confirmed outcome; no automatic replay is permitted"},
                )
            )
            raise
        except Exception as exc:
            unknown = dispatched or not isinstance(exc, (ValueError, AccessDenied))
            if not unknown and data["kind"] == "publish":
                unknown = await self._publication_intent(data["run_id"])
            if not unknown:
                await self._observation(
                    request_id,
                    "worker_pre_effect_failure",
                    {
                        "request_digest": data["request_digest"],
                        "failure_kind": "authorization" if isinstance(exc, AccessDenied) else "validation",
                        "before_delivery_dispatch": True,
                    },
                )
            await self._finish(
                request_id,
                "needs_reconciliation" if unknown else "failed",
                {
                    "detail": "Execution outcome needs observation"
                    if unknown
                    else "Worker rejected the request before completion",
                    "failure_kind": "authorization" if isinstance(exc, AccessDenied) else "execution",
                },
            )

    async def reconcile(self, run_id: str, request_id: str, body: ExecutionRequestReconciliation) -> Data:
        from firefly_forge.journal import Journal

        await self.store.admit_credentials(body.model_dump())
        observed = await self.get(request_id)
        if observed["run_id"] != identifier(run_id).hex:
            raise KeyError("Execution request not found in this run")
        if observed["kind"] == "git_delivery":
            raise ValueError(
                "Inspect exact Git delivery observations; generic reconciliation cannot settle its custody"
            )
        run = await self.store.get_run(run_id)
        actor = self._actor(run)
        permission: Literal["release", "execute"] = (
            "release" if observed["kind"] == "publish" or run.get("publish") else "execute"
        )
        access = ProjectAccess(self.store)
        await access.authorize_actor(actor, run.get("project_id"), permission)
        key = body.reconciliation_id
        previous = observed.get("reconciliation")
        if previous:
            if previous["id"] != key:
                raise ValueError("The request already has another reconciliation")
            return observed
        if observed["status"] != "needs_reconciliation":
            raise ValueError("Only unknown execution requests require reconciliation")
        entries = await self.events(request_id)
        if not self.verify_chain(entries) or digest(observed["request"]) != observed["request_digest"]:
            raise ValueError("Execution request receipt integrity failed")
        proof: Data | None = None
        outcome = "completed"
        for entry in entries:
            evidence = entry["evidence"]
            if evidence.get("request_digest") != observed["request_digest"]:
                continue
            if entry["kind"] == "worker_effect_receipt":
                proof = {"source": "worker_effect_receipt", "receipt_digest": entry["digest"], **evidence}
            elif entry["kind"] == "worker_pre_effect_failure" and self._pre_effect_failure(
                entries, observed["request_digest"]
            ):
                proof = {
                    "source": "worker_pre_effect_failure",
                    "receipt_digest": entry["digest"],
                    "result": {"failure_kind": evidence["failure_kind"]},
                }
                outcome = "failed"
        if observed["kind"] == "publish" and outcome != "failed":
            journal = Journal(self.store)
            journal_entries = await journal.entries(run_id)
            proof = self._publication_receipt(journal_entries, observed["request"], run)
        if proof is None:
            raise ValueError("The effect remains unknown without a trusted terminal receipt")
        if outcome == "failed" and observed["kind"] == "publish" and await self._publication_intent(run_id):
            raise ValueError("Publication remains unknown while a delivery intent has no reconciled terminal receipt")
        async with self.store._unit():
            locked_run = await self.store.run_records.find_by_id(identifier(run_id), lock=LockMode.PESSIMISTIC_WRITE)
            row = await self.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if locked_run is None or row is None:
                raise KeyError("Execution request not found")
            await access.authorize_actor(actor, locked_run.data.get("project_id"), permission)
            existing = row.data.get("reconciliation")
            if existing:
                if existing["id"] != key:
                    raise ValueError("The request already has another reconciliation")
                return copy.deepcopy(row.data)
            if row.status != "needs_reconciliation" or row.data["request_digest"] != observed["request_digest"]:
                raise ValueError("The request changed during reconciliation")
            current = locked_run.data
            if outcome == "completed":
                for field in ("source_tree", "checkpoint_revision", "branch"):
                    admitted = row.data["request"].get(field)
                    if admitted is not None and admitted != current.get(field):
                        raise ValueError("The admitted source changed; the receipt cannot settle this request")
                    if proof.get(field) != current.get(field):
                        raise ValueError("The terminal receipt does not match the current source")
                if row.kind == "publish" and (
                    current.get("publication_status") != "published"
                    or proof["result"].get("pr_url") != current.get("pr_url")
                ):
                    raise ValueError("Publication remains unknown without its matching terminal receipt")
                if row.kind == "cancel" and (
                    current["status"] not in TERMINAL
                    or proof.get("run_status") != current["status"]
                    or not proof["result"].get("worker_drained")
                ):
                    raise ValueError("Cancellation remains unknown without a drained terminal worker receipt")
            receipt = {"id": key, "actor": actor, "outcome": outcome, "proof": proof}
            await self._event(row, "reconciliation", receipt)
            row.status = outcome
            row.data = {
                **row.data,
                "status": outcome,
                "result": proof["result"],
                "reconciliation": receipt,
                "updated_at": now(),
            }
            await self.records.save(row)
            field = "publication_request" if row.kind == "publish" else "cancel_request"
            locked_run.data = {
                **current,
                field: {
                    **current.get(field, {}),
                    "status": "acknowledged" if row.kind == "cancel" and outcome == "completed" else outcome,
                },
            }
            await self.store.run_records.save(locked_run)
            return copy.deepcopy(row.data)

    async def recover(self) -> int:
        self.runtime.require_execution()
        await self.store.assert_ownership()
        count = 0
        for _ in range(10):
            async with self.store._unit():
                identifiers = [
                    row.id.hex
                    for kind in ("cancel", "publish")
                    for row in await self.records.state("executing", kind=kind)
                ]
            if not identifiers:
                return count
            for request_id in identifiers:
                await self._finish(
                    request_id,
                    "needs_reconciliation",
                    {"detail": "Execution owner restarted; observe the effect before reconciliation"},
                )
                count += 1
        return count

    async def start(self) -> None:
        if not self.runtime.executes_jobs or self.poller is not None:
            return
        await self.recover()
        if self.runner_continuations is not None:
            await self.runner_continuations.recover()
        if self.agent_restores is not None:
            await self.agent_restores.recover()
        if self.invalid_read_continuities is not None:
            await self.invalid_read_continuities.recover()
        if self.git_deliveries is not None:
            await self.git_deliveries.recover()
        if self.github_projects is not None:
            await self.github_projects.recover()
        self.poller = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self.poller:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)
        for task in self.active.values():
            task.cancel()
        await asyncio.gather(*self.active.values(), return_exceptions=True)

    async def _poll(self) -> None:
        while True:
            await self.store.assert_ownership()
            self.active = {key: task for key, task in self.active.items() if not task.done()}
            async with self.store._unit():
                queued = [copy.deepcopy(row.data) for row in await self.records.state("queued", kind="cancel")]
                queued += [copy.deepcopy(row.data) for row in await self.records.state("queued", kind="publish")]
                if self.runner_continuations is not None:
                    queued += [
                        copy.deepcopy(row.data) for row in await self.records.state("queued", kind="runner_continue")
                    ]
                if self.agent_restores is not None:
                    queued += [
                        copy.deepcopy(row.data) for row in await self.records.state("queued", kind="agent_restore")
                    ]
                if self.invalid_read_continuities is not None:
                    queued += [
                        copy.deepcopy(row.data) for row in await self.records.state("queued", kind="invalid_read")
                    ]
                if self.git_deliveries is not None:
                    queued += [
                        copy.deepcopy(row.data) for row in await self.records.state("queued", kind="git_delivery")
                    ]
                if self.github_projects is not None:
                    queued += [
                        copy.deepcopy(row.data) for row in await self.records.state("queued", kind="github_board")
                    ]
            for row in queued:
                if row["kind"] == "cancel":
                    await self.execute(row["id"])
                elif not self.active:
                    if row["kind"] == "github_board":
                        action = self.github_projects.execute
                    elif row["kind"] == "git_delivery":
                        action = self.git_deliveries.execute
                    elif row["kind"] == "agent_restore":
                        action = self.agent_restores.execute
                    elif row["kind"] == "invalid_read":
                        action = self.invalid_read_continuities.execute
                    elif row["kind"] == "runner_continue":
                        if self.runner_continuations is None:
                            continue
                        action = self.runner_continuations.execute
                    else:
                        action = self.execute
                    self.active[row["id"]] = asyncio.create_task(action(row["id"]))
            if self.github_projects is not None and not self.active:
                await self.github_projects.queue_automatic()
            await asyncio.sleep(0.2)
