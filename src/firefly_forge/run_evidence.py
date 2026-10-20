# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded read-only projections of retained run evidence, never effect replay."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyfly.container import service
from pyfly.data.transaction import (
    Isolation,
    Propagation,
    TransactionTemplate,
    TransactionTimedOutError,
    current_unit_of_work,
)
from sqlalchemy import Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from firefly_forge.activity_models import ActivityUnavailable, validate_descriptor
from firefly_forge.journal import Journal, digest
from firefly_forge.persistence import (
    ForgeEventEntity,
    ForgeJournalEntity,
    ForgePlanEntity,
    ForgeRunEntity,
    ForgeTaskEntity,
)
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectRevisionEntity
from firefly_forge.store import Store, identifier
from firefly_forge.tool_persistence import ForgeToolInvocationEntity

Data = dict[str, Any]
MAX_BYTES, MAX_RECORD_BYTES, PAGE_BYTES = 16_777_216, 1_048_576, 1_048_576
LIMITS = {"journal": 2048, "activity": 4096, "tools": 1000, "tasks": 128, "artifacts": 128}


class EvidenceUnavailable(ValueError):
    """No private or partially verified evidence may accompany this failure."""


class EvidenceCutChanged(ValueError):
    pass


class EvidenceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    expected_cut: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    expected_filter: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    after: int = Field(default=0, ge=0, le=8192)
    limit: int = Field(default=50, ge=1, le=50)
    kind: Literal["all", "journal", "activity", "tool", "artifact"] = "all"
    task_id: str | None = Field(default=None, min_length=1, max_length=100)
    text: str = Field(default="", max_length=200)
    record_id: str | None = Field(
        default=None, pattern=r"^(journal:[0-9]{1,4}|activity:[a-f0-9]{32}|tool:[a-f0-9]{32}|artifact:[0-9]{1,4})$"
    )

    @model_validator(mode="after")
    def bound_page(self) -> EvidenceQuery:
        if self.after and (self.expected_cut is None or self.expected_filter is None):
            raise ValueError("Continuing a page requires its exact cut and filter")
        if self.record_id is not None and (self.after or self.kind != "all" or self.task_id is not None or self.text):
            raise ValueError("Read an exact record separately from filters")
        return self


class EvidenceExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    expected_cut: str = Field(pattern=r"^[a-f0-9]{64}$")


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def require(value: Any) -> None:
    if not value:
        raise EvidenceUnavailable("evidence_integrity")


@dataclass(frozen=True)
class EvidenceSnapshot:
    manifest: Data
    records: list[Data]
    members: dict[str, Any]
    repositories: frozenset[str]


