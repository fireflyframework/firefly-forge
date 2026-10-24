# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit unverified checkpoints of an accepted failed native implementation unit."""

from __future__ import annotations

import copy
import hashlib
import time
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from firefly_forge.agent_roles import worker_feedback_model
from firefly_forge.git_delivery_models import GitDeliveryPolicyV1, GitFailedUnitAdoptionV1
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import identifier
from firefly_forge.verification_models import verification_source

Data = dict[str, Any]
KIND = "git_failed_unit_adoption"


def clock_ms() -> int:
    return int(time.time() * 1000)


def failed_unit_proof(run: Data, task: Data, entries: list[Data], observation: Data) -> Data:
    """Accepted failed feedback is closed output, never a completed implementation."""
    task = {key: task.get(key) for key in ("id", "status", "attempt", "idempotency_key")}
    feedback = run.get("worker_feedback") or {}
    attempt = task.get("attempt")
    if type(attempt) is not int or not 1 <= attempt <= 100:
        raise ValueError("A failed checkpoint requires its actual canonical implementation attempt")
    key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{attempt}").hex
    messages = [e for e in entries if e["kind"] == "worker_feedback"]
    dispatches = [e for e in entries if e["kind"] == "provider_dispatch_intent"]
    intent = [e for e in entries if e["kind"] == "intent" and e["idempotency_key"] == key]
    if (
        run.get("status") != "failed"
        or run.get("active_task") != "implement"
        or task.get("id") != "implement"
        or task.get("status") != "running"
        or task.get("idempotency_key") != key
        or run.get("implementation_rounds") != attempt
        or (run.get("worker_request") or {}).get("status") == "pending"
        or not messages
        or messages[-1]["payload"] != feedback
        or messages[-1]["task_id"] != "implement"
        or not dispatches
        or len(intent) != 1
        or dispatches[-1]["idempotency_key"] != key + ":provider"
        or dispatches[-1]["payload"].get("task_key") != key
        or dispatches[-1]["payload"].get("attempt") != attempt
        or not intent[0]["sequence"] < dispatches[-1]["sequence"] < messages[-1]["sequence"]
        or (run.get("provider_dispatch") or {}).get("task_key") != key
        or feedback.get("disposition") != "failed"
        or feedback.get("request") is not None
        or feedback.get("role_id") != "implementer"
        or feedback.get("task_id") != "implement"
        or feedback.get("attempt") != attempt
        or observation.get("protocol") != "native_completed_turn_v1"
        or observation.get("dispatch_digest") != dispatches[-1]["digest"]
        or observation.get("summary_sha256") != hashlib.sha256(run.get("summary", "").encode()).hexdigest()
        or observation.get("usage_digest") != digest(run.get("usage") or {})
        or observation.get("implementation_success") is not False
        or observation.get("process_exit_attested") is not False
    ):
        raise ValueError("The failed unit lost its exact accepted feedback, task or completed native output")
    worker_feedback_model(feedback).model_validate(feedback)
    return {
        "task": copy.deepcopy(task),
        "failed_feedback_digest": messages[-1]["digest"],
        "dispatch_digest": dispatches[-1]["digest"],
        "observation": copy.deepcopy(observation),
        "verification_status": "partial_unverified",
    }


async def native_entries(deliveries: Any, run_id: str) -> list[Data]:
    # Internal hashes/proofs stay inside the owner unit; no protected-content admission suspends its lease.
    async with deliveries.store._unit():
        rows = await deliveries.journal.records.find_for_run(identifier(run_id))
        entries = [{**copy.deepcopy(row.data), "digest": row.digest} for row in rows]
    import json

    if (
        len(entries) > 10000
        or len(json.dumps(entries).encode()) > 16 * 1024 * 1024
        or not deliveries.journal.verify_chain(entries)
    ):
        raise ValueError("Failed-unit adoption requires its intact bounded native journal")
    return entries


