# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable API-to-owner checkpoint tickets; unknown effects are never requeued."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text

from firefly_forge.execution_request_persistence import ForgeExecutionRequestEntity
from firefly_forge.execution_requests import digest as request_digest
from firefly_forge.git_deliveries import KIND, binding
from firefly_forge.git_delivery_models import GitDeliveryInputV1, GitDeliveryPolicyV1
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import identifier, now

Data = dict[str, Any]


class GitDeliveryRequests:
    def __init__(self, deliveries: Any, requests: Any) -> None:
        self.deliveries, self.requests, self.store = deliveries, requests, deliveries.store

    async def request(self, run_id: str, body: GitDeliveryInputV1) -> Data:
        user = CURRENT_PRINCIPAL.get()
        if user is None:
            raise AccessDenied("Sign in before requesting a Git checkpoint")
        actor = ProjectAccess.actor_snapshot(user)
        run = await self.store.get_run(run_id)
        from firefly_forge.git_failed_unit_adoption import GitFailedUnitAdoptions, clock_ms, effective_policy

        adoption_expires = (
            clock_ms() + body.explicit_adoption.valid_for_seconds * 1000 if body.explicit_adoption else None
        )
        if body.explicit_adoption is not None:
            async with self.store._unit():
                retained = await self.requests.records.find_by_id(identifier(body.request_id))
                if retained is not None:
                    if (
                        retained.kind != KIND
                        or retained.run_id.hex != run_id
                        or retained.data["request"]["input"] != body.model_dump(mode="json")
                        or retained.data["request"]["actor"] != actor
                    ):
                        raise ValueError("The original failed-unit request belongs to another input or actor")
                    return await self.get(run_id, body.request_id)
            adoptions = GitFailedUnitAdoptions(self.deliveries)
            candidate = await adoptions.candidate(run_id, actor)
            adoptions.compare(candidate, body.explicit_adoption)
            if body.expected_head != candidate["expected_head"]:
                raise ValueError("The adopted source HEAD differs from the explicit review")
            policy = GitDeliveryPolicyV1.model_validate(candidate["policy"])
        else:
            if run.get("git_failed_unit_adoption"):
                raise ValueError(
                    "An adopted failed unit has one original checkpoint request; inspect it without replay"
                )
            policy = await effective_policy(self.deliveries, run)
        if not policy.enabled or run["status"] not in {"paused", "failed"}:
            raise ValueError("Checkpointing requires an enabled policy and a quiescent paused or failed run")
        await self.deliveries._authorize(run, actor, "release" if policy.remote == "draft_pr" else "execute")
        incoming: Data = {"input": body.model_dump(mode="json"), "actor": actor, "binding": binding(run)}
        if adoption_expires is not None:
            incoming["explicit_adoption_expires_at_ms"] = adoption_expires
        await self.store.admit_credentials(incoming)
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:execution-request-admission:" + self.store.schema},
            )
            locked = await self.deliveries.journal._run(run_id)
            current = {**copy.deepcopy(locked.data), "id": run_id, "status": locked.status}
            if binding(current) != binding(run) or CURRENT_PRINCIPAL.get() != user:
                raise ValueError("The checkpoint run or caller changed during admission")
            await self.deliveries._authorize(current, actor, "release" if policy.remote == "draft_pr" else "execute")
            if body.explicit_adoption is not None:
                current_candidate = await GitFailedUnitAdoptions(self.deliveries).candidate(run_id, actor)
                GitFailedUnitAdoptions.compare(current_candidate, body.explicit_adoption)
            old = await self.requests.records.find_by_id(identifier(body.request_id))
            if old is not None:
                if old.kind != KIND or old.run_id.hex != run_id or old.data["request"] != incoming:
                    raise ValueError("Checkpoint request identity belongs to another input or actor")
            else:
                rows = await self.requests.records.for_run(identifier(run_id), KIND)
                if any(r.status in {"queued", "executing", "needs_reconciliation"} for r in rows):
                    raise ValueError("A prior Git delivery is pending or unknown; inspect its exact receipt")
                if len(rows) >= 50 or await self.requests.records.pending_count() >= 500:
                    raise ValueError("The finite Git delivery request queue is full")
                data = {
                    "id": body.request_id,
                    "run_id": run_id,
                    "kind": KIND,
                    "status": "queued",
                    "request": incoming,
                    "request_digest": request_digest(incoming),
                    "generation": None,
                    "result": None,
                    "created_at": now(),
                    "updated_at": now(),
                }
                row = ForgeExecutionRequestEntity(
                    id=identifier(body.request_id),
                    run_id=identifier(run_id),
                    kind=KIND,
                    request_key=body.request_id,
                    status="queued",
                    data=data,
                )
                await self.requests.records.save(row)
                await self.requests._event(row, "request_admitted", {"request_digest": data["request_digest"]})
            if adoption_expires is not None and clock_ms() >= adoption_expires:
                raise ValueError("The explicit failed-unit consent expired before request admission")
        return await self.get(run_id, body.request_id)

    async def get(self, run_id: str, request_id: str) -> Data:
        user = CURRENT_PRINCIPAL.get()
        if user is None:
            raise AccessDenied("Sign in to inspect checkpoint requests")
        await self.deliveries._authorize(await self.store.get_run(run_id), ProjectAccess.actor_snapshot(user), "read")
        async with self.store._unit():
            row = await self.requests.records.find_by_id(identifier(request_id))
            if row is None or row.kind != KIND or row.run_id.hex != run_id:
                raise KeyError("Git checkpoint request not found")
            data = copy.deepcopy(row.data)
            if row.status != data.get("status") or row.id.hex != data.get("id") or data.get("run_id") != run_id:
                raise ValueError("Git checkpoint request scalar identity differs")
        events = await self.requests.events(request_id)
        if (
            not events
            or not self.requests.verify_chain(events)
            or request_digest(data["request"]) != data["request_digest"]
        ):
            raise ValueError("Git checkpoint request receipt integrity differs")
        if (
            any(e["request_id"] != request_id for e in events)
            or events[0]["kind"] != "request_admitted"
            or events[0]["evidence"] != {"request_digest": data["request_digest"]}
        ):
            raise ValueError("Git checkpoint request has no exact original admission receipt")
        if data["status"] in {"completed", "failed", "needs_reconciliation"}:
            if events[-1]["kind"] != "worker_" + data["status"] or events[-1]["evidence"] != data["result"]:
                raise ValueError("Git checkpoint result differs from its immutable worker receipt")
        elif data["status"] == "queued":
            if events[-1]["kind"] != "request_admitted" or data["result"] is not None:
                raise ValueError("Queued Git checkpoint has an inconsistent effect history")
        elif data["status"] != "executing" or events[-1]["kind"] != "worker_claim":
            raise ValueError("Git checkpoint request has an unknown custody state")
        return {**data, "events": events}

    async def _finish(self, admitted: Data, status: str, result: Data) -> None:
        async with self.store.source_budget_admission_unit():
            row = await self.requests.records.find_by_id(identifier(admitted["id"]), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.status != "executing" or row.data != admitted:
                raise ValueError("Checkpoint ticket custody changed; preserve the newer owner outcome")
            row.status = status
            row.data = {**row.data, "status": status, "result": result, "updated_at": now()}
            await self.requests.records.save(row)
            await self.requests._event(row, "worker_" + status, result)

    async def execute(self, request_id: str) -> None:
        self.requests.runtime.require_execution()
        if not await self.requests._claim(request_id):
            return
        async with self.store._unit():
            row = await self.requests.records.find_by_id(identifier(request_id))
            if row is None or row.kind != KIND or row.status != "executing":
                raise ValueError("The claimed Git delivery ticket changed")
            claimed = copy.deepcopy(row.data)
        try:
            request = claimed["request"]
            run = await self.store.get_run(claimed["run_id"])
            if binding(run) != request["binding"]:
                raise ValueError("The admitted checkpoint run changed before worker dispatch")
            body = GitDeliveryInputV1.model_validate(request["input"])
            if body.explicit_adoption is not None:
                from firefly_forge.git_failed_unit_adoption import GitFailedUnitAdoptions

                await GitFailedUnitAdoptions(self.deliveries).adopt(
                    claimed["run_id"],
                    body.request_id,
                    request["actor"],
                    body.explicit_adoption,
                    expires=request["explicit_adoption_expires_at_ms"],
                )
            result = await self.deliveries.checkpoint(
                claimed["run_id"],
                request_id=body.request_id,
                expected_head=body.expected_head,
                expected_source_tree=body.expected_source_tree,
                expected_source_digest=body.expected_source_digest,
                actor=request["actor"],
            )
            await self._finish(claimed, "completed", result)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(
                    claimed,
                    "needs_reconciliation",
                    {"detail": "The checkpoint owner stopped; inspect local and remote state without replay"},
                )
            )
            raise
        except Exception:
            entries = await self.deliveries._entries(claimed["run_id"])
            started = any(
                e["kind"] == "git_delivery_intent" and e["payload"].get("request_id") == request_id for e in entries
            )
            await self._finish(
                claimed,
                "needs_reconciliation" if started else "failed",
                {"detail": "Inspect the recorded checkpoint intent and current authority before another request"},
            )

    async def recover(self) -> None:
        self.requests.runtime.require_execution()
        async with self.store._unit():
            rows = [copy.deepcopy(row.data) for row in await self.requests.records.state("executing", kind=KIND)]
        for row in rows:
            await self._finish(
                row,
                "needs_reconciliation",
                {"detail": "Owner restarted; observe the retained Git intent instead of repeating it"},
            )
