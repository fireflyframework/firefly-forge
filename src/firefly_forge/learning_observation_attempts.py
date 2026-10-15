# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Once-only observation attempts with exact pre-dispatch refusal evidence."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from flycanon_sdk import CanonError  # type: ignore[import-untyped]
from pydantic import ValidationError
from pyfly.container import service

from firefly_forge.journal import digest
from firefly_forge.learning_models import LearningProjectionCitation
from firefly_forge.learning_observation_attempt_models import (
    LearningObservationAttemptInput,
    LearningObservationAttemptReceipt,
    observation_attempt_id,
)
from firefly_forge.learning_observation_attempt_persistence import (
    ForgeLearningObservationAttemptEntity,
    ForgeLearningObservationAttemptRepository,
)
from firefly_forge.learning_projection import LearningProjection
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.store import identifier

if TYPE_CHECKING:
    from firefly_forge.learning_persistence import ForgeLearningRecordEntity

Data = dict[str, Any]


@service
class LearningObservationAttempts:
    def __init__(self, projection: LearningProjection) -> None:
        self.projection = projection
        self.store, self.ledger = projection.store, projection.ledger
        self.attempts = ForgeLearningObservationAttemptRepository()

    @staticmethod
    def event_request_id(record_id: str, attempt_id: str) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, "forge:observation:v2:" + record_id + ":" + attempt_id).hex

    @staticmethod
    def _actor() -> str:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None:
            raise AccessDenied("Current review authority is required for an observation attempt")
        return actor.id.hex

    async def _locked(self, record_id: str) -> ForgeLearningRecordEntity:
        record = await self.projection._record(record_id, review=True)
        await self.projection.journal._run(record.run_id.hex)
        return await self.projection._record(record_id, review=True, locked=True)

    @staticmethod
    def _view(row: ForgeLearningObservationAttemptEntity) -> Data:
        data = copy.deepcopy(row.data)
        if (
            data.get("state") != row.state
            or data.get("record_id") != row.record_id.hex
            or data.get("attempt_id") != row.attempt_id
            or data.get("digest") != digest({key: value for key, value in data.items() if key != "digest"})
        ):
            raise ValueError("Observation attempt receipt integrity check failed")
        return LearningObservationAttemptReceipt.model_validate(data).model_dump(mode="json")

    async def _change(self, row: ForgeLearningObservationAttemptEntity, state: str, **result: Any) -> Data:
        row.state = state
        row.data = {**row.data, "state": state, **result}
        row.data = {**row.data, "digest": digest({k: v for k, v in row.data.items() if k != "digest"})}
        await self.attempts.save(row)
        return self._view(row)

    async def _claim(self, record_id: str, body: LearningObservationAttemptInput) -> tuple[Data, bool]:
        actor = self._actor()
        await self.store.admit_credentials(body.model_dump(exclude={"attempt_id", "source_id"}))
        async with self.store._unit():
            record = await self._locked(record_id)
            prior = await self.attempts.for_attempt(record.id, body.attempt_id, locked=True)
            if prior is not None:
                view = self._view(prior)
                if view["input_digest"] != digest(body.model_dump()) or view["actor_id"] != actor:
                    raise ValueError("Observation attempt identity already belongs to another request")
                return view, False
            if await self.attempts.count_for_record(record.id) >= 100:
                raise ValueError("Observation attempt history exceeds its bound")
            history = await self.attempts.for_record(record.id)
            if any(self._view(item)["state"] != "not_dispatched" for item in history):
                raise ValueError(
                    "Read the original observation attempt; unresolved operations cannot use a fresh identity"
                )
            at = await self.ledger.records_repository.database_time()
            data = {
                "version": 2,
                "attempt_id": body.attempt_id,
                "record_id": record.id.hex,
                "repository_id": record.repository_id.hex,
                "project_id": record.data["envelope"]["context"].get("project_id"),
                "run_id": record.run_id.hex,
                "actor_id": actor,
                "input_digest": digest(body.model_dump()),
                "source_digest": digest({"source_id": body.source_id}),
                **body.model_dump(exclude={"attempt_id", "source_id"}),
                "state": "preparing",
                "disposition": None,
                "created_at": at.isoformat(),
                "finished_at": None,
                "observation": None,
            }
            data["digest"] = digest(data)
            row = ForgeLearningObservationAttemptEntity(
                id=uuid.uuid4(), record_id=record.id, attempt_id=body.attempt_id, state="preparing", data=data
            )
            await self.attempts.save(row)
            return self._view(row), True

    async def receipt(self, record_id: str, attempt_id: str) -> Data:
        observation_attempt_id(attempt_id)
        actor = self._actor()
        async with self.store._unit():
            record = await self.projection._record(record_id, review=True)
            row = await self.attempts.for_attempt(record.id, attempt_id)
            if row is None:
                raise KeyError("Exact observation attempt not found; absence cannot authorize another request")
            view = self._view(row)
            if (
                view["actor_id"] != actor
                or view["repository_id"] != record.repository_id.hex
                or view["run_id"] != record.run_id.hex
                or view["project_id"] != record.data["envelope"]["context"].get("project_id")
            ):
                raise AccessDenied("Observation attempt does not belong to the current actor and scope")
            if row.state == "verified":
                event_id = self.event_request_id(record.id.hex, attempt_id)
                event = await self.ledger.events.for_request(record.id, event_id)
                if (
                    event is None
                    or view["observation"] != {"event_id": event.id.hex, "request_id": event_id, "digest": event.digest}
                    or event.data.get("payload", {}).get("attempt_id") != attempt_id
                    or event.data.get("payload", {}).get("input_digest") != view["input_digest"]
                ):
                    raise ValueError("Observation event does not match the exact attempt")
                observed = await self.projection.observation_receipt(record.id.hex, event_id)
                if any(
                    observed[key] != view[key]
                    for key in (
                        "actor_id",
                        "projection_id",
                        "submission_request_id",
                        "workspace_id",
                        "target_revision",
                        "payload_digest",
                        "content_sha256",
                    )
                ):
                    raise ValueError("Observation event admission does not match the attempt")
            await self.store.admit_credentials(view)
            await self.projection._record(record_id, review=True)
            return view

    async def _prepare(self, record_id: str, body: LearningObservationAttemptInput) -> tuple[str, Data]:
        async with self.store._unit():
            record = await self.projection._record(record_id, review=True)
            row = await self.projection._projection(record)
            if record.state != "verified" or row.state not in {"source_admitted", "unknown"}:
                raise ValueError("An admitted unresolved projection is required")
            identity = self.projection._observation_identity(row)
            selected = body.source_id or row.data.get("source_id")
            if not isinstance(selected, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", selected):
                raise ValueError("An existing source must be selected")
            if row.data.get("source_id") and row.data["source_id"] != selected:
                raise ValueError("Projection source identity cannot be replaced")
            expected = {
                key: identity[key]
                for key in (
                    "projection_id",
                    "submission_request_id",
                    "workspace_id",
                    "payload_digest",
                    "content_sha256",
                )
            }
            expected["target_revision"] = identity["target"]["revision"]
            if expected != body.model_dump(exclude={"attempt_id", "source_id"}):
                raise ValueError("Projection admission identity changed")
        await self.store.admit_credentials(selected)
        self.projection.knowledge.settings({"knowledge_snapshot": identity["target"]})
        return selected, identity

    async def _finish_local(self, record_id: str, body: LearningObservationAttemptInput, state: str) -> Data:
        async with self.store._unit():
            record = await self._locked(record_id)
            row = await self.attempts.for_attempt(record.id, body.attempt_id, locked=True)
            if (
                row is None
                or row.data["actor_id"] != self._actor()
                or row.data["input_digest"] != digest(body.model_dump())
            ):
                raise ValueError("Exact observation claim is required")
            expected = "preparing" if state == "not_dispatched" else "dispatched"
            if row.state != expected:
                return self._view(row)
            at = await self.ledger.records_repository.database_time()
            return await self._change(
                row,
                state,
                finished_at=at.isoformat(),
                disposition="local_admission_refused" if state == "not_dispatched" else None,
            )

    async def observe(self, record_id: str, body: LearningObservationAttemptInput) -> Data:
        record_id = identifier(record_id).hex
        _, fresh = await self._claim(record_id, body)
        if not fresh:
            return await self.receipt(record_id, body.attempt_id)
        try:
            selected, identity = await self._prepare(record_id, body)
        except ValueError:
            await self._finish_local(record_id, body, "not_dispatched")
            return await self.receipt(record_id, body.attempt_id)
        async with self.store._unit():
            record = await self._locked(record_id)
            projection = await self.projection._projection(record)
            row = await self.attempts.for_attempt(record.id, body.attempt_id, locked=True)
            if row is None or row.state != "preparing":
                raise ValueError("Exact undispatched observation claim is required")
            self._view(row)
            if row.data["actor_id"] != self._actor() or row.data["input_digest"] != digest(body.model_dump()):
                raise ValueError("Observation claim identity changed")
            if record.state != "verified" or self.projection._observation_identity(projection) != identity:
                raise ValueError("Projection identity changed before dispatch")
            if projection.state not in {"source_admitted", "unknown"}:
                raise ValueError("Projection is no longer available for observation")
            await self._change(row, "dispatched")
        try:
            async with asyncio.timeout(60):
                citations = await self._read(record_id, selected, identity)
                await self._complete(record_id, body, selected, identity, citations)
        except (CanonError, httpx.HTTPError, ValidationError, json.JSONDecodeError, TimeoutError, ValueError):
            await self._finish_local(record_id, body, "unknown")
        return await self.receipt(record_id, body.attempt_id)

    async def _read(self, record_id: str, selected: str, identity: Data) -> list[Data]:
        await self.store.admit_credentials(selected)
        async with self.store._unit():
            record = await self.projection._record(record_id, review=True)
            projection = await self.projection._projection(record)
            if (
                record.state != "verified"
                or self.projection._observation_identity(projection) != identity
                or projection.state not in {"source_admitted", "unknown"}
            ):
                raise ValueError("Projection admission identity changed before client dispatch")
        settings = self.projection.knowledge.settings({"knowledge_snapshot": identity["target"]})
        async with self.projection.knowledge.client(settings, identity["workspace_id"]) as client:
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
                raise ValueError("Source does not match admitted indexed evidence")
            found = await client.search("Observed local procedure " + record_id, top_k=8, source_ids=[selected])
            citations = [
                {"source_id": hit.source_id, "chunk_id": hit.chunk_id}
                for hit in found.hits
                if hit.source_id == selected and hit.chunk_id
            ]
            if not citations or len(citations) > 8:
                raise ValueError("Source has no bounded admitted citations")
            return [LearningProjectionCitation.model_validate(item).model_dump() for item in citations]

    async def _complete(
        self,
        record_id: str,
        body: LearningObservationAttemptInput,
        selected: str,
        identity: Data,
        citations: list[Data],
    ) -> None:
        async with self.store._unit():
            record = await self._locked(record_id)
            projection = await self.projection._projection(record)
            row = await self.attempts.for_attempt(record.id, body.attempt_id, locked=True)
            if row is None or row.state != "dispatched" or row.data["actor_id"] != self._actor():
                raise ValueError("Exact dispatched observation claim is required")
            self._view(row)
            if row.data["input_digest"] != digest(body.model_dump()):
                raise ValueError("Observation claim input changed")
            if (
                record.state != "verified"
                or self.projection._observation_identity(projection) != identity
                or projection.state not in {"source_admitted", "unknown"}
                or (projection.data.get("source_id") and projection.data["source_id"] != selected)
            ):
                raise ValueError("Projection admission identity changed during observation")
            observation = {
                **body.model_dump(exclude={"source_id"}),
                "source_id": selected,
                "citations": citations,
                "state": "verified",
                "input_digest": row.data["input_digest"],
            }
            await self.store.admit_credentials(observation)
            await self.projection._record(record_id, review=True)
            await self.projection.knowledge._assert_ownership()
            event_request = self.event_request_id(record_id, body.attempt_id)
            event = await self.ledger._append(record, event_request, "projection_observation", observation)
            projection.state = "verified"
            projection.data = {
                **projection.data,
                "source_id": selected,
                "source_status": "ingested",
                "citations": citations,
                "reconciliation_id": event_request,
            }
            await self.ledger.projections.save(projection)
            retained = await self.ledger.events.for_request(record.id, event_request)
            if retained is None:
                raise ValueError("Exact observation event was not retained")
            at = await self.ledger.records_repository.database_time()
            await self._change(
                row,
                "verified",
                disposition="verified",
                finished_at=at.isoformat(),
                observation={
                    "event_id": retained.id.hex,
                    "request_id": event_request,
                    "digest": event["digest"],
                },
            )