class GitFailedUnitAdoptions:
    def __init__(self, deliveries: Any) -> None:
        self.d = deliveries
        self.store = deliveries.store

    async def _authority(self, run: Data, actor: Data) -> Data:
        from firefly_forge.git_delivery_policy import GitDeliveryPolicies

        current = CURRENT_PRINCIPAL.get()
        if actor.get("authentication_method") == "scm_mapping":
            raise AccessDenied("A native current administrator must adopt an unverified failed checkpoint")
        access = ProjectAccess(self.store)
        if (current is not None and access.actor_snapshot(current) != actor) or not await access.authorize_actor(
            actor, None, "members"
        ):
            raise AccessDenied("The failed-checkpoint adoption administrator changed")
        await self.d._authorize(run, actor, "execute")
        saved = await GitDeliveryPolicies(self.store).snapshot(run.get("project_id"))
        if not saved or not GitDeliveryPolicyV1.model_validate(saved["policy"]).enabled:
            raise ValueError("Enable and review the current project Git delivery policy before adopting this run")
        if GitDeliveryPolicyV1.model_validate(saved["policy"]).remote == "draft_pr":
            await self.d._authorize(run, actor, "release")
        return saved

    async def _settled(self, run: Data) -> None:
        active = self.d.engine.active.get(run["id"])
        if active is not None and not active.done():
            raise ValueError("The failed unit still has an active coding owner")
        if (
            self.d.engine.stopping
            or run.get("implementation_pending")
            or run.get("bridge_completion_hold")
            or (run.get("cancel_request") or {}).get("status") == "requested"
        ):
            raise ValueError("The failed unit is cancelled, draining or has an unknown provider effect")
        if (run.get("provider_recovery") or {}).get("status") in {
            "reserved",
            "executing",
            "unknown",
            "needs_reconciliation",
        }:
            raise ValueError("The provider invocation is not settled")
        broker = self.d.engine.tool_broker
        if broker is not None and await broker.unresolved(run["id"]):
            raise ValueError("Unresolved tools block failed-unit adoption")
        for execution in await self.d.journal.sandbox_executions(run["id"]):
            effects = execution.get("effects") or {}
            if not effects or any(
                e.get("status") != "done"
                or e.get("evidence", {}).get("outcome") == "interrupted"
                or e.get("evidence", {}).get("workspace_import") == "requires_reconciliation"
                for e in effects.values()
            ):
                raise ValueError("Unresolved command effects block failed-unit adoption")

    async def candidate(self, run_id: str, actor: Data) -> Data:
        from firefly_forge.source_result_readmission import SourceCompletedOutputReadmissions
        from firefly_forge.source_snapshot import git_index_identity
        from firefly_forge.workspace_reconciliation import _git

        run = await self.store.get_run(run_id)
        if run.get("git_delivery_policy") or run.get("git_failed_unit_adoption"):
            raise ValueError("Adoption is only for an existing run without frozen Git delivery consent")
        saved = await self._authority(run, actor)
        await self._settled(run)
        entries = await native_entries(self.d, run_id)
        self.d._remote_state(entries)
        if any(e["kind"].startswith("git_delivery_") or e["kind"] == KIND for e in entries):
            raise ValueError("A prior Git delivery cannot be replaced by failed-unit adoption")
        task = next(t for t in (await self.d.journal.plan(run_id))["tasks"] if t["id"] == "implement")
        dispatches = [e for e in entries if e["kind"] == "provider_dispatch_intent"]
        if not dispatches:
            raise ValueError("A failed unit requires its retained native dispatch")
        observer = SourceCompletedOutputReadmissions(SimpleNamespace(recoveries=SimpleNamespace(store=self.store)))
        observation = await observer._observation(run, dispatches[-1])
        proof = failed_unit_proof(run, task, entries, observation)
        root = self.d.workspace(run)
        from firefly_forge.git_workspace import metadata_digest

        head = _git(root, "rev-parse", "HEAD").decode().strip()
        if head != (run.get("checkpoint_revision") or run.get("base_revision")) or metadata_digest(root) != run.get(
            "git_metadata_digest"
        ):
            raise ValueError("Protected Git identity differs from the retained native baseline")
        source = verification_source(root)
        index = git_index_identity(root).sha256
        value = {
            "version": 1,
            "run_id": run_id,
            "actor": actor,
            "policy": saved["policy"],
            "policy_digest": saved["digest"],
            "expected_run_digest": digest({k: v for k, v in run.items() if k != "events"}),
            "journal_digest": entries[-1]["digest"],
            "source_digest": source,
            "index_digest": index,
            "expected_head": head,
            "failed_feedback_digest": proof["failed_feedback_digest"],
            "proof": proof,
        }
        if await self.store.get_run(run_id) != run or verification_source(root) != source:
            raise ValueError("The failed unit changed while its candidate was observed")
        await self._authority(run, actor)
        return {**value, "digest": digest(value)}

    @staticmethod
    def compare(candidate: Data, body: GitFailedUnitAdoptionV1) -> None:
        fields = {
            "candidate_digest": "digest",
            "policy_digest": "policy_digest",
            "failed_feedback_digest": "failed_feedback_digest",
            "expected_run_digest": "expected_run_digest",
            "journal_digest": "journal_digest",
            "source_digest": "source_digest",
        }
        if any(getattr(body, k) != candidate[v] for k, v in fields.items()):
            raise ValueError("The explicitly reviewed failed-unit candidate changed")

    async def adopt(
        self, run_id: str, request_id: str, actor: Data, body: GitFailedUnitAdoptionV1, *, expires: int
    ) -> None:
        if type(expires) is not int or not clock_ms() < expires <= clock_ms() + body.valid_for_seconds * 1000:
            raise ValueError("The original finite failed-unit request expired before owner dispatch")
        await self.store.assert_ownership()
        candidate = await self.candidate(run_id, actor)
        self.compare(candidate, body)
        await self.store.admit_credentials({"request": body.model_dump(), "candidate": candidate})
        async with self.d._owned(run_id, actor):
            current = await self.candidate(run_id, actor)
            if current != candidate:
                raise ValueError("Failed-unit adoption changed before its owned native commit")
            event = await self.d._append(
                run_id,
                KIND,
                request_id,
                {
                    "version": 1,
                    "request_id": request_id,
                    "request": body.model_dump(),
                    "actor": actor,
                    "candidate": candidate,
                    "expires_at_ms": expires,
                    "verification_status": "partial_unverified",
                },
            )
            await self.store.update(
                run_id, git_failed_unit_adoption={"request_id": request_id, "receipt_digest": event["digest"]}
            )
            await self._authority(await self.store.get_run(run_id), actor)
            final = await self.store.get_run(run_id)
            from firefly_forge.source_snapshot import git_index_identity
            from firefly_forge.workspace_reconciliation import _git

            root = self.d.workspace(final)
            if (
                verification_source(root) != candidate["source_digest"]
                or _git(root, "rev-parse", "HEAD").decode().strip() != candidate["expected_head"]
                or git_index_identity(root).sha256 != candidate["index_digest"]
                or clock_ms() >= expires
            ):
                raise ValueError("The finite failed-unit adoption, exact index or source changed before commit")