def build_evidence(
    *,
    run: Data,
    plan: Data,
    journal: list[Data],
    tasks: list[Data],
    tools: list[Data],
    activity: list[Data],
    project_revision: Data | None = None,
) -> EvidenceSnapshot:
    """Inputs are native scalar-checked rows from one bounded database snapshot."""
    require(journal and Journal.verify_chain(journal))
    require(plan.get("digest") == digest({k: v for k, v in plan.items() if k != "digest"}))
    for key in ("run_id", "repository_id", "project_id", "component_id", "component_snapshot", "role_snapshot"):
        require(plan.get(key) == (run.get("id") if key == "run_id" else run.get(key)))
    require(all(entry["run_id"] == run["id"] for entry in journal))
    require(
        any(
            e["kind"] == "plan" and e["payload"] == {"revision": plan["revision"], "digest": plan["digest"]}
            for e in journal
        )
    )
    repositories = {identifier(plan["repository_id"]).hex}
    if plan.get("project_id"):
        from firefly_forge.project_models import ProjectDefinition

        component = plan.get("component_snapshot") or {}
        require(project_revision is not None)
        revision = project_revision or {}
        definition = revision.get("definition", {})
        ProjectDefinition.model_validate(definition)
        require(
            revision.get("id") == plan["project_id"] and revision.get("revision") == component.get("project_revision")
        )
        require(component.get("project_definition") == definition)
        project_digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
        require(component.get("project_digest") == revision.get("digest") == project_digest)
        selected = next((c for c in definition["components"] if c["id"] == plan.get("component_id")), None)
        require(selected is not None and selected["repository_id"] == plan["repository_id"])
        repositories.update(identifier(c["repository_id"]).hex for c in definition["components"])
        repositories.add(identifier(definition["product_repository_id"]).hex)
    else:
        require(plan.get("component_snapshot") is None and plan.get("component_id") is None)
    for name, rows in [("journal", journal), ("activity", activity), ("tasks", tasks), ("tools", tools)]:
        if len(rows) > LIMITS[name]:
            raise EvidenceUnavailable("evidence_limit")
    for row in tools:
        admission = row["data"]["admission"]
        require(row["data"]["request_digest"] == digest(admission))
        require(all(admission.get(k) == row.get(k) for k in ("run_id", "task_id", "tool", "correlation_id")))
        require(
            admission.get("repository_id") == plan["repository_id"]
            and admission.get("project_id") == plan.get("project_id")
            and admission.get("component_id") == plan.get("component_id")
        )
        intent = next(
            (
                e
                for e in journal
                if e["kind"] == "tool_intent"
                and e["idempotency_key"] == row["correlation_id"]
                and e["payload"] == admission
            ),
            None,
        )
        require(intent and intent["task_id"] == row["task_id"])
        outcomes = [
            e
            for e in journal
            if (
                e["idempotency_key"] == row["correlation_id"]
                or e["kind"] == "tool_reconciled"
                and e["payload"].get("correlation_id") == row["correlation_id"]
            )
            and e["kind"] in {"tool_receipt", "tool_unknown", "tool_reconciled"}
        ]
        require(all(e["sequence"] > (intent or {})["sequence"] for e in outcomes))
        if row["status"] in {"succeeded", "rejected", "unknown"}:
            require(outcomes and outcomes[-1]["kind"] != "tool_reconciled")
            kind = "tool_unknown" if row["status"] == "unknown" else "tool_receipt"
            receipt = next(
                (e for e in journal if e["kind"] == kind and e["idempotency_key"] == row["correlation_id"]), None
            )
            require(receipt and receipt["task_id"] == row["task_id"] and receipt["payload"]["status"] == row["status"])
            require(receipt and receipt["payload"]["tool"] == row["tool"])
            for key in ("result", "error", "observation"):
                require((receipt or {})["payload"].get(key) == row["data"].get(key))
        elif row["status"] == "abandoned":
            require(outcomes and outcomes[-1]["kind"] == "tool_reconciled")
            require(all(e["kind"] == "tool_unknown" for e in outcomes[:-1]))
            require(
                any(
                    e["kind"] == "tool_reconciled"
                    and e["idempotency_key"] == row["data"].get("reconciliation_id")
                    and e["payload"].get("correlation_id") == row["correlation_id"]
                    and e["task_id"] == row["task_id"]
                    and e["payload"].get("decision") == "abandoned_without_replay"
                    and e["payload"].get("actor") == row["data"].get("decision_actor")
                    for e in journal
                )
            )
        else:
            require(row["status"] == "pending" and not outcomes)
    if len(run.get("artifacts", [])) > LIMITS["artifacts"]:
        raise EvidenceUnavailable("evidence_limit")
    artifacts = []
    for ref in run.get("artifacts", []):
        require(isinstance(ref, dict) and isinstance(ref.get("name"), str))
        anchor = next(
            (
                e
                for e in journal
                if e["kind"] == "receipt"
                and ref in e["payload"].get("artifacts", [])
                and isinstance(ref.get("sha256"), str)
                and len(ref["sha256"]) == 64
                and all(c in "abcdef0123456789" for c in ref["sha256"])
            ),
            None,
        )
        artifacts.append(
            {
                "reference": copy.deepcopy(ref),
                "integrity": "receipt_bound" if anchor else "not_historically_bound",
                "receipt": {"sequence": anchor["sequence"], "digest": anchor["digest"]} if anchor else None,
            }
        )
    projection = {
        k: copy.deepcopy(run[k])
        for k in (
            "id",
            "repository_id",
            "project_id",
            "component_id",
            "status",
            "pause",
            "execution_actor",
            "base_revision",
            "head",
            "source_tree",
            "checkpoint_revision",
            "branch",
        )
        if k in run
    }
    plan_projection = {
        k: copy.deepcopy(plan[k])
        for k in (
            "version",
            "revision",
            "run_id",
            "repository_id",
            "project_id",
            "component_id",
            "operation",
            "brief",
            "backend",
            "frontend",
            "tasks",
            "commands",
        )
        if k in plan
    }
    members = {
        "run.json": projection,
        "plan.json": {"source_digest": plan["digest"], "projection": plan_projection},
        "tasks.json": tasks,
        "journal.json": journal,
        "tools.json": tools,
        "activity.json": activity,
        "artifacts.json": artifacts,
    }
    if sum(len(encoded(v)) for v in members.values()) > MAX_BYTES:
        raise EvidenceUnavailable("evidence_limit")
    entries = [
        {"name": name, "bytes": len(encoded(body)), "sha256": hashlib.sha256(encoded(body)).hexdigest()}
        for name, body in sorted(members.items())
    ]
    records: list[Data] = []
    for row in journal:
        records.append(
            {
                "id": f"journal:{row['sequence']}",
                "kind": "journal",
                "task_id": row["task_id"],
                "payload": row,
                "provenance": "immutable_wal",
            }
        )
    for row in activity:
        records.append(
            {
                "id": "activity:" + row["id"],
                "kind": "activity",
                "task_id": None,
                "payload": row,
                "provenance": "observed_row",
            }
        )
    for row in tools:
        records.append(
            {
                "id": "tool:" + row["correlation_id"],
                "kind": "tool",
                "task_id": row["task_id"],
                "payload": row,
                "provenance": "wal_bound_invocation",
            }
        )
    for index, ref in enumerate(artifacts):
        records.append(
            {
                "id": f"artifact:{index}",
                "kind": "artifact",
                "task_id": None,
                "payload": ref,
                "provenance": ref["integrity"],
            }
        )
    manifest = {
        "version": 1,
        "run": projection,
        "repositories": sorted(repositories),
        "plan_digest": plan["digest"],
        "journal": {"count": len(journal), "head": journal[-1]["digest"], "valid": True},
        "artifacts": artifacts,
        "members": entries,
        "complete": True,
        "limits": {**LIMITS, "json_bytes": MAX_BYTES, "snapshot_seconds": 10},
        "execution_actor_provenance": "observed_run_row",
        "run_provenance": "observed_run_row",
        "artifact_bytes": "not_included",
    }
    archived = False
    for row in activity:
        data = row.get("data", {})
        if "activity_content" in data:
            try:
                descriptor = validate_descriptor(data["activity_content"])
                require(
                    (descriptor["run_id"], descriptor["event_id"], descriptor["stage"])
                    == (run["id"], row["id"], data.get("stage"))
                )
            except ActivityUnavailable as exc:
                raise EvidenceUnavailable("evidence_integrity") from exc
            archived = True
    if archived:
        manifest["activity_content_bytes"] = "not_included_with_bound_references"
    manifest["digest"] = digest(manifest)
    if len(encoded(manifest)) + sum(len(encoded(v)) for v in members.values()) > MAX_BYTES:
        raise EvidenceUnavailable("evidence_limit")
    return EvidenceSnapshot(manifest, records, members, frozenset(repositories))


