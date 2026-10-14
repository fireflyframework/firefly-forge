# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One-shot flycanon projections with content-bound observations and explicit recovery."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
from flycanon_sdk import CanonAPIError, CanonError, SourceMetadata, SubmitSourceJsonPayload, WorkspaceCreate  # type: ignore[import-untyped]
from pydantic import ValidationError
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.journal import Journal, digest
from firefly_forge.knowledge import KnowledgeManager, snapshot_knowledge
from firefly_forge.learning import LearningLedger
from firefly_forge.learning_models import LearningContext
from firefly_forge.learning_persistence import ForgeLearningProjectionEntity, ForgeLearningRecordEntity
from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
from firefly_forge.store import Store, identifier

Data = dict[str, Any]


@service
class LearningProjection:
    def __init__(self, store: Store, journal: Journal, ledger: LearningLedger, knowledge: KnowledgeManager) -> None:
        self.store, self.journal, self.ledger, self.knowledge = store, journal, ledger, knowledge

    async def _record(
        self, record_id: str, *, review: bool = False, locked: bool = False, cleanup: bool = False
    ) -> ForgeLearningRecordEntity:
        record = (
            await self.ledger.records_repository.find_by_id(identifier(record_id), lock=LockMode.PESSIMISTIC_WRITE)
            if locked
            else await self.ledger.records_repository.find_by_id(identifier(record_id))
        )
        if record is None:
            raise KeyError("Learning record not found")
        actor = CURRENT_PRINCIPAL.get()
        if actor is not None and not cleanup:
            project_id = record.data["envelope"]["context"].get("project_id")
            if project_id:
                await ProjectAccess(self.store).require_project(actor, project_id, "review" if review else "read")
            elif not await ProjectAccess(self.store).current_administrator(actor):
                raise ValueError("Unassigned learning projection requires a factory administrator")
        return record

    async def _projection(self, record: ForgeLearningRecordEntity) -> ForgeLearningProjectionEntity:
        row = await self.ledger.projections.for_record(record.id)
        if row is None:
            raise KeyError("Learning projection not found")
        return row

    async def status(self, record_id: str) -> Data:
        async with self.store._unit():
            record = await self._record(record_id)
            row = await self._projection(record)
            if row is None:
                raise KeyError("Learning projection not found")
            return {
                "id": row.id.hex,
                "record_id": record.id.hex,
                "state": row.state,
                **copy.deepcopy({key: value for key, value in row.data.items() if key != "payload"}),
            }

    @staticmethod
    def _body(payload: Data) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    @staticmethod
    def workspace_id(context: LearningContext) -> str:
        if context.project_id is None:
            return "forge-learning-" + context.repository_id
        identity = {"version": 1, "project_id": context.project_id, "repository_id": context.repository_id}
        return "forge-learning-" + digest(identity)[:48]

    async def submit(self, record_id: str, *, request_id: str) -> Data:
        identifier(request_id)
        async with self.store._unit():
            record = await self._record(record_id, review=True)
            await self.journal._run(record.run_id.hex)
            record = await self._record(record.id.hex, review=True, locked=True)
            if record.state != "verified" or record.kind != "local_outcome":
                raise ValueError("Only verified local source outcomes can be projected")
            row = await self._projection(record)
            if row is None:
                raise KeyError("Learning projection not found")
            if row.state in {"source_admitted", "verified"}:
                return await self.status(record_id)
            if row.state != "pending":
                raise ValueError("Learning projection requires explicit reconciliation; it cannot be replayed")
            settings = self.knowledge.settings()
            if not settings["enabled"] or not settings["api_key"]:
                raise ValueError("Learning projection requires enabled scoped knowledge and a platform API key")
            target = snapshot_knowledge(self.store.configuration)
            body = self._body(row.data["payload"])
            if len(body) > 64_000 or digest(row.data["payload"]) != row.payload_digest:
                raise ValueError("Learning projection payload differs from its bounded evidence")
            settings = self.knowledge.settings({"knowledge_snapshot": target})
            context = LearningContext.model_validate(record.data["envelope"]["context"])
            workspace = self.workspace_id(context)
            WorkspaceCreate(id=workspace, name="Scoped factory learning")
            row.state = "executing"
            at = await self.ledger.records_repository.database_time()
            row.data = {
                **row.data,
                "request_id": request_id,
                "target": target,
                "workspace_id": workspace,
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "submission_started": False,
                "lease_expires_at": (at + timedelta(seconds=120)).timestamp(),
            }
            await self.ledger.projections.save(row)
            await self.ledger._append(
                record,
                request_id,
                "projection_intent",
                {
                    "projection_id": row.id.hex,
                    "payload_digest": row.payload_digest,
                    "target": target,
                    "workspace_id": workspace,
                },
            )
        try:
            async with asyncio.timeout(60), self.knowledge.client(settings, workspace) as client:
                try:
                    await client.get_workspace(workspace)
                except CanonAPIError as exc:
                    if exc.status_code != 404:
                        raise
                    async with self.store._unit():
                        await self._record(record.id.hex, review=True)
                    await self.knowledge._assert_ownership()
                    await client.create_workspace(WorkspaceCreate(id=workspace, name="Scoped factory learning"))
                async with self.store._unit():
                    await self.journal._run(record.run_id.hex)
                    locked = await self._record(record.id.hex, review=True, locked=True)
                    if locked.state != "verified":
                        raise ValueError("Learning evidence was invalidated before projection dispatch")
                    current = await self._projection(record)
                    current.data = {**current.data, "submission_started": True}
                    await self.ledger.projections.save(current)
                await self.knowledge._assert_ownership()
                source = await client.submit_source(
                    SubmitSourceJsonPayload(
                        kind="text",
                        filename="forge-learning-" + record_id + ".json",
                        content_type="application/json",
                        content_base64=base64.b64encode(body).decode(),
                        metadata=SourceMetadata(
                            title="Observed local procedure " + record_id,
                            domain="engineering",
                            tags=["forge", "learning", record.repository_id.hex],
                            extra={
                                "learning_record_id": record_id,
                                "payload_digest": row.payload_digest,
                                "project_id": record.data["envelope"]["context"].get("project_id"),
                                "run_id": record.run_id.hex,
                            },
                        ),
                    )
                )
                await self.knowledge._assert_ownership()
            if source.content_sha256 != hashlib.sha256(body).hexdigest():
                raise ValueError("Submitted learning source does not match its admitted content")
            async with self.store._unit():
                await self.journal._run(record.run_id.hex)
                await self._record(record.id.hex, review=True, locked=True)
                await self.knowledge._assert_ownership()
                current = await self._projection(record)
                if current.state != "executing":
                    raise ValueError("Learning projection already has a recorded outcome")
                current.state = "source_admitted"
                current.data = {**current.data, "source_id": source.id, "source_status": source.status}
                await self.ledger.projections.save(current)
                await self.ledger._append(
                    record,
                    uuid.uuid5(uuid.NAMESPACE_URL, request_id + ":source-admitted").hex,
                    "projection_receipt",
                    {"source_id": source.id, "state": "source_admitted", "content_sha256": source.content_sha256},
                )
            return await self.status(record_id)
        except asyncio.CancelledError:
            await asyncio.shield(self._unknown(record_id))
            raise
        except Exception:
            await self._unknown(record_id)
            raise ValueError("Learning projection outcome is unknown; reconcile it before another submission") from None

    async def _unknown(self, record_id: str) -> None:
        async with self.store._unit():
            record = await self._record(record_id, cleanup=True)
            await self.journal._run(record.run_id.hex)
            await self._record(record.id.hex, locked=True, cleanup=True)
            row = await self._projection(record)
            if row.state == "executing":
                row.state = "unknown"
                await self.ledger.projections.save(row)
                await self.ledger._append(
                    record,
                    uuid.uuid5(uuid.NAMESPACE_URL, row.data["request_id"] + ":unknown").hex,
                    "projection_unknown",
                    {"projection_id": row.id.hex, "submission_started": row.data["submission_started"]},
                )

    async def observe(self, record_id: str, *, source_id: str | None = None, request_id: str) -> Data:
        try:
            async with asyncio.timeout(60):
                return await self._observe(record_id, source_id=source_id, request_id=request_id)
        except (CanonError, httpx.HTTPError, ValidationError, json.JSONDecodeError, TimeoutError):
            raise ValueError("Learning source observation failed; check scoped access and service readiness") from None

    async def _observe(self, record_id: str, *, source_id: str | None = None, request_id: str) -> Data:
        identifier(request_id)
        async with self.store._unit():
            record = await self._record(record_id, review=True)
            row = await self._projection(record)
            if row is None or not row.data.get("target"):
                raise ValueError("Learning projection has no admitted target to observe")
            selected = source_id or row.data.get("source_id")
            if (
                not isinstance(selected, str)
                or not selected
                or len(selected) > 200
                or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in selected)
            ):
                raise ValueError("Select an actual bounded source identifier for reconciliation")
            if row.state == "verified":
                if selected != row.data["source_id"]:
                    raise ValueError("Projection already belongs to another verified source")
                return await self.status(record_id)
            if row.state not in {"source_admitted", "unknown", "executing"}:
                raise ValueError("Projection has no remote outcome to reconcile")
            if row.state == "executing":
                at = await self.ledger.records_repository.database_time()
                if row.data["lease_expires_at"] > at.timestamp():
                    raise ValueError("Learning submission may still be running; wait for its bounded lease")
            if row.data.get("source_id") and selected != row.data["source_id"]:
                raise ValueError("Projection already belongs to another admitted source")
            settings = self.knowledge.settings({"knowledge_snapshot": row.data["target"]})
            body = self._body(row.data["payload"])
            workspace = row.data["workspace_id"]
        async with self.knowledge.client(settings, workspace) as client:
            source = await client.get_source(selected)
            if (
                source.filename != "forge-learning-" + record_id + ".json"
                or source.content_sha256 != hashlib.sha256(body).hexdigest()
                or source.metadata is None
                or source.metadata.extra.get("learning_record_id") != record_id
                or source.metadata.extra.get("payload_digest") != row.payload_digest
                or source.status != "ingested"
                or source.n_chunks < 1
            ):
                raise ValueError("Learning source observation does not match indexed scoped evidence")
            found = await client.search("Observed local procedure " + record_id, top_k=8, source_ids=[selected])
            citations = [
                {"source_id": hit.source_id, "chunk_id": hit.chunk_id}
                for hit in found.hits
                if hit.source_id == selected and hit.chunk_id
            ]
            if not citations:
                raise ValueError("Learning source has no admitted retrievable citations")
        async with self.store._unit():
            record = await self._record(record_id, review=True)
            await self.journal._run(record.run_id.hex)
            record = await self._record(record.id.hex, review=True, locked=True)
            if record.state != "verified":
                raise ValueError("Learning evidence was invalidated before source observation")
            row = await self._projection(record)
            if row.state == "verified":
                if row.data["source_id"] != selected:
                    raise ValueError("Projection already belongs to another verified source")
                return await self.status(record_id)
            if row.state not in {"executing", "source_admitted", "unknown"}:
                raise ValueError("Projection has no remote outcome to reconcile")
            row.state = "verified"
            await self.knowledge._assert_ownership()
            row.data = {
                **row.data,
                "source_id": selected,
                "source_status": source.status,
                "citations": citations,
                "reconciliation_id": request_id,
            }
            await self.ledger.projections.save(row)
            await self.ledger._append(
                record,
                request_id,
                "projection_observation",
                {
                    "source_id": selected,
                    "content_sha256": source.content_sha256,
                    "citations": citations,
                    "state": "verified",
                },
            )
        return await self.status(record_id)