async def effective_policy(
    deliveries: Any, run: Data, *, actor: Data | None = None, writing: bool = False
) -> GitDeliveryPolicyV1:
    marker = run.get("git_failed_unit_adoption")
    if not marker:
        return GitDeliveryPolicyV1.model_validate(run.get("git_delivery_policy") or {})
    if run.get("git_delivery_policy"):
        raise ValueError("Failed-unit adoption cannot replace frozen Git delivery consent")
    entries = await native_entries(deliveries, run["id"])
    matches = [
        e
        for e in entries
        if e["kind"] == KIND
        and e["idempotency_key"] == marker.get("request_id")
        and e["digest"] == marker.get("receipt_digest")
    ]
    if len(matches) != 1:
        raise ValueError("The run has no exact immutable failed-unit adoption receipt")
    event = matches[0]
    payload = event["payload"]
    candidate = payload["candidate"]
    body = GitFailedUnitAdoptionV1.model_validate(payload["request"])
    GitFailedUnitAdoptions.compare(candidate, body)
    if (
        candidate["digest"] != digest({k: v for k, v in candidate.items() if k != "digest"})
        or payload["request_id"] != event["idempotency_key"]
        or payload["actor"] != candidate["actor"]
        or payload["verification_status"] != "partial_unverified"
    ):
        raise ValueError("Failed-unit adoption proof integrity differs")
    task = next(t for t in (await deliveries.journal.plan(run["id"]))["tasks"] if t["id"] == "implement")
    before = [e for e in entries if e["sequence"] < event["sequence"]]
    if failed_unit_proof(run, task, before, candidate["proof"]["observation"]) != candidate["proof"]:
        raise ValueError("The adopted failed implementation unit changed")
    if writing:
        if actor is None or actor != payload["actor"] or clock_ms() >= payload["expires_at_ms"]:
            raise AccessDenied("The original failed-unit actor or finite consent is no longer current")
        saved = await GitFailedUnitAdoptions(deliveries)._authority(run, actor)
        if saved["digest"] != candidate["policy_digest"] or saved["policy"] != candidate["policy"]:
            raise ValueError("The adopted current project Git policy changed")
        if (
            verification_source(deliveries.workspace(run)) != candidate["source_digest"]
            or clock_ms() >= payload["expires_at_ms"]
        ):
            raise ValueError("The reviewed partial source or finite consent changed before Git delivery")
    return GitDeliveryPolicyV1.model_validate(candidate["policy"])