def evidence_page(snapshot: EvidenceSnapshot, query: EvidenceQuery) -> Data:
    if query.expected_cut is not None and query.expected_cut != snapshot.manifest["digest"]:
        raise EvidenceCutChanged("evidence_cut_changed")
    filters = {k: getattr(query, k) for k in ("kind", "task_id", "text", "record_id")}
    filter_digest = digest(filters)
    if query.expected_filter is not None and query.expected_filter != filter_digest:
        raise EvidenceCutChanged("evidence_filter_changed")
    rows = [
        r
        for r in snapshot.records
        if (query.kind == "all" or r["kind"] == query.kind)
        and (query.task_id is None or r["task_id"] == query.task_id)
        and (query.record_id is None or r["id"] == query.record_id)
        and (not query.text or query.text.casefold() in encoded(r["payload"]).decode().casefold())
    ]
    if query.record_id is not None and not rows:
        raise KeyError("Evidence record not found in this run")
    if query.after > len(rows):
        raise EvidenceCutChanged("evidence_page_changed")
    items = []
    for row in rows[query.after : query.after + query.limit]:
        raw = encoded(row["payload"])
        if query.record_id is not None and len(raw) > MAX_RECORD_BYTES:
            raise EvidenceUnavailable("evidence_record_limit")
        preview = raw[:2048].decode("utf-8", errors="ignore")
        items.append(
            {
                **{k: v for k, v in row.items() if k != "payload"},
                "payload_digest": hashlib.sha256(raw).hexdigest(),
                "payload_bytes": len(raw),
                "omitted_bytes": 0 if query.record_id else len(raw) - len(preview.encode()),
                "payload": row["payload"] if query.record_id else {"preview": preview},
            }
        )
    result = {
        "version": 1,
        "manifest": snapshot.manifest,
        "filter_digest": filter_digest,
        "items": items,
        "matched": len(rows),
        "next_after": query.after + len(items) if query.after + len(items) < len(rows) else None,
    }
    result["digest"] = digest(result)
    if len(encoded(result)) > PAGE_BYTES:
        raise EvidenceUnavailable("evidence_page_limit")
    return result


