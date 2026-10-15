# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One-shot flycanon projections with content-bound observations and explicit recovery."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
from flycanon_sdk import (  # type: ignore[import-untyped]
    CanonAPIError,
    CanonError,
    SearchRequest,
    SourceMetadata,
    SubmitSourceJsonPayload,
    WorkspaceCreate,
)
from pydantic import ValidationError
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.journal import Journal, digest
from firefly_forge.knowledge import KnowledgeManager, snapshot_knowledge
from firefly_forge.learning import LearningLedger
from firefly_forge.learning_configuration import frozen_learning
from firefly_forge.learning_models import (
    LearningContext,
    LearningProjectionCitation,
    LearningProjectionObservationReceipt,
)
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
                "payload_digest": row.payload_digest,
            }

    async def observation_receipt(self, record_id: str, request_id: str) -> Data:
        identifier(request_id)
        async with self.store._unit():
            record = await self._record(record_id, review=True)
            event = await self.ledger.events.for_request(record.id, request_id)
            if event is None or event.data.get("kind") != "projection_observation":
                raise KeyError("Learning projection observation request not observed")
            data = event.data
            payload = data.get("payload")
            if (
                event.digest != data.get("digest")
                or event.digest != digest({key: value for key, value in data.items() if key != "digest"})
                or data.get("record_id") != record.id.hex
                or data.get("run_id") != record.run_id.hex
                or data.get("request_id") != request_id
                or data.get("sequence") != event.sequence
                or not isinstance(payload, dict)
                or payload.get("state") != "verified"
            ):
                raise ValueError("Learning projection observation receipt integrity check failed")
            projection = await self._projection(record)
            result = {
                "event_id": event.id.hex,
                "record_id": record.id.hex,
                "repository_id": record.repository_id.hex,
                "run_id": record.run_id.hex,
                "request_id": request_id,
                "kind": "projection_observation",
                "event_state": "verified",
                "current_record_state": record.state,
                "current_projection_state": projection.state,
                "actor_id": data.get("actor_id"),
                **{
                    key: payload.get(key)
                    for key in (
                        "projection_id",
                        "submission_request_id",
                        "workspace_id",
                        "target_revision",
                        "payload_digest",
                    )
                },
                "source_id": payload.get("source_id"),
                "content_sha256": payload.get("content_sha256"),
                "citations": payload.get("citations"),
                "created_at": data["created_at"],
                "sequence": event.sequence,
                "digest": event.digest,
            }
            await self.store.admit_credentials(result)
            return LearningProjectionObservationReceipt.model_validate(result).model_dump(mode="json")

    @staticmethod
    def _body(payload: Data) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    @staticmethod
    def workspace_id(context: LearningContext) -> str:
        if context.project_id is None:
            return "forge-learning-" + context.repository_id
        identity = {"version": 1, "project_id": context.project_id, "repository_id": context.repository_id}
        return "forge-learning-" + digest(identity)[:48]

    @staticmethod
    def _identity(row: ForgeLearningProjectionEntity) -> Data:
        return {
            "projection_id": row.id.hex,
            "record_id": row.record_id.hex,
            "state": row.state,
            "payload_digest": row.payload_digest,
            "mapping_digest": digest(row.data),
        }

    @staticmethod
    def _observation_identity(row: ForgeLearningProjectionEntity) -> Data:
        payload = row.data["payload"]
        content_sha256 = hashlib.sha256(LearningProjection._body(payload)).hexdigest()
        if digest(payload) != row.payload_digest or row.data.get("content_sha256") != content_sha256:
            raise ValueError("Learning projection admission identity does not match its content")
        return {
            "projection_id": row.id.hex,
            "record_id": row.record_id.hex,
            "payload_digest": row.payload_digest,
            "target": copy.deepcopy(row.data["target"]),
            "workspace_id": row.data["workspace_id"],
            "submission_request_id": row.data["request_id"],
            "content_sha256": content_sha256,
        }

    def _same_target(self, run: Data, target: Data | None) -> bool:
        captured = run.get("knowledge_snapshot")
        if not captured or not target:
            return False
        # Each capture has its own revision file even when its frozen settings are identical.
        if {key: value for key, value in captured.items() if key != "revision"} != {
            key: value for key, value in target.items() if key != "revision"
        }:
            return False
        try:
            return self.knowledge.settings(run) == self.knowledge.settings({"knowledge_snapshot": target})
        except ValueError:
            return False

    async def retrieve(self, run_id: str, query: str, observed: Data, candidate_id: str) -> Data:
        """Read only the exact eligible record's verified, frozen-scope projection."""
        async with self.store._unit():
            run = await self.store.get_run(run_id)
            policy = frozen_learning(run.get("learning_snapshot"))["settings"]
            if not policy.get("projected_retrieval", False):
                return {"state": "disabled"}
            decision = await self.ledger.lookup(run_id, query, observed)
            if not decision["eligible"] or decision.get("candidate_id") != candidate_id:
                return {"state": "unavailable", "reason": "record_ineligible"}
            record = decision["outcome"]
            row = await self.ledger.projections.for_record(identifier(candidate_id))
            if row is None or row.state != "verified":
                return {"state": "unavailable", "reason": "projection_unverified"}
            context = LearningContext.model_validate(observed)
            body = self._body(row.data.get("payload", {}))
            workspace = self.workspace_id(context)
            if (
                row.data.get("workspace_id") != workspace
                or not self._same_target(run, row.data.get("target"))
                or row.payload_digest != digest(record["envelope"])
                or row.payload_digest != digest(row.data.get("payload", {}))
                or len(body) > 64_000
                or row.data.get("content_sha256") != hashlib.sha256(body).hexdigest()
                or not row.data.get("source_id")
            ):
                return {"state": "unavailable", "reason": "projection_identity_mismatch"}
            selected = copy.deepcopy(row.data)
            identity = self._identity(row)
            settings = self.knowledge.settings(run)
        try:
            async with asyncio.timeout(20), self.knowledge.client(settings, workspace) as client:
                source = (
                    await client.agent.get_source(selected["source_id"])
                    if settings["agent_token"]
                    else await client.get_source(selected["source_id"])
                )
                if (
                    source.id != selected["source_id"]
                    or source.filename != "forge-learning-" + candidate_id + ".json"
                    or source.content_sha256 != selected["content_sha256"]
                    or source.status != "ingested"
                    or source.n_chunks < 1
                    or source.metadata is None
                    or source.metadata.extra.get("learning_record_id") != candidate_id
                    or source.metadata.extra.get("payload_digest") != identity["payload_digest"]
                    or source.metadata.extra.get("project_id") != context.project_id
                    or source.metadata.extra.get("run_id") != record["envelope"]["run_id"]
                ):
                    return {"state": "unavailable", "reason": "source_identity_mismatch"}
                found = (
                    await client.agent.search(
                        SearchRequest(query=query, top_k=8, source_ids=[source.id]), idempotency_key=uuid.uuid4().hex
                    )
                    if settings["agent_token"]
                    else await client.search(query, top_k=8, source_ids=[source.id])
                )
                if not found.hits or any(hit.source_id != source.id or not hit.chunk_id for hit in found.hits):
                    return {"state": "unavailable", "reason": "source_citation_unavailable"}
                citations = [
                    {"workspace_id": workspace, "source_id": hit.source_id, "chunk_id": hit.chunk_id}
                    for hit in found.hits[:8]
                ]
        except (CanonError, httpx.HTTPError, ValidationError, json.JSONDecodeError, TimeoutError):
            return {"state": "unavailable", "reason": "projection_service_unavailable"}
        return {
            **identity,
            "state": "verified",
            "citations": citations,
            "citations_digest": digest({"citations": citations}),
        }

    async def retain(self, run_id: str, observed: Data, selected: Data) -> Callable[[], None]:
        """Lock order is local record (held by ledger.retain), then exact projection."""
        sessions = self.store.session_provider
        if sessions is None or sessions.current() is None:
            raise RuntimeError("Retained projected evidence requires an active admission unit")
        message = "Retained projected evidence changed; request an explicit refresh"
        async with self.store._unit():
            run = await self.store.get_run(run_id)
            if not frozen_learning(run.get("learning_snapshot"))["settings"].get("projected_retrieval", False):
                raise ValueError(message)
            row = await self.ledger.projections.find_by_id(
                identifier(selected["projection_id"]), lock=LockMode.PESSIMISTIC_READ
            )
            expected = {
                key: selected[key]
                for key in ("projection_id", "record_id", "state", "payload_digest", "mapping_digest")
            }
            if (
                row is None
                or self._identity(row) != expected
                or not self._same_target(run, row.data.get("target"))
                or row.data.get("workspace_id") != self.workspace_id(LearningContext.model_validate(observed))
                or not selected.get("citations")
                or selected.get("citations_digest") != digest({"citations": selected["citations"]})
                or any(
                    citation.get("source_id") != row.data.get("source_id")
                    or citation.get("workspace_id") != row.data.get("workspace_id")
                    or not citation.get("chunk_id")
                    for citation in selected["citations"]
                )
            ):
                raise ValueError(message)
            target = copy.deepcopy(run.get("knowledge_snapshot"))

            def guard() -> None:
                if (
                    self._identity(row) != expected
                    or run.get("knowledge_snapshot") != target
                    or not self._same_target(run, row.data.get("target"))
                ):
                    raise ValueError(message)

            return guard

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
            identity = self._observation_identity(row)
            await self.store.admit_credentials(selected)
            settings = self.knowledge.settings({"knowledge_snapshot": identity["target"]})
            workspace = identity["workspace_id"]
        async with self.knowledge.client(settings, workspace) as client:
            source = await client.get_source(selected)
            if (
                source.id != selected
                or source.filename != "forge-learning-" + record_id + ".json"
                or source.content_sha256 != identity["content_sha256"]
                or source.metadata is None
                or source.metadata.extra.get("learning_record_id") != record_id
                or source.metadata.extra.get("payload_digest") != identity["payload_digest"]
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
            if not citations or len(citations) > 8:
                raise ValueError("Learning source has no bounded admitted retrievable citations")
            citations = [LearningProjectionCitation.model_validate(item).model_dump() for item in citations]
        async with self.store._unit():
            record = await self._record(record_id, review=True)
            await self.journal._run(record.run_id.hex)
            record = await self._record(record.id.hex, review=True, locked=True)
            if record.state != "verified":
                raise ValueError("Learning evidence was invalidated before source observation")
            row = await self._projection(record)
            if self._observation_identity(row) != identity:
                raise ValueError("Learning projection admission identity changed during observation")
            if row.data.get("source_id") and row.data["source_id"] != selected:
                raise ValueError("Projection already belongs to another admitted source")
            if row.state == "verified":
                if row.data["source_id"] != selected:
                    raise ValueError("Projection already belongs to another verified source")
                return await self.status(record_id)
            if row.state not in {"executing", "source_admitted", "unknown"}:
                raise ValueError("Projection has no remote outcome to reconcile")
            observation = {
                "projection_id": identity["projection_id"],
                "submission_request_id": identity["submission_request_id"],
                "workspace_id": identity["workspace_id"],
                "target_revision": identity["target"]["revision"],
                "payload_digest": identity["payload_digest"],
                "source_id": selected,
                "content_sha256": identity["content_sha256"],
                "citations": citations,
                "state": "verified",
            }
            await self.store.admit_credentials(observation)
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
            await self.ledger._append(record, request_id, "projection_observation", observation)
        return await self.status(record_id)
