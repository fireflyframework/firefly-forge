# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact, currently authorized reads of observed sanitized activity content."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pyfly.container import service
from pyfly.data.transaction import (
    Isolation,
    Propagation,
    TransactionTemplate,
    TransactionTimedOutError,
    current_unit_of_work,
)
from sqlalchemy import func, select

from firefly_forge.activity_models import (
    MAX_INDEX_BYTES,
    ActivityContentQuery,
    ActivitySummary,
    ActivityUnavailable,
    canonical,
    content_chunk,
    validate_descriptor,
)
from firefly_forge.activity_persistence import ForgeEventContentRepository
from firefly_forge.journal import digest
from firefly_forge.persistence import ForgeEventEntity, ForgePlanEntity, ForgeRunEntity
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.project_persistence import ForgeProjectRevisionEntity
from firefly_forge.run_evidence import RunEvidence
from firefly_forge.store import Store, identifier

Data = dict[str, Any]


@dataclass(frozen=True)
class ActivityObservation:
    run: Data
    repositories: frozenset[str]
    event: Data
    scope_digest: str


@service
class ActivityArchive:
    def __init__(
        self, store: Store, records: ForgeEventContentRepository | None = None, evidence: RunEvidence | None = None
    ) -> None:
        self.store = store
        self.records = records or getattr(store, "event_content_records", None) or ForgeEventContentRepository()
        self.evidence = evidence or RunEvidence(store)

    async def _header(self, run_id: str) -> Data:
        async with self.store._unit() as session:
            header = (
                await session.execute(
                    select(
                        ForgeRunEntity.id,
                        ForgeRunEntity.repository_id,
                        func.left(ForgeRunEntity.data["project_id"].as_string(), 33),
                        func.left(ForgeRunEntity.data["component_id"].as_string(), 101),
                    ).where(ForgeRunEntity.id == identifier(run_id))
                )
            ).one_or_none()
            if header is None:
                raise KeyError("Run not found")
            return {
                "id": header[0].hex,
                "repository_id": header[1].hex,
                "project_id": header[2],
                "component_id": header[3],
            }

    async def _capture(self, run_id: str, event_id: str) -> ActivityObservation:
        try:
            async with self.store._unit():
                unit = current_unit_of_work()
                assert unit is not None
                manager = unit.manager
            async with (
                TransactionTemplate(
                    manager,
                    propagation=Propagation.REQUIRES_NEW,
                    isolation=Isolation.REPEATABLE_READ,
                    read_only=True,
                    timeout=10,
                ).transaction(),
                self.store._unit() as session,
            ):
                header = await self._header(run_id)
                remaining = [1048576]
                plans = await self.evidence._rows(
                    session,
                    select(ForgePlanEntity)
                    .where(ForgePlanEntity.run_id == identifier(run_id))
                    .order_by(ForgePlanEntity.revision.desc())
                    .limit(1),
                    1,
                    remaining,
                )
                repositories = {header["repository_id"]}
                plan_digest = None
                if plans:
                    plan = plans[0]
                    if (
                        digest(plan.data) != plan.digest
                        or plan.data.get("run_id") != run_id
                        or plan.data.get("repository_id") != header["repository_id"]
                        or plan.data.get("project_id") != header["project_id"]
                        or plan.data.get("component_id") != header["component_id"]
                    ):
                        raise ActivityUnavailable("activity_integrity")
                    plan_digest = plan.digest
                if header["project_id"]:
                    if not plans:
                        raise ActivityUnavailable("activity_scope_unavailable")
                    component = plans[0].data.get("component_snapshot") or {}
                    definition = component.get("project_definition") or {}
                    ProjectDefinition.model_validate(definition)
                    revisions = await self.evidence._rows(
                        session,
                        select(ForgeProjectRevisionEntity)
                        .where(
                            ForgeProjectRevisionEntity.project_id == identifier(header["project_id"]),
                            ForgeProjectRevisionEntity.revision == component.get("project_revision"),
                        )
                        .limit(1),
                        1,
                        remaining,
                    )
                    expected = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
                    selected = next((c for c in definition["components"] if c["id"] == header["component_id"]), None)
                    if (
                        len(revisions) != 1
                        or revisions[0].data.get("definition") != definition
                        or revisions[0].data.get("digest") != expected
                        or component.get("project_digest") != expected
                        or not selected
                        or selected["repository_id"] != header["repository_id"]
                    ):
                        raise ActivityUnavailable("activity_integrity")
                    repositories.update(identifier(c["repository_id"]).hex for c in definition["components"])
                    repositories.add(identifier(definition["product_repository_id"]).hex)
                rows = await self.evidence._rows(
                    session,
                    select(ForgeEventEntity).where(
                        ForgeEventEntity.id == identifier(event_id), ForgeEventEntity.run_id == identifier(run_id)
                    ),
                    1,
                    [MAX_INDEX_BYTES],
                )
                if not rows:
                    raise KeyError("Activity event not found in this run")
                event = copy.deepcopy(rows[0].data)
                if not isinstance(event, dict) or event.get("id") != event_id:
                    raise ActivityUnavailable("activity_integrity")
                descriptor = validate_descriptor(event.get("activity_content"))
                if (descriptor["run_id"], descriptor["event_id"], descriptor["stage"]) != (
                    run_id,
                    event_id,
                    event.get("stage"),
                ):
                    raise ActivityUnavailable("activity_integrity")
                if "activity_summary" in event:
                    summary = ActivitySummary.model_validate(event["activity_summary"])
                    if (
                        type(event["activity_summary"].get("version")) is not int
                        or summary.sanitized_message_bytes != descriptor["sanitized_message_bytes"]
                        or summary.sanitized_message_sha256 != descriptor["sanitized_message_sha256"]
                    ):
                        raise ActivityUnavailable("activity_integrity")
                scope = {
                    "run": header,
                    "repositories": sorted(repositories),
                    "plan_digest": plan_digest,
                    "event": event,
                }
                return ActivityObservation(
                    header, frozenset(repositories), event, hashlib.sha256(canonical(scope)).hexdigest()
                )
        except ActivityUnavailable:
            raise
        except TransactionTimedOutError as exc:
            raise ActivityUnavailable("activity_snapshot_timeout") from exc
        except (ValueError, TypeError) as exc:
            raise ActivityUnavailable("activity_integrity") from exc

    async def content(self, run_id: str, event_id: str, query: ActivityContentQuery) -> Data:
        if current_unit_of_work() is not None:
            raise ActivityUnavailable("activity_caller_transaction")
        run_id, event_id = identifier(run_id).hex, identifier(event_id).hex
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("Activity content requires a current native reader")
        header = await self._header(run_id)
        await self.evidence._authorize(header, frozenset({header["repository_id"]}), reader)
        observation = await self._capture(run_id, event_id)
        await self.evidence._authorize(observation.run, observation.repositories, reader)
        descriptor = validate_descriptor(observation.event.get("activity_content"))
        if descriptor["descriptor_sha256"] != query.expected_descriptor:
            raise ActivityUnavailable("activity_cut_changed")
        if descriptor["status"] != "available":
            raise ActivityUnavailable("activity_budget_exceeded")
        async with self.store._unit():
            payload = await self.records.payload(
                identifier(run_id),
                identifier(event_id),
                descriptor["sanitized_message_bytes"],
                descriptor["sanitized_message_sha256"],
            )
        result = content_chunk(descriptor, payload, query)
        try:
            text = payload.decode("utf-8")
            await self.store.admit_credentials(text)
        except ValueError as exc:
            raise ActivityUnavailable("activity_credential_admission") from exc
        await self.evidence._authorize(observation.run, observation.repositories, reader)
        current = await self._capture(run_id, event_id)
        if current.scope_digest != observation.scope_digest:
            raise ActivityUnavailable("activity_cut_changed")
        await self.evidence._authorize(current.run, current.repositories, reader)
        try:
            self.store.reject_credentials(text)
        except ValueError as exc:
            raise ActivityUnavailable("activity_credential_admission") from exc
        return result