def evidence_zip(snapshot: EvidenceSnapshot) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.comment = b""
        for name, body in sorted({**snapshot.members, "manifest.json": snapshot.manifest}.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.extra = info.comment = b""
            info.external_attr = 0o100600 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, encoded(body))
    return output.getvalue()


@service
class RunEvidence:
    def __init__(self, store: Store) -> None:
        self.store, self.access = store, ProjectAccess(store)

    async def _authorize(self, run: Data, repositories: frozenset[str], reader: Any) -> None:
        if reader is None or CURRENT_PRINCIPAL.get() != reader or reader.authentication_method == "scm_mapping":
            raise AccessDenied("Run evidence requires a current native reader")
        if run.get("project_id"):
            await self.access.require_project(reader, run["project_id"], "read")
        elif not await self.access.current_administrator(reader):
            raise AccessDenied("Unassigned evidence requires a factory administrator")
        for repo in sorted(repositories):
            await self.access.require_repository(reader, repo, "read")

    async def _rows(self, session: AsyncSession, statement: Select[Any], limit: int, remaining: list[int]) -> list[Any]:
        sample = statement.with_only_columns(statement.selected_columns.data).subquery()
        count, size = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(func.octet_length(cast(sample.c.data, Text))), 0))
            )
        ).one()
        if not isinstance(count, int) or not isinstance(size, int) or count > limit or size > remaining[0]:
            raise EvidenceUnavailable("evidence_limit")
        remaining[0] -= size
        return list(await session.scalars(statement))

    async def _capture(self, run_id: str) -> EvidenceSnapshot:
        try:
            return await self._snapshot(run_id)
        except EvidenceUnavailable:
            raise
        except TransactionTimedOutError as exc:
            raise EvidenceUnavailable("snapshot_timeout") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise EvidenceUnavailable("evidence_integrity") from exc

    async def _snapshot(self, run_id: str) -> EvidenceSnapshot:
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
            remaining = [MAX_BYTES]
            runs = await self._rows(
                session, select(ForgeRunEntity).where(ForgeRunEntity.id == identifier(run_id)), 1, remaining
            )
            if not runs:
                raise KeyError("Run not found")
            run = runs[0]
            require(
                run.data.get("id") == run.id.hex
                and run.data.get("repository_id") == run.repository_id.hex
                and run.data.get("status") == run.status
            )
            plans = await self._rows(
                session,
                select(ForgePlanEntity)
                .where(ForgePlanEntity.run_id == run.id)
                .order_by(ForgePlanEntity.revision.desc())
                .limit(1),
                1,
                remaining,
            )
            if not plans:
                raise EvidenceUnavailable("plan_not_observed")
            plan = plans[0]
            require(plan.data.get("revision") == plan.revision and digest(plan.data) == plan.digest)
            journals = await self._rows(
                session,
                select(ForgeJournalEntity)
                .where(ForgeJournalEntity.run_id == run.id)
                .order_by(ForgeJournalEntity.sequence),
                LIMITS["journal"],
                remaining,
            )
            for row in journals:
                require(
                    all(
                        row.data.get(k) == v
                        for k, v in {
                            "run_id": run_id,
                            "sequence": row.sequence,
                            "task_id": row.task_id,
                            "kind": row.kind,
                            "idempotency_key": row.idempotency_key,
                            "digest": row.digest,
                            "previous_hash": row.previous_hash,
                        }.items()
                    )
                )
            tasks = await self._rows(
                session,
                select(ForgeTaskEntity).where(ForgeTaskEntity.run_id == run.id).order_by(ForgeTaskEntity.task_id),
                LIMITS["tasks"],
                remaining,
            )
            require(all(row.plan_id == plan.id for row in tasks))
            tools = await self._rows(
                session,
                select(ForgeToolInvocationEntity)
                .where(ForgeToolInvocationEntity.run_id == run.id)
                .order_by(ForgeToolInvocationEntity.correlation_id),
                LIMITS["tools"],
                remaining,
            )
            events = await self._rows(
                session,
                select(ForgeEventEntity).where(ForgeEventEntity.run_id == run.id).order_by(ForgeEventEntity.sequence),
                LIMITS["activity"],
                remaining,
            )
            require(all(row.data.get("id") == row.id.hex for row in events))
            historical = None
            if plan.data.get("project_id"):
                component = plan.data.get("component_snapshot") or {}
                require(type(component.get("project_revision")) is int and component["project_revision"] > 0)
                revisions = await self._rows(
                    session,
                    select(ForgeProjectRevisionEntity).where(
                        ForgeProjectRevisionEntity.project_id == identifier(plan.data["project_id"]),
                        ForgeProjectRevisionEntity.revision == component.get("project_revision"),
                    ),
                    1,
                    remaining,
                )
                require(len(revisions) == 1)
                historical = revisions[0].data
            return build_evidence(
                run=run.data,
                plan={**plan.data, "digest": plan.digest},
                journal=[r.data for r in journals],
                tasks=[
                    {"id": r.task_id, "attempt": r.attempt, "observed_status": r.status, "data": r.data} for r in tasks
                ],
                tools=[
                    {
                        "id": r.id.hex,
                        "run_id": run_id,
                        "task_id": r.task_id,
                        "tool": r.tool,
                        "correlation_id": r.correlation_id,
                        "status": r.status,
                        "data": r.data,
                    }
                    for r in tools
                ],
                activity=[{"id": r.id.hex, "sequence": r.sequence, "data": r.data} for r in events],
                project_revision=historical,
            )

    async def _read(self, run_id: str) -> EvidenceSnapshot:
        if current_unit_of_work() is not None:
            raise EvidenceUnavailable("caller_transaction")
        run_id = identifier(run_id).hex
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("Run evidence requires authentication")
        # Only native run metadata is read before authorizing access to the full cut.
        async with self.store._unit() as session:
            rows = await session.execute(
                select(
                    ForgeRunEntity.repository_id, func.left(ForgeRunEntity.data["project_id"].as_string(), 33)
                ).where(ForgeRunEntity.id == identifier(run_id))
            )
            header = rows.one_or_none()
            if header is None:
                raise KeyError("Run not found")
            await self._authorize({"repository_id": header[0].hex, "project_id": header[1]}, frozenset(), reader)
        snapshot = await self._capture(run_id)
        await self._authorize(snapshot.manifest["run"], snapshot.repositories, reader)
        try:
            await self.store.admit_credentials([snapshot.manifest, snapshot.members])
        except ValueError as exc:
            raise EvidenceUnavailable("credential_admission") from exc
        await self._authorize(snapshot.manifest["run"], snapshot.repositories, reader)
        current = await self._capture(run_id)
        if current.manifest["digest"] != snapshot.manifest["digest"]:
            raise EvidenceCutChanged("evidence_cut_changed")
        await self._authorize(current.manifest["run"], current.repositories, reader)
        try:
            self.store.reject_credentials([snapshot.manifest, snapshot.members])
        except ValueError as exc:
            raise EvidenceUnavailable("credential_admission") from exc
        return snapshot

    async def query(self, run_id: str, query: EvidenceQuery) -> Data:
        return evidence_page(await self._read(run_id), query)

    async def export(self, run_id: str, expected_cut: str) -> bytes:
        selected = EvidenceExportInput(expected_cut=expected_cut)
        query = EvidenceQuery(expected_cut=selected.expected_cut)
        snapshot = await self._read(run_id)
        evidence_page(snapshot, query)
        return evidence_zip(snapshot)

    async def for_worker(self, run_id: str, query: EvidenceQuery, *, task_id: str) -> Data:
        from firefly_forge.agent_roles import stage_allowed_tools
        from firefly_forge.identity import Principal
        from firefly_forge.identity_persistence import ForgeIdentityUserRepository

        if query.limit > 10:
            raise ValueError("Native evidence reads are limited to ten records")
        async with self.store._unit() as session:
            rows = await self._rows(
                session, select(ForgeRunEntity).where(ForgeRunEntity.id == identifier(run_id)), 1, [MAX_BYTES]
            )
            if not rows:
                raise KeyError("Run not found")
            run = rows[0].data
            await self.access.authorize_execution(run)
            actor = run.get("execution_actor") or {}
            method = actor.get("authentication_method")
            if method not in {"local_bootstrap", "oidc_bearer", "oidc_session"} or not actor.get("user_id"):
                raise AccessDenied("Native evidence queries require the original native identity")
            if "read_run_evidence" not in (
                stage_allowed_tools(
                    run.get("role_snapshot", {}), task_id, "agentic" if run["agent"] == "api" else run["agent"]
                )
                or set()
            ):
                raise AccessDenied("The frozen role does not grant retained evidence reads")
            user = await ForgeIdentityUserRepository().find_by_id(identifier(actor["user_id"]))
            if user is None or not user.enabled:
                raise AccessDenied("The original execution identity is unavailable")
            reader = Principal(
                user.id,
                user.issuer,
                user.subject,
                authentication_method=method,
                api_roles=frozenset(user.api_roles),
                api_audience=actor.get("api_audience"),
            )
        token = CURRENT_PRINCIPAL.set(reader)
        try:
            result = await self.query(run_id, query)
            if len(encoded(result)) > 32768:
                raise EvidenceUnavailable("native_evidence_result_limit")
            async with self.store._unit():
                fresh = await self.store.run_records.find_by_id(identifier(run_id))
                if fresh is None or fresh.data.get("execution_actor") != actor:
                    raise AccessDenied("The original execution identity changed")
                await self.access.authorize_execution(fresh.data)
            return result
        finally:
            CURRENT_PRINCIPAL.reset(token)
