# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Factory-owned Git checkpoints; the original baseline and unknown effects never reset."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from firefly_forge.git_delivery_models import GitDeliveriesViewV1, GitDeliveryPolicyV1, GitDeliveryViewV1
from firefly_forge.git_workspace import metadata_digest
from firefly_forge.scm import checked

Data = dict[str, Any]
KIND = "git_delivery"


def delivery_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def binding(run: Data) -> str:
    return delivery_digest(
        {
            key: run.get(key)
            for key in (
                "id",
                "repository_id",
                "project_id",
                "base_revision",
                "branch",
                "component_snapshot",
                "scm_snapshot",
                "config_revision",
                "execution_actor",
                "git_delivery_policy",
                "git_delivery_policy_digest",
                "git_failed_unit_adoption",
                "git_metadata_digest",
                "status",
                "active_task",
                "implementation_rounds",
                "implementation_pending",
                "worker_request",
                "provider_recovery",
                "bridge_completion_hold",
                "cancel_request",
                "checkpoint_revision",
            )
            if key != "git_failed_unit_adoption" or run.get(key) is not None
        }
    )


class GitDeliveries:
    def __init__(self, store: Any, engine: Any, journal: Any, *, workspace: Any = None, authorize: Any = None) -> None:
        self.store, self.engine, self.journal = store, engine, journal
        self.workspace = workspace or (lambda run: engine.workspaces / run["id"])
        self.authorization = authorize
        self.http_client = httpx.AsyncClient
        if not hasattr(engine, "git_delivery_locks"):
            engine.git_delivery_locks = {}
        self.locks: dict[str, asyncio.Lock] = engine.git_delivery_locks

    async def _effective_policy(self, run: Data) -> GitDeliveryPolicyV1:
        from firefly_forge.git_failed_unit_adoption import effective_policy

        return await effective_policy(self, run)

    async def candidate(self, run_id: str, *, actor: Data | None = None) -> Data:
        from firefly_forge.git_delivery_models import GitDeliveryCandidateV1
        from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
        from firefly_forge.verification_models import verification_source

        if actor is None:
            user = CURRENT_PRINCIPAL.get()
            if user is None:
                raise ValueError("Sign in before reviewing current checkpoint source")
            actor = ProjectAccess.actor_snapshot(user)
        run = await self.store.get_run(run_id)
        policy = GitDeliveryPolicyV1.model_validate(run.get("git_delivery_policy") or {})
        if not policy.enabled or run["status"] not in {"paused", "failed"} or run.get("git_failed_unit_adoption"):
            raise ValueError("Review a quiescent run with its original enabled checkpoint policy")
        await self._authorize(run, actor, "release" if policy.remote == "draft_pr" else "execute")
        active = self.engine.active.get(run_id)
        if (
            active is not None
            and not active.done()
            or run.get("implementation_pending")
            or run.get("bridge_completion_hold")
        ):
            raise ValueError("Wait for the current source owner to settle before reviewing a checkpoint")
        if self.authorization is None:
            from firefly_forge.git_delivery_policy import GitDeliveryPolicies

            saved = await GitDeliveryPolicies(self.store).snapshot(run.get("project_id"))
            tasks = (await self.journal.plan(run_id))["tasks"]
            if (
                not saved
                or not saved["policy"]["enabled"]
                or not any(task["id"] == "implement" and task["status"] in {"done", "paused"} for task in tasks)
            ):
                raise ValueError("The current checkpoint policy or closed implementation unit is unavailable")
        view = await self.view(run_id, actor=actor)
        root = self.workspace(run)
        head = view["latest_checkpoint"] or view["original_base_revision"]
        source = verification_source(root)
        if (
            await checked(["git", "rev-parse", "HEAD"], root, 10) != head
            or metadata_digest(root) != run.get("git_metadata_digest")
            or await self.store.get_run(run_id) != run
            or verification_source(root) != source
        ):
            raise ValueError("The current checkpoint source or Git identity changed during review")
        await self._authorize(run, actor, "release" if policy.remote == "draft_pr" else "execute")
        return GitDeliveryCandidateV1(run_id=run_id, expected_head=head, expected_source_digest=source).model_dump(
            mode="json"
        )

    async def _authorize(
        self, run: Data, actor: Data | None, permission: Literal["read", "execute", "release"]
    ) -> None:
        if self.authorization is not None:
            await self.authorization(run, actor, permission)
            return
        from firefly_forge.project_access import ProjectAccess

        if actor is None or not run.get("execution_actor"):
            raise ValueError("Intermediate Git delivery requires explicit current execution and delivery actors")
        await ProjectAccess(self.store).authorize_actor(actor, run.get("project_id"), permission)
        if permission != "read":
            await ProjectAccess(self.store).authorize_execution(run)
            await self.store.assert_campaign_current(run)

    async def _guard(self, run_id: str, expected: Data, actor: Data | None, *, boundary: object | None = None) -> Data:
        await self.store.assert_ownership()
        current = await self.store.get_run(run_id)
        from firefly_forge.git_failed_unit_adoption import effective_policy

        policy = await effective_policy(self, current, actor=actor, writing=True)
        if not policy.enabled or self.engine.stopping:
            raise ValueError("Intermediate Git delivery is disabled or the execution owner is draining")
        if self.authorization is None:
            from firefly_forge.git_delivery_policy import GitDeliveryPolicies

            saved = await GitDeliveryPolicies(self.store).snapshot(current.get("project_id"))
            if not saved or not GitDeliveryPolicyV1.model_validate(saved["policy"]).enabled:
                raise ValueError("Current project Git delivery consent is disabled")
            tasks = (await self.journal.plan(run_id))["tasks"]
            implementation = next((task for task in tasks if task["id"] == "implement"), None)
            if not current.get("git_failed_unit_adoption") and (
                implementation is None or implementation["status"] not in {"done", "paused"}
            ):
                raise ValueError("Git delivery requires a recorded closed implementation unit")
            if implementation is not None and implementation["status"] == "paused":
                pending = current.get("worker_request") or {}
                from pyfly.data.transaction import current_unit_of_work

                if current_unit_of_work() is None:
                    entries = await self._entries(run_id)
                else:
                    # Public Journal.entries may suspend its caller to admit protected content.
                    # The owned writer instead verifies native rows without exposing those payloads.
                    async with self.store._unit():
                        from firefly_forge.store import identifier

                        rows = await self.journal.records.find_for_run(identifier(run_id))
                    entries = [{**copy.deepcopy(row.data), "digest": row.digest} for row in rows]
                    if (
                        len(entries) > 10000
                        or len(json.dumps(entries).encode()) > 16 * 1024 * 1024
                        or not self.journal.verify_chain(entries)
                    ):
                        raise ValueError("Paused Git delivery requires intact bounded native journal rows")
                if pending.get("status") != "pending" or not any(
                    e["kind"] == "worker_feedback"
                    and (e["payload"].get("request") or {}).get("id") == pending.get("id")
                    for e in entries
                ):
                    raise ValueError("A paused checkpoint requires its exact durable typed worker question")
        active = self.engine.active.get(run_id)
        if active is not None and not active.done() and (active is not asyncio.current_task() or boundary is not self):
            raise ValueError("A coding worker still owns this workspace; wait for a completed unit")
        if boundary is not self and current["status"] not in {"paused", "failed"}:
            raise ValueError("Explicit checkpointing requires a known quiescent paused or failed run")
        if (
            current.get("bridge_completion_hold")
            or current.get("implementation_pending")
            or current.get("cancel_request", {}).get("status") == "requested"
            or (current.get("provider_recovery") or {}).get("status")
            in {"unknown", "executing", "needs_reconciliation"}
            or current["status"] in {"needs_reconciliation", "interrupted", "cancelled"}
        ):
            raise ValueError("An unknown or unsettled execution outcome blocks Git delivery")
        if self.engine.tool_broker and await self.engine.tool_broker.unresolved(run_id):
            raise ValueError("An unresolved tool outcome blocks Git delivery")
        await self._authorize(current, actor, "release" if policy.remote == "draft_pr" else "execute")
        if self.engine.connections:
            repository = await self.store.repository(current["repository_id"])
            self.engine.connections.assert_snapshot(repository, current.get("scm_snapshot"))
        await self.store.assert_ownership()
        final = await self.store.get_run(run_id)
        if binding(final) != binding(expected):
            raise ValueError("Run ownership, policy or source binding changed during Git admission")
        if self.engine.stopping:
            raise ValueError("The execution owner started draining before Git delivery")
        if final.get("git_failed_unit_adoption"):
            await effective_policy(self, final, actor=actor, writing=True)
        return dict(final)

    async def _entries(self, run_id: str) -> list[Data]:
        entries = await self.journal.entries(run_id)
        verifier = getattr(self.journal, "verify_chain", None)
        if (
            not callable(verifier)
            or len(entries) > 10000
            or len(json.dumps(entries).encode()) > 16 * 1024 * 1024
            or not verifier(entries)
        ):
            raise ValueError("The execution journal integrity is unavailable")
        return list(entries)

    @staticmethod
    def _records(entries: list[Data]) -> list[Data]:
        receipts: list[Data] = []
        for entry in entries:
            if entry["kind"] != "git_delivery_receipt":
                continue
            payload = entry["payload"]
            GitDeliveryViewV1.model_validate(
                {key: payload[key] for key in GitDeliveryViewV1.model_fields if key in payload}
            )
            expected = delivery_digest({k: v for k, v in payload.items() if k != "checkpoint_digest"})
            if payload["checkpoint_digest"] != expected or payload["sequence"] != len(receipts) + 1:
                raise ValueError("Checkpoint receipt digest or sequence differs")
            if receipts and (
                payload["parent_revision"] != receipts[-1]["revision"]
                or payload["original_base_revision"] != receipts[0]["original_base_revision"]
            ):
                raise ValueError("Checkpoint receipt no longer extends the immutable factory chain")
            intent = next(
                (
                    e
                    for e in entries
                    if e["kind"] == "git_delivery_intent" and e["payload"].get("request_id") == payload["request_id"]
                ),
                None,
            )
            if (
                intent is None
                or intent["sequence"] >= entry["sequence"]
                or any(
                    payload[k] != intent["payload"][k]
                    for k in (
                        "sequence",
                        "original_base_revision",
                        "parent_revision",
                        "source_tree",
                        "changed_files",
                        "patch_sha256",
                    )
                )
            ):
                raise ValueError("Checkpoint receipt has no exact earlier admitted intent")
            if (
                payload.get("source_evidence") is not None or intent["payload"].get("source_evidence") is not None
            ) and any(
                payload.get(key) != intent["payload"].get(key)
                for key in ("source_evidence", "changed_file_count", "changed_files_preview_truncated")
            ):
                raise ValueError("Checkpoint artifact receipt differs from its exact admitted intent")
            receipts.append(copy.deepcopy(payload))
        return receipts

    async def _append(self, run_id: str, kind: str, key: str, payload: Data) -> Data:
        return dict(await self.journal.git_delivery_append(run_id, kind, key, payload))

    @asynccontextmanager
    async def _owned(self, run_id: str, actor: Data | None) -> AsyncIterator[Any]:
        async with self.store.source_budget_admission_unit() as session:
            if self.authorization is None:
                from pyfly.data.relational.sqlalchemy import LockMode
                from sqlalchemy import text

                from firefly_forge.identity_persistence import ForgeIdentityUserRepository
                from firefly_forge.project_persistence import ForgeProjectRepository
                from firefly_forge.store import identifier

                run = await self.store.get_run(run_id)
                if actor is None or not run.get("execution_actor") or not run.get("project_id"):
                    raise ValueError("Checkpoint ownership requires current project and actor provenance")
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                    {"key": self.store.schema + ":members:" + run["project_id"]},
                )
                for user_id in sorted({actor["user_id"], run["execution_actor"]["user_id"]}):
                    user = await ForgeIdentityUserRepository().find_by_id(
                        identifier(user_id), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if user is None or not user.enabled:
                        raise ValueError("Checkpoint execution or delivery identity is no longer enabled")
                await ForgeProjectRepository().find_by_id(
                    identifier(run["project_id"]), lock=LockMode.PESSIMISTIC_WRITE
                )
                await self.journal._run(run_id)
            yield session

    async def checkpoint(
        self,
        run_id: str,
        *,
        request_id: str,
        expected_head: str,
        actor: Data | None,
        expected_source_tree: str | None = None,
        expected_source_digest: str | None = None,
        boundary: object | None = None,
        final: bool = False,
    ) -> Data:
        if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
            raise ValueError("Keep a canonical checkpoint request identity before admission")
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = await self.store.get_run(run_id)
            from firefly_forge.git_failed_unit_adoption import effective_policy

            policy = await effective_policy(self, run, actor=actor, writing=True)
            entries = await self._entries(run_id)
            receipts = self._records(entries)
            self._remote_state(entries)
            request_cut = {
                "request_id": request_id,
                "expected_head": expected_head,
                "expected_source_tree": expected_source_tree,
                "actor": actor,
            }
            if expected_source_digest is not None:
                request_cut["expected_source_digest"] = expected_source_digest
            request_digest = delivery_digest(request_cut)
            prior = next(
                (e for e in entries if e["kind"] == "git_delivery_intent" and e["payload"]["request_id"] == request_id),
                None,
            )
            if prior is not None:
                await self._authorize(run, actor, "read")
                if prior["payload"]["request_digest"] != request_digest:
                    raise ValueError("Checkpoint request identity belongs to another source or actor")
                retained = next((r for r in receipts if r["request_id"] == request_id), None)
                if retained is None:
                    raise ValueError("Checkpoint intent has an unknown outcome; inspect it instead of replaying Git")
                return dict(
                    next(r for r in (await self.view(run_id, actor=actor))["items"] if r["request_id"] == request_id)
                )
            if any(
                e["kind"] == "git_delivery_intent"
                and not any(r["request_id"] == e["payload"]["request_id"] for r in receipts)
                for e in entries
            ):
                raise ValueError("A prior checkpoint has an unknown outcome; no new request may bypass it")
            if len(receipts) >= policy.max_checkpoints - (0 if final else 1):
                raise ValueError("The finite Git checkpoint limit is exhausted")
            await self._guard(run_id, run, actor, boundary=boundary)
            workspace = self.workspace(run)
            base = run.get("base_revision")
            parent = receipts[-1]["revision"] if receipts else base
            if (
                not base
                or expected_head != parent
                or await checked(["git", "rev-parse", "HEAD"], workspace, 10) != parent
            ):
                raise ValueError("Workspace HEAD differs from the original baseline or last factory checkpoint")
            if await checked(["git", "symbolic-ref", "--short", "HEAD"], workspace, 10) != run["branch"]:
                raise ValueError("Workspace branch differs from the admitted factory implementation branch")
            if receipts and any(r["original_base_revision"] != base for r in receipts):
                raise ValueError("The original Git baseline changed")
            metadata = metadata_digest(workspace)
            if metadata != run.get("git_metadata_digest"):
                raise ValueError("Protected Git metadata changed before checkpointing")
            settings = (
                self.store.configuration.load_snapshot(run["config_revision"]) if run.get("config_revision") else {}
            )
            from firefly_forge.verification_models import verification_source

            if expected_source_digest is not None and verification_source(workspace) != expected_source_digest:
                raise ValueError("The reviewed current source changed before checkpointing")
            from firefly_forge.git_diff_artifacts import read_diff_index_page
            from firefly_forge.git_diff_models import GitDiffBudget
            from firefly_forge.source_snapshot import SourceDiffSnapshotV1, diff_artifact_root

            budget = policy.diff_budget or GitDiffBudget()
            _, snapshot, tree = await self.engine._snapshot(
                workspace, parent, secrets=settings, expected_head=parent, budget=budget
            )
            if not isinstance(snapshot, SourceDiffSnapshotV1):
                raise ValueError("Checkpointing requires retained typed source diff evidence")
            artifact = snapshot.artifact
            if artifact.file_count == 0 or artifact.byte_count == 0:
                raise ValueError("Checkpointing requires a nonempty source change")
            if expected_source_tree is not None and tree != expected_source_tree:
                raise ValueError("The reviewed checkpoint source tree changed")
            if expected_source_digest is not None and verification_source(workspace) != expected_source_digest:
                raise ValueError("The reviewed current source changed while preparing its checkpoint tree")
            component = (run.get("component_snapshot") or {}).get("path", ".")
            artifactroot = diff_artifact_root(self.engine.workspaces.resolve(), run_id)
            files: list[str] = []
            count = 0
            async with asyncio.timeout(budget.timeout_seconds):
                for page in range(artifact.index_page_count):
                    index = await asyncio.to_thread(read_diff_index_page, artifactroot, artifact, page)
                    for item in index.files:
                        if component != "." and not item.path.startswith(component + "/"):
                            raise ValueError("Checkpoint source leaves the selected component")
                        if len(files) < 100:
                            files.append(item.path)
                        count += 1
            if count != artifact.file_count:
                raise ValueError("Checkpoint file evidence differs from its retained manifest")
            await self.store.admit_credentials(request_cut)
            await self._guard(run_id, run, actor, boundary=boundary)
            evidence = {"version": 2, "diff_artifact": artifact.model_dump(mode="json")}
            preview_truncated = len(files) < count
            intent = {
                **request_cut,
                "request_digest": request_digest,
                "sequence": len(receipts) + 1,
                "original_base_revision": base,
                "parent_revision": parent,
                "source_tree": tree,
                "patch_sha256": artifact.sha256,
                "source_evidence": evidence,
                "changed_file_count": count,
                "changed_files": files,
                "changed_files_preview_truncated": preview_truncated,
                "binding": binding(run),
            }
            key = "git:" + request_id
            async with self._owned(run_id, actor):
                await self._guard(run_id, run, actor, boundary=boundary)
                await self._append(run_id, "git_delivery_intent", key, intent)
            async with self._owned(run_id, actor):
                await self._guard(run_id, run, actor, boundary=boundary)
                if (
                    metadata_digest(workspace) != metadata
                    or await checked(["git", "rev-parse", "HEAD"], workspace, 10) != parent
                    or await checked(["git", "write-tree"], workspace, 10) != tree
                    or await checked(["git", "symbolic-ref", "--short", "HEAD"], workspace, 10) != run["branch"]
                ):
                    raise ValueError("Checkpoint Git state changed after its durable intent")
                message = (
                    f"Checkpoint {run.get('active_task', 'implement')} unit {run.get('implementation_rounds', 1)} "
                    f"for forge run {run_id}"
                )
                if run.get("git_failed_unit_adoption"):
                    message = "[UNVERIFIED] " + message
                await checked(
                    [
                        "git",
                        "-c",
                        "user.name=firefly forge",
                        "-c",
                        "user.email=forge@getfirefly.io",
                        "commit",
                        "-m",
                        message,
                    ],
                    workspace,
                    15,
                )
                revision = await checked(["git", "rev-parse", "HEAD"], workspace, 10)
                if (
                    await checked(["git", "rev-parse", "HEAD^{tree}"], workspace, 10) != tree
                    or await checked(["git", "rev-parse", "HEAD^"], workspace, 10) != parent
                ):
                    raise ValueError("The committed checkpoint does not extend the exact admitted source")
                await self._guard(run_id, run, actor, boundary=boundary)
                receipt = {
                    "version": 1,
                    "run_id": run_id,
                    "request_id": request_id,
                    "sequence": len(receipts) + 1,
                    "original_base_revision": base,
                    "parent_revision": parent,
                    "revision": revision,
                    "source_tree": tree,
                    "task_id": run.get("active_task") or "implement",
                    "attempt": max(1, run.get("implementation_rounds", 1)),
                    "changed_file_count": count,
                    "changed_files": files,
                    "changed_files_preview_truncated": preview_truncated,
                    "source_evidence": evidence,
                    "patch_sha256": intent["patch_sha256"],
                    "branch": run["branch"],
                    "local_status": "checkpointed",
                    "remote_status": "not_requested",
                    "pr_url": None,
                    "created_at": datetime.now(UTC).isoformat(),
                }
                if run.get("git_failed_unit_adoption"):
                    receipt["verification_status"] = "partial_unverified"
                receipt["checkpoint_digest"] = delivery_digest(receipt)
                await self._append(run_id, "git_delivery_receipt", key, receipt)
                await self.store.update(run_id, checkpoint_revision=revision, git_delivery_head=revision)
            await self._activity(run_id, receipt)
            if policy.remote == "draft_pr" and not final:
                await self._publish_checkpoint(run_id, receipt, actor, boundary)
                return dict((await self.view(run_id, actor=actor))["items"][-1])
            return receipt

    @staticmethod
    def _remote_state(entries: list[Data], *, allow_pending: bool = False) -> Data | None:
        last = None
        for e in entries:
            if e["kind"] == "git_delivery_remote_intent":
                match = next(
                    (
                        r
                        for r in entries
                        if r["kind"] == "git_delivery_remote_receipt" and r["idempotency_key"] == e["idempotency_key"]
                    ),
                    None,
                )
                if match is None:
                    if allow_pending:
                        continue
                    raise ValueError(
                        "An intermediate remote outcome is unknown; reconcile by read-only observation, never replay"
                    )
                if match["sequence"] <= e["sequence"] or match["payload"].get("intent_digest") != e["digest"]:
                    raise ValueError("Remote checkpoint receipt custody differs")
                payload = match["payload"]
                if (
                    payload.get("revision") != e["payload"]["revision"]
                    or payload.get("request_id") != e["payload"]["request_id"]
                    or payload.get("remote_status") not in {"draft", "ready"}
                    or payload.get("reference", {}).get("body_sha256") != e["payload"]["body_sha256"]
                ):
                    raise ValueError("Remote checkpoint receipt does not confirm the admitted source/body")
                last = match["payload"]
        return last

    async def view(self, run_id: str, *, actor: Data | None = None) -> Data:
        if actor is None:
            from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess

            user = CURRENT_PRINCIPAL.get()
            if user is None:
                raise ValueError("Sign in to inspect Git delivery evidence")
            actor = ProjectAccess.actor_snapshot(user)
        run = await self.store.get_run(run_id)
        await self._authorize(run, actor, "read")
        entries = await self._entries(run_id)
        receipts = self._records(entries)
        self._remote_state(entries, allow_pending=True)
        items = []
        for receipt in receipts:
            value = {key: receipt[key] for key in GitDeliveryViewV1.model_fields if key in receipt}
            remote = [
                e
                for e in entries
                if e["kind"] == "git_delivery_remote_receipt"
                and e["payload"].get("request_id") == receipt["request_id"]
            ]
            if remote:
                value.update(
                    remote_status=remote[-1]["payload"]["remote_status"],
                    pr_url=remote[-1]["payload"]["reference"]["url"],
                )
            elif any(
                e["kind"] == "git_delivery_remote_intent" and e["payload"].get("request_id") == receipt["request_id"]
                for e in entries
            ):
                value["remote_status"] = "unknown"
            items.append(value)
        return GitDeliveriesViewV1(
            version=1,
            run_id=run_id,
            policy=await self._effective_policy(run),
            original_base_revision=run.get("base_revision") or None,
            latest_checkpoint=receipts[-1]["revision"] if receipts else None,
            items=[GitDeliveryViewV1.model_validate(item) for item in items],
        ).model_dump(mode="json")

    async def observations(self, run_id: str, request_id: str) -> Data:
        from firefly_forge.git_delivery_remote import DraftPullRequests
        from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
        from firefly_forge.store import identifier

        identifier(request_id)
        user = CURRENT_PRINCIPAL.get()
        if user is None:
            raise ValueError("Sign in to inspect an ambiguous Git delivery")
        actor = ProjectAccess.actor_snapshot(user)
        run = await self.store.get_run(run_id)
        await self._authorize(run, actor, "read")
        entries = await self._entries(run_id)
        intent = next(
            (e for e in entries if e["kind"] == "git_delivery_intent" and e["payload"].get("request_id") == request_id),
            None,
        )
        if intent is None:
            raise KeyError("Git delivery intent not found")
        workspace = self.workspace(run)
        if metadata_digest(workspace) != run.get("git_metadata_digest"):
            raise ValueError("Protected Git metadata changed; refuse observation through that workspace")
        head = await checked(["git", "rev-parse", "HEAD"], workspace, 10)
        tree = await checked(["git", "rev-parse", "HEAD^{tree}"], workspace, 10)
        parents = (await checked(["git", "show", "-s", "--format=%P", "HEAD"], workspace, 10)).split()
        result = {
            "version": 1,
            "run_id": run_id,
            "request_id": request_id,
            "intent_digest": intent["digest"],
            "observed_head": head,
            "observed_tree": tree,
            "matches_admitted_parent_and_tree": parents == [intent["payload"]["parent_revision"]]
            and tree == intent["payload"]["source_tree"],
            "local_receipt_recorded": any(r["request_id"] == request_id for r in self._records(entries)),
            "remote": None,
            "effects_performed": False,
            "automatic_replay_allowed": False,
        }
        remote = [
            e
            for e in entries
            if e["kind"] == "git_delivery_remote_intent" and e["payload"].get("request_id") == request_id
        ]
        if remote:
            await self._authorize(run, actor, "release")
            repository = await self.store.repository(run["repository_id"])
            if not self.engine.connections or not repository.get("connection_provider"):
                raise ValueError("Read-only remote reconciliation requires the current explicit repository connection")
            self.engine.connections.assert_snapshot(repository, run.get("scm_snapshot"))
            authentication = await self.engine.connections.authentication(repository)
            environment = await self.engine.connections.git_environment(repository)
            observed = await self._remote_revision(repository, run["branch"], environment)
            payload = remote[-1]["payload"]
            reference = None
            if observed is not None and observed == payload["revision"]:
                async with self.http_client(follow_redirects=False) as client:
                    try:
                        reference = await DraftPullRequests(client, repository, authentication).find(
                            run["branch"], observed, draft=payload.get("purpose") != "verified_ready_for_review"
                        )
                    except ValueError:
                        reference = None
            result["remote"] = {
                "observed_revision": observed,
                "reference": reference,
                "matches_admitted_body": bool(reference and reference["body_sha256"] == payload["body_sha256"]),
                "intent_digest": remote[-1]["digest"],
            }
            await self.engine.connections.authentication(repository)
        await self._authorize(run, actor, "read")
        if CURRENT_PRINCIPAL.get() != user or binding(await self.store.get_run(run_id)) != binding(run):
            raise ValueError("Git delivery scope changed during read-only observation")
        return result

    async def _remote_credentials(
        self, run: Data, actor: Data | None, boundary: object | None
    ) -> tuple[Data, tuple[str, str], Data]:
        from firefly_forge.git_delivery_remote import draft_capability

        await self._guard(run["id"], run, actor, boundary=boundary)
        repository = await self.store.repository(run["repository_id"])
        if (
            not draft_capability(repository["provider"])
            or not repository.get("connection_provider")
            or not self.engine.connections
        ):
            raise ValueError("This checkpoint requires a connected provider with a qualified draft lifecycle")
        authentication = await self.engine.connections.authentication(repository)
        environment = await self.engine.connections.git_environment(repository)
        await self._guard(run["id"], run, actor, boundary=boundary)
        return repository, authentication, environment

    async def _remote_revision(self, repository: Data, branch: str, environment: Data) -> str | None:
        from firefly_forge.scm import git_credentials

        async with git_credentials(environment) as configured:
            value = await checked(
                ["git", "ls-remote", "--", repository["url"], "refs/heads/" + branch],
                self.engine.workspaces,
                30,
                env=configured,
            )
        if not value:
            return None
        parts = value.split("\t")
        if (
            len(parts) != 2
            or parts[1] != "refs/heads/" + branch
            or len(parts[0]) not in {40, 64}
            or any(c not in "0123456789abcdef" for c in parts[0])
        ):
            raise ValueError("Remote branch observation is ambiguous")
        return parts[0]

    async def _assert_checkpoint_source(self, run: Data, receipt: Data) -> None:
        workspace = self.workspace(run)
        if (
            metadata_digest(workspace) != run.get("git_metadata_digest")
            or await checked(["git", "rev-parse", "HEAD"], workspace, 10) != receipt["revision"]
            or await checked(["git", "rev-parse", "HEAD^{tree}"], workspace, 10) != receipt["source_tree"]
            or await checked(["git", "status", "--porcelain", "--untracked-files=all"], workspace, 10)
        ):
            raise ValueError("Checkpoint source or Git identity changed during remote delivery")

    async def _publish_checkpoint(
        self, run_id: str, receipt: Data, actor: Data | None, boundary: object | None
    ) -> None:
        from firefly_forge.communication import CommunicationGitDeliveryV1
        from firefly_forge.git_delivery_remote import DraftPullRequests
        from firefly_forge.scm import push

        run = await self.store.get_run(run_id)
        entries = await self._entries(run_id)
        previous = self._remote_state(entries)
        repository, authentication, environment = await self._remote_credentials(run, actor, boundary)
        context = CommunicationGitDeliveryV1(
            checkpoint_revision=receipt["revision"],
            source_tree=receipt["source_tree"],
            sequence=receipt["sequence"],
            changed_file_count=receipt["changed_file_count"],
            implementation_disposition="failed" if run.get("git_failed_unit_adoption") else None,
        )
        await self.store.admit_credentials(context.model_dump())
        native = (
            f"### Intermediate checkpoint {receipt['sequence']}\n\n"
            "The factory retained this implementation checkpoint. Verification and independent review are pending."
        )
        drafted = await self.engine.communication_writer.write(run, native, lambda text: text, git_delivery=context)
        evidence = drafted["communication"]
        async with self._owned(run_id, actor):
            await self._guard(run_id, run, actor, boundary=boundary)
            await self._append(
                run_id,
                "git_delivery_draft",
                "git-draft:" + receipt["request_id"],
                {
                    "request_id": receipt["request_id"],
                    "body": drafted.get("body", "") if evidence.get("mode") != "held" else "",
                    "communication": evidence,
                },
            )
        if evidence.get("mode") == "held" or not drafted.get("body"):
            raise ValueError("Selected checkpoint communication is held; no branch or draft PR was published")
        body = drafted["body"] + f"\n\n<!-- forge-git-delivery:{run_id} -->"
        title = drafted["body"].splitlines()[0].removeprefix("### ")[:100]
        if run.get("git_failed_unit_adoption"):
            title = "[UNVERIFIED] " + title[:87]
        await self.store.admit_credentials({"title": title, "body": body, "communication": evidence})
        await self._guard(run_id, run, actor, boundary=boundary)
        self.engine.communication_writer.assert_current(evidence)
        existing = await self._remote_revision(repository, run["branch"], environment)
        expected = previous["revision"] if previous else None
        if existing != expected:
            raise ValueError(
                "Remote branch differs from the last confirmed factory delivery; do not overwrite or adopt it"
            )
        key = "git-remote:" + receipt["request_id"]
        payload = {
            "request_id": receipt["request_id"],
            "revision": receipt["revision"],
            "checkpoint_digest": receipt["checkpoint_digest"],
            "branch": run["branch"],
            "repository_id": run["repository_id"],
            "repository_url": repository["url"],
            "prior_reference": previous["reference"] if previous else None,
            "title": title,
            "body": body,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "communication": evidence,
            "actor": actor,
        }
        async with self._owned(run_id, actor):
            await self._guard(run_id, run, actor, boundary=boundary)
            intent = await self._append(run_id, "git_delivery_remote_intent", key, payload)
            await self.store.update(
                run_id,
                git_delivery_summary={
                    "revision": receipt["revision"],
                    "source_tree": receipt["source_tree"],
                    "sequence": receipt["sequence"],
                    "remote_status": "publishing",
                    "pr_url": previous["reference"]["url"] if previous else None,
                },
            )

        # The durable intent remains unresolved on cancellation, transport loss or malformed success.
        async def guard() -> None:
            await self._assert_checkpoint_source(run, receipt)
            async with self._owned(run_id, actor):
                await self._guard(run_id, run, actor, boundary=boundary)
                self.engine.communication_writer.assert_current(evidence)

        await guard()
        await push(
            self.workspace(run),
            run["branch"],
            env=environment,
            repository_url=repository["url"],
            revision=receipt["revision"],
            expected_remote_revision=expected,
        )
        if await self._remote_revision(repository, run["branch"], environment) != receipt["revision"]:
            raise ValueError("The pushed branch has an unconfirmed revision")

        async with self.http_client(follow_redirects=False) as client:
            provider = DraftPullRequests(client, repository, authentication, guard=guard)
            if previous:
                reference = await provider.update(
                    previous["reference"], run["branch"], receipt["revision"], title, body
                )
            else:
                if await provider.find(run["branch"], receipt["revision"]) is not None:
                    raise ValueError("An unowned draft already exists for this branch; do not create or update another")
                reference = await provider.create(run["branch"], receipt["revision"], title, body)
        if reference["body_sha256"] != payload["body_sha256"]:
            raise ValueError("Draft response does not confirm the exact admitted body")
        await guard()
        async with self._owned(run_id, actor):
            await self._guard(run_id, run, actor, boundary=boundary)
            self.engine.communication_writer.assert_current(evidence)
            result = {
                "request_id": receipt["request_id"],
                "revision": receipt["revision"],
                "reference": reference,
                "remote_status": "draft",
                "intent_digest": intent["digest"],
            }
            await self._append(run_id, "git_delivery_remote_receipt", key, result)
            await self.store.update(run_id, git_delivery_draft=reference)
        await self._activity(run_id, {**receipt, "remote_status": "draft", "pr_url": reference["url"]})

    async def _activity(self, run_id: str, receipt: Data) -> None:
        summary = {key: receipt[key] for key in ("revision", "source_tree", "sequence", "remote_status", "pr_url")}
        async with self._owned(run_id, (await self.store.get_run(run_id)).get("execution_actor")):
            current = await self.store.get_run(run_id)
            if (
                current.get("checkpoint_revision") != receipt["revision"]
                or (current.get("git_delivery_summary") or {}).get("sequence", 0) > receipt["sequence"]
            ):
                return
            await self.store.update(run_id, git_delivery_summary=summary)
        if callable(event := getattr(self.store, "event", None)):
            await event(
                run_id,
                "git_delivery",
                f"Factory checkpoint {summary['sequence']}: {summary['revision']} ({summary['remote_status']})"
                + (f" {summary['pr_url']}" if summary["pr_url"] else ""),
            )

    async def hold_paused(self, expected: Data, error_type: str) -> None:
        hold = {
            "status": "held",
            "question_id": expected["worker_request"]["id"],
            "attempt": expected.get("implementation_rounds", 0),
            "error_type": error_type[:100],
            "reason": "The automatic Git checkpoint did not confirm. Inspect its receipt before resuming.",
        }
        # Retaining a refusal grants no Git authority; it still requires the current physical owner.
        async with self.store.source_budget_admission_unit():
            await self.store.assert_ownership()
            await self.journal._run(expected["id"])
            current = await self.store.get_run(expected["id"])
            if (
                current["status"] != "paused"
                or current.get("worker_request") != expected.get("worker_request")
                or current.get("execution_actor") != expected.get("execution_actor")
                or current.get("implementation_rounds") != expected.get("implementation_rounds")
            ):
                raise ValueError("The worker question changed before its Git hold could be retained")
            key = uuid.uuid5(uuid.NAMESPACE_URL, f"forge:git-hold:{expected['id']}:{hold['question_id']}").hex
            record = await self._append(expected["id"], "git_delivery_hold", key, hold)
            await self.store.update(expected["id"], git_delivery_hold=record["payload"])

    async def automatic(self, run_id: str, *, paused: bool = False) -> Data | None:
        run = await self.store.get_run(run_id)
        policy = GitDeliveryPolicyV1.model_validate(run.get("git_delivery_policy") or {})
        if not policy.enabled or (paused and not policy.on_pause):
            return None
        receipts = self._records(await self._entries(run_id))
        if len(receipts) >= policy.max_checkpoints - 1:
            return None  # The finite bound reserves the final verified checkpoint.
        units = int(run.get("implementation_rounds", 0))
        if not paused and (units < 1 or units % policy.every_units):
            return None
        parent = receipts[-1]["revision"] if receipts else run["base_revision"]
        # A closed unit with no new staged tree does not create an empty checkpoint.
        await self._guard(run_id, run, run.get("execution_actor"), boundary=self)
        from firefly_forge.git_diff_models import GitDiffBudget

        _files, _diff, tree = await self.engine._snapshot(
            self.workspace(run),
            parent,
            allow_unchanged=True,
            expected_head=parent,
            budget=policy.diff_budget or GitDiffBudget(),
        )
        if tree == await checked(["git", "rev-parse", "HEAD^{tree}"], self.workspace(run), 10):
            return None
        request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"forge:git-delivery:{run_id}:{parent}:{tree}").hex
        return await self.checkpoint(
            run_id,
            request_id=request_id,
            expected_head=parent,
            expected_source_tree=tree,
            actor=run.get("execution_actor"),
            boundary=self,
        )

    async def assert_continuable(self, run: Data) -> None:
        if not run.get("git_delivery_policy", {}).get("enabled"):
            return
        entries = await self._entries(run["id"])
        receipts = self._records(entries)
        self._remote_state(entries)
        completed = {r["request_id"] for r in receipts}
        if any(e["kind"] == "git_delivery_intent" and e["payload"]["request_id"] not in completed for e in entries):
            raise ValueError("Retained Git commit outcome is unknown; coding cannot resume across it")
        if receipts and (
            receipts[-1]["revision"] != run.get("checkpoint_revision")
            or receipts[0]["original_base_revision"] != run.get("base_revision")
        ):
            raise ValueError("Retained factory checkpoint head or original baseline changed")
        if not receipts and run.get("checkpoint_revision"):
            raise ValueError("An unrecorded checkpoint head cannot become this run's factory baseline")

    async def final_start(self, run: Data, actor: Data | None, draft: Data) -> Data:
        if run.get("git_failed_unit_adoption"):
            raise ValueError("An adopted failed checkpoint remains unverified; it cannot be promoted to final delivery")
        from firefly_forge.git_delivery_remote import DraftPullRequests

        await self.engine._assert_delivery_tree(run["id"], self.workspace(run))
        entries = await self._entries(run["id"])
        previous = self._remote_state(entries)
        repository, authentication, environment = await self._remote_credentials(run, actor, self)
        expected = previous["revision"] if previous else None
        if await self._remote_revision(repository, run["branch"], environment) != expected:
            raise ValueError("Remote branch differs from its confirmed intermediate checkpoint")
        if previous:
            if not isinstance(expected, str):
                raise ValueError("The retained remote checkpoint lacks an exact revision")
            async with self.http_client(follow_redirects=False) as client:
                await DraftPullRequests(client, repository, authentication).read(
                    previous["reference"]["url"], run["branch"], expected
                )
        receipts = self._records(entries)
        if not receipts or receipts[-1]["revision"] != run["checkpoint_revision"]:
            raise ValueError("Final publication must name the exact retained factory checkpoint")
        receipt = receipts[-1]
        key = "git-ready:" + receipt["request_id"]
        payload = {
            "request_id": receipt["request_id"],
            "revision": receipt["revision"],
            "checkpoint_digest": receipt["checkpoint_digest"],
            "branch": run["branch"],
            "repository_id": run["repository_id"],
            "prior_reference": previous["reference"] if previous else None,
            "title": draft["title"],
            "body": draft["body"],
            "body_sha256": draft["body_sha256"],
            "actor": actor,
            "purpose": "verified_ready_for_review",
        }
        async with self._owned(run["id"], actor):
            await self._guard(run["id"], run, actor, boundary=self)
            intent = await self._append(run["id"], "git_delivery_remote_intent", key, payload)
        return {
            "intent": intent,
            "reference": previous["reference"] if previous else None,
            "authentication": authentication,
            "environment": environment,
            "repository": repository,
            "run": run,
            "actor": actor,
            "checkpoint": receipt,
        }

    async def _assert_publication_binding(self, run: Data, draft: Data) -> None:
        repository = await self.store.repository(run["repository_id"])
        current = await self.store.get_run(run["id"])
        if self.engine._publication_binding(current, repository) != draft["binding_sha256"]:
            raise ValueError("Publication facts or destination changed after drafting")
        self.engine.communication_writer.assert_current(draft["communication"])

    async def final_ready(self, prepared: Data, draft: Data) -> str:
        from firefly_forge.git_delivery_remote import DraftPullRequests

        run, actor = prepared["run"], prepared["actor"]
        if run.get("git_failed_unit_adoption"):
            raise ValueError("An adopted failed checkpoint remains unverified; it cannot be marked ready")
        intent = prepared["intent"]

        async def guard() -> None:
            await self._assert_checkpoint_source(run, prepared["checkpoint"])
            await self.engine._assert_publication_current(run["id"], draft, actor)
            async with self._owned(run["id"], actor):
                await self._guard(run["id"], run, actor, boundary=self)
                await self._assert_publication_binding(run, draft)

        await guard()
        async with self.http_client(follow_redirects=False) as client:
            provider = DraftPullRequests(client, prepared["repository"], prepared["authentication"], guard=guard)
            if prepared["reference"]:
                reference = await provider.update(
                    prepared["reference"], run["branch"], run["checkpoint_revision"], draft["title"], draft["body"]
                )
            else:
                if await provider.find(run["branch"], run["checkpoint_revision"]) is not None:
                    raise ValueError("An unowned draft exists; final publication cannot adopt or duplicate it")
                reference = await provider.create(
                    run["branch"], run["checkpoint_revision"], draft["title"], draft["body"]
                )
            if reference["body_sha256"] != draft["body_sha256"]:
                raise ValueError("The final draft body has an unknown provider outcome")
            reference = await provider.ready(reference, run["branch"], run["checkpoint_revision"])
        await guard()
        async with self._owned(run["id"], actor):
            await self._guard(run["id"], run, actor, boundary=self)
            await self._assert_publication_binding(run, draft)
            result = {
                "request_id": intent["payload"]["request_id"],
                "revision": run["checkpoint_revision"],
                "reference": reference,
                "remote_status": "ready",
                "intent_digest": intent["digest"],
            }
            await self._append(run["id"], "git_delivery_remote_receipt", intent["idempotency_key"], result)
        await self._activity(
            run["id"], {**prepared["checkpoint"], "remote_status": "ready", "pr_url": reference["url"]}
        )
        return str(reference["url"])
