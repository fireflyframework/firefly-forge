# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Server-resolved checkpoint source for a fresh nonpublishing admission."""

from __future__ import annotations

import copy
from typing import Any, cast

from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import select

from firefly_forge.checkpoint_test_models import ResolvedCheckpointV1
from firefly_forge.git_deliveries import GitDeliveries, delivery_digest
from firefly_forge.models import RunInput
from firefly_forge.persistence import ForgeJournalEntity
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm import source_revision
from firefly_forge.store import identifier

Data = dict[str, Any]


def parent_scope(run: Data) -> str:
    return delivery_digest(
        {
            key: run.get(key)
            for key in (
                "id",
                "repository_id",
                "project_id",
                "component_id",
                "component_snapshot",
                "scm_snapshot",
                "branch",
                "base_revision",
                "issue_context",
            )
        }
    )


class CheckpointTestSources:
    def __init__(self, deliveries: GitDeliveries) -> None:
        self.deliveries = deliveries
        self.store = deliveries.store

    @staticmethod
    def _actor() -> Any:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None or actor.authentication_method not in {"oidc_session", "oidc_bearer", "local_bootstrap"}:
            raise AccessDenied("Checkpoint source admission requires a current native authenticated user")
        return actor

    async def _authorize(self, run: Data, actor: Any) -> None:
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("Checkpoint source admission identity changed")
        access = ProjectAccess(self.store)
        await self.deliveries._authorize(run, access.actor_snapshot(actor), "read")
        await access.authorize_actor(access.actor_snapshot(actor), run["project_id"], "execute")
        await access.require_repository(actor, run["repository_id"], "execute")
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("Checkpoint source admission identity changed")

    @staticmethod
    def _scope(request: RunInput, run: Data) -> None:
        if (
            request.source_checkpoint is None
            or request.operation not in {"test", "maintain"}
            or request.publish is not False
            or any(run.get(key) != getattr(request, key) for key in ("repository_id", "project_id", "component_id"))
        ):
            raise ValueError("The checkpoint must belong to the original repository and project component")
        if request.issue_context is not None and request.issue_context != run.get("issue_context"):
            raise ValueError("Checkpoint issue provenance must match its retained parent")

    async def _head(self, run_id: str) -> str | None:
        async with self.store._unit() as session:
            return cast(
                str | None,
                await session.scalar(
                    select(ForgeJournalEntity.digest)
                    .where(ForgeJournalEntity.run_id == identifier(run_id))
                    .order_by(ForgeJournalEntity.sequence.desc())
                    .limit(1)
                ),
            )

    async def _observe_revision(self, repository: Data, parent: Data, receipt: Data) -> str:
        connections = self.deliveries.engine.connections
        if connections is None:
            raise ValueError("Current SCM source access is unavailable")
        connections.assert_snapshot(repository, parent.get("scm_snapshot"))
        environment = await connections.git_environment(repository)
        revision = await source_revision({**repository, "default_branch": receipt["branch"]}, env=environment)
        if revision != receipt["revision"]:
            raise ValueError("The remote checkpoint source changed or is unavailable")
        connections.assert_snapshot(repository, parent.get("scm_snapshot"))
        return revision

    async def prepare(self, request: RunInput) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Inspect checkpoint source outside the native transaction")
        request = RunInput.model_validate(request.model_dump(mode="json"))
        reference = request.source_checkpoint
        if reference is None:
            raise ValueError("Select an exact retained checkpoint")
        actor = self._actor()
        run = await self.store.get_run(reference.run_id)
        self._scope(request, run)
        await self._authorize(run, actor)
        entries = await self.deliveries._entries(run["id"])
        receipts = self.deliveries._records(entries)
        self.deliveries._remote_state(entries)
        selected = [r for r in receipts if r["request_id"] == reference.request_id]
        if len(selected) != 1:
            raise ValueError("The selected checkpoint receipt is unavailable")
        receipt = selected[0]
        if (
            receipt["run_id"] != run["id"]
            or receipt["checkpoint_digest"] != reference.expected_checkpoint_digest
            or receipt["revision"] != reference.expected_revision
            or receipt["branch"] != run["branch"]
        ):
            raise ValueError("The selected checkpoint receipt changed")
        remote = [
            e
            for e in entries
            if e["kind"] == "git_delivery_remote_receipt"
            and e["payload"].get("request_id") == reference.request_id
            and e["payload"].get("revision") == receipt["revision"]
            and e["payload"].get("remote_status") in {"draft", "ready"}
        ]
        if not remote:
            raise ValueError("Checkpoint source requires a confirmed remote checkpoint receipt")
        repository = await self.store.repository(request.repository_id)
        observed = await self._observe_revision(repository, run, receipt)
        if observed != receipt["revision"]:
            raise ValueError("The observed checkpoint revision differs from its receipt")
        await self._authorize(run, actor)
        current = await self.store.get_run(run["id"])
        if parent_scope(current) != parent_scope(run) or await self._head(run["id"]) != entries[-1]["digest"]:
            raise ValueError("Checkpoint provenance changed during source inspection")
        self._scope(request, current)
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("Checkpoint source admission identity changed")
        value = dict(
            version=1,
            reference=reference.model_dump(),
            repository_id=request.repository_id,
            project_id=request.project_id,
            component_id=request.component_id,
            revision=receipt["revision"],
            source_tree=receipt["source_tree"],
            branch=receipt["branch"],
            remote_receipt_digest=remote[-1]["digest"],
            journal_head=entries[-1]["digest"],
            parent_scope_digest=parent_scope(run),
        )
        if request.operation != "test":
            value["operation"] = request.operation
        value["digest"] = delivery_digest(value)
        return ResolvedCheckpointV1.model_validate(value).model_dump(mode="json")

    async def revalidate_locked(self, request: RunInput, snapshot: Data) -> None:
        if current_unit_of_work() is None:
            raise ValueError("Revalidate checkpoint source in the native transaction")
        value = ResolvedCheckpointV1.model_validate(snapshot).model_dump(mode="json")
        if value["digest"] != delivery_digest({k: v for k, v in value.items() if k != "digest"}):
            raise ValueError("Resolved checkpoint source integrity failed")
        if (
            request.source_checkpoint is None
            or request.source_checkpoint.model_dump() != value["reference"]
            or request.operation != value.get("operation", "test")
            or request.publish is not False
            or any(getattr(request, key) != value[key] for key in ("repository_id", "project_id", "component_id"))
        ):
            raise ValueError("Resolved checkpoint no longer matches the requested source")
        actor = self._actor()
        row = await self.store.run_records.find_by_id(
            identifier(value["reference"]["run_id"]), lock=LockMode.PESSIMISTIC_WRITE
        )
        if row is None:
            raise ValueError("The source run is unavailable")
        run = copy.deepcopy(row.data)
        self._scope(request, run)
        await self._authorize(run, actor)
        if parent_scope(run) != value["parent_scope_digest"] or await self._head(run["id"]) != value["journal_head"]:
            raise ValueError("Checkpoint provenance changed after preparation")
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("Checkpoint source admission identity changed")
