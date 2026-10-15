# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read-only campaign snapshots cite persisted evidence, never live SCM or estimated completion."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field
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

from firefly_forge.journal import ZERO_HASH, Journal, digest
from firefly_forge.persistence import (
    ForgeEventEntity,
    ForgeJournalEntity,
    ForgePlanEntity,
    ForgeRunEntity,
    ForgeTaskEntity,
)
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_campaign_persistence import (
    ForgeCampaignChildEntity,
    ForgeCampaignEntity,
    ForgeCampaignEventEntity,
)
from firefly_forge.project_campaigns import ProjectCampaigns
from firefly_forge.store import Store, identifier

Data = dict[str, Any]
ReportState = Literal["verified", "blocked", "unknown", "in_progress", "not_started"]
MAX_CHILDREN, MAX_EVENTS, MAX_TASKS, MAX_JOURNAL = 200, 1024, 128, 2048
MAX_EVIDENCE_BYTES = 16_777_216


class ReportReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sequence: int
    digest: str


class ReportJournal(ReportReference):
    head: str


class ReportTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    attempt: int
    state: Literal["receipted", "in_progress", "not_started", "unknown", "blocked"]
    evidence_digest: str | None
    receipt: ReportReference | None


class ReportCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    command_digest: str
    argument_count: int
    exit_code: int
    receipt: ReportReference


class ReportAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    attempt: int
    run_id: str
    admission_digest: str


class ReportCampaign(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    recorded_status: str | None = None
    project_digest: str | None = None
    request_digest: str | None = None
    journal_head: str | None = None
    event_count: int | None = None


class ReportComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    component_id: str
    repository_id: str
    run_id: str
    attempt: int
    state: ReportState
    recorded_status: str
    reason: str | None
    verification: Literal["verified", "not_verified"]
    admission_digest: str
    run_digest: str
    source_base: str
    branch: str | None
    branch_receipt: ReportReference | None
    checkpoint_revision: str | None
    source_tree: str | None
    source_tree_receipt: ReportReference | None
    checkpoint_receipt: ReportReference | None
    plan_digest: str | None
    journal: ReportJournal
    tasks: list[ReportTask]
    checks: list[ReportCheck]
    prior_attempts: list[ReportAttempt]
    depends_on: list[str]


class CampaignReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    project_id: str
    campaign: ReportCampaign
    state: ReportState
    reason: str | None
    counts: dict[str, int] | None
    components: list[ReportComponent]
    milestones: Literal["not_configured"] = "not_configured"
    waves: Literal["not_configured"] = "not_configured"
    scm_freshness: Literal["not_observed"] = "not_observed"
    limits: dict[str, int] = Field(
        default_factory=lambda: {
            "attempts": MAX_CHILDREN,
            "campaign_events": MAX_EVENTS,
            "tasks_per_run": MAX_TASKS,
            "journal_entries_per_run": MAX_JOURNAL,
            "run_events_per_run": MAX_JOURNAL,
            "snapshot_seconds": 10,
            "evidence_json_bytes": MAX_EVIDENCE_BYTES,
        }
    )
    digest: str


class CampaignNotFound(KeyError):
    pass


class ReportUnavailable(Exception):
    pass


def require(condition: Any) -> None:
    if not condition:
        raise ReportUnavailable("evidence_integrity")


def reference(entry: Data) -> Data:
    return {"sequence": entry["sequence"], "digest": entry["digest"]}


@service
class CampaignReports:
    def __init__(self, store: Store, campaigns: ProjectCampaigns, journal: Journal) -> None:
        self.store, self.campaigns, self.journal = store, campaigns, journal
        self.access = ProjectAccess(store)

    async def _authorize(self, project_id: str, repositories: set[str]) -> None:
        principal = CURRENT_PRINCIPAL.get()
        if principal is None:
            raise AccessDenied("Campaign reports require an authenticated reader")
        await self.access.require_project(principal, project_id, "read")
        for repository_id in sorted(repositories):
            await self.access.require_repository(principal, repository_id, "read")

    async def report(self, project_id: str, campaign_id: str) -> Data:
        project_id, campaign_id = identifier(project_id).hex, identifier(campaign_id).hex
        await self._authorize(project_id, set())
        # Capture the Store's actual datasource manager without installing a global registry.
        async with self.store._unit():
            unit = current_unit_of_work()
            assert unit is not None
            manager = unit.manager
        try:
            async with TransactionTemplate(
                manager,
                propagation=Propagation.REQUIRES_NEW,
                isolation=Isolation.REPEATABLE_READ,
                read_only=True,
                timeout=10,
            ).transaction():
                result, repositories = await self._snapshot(project_id, campaign_id)
        except CampaignNotFound:
            raise
        except (ReportUnavailable, TransactionTimedOutError, KeyError, ValueError, TypeError) as error:
            result = {
                "project_id": project_id,
                "campaign": {"id": campaign_id},
                "state": "unknown",
                "reason": str(error)
                if isinstance(error, ReportUnavailable)
                else "snapshot_timeout"
                if isinstance(error, TransactionTimedOutError)
                else "evidence_integrity",
                "counts": None,
                "components": [],
            }
            repositories = set()
        result = CampaignReport(**result, digest=ZERO_HASH).model_dump(mode="json")
        result["digest"] = digest({key: value for key, value in result.items() if key != "digest"})
        await self._authorize(project_id, repositories)
        await self.store.admit_credentials(result)
        await self._authorize(project_id, repositories)
        return result

    async def _rows(self, session: AsyncSession, statement: Select[Any], limit: int, remaining: list[int]) -> list[Any]:
        sample = statement.with_only_columns(statement.selected_columns.data).subquery()
        count, size = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(func.octet_length(cast(sample.c.data, Text))), 0))
            )
        ).one()
        if not isinstance(count, int) or not isinstance(size, int):
            raise ReportUnavailable("evidence_integrity")
        if count > limit or size > remaining[0]:
            raise ReportUnavailable("evidence_limit")
        remaining[0] -= size
        return list(await session.scalars(statement))

    async def _snapshot(self, project_id: str, campaign_id: str) -> tuple[Data, set[str]]:
        async with self.store._unit() as session:
            remaining = [MAX_EVIDENCE_BYTES]
            headers = await self._rows(
                session,
                select(ForgeCampaignEntity)
                .where(
                    ForgeCampaignEntity.id == identifier(campaign_id),
                    ForgeCampaignEntity.project_id == identifier(project_id),
                )
                .limit(1),
                1,
                remaining,
            )
            if not headers:
                raise CampaignNotFound("Delivery campaign not found in this project")
            campaign = headers[0]
            children = await self._rows(
                session,
                select(ForgeCampaignChildEntity)
                .where(ForgeCampaignChildEntity.campaign_id == campaign.id)
                .order_by(ForgeCampaignChildEntity.component_id, ForgeCampaignChildEntity.attempt)
                .limit(MAX_CHILDREN + 1),
                MAX_CHILDREN,
                remaining,
            )
            events = await self._rows(
                session,
                select(ForgeCampaignEventEntity)
                .where(ForgeCampaignEventEntity.campaign_id == campaign.id)
                .order_by(ForgeCampaignEventEntity.sequence)
                .limit(MAX_EVENTS + 1),
                MAX_EVENTS,
                remaining,
            )
            entries = [copy.deepcopy(row.data) for row in events]
            require(entries and self.campaigns._verify(entries))
            require(
                all(
                    row.data["campaign_id"] == campaign_id
                    and row.data["sequence"] == row.sequence
                    and row.data["key"] == row.key
                    for row in events
                )
            )
            require(entries[0]["kind"] == "launch")
            states = [entry for entry in entries if entry["kind"] == "state"]
            require(campaign.status == (states[-1]["payload"]["status"] if states else "running"))
            frozen = campaign.data["project_snapshot"]
            require(
                frozen["id"] == project_id
                and frozen["digest"]
                == hashlib.sha256(json.dumps(frozen["definition"], sort_keys=True).encode()).hexdigest()
            )
            launch = entries[0]["payload"]
            require(launch["request_digest"] == campaign.data["request_digest"] == digest(campaign.data["launch"]))
            require(launch["project_digest"] == campaign.data["project_snapshot"]["digest"])
            current = campaign.data["current_attempts"]
            require(set(current) == set(launch["component_ids"]))
            require(all(current[c] == max(row.attempt for row in children if row.component_id == c) for c in current))
            reports, repositories = [], set()
            for child in children:
                admitted = child.data["admission"]
                repository_id = admitted["repository_id"]
                repositories.add(repository_id)
                require(child.data["admission_digest"] == digest(admitted))
                require(admitted["id"] == child.run_id.hex and admitted["project_id"] == project_id)
                if current[child.component_id] != child.attempt:
                    continue
                runs = await self._rows(
                    session, select(ForgeRunEntity).where(ForgeRunEntity.id == child.run_id).limit(1), 1, remaining
                )
                require(runs)
                run_row = runs[0]
                run = copy.deepcopy(run_row.data)
                context = run.get("campaign_context") or {}
                require(
                    run["id"] == child.run_id.hex
                    and run["status"] == run_row.status
                    and run["repository_id"] == repository_id
                    and run["project_id"] == project_id
                    and run.get("component_snapshot") == admitted.get("component_snapshot")
                    and context.get("campaign_id") == campaign_id
                    and context.get("component_id") == child.component_id
                    and context.get("attempt") == child.attempt
                    and context.get("source_base") == launch["source_bases"][repository_id]
                )
                item = await self._run(run, campaign.data["launch"]["dependency_gate"], remaining)
                reports.append(
                    {
                        **item,
                        "component_id": child.component_id,
                        "repository_id": repository_id,
                        "attempt": child.attempt,
                        "admission_digest": child.data["admission_digest"],
                        "source_base": context["source_base"],
                        "depends_on": child.data["depends_on"],
                        "prior_attempts": [
                            {
                                "attempt": old.attempt,
                                "run_id": old.run_id.hex,
                                "admission_digest": old.data["admission_digest"],
                            }
                            for old in children
                            if old.component_id == child.component_id and old.attempt < child.attempt
                        ],
                    }
                )
            require(len(reports) == len(current))
            counts = dict(sorted(Counter(item["state"] for item in reports).items()))
            state = next((s for s in ("unknown", "blocked", "in_progress", "not_started") if counts.get(s)), "verified")
            return {
                "project_id": project_id,
                "campaign": {
                    "id": campaign_id,
                    "recorded_status": campaign.status,
                    "project_digest": launch["project_digest"],
                    "request_digest": campaign.data["request_digest"],
                    "journal_head": entries[-1]["digest"],
                    "event_count": len(entries),
                },
                "state": state,
                "reason": None,
                "counts": counts,
                "components": reports,
            }, repositories

    async def _run(self, run: Data, gate: str, remaining: list[int]) -> Data:
        async with self.store._unit() as session:
            run_id = identifier(run["id"])
            plans = await self._rows(
                session,
                select(ForgePlanEntity)
                .where(ForgePlanEntity.run_id == run_id)
                .order_by(ForgePlanEntity.revision.desc())
                .limit(1),
                1,
                remaining,
            )
            plan = plans[0] if plans else None
            tasks = await self._rows(
                session,
                select(ForgeTaskEntity)
                .where(ForgeTaskEntity.run_id == run_id)
                .order_by(ForgeTaskEntity.task_id)
                .limit(MAX_TASKS + 1),
                MAX_TASKS,
                remaining,
            )
            rows = await self._rows(
                session,
                select(ForgeJournalEntity)
                .where(ForgeJournalEntity.run_id == run_id)
                .order_by(ForgeJournalEntity.sequence)
                .limit(MAX_JOURNAL + 1),
                MAX_JOURNAL,
                remaining,
            )
            entries = [copy.deepcopy(row.data) for row in rows]
            require(Journal.verify_chain(entries))
            require(
                all(
                    row.digest == row.data["digest"]
                    and row.kind == row.data["kind"]
                    and row.task_id == row.data["task_id"]
                    and row.idempotency_key == row.data["idempotency_key"]
                    and row.data["run_id"] == run["id"]
                    for row in rows
                )
            )
            if plan is not None:
                require(
                    digest(plan.data) == plan.digest
                    and any(e["kind"] == "plan" and e["payload"].get("digest") == plan.digest for e in entries)
                )
            else:
                require(not tasks and not entries)
            planned = {item["id"] for item in plan.data["tasks"]} if plan else set()
            task_reports: list[Data] = []
            receipts: dict[str, Data] = {}
            for task in tasks:
                require(plan is not None and task.plan_id == plan.id)
                require(
                    task.task_id in planned
                    or (
                        task.data.get("kind") == "remote"
                        and task.task_id.startswith(("publish", "deploy", "rollback", "memory"))
                    )
                )
                receipt = None
                if task.status in {"done", "running", "needs_reconciliation"}:
                    key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:{task.task_id}:{task.attempt}").hex
                    require(task.data.get("idempotency_key") == key)
                    causal = [
                        entry
                        for entry in entries
                        if entry["task_id"] == task.task_id
                        and entry["idempotency_key"] == key
                        and entry["kind"] == "intent"
                    ]
                    require(len(causal) == 1)
                    if task.status == "done":
                        completed = [
                            entry
                            for entry in entries
                            if entry["task_id"] == task.task_id
                            and entry["idempotency_key"] == key
                            and entry["kind"] == "receipt"
                        ]
                        require(len(completed) == 1)
                        receipt = completed[0]
                        require(
                            causal[0]["sequence"] < receipt["sequence"]
                            and receipt["payload"] == task.data.get("evidence")
                        )
                        receipts[task.task_id] = receipt
                task_reports.append(
                    {
                        "id": task.task_id,
                        "attempt": task.attempt,
                        "state": "receipted"
                        if task.status == "done"
                        else "in_progress"
                        if task.status == "running"
                        else "not_started"
                        if task.status == "queued"
                        else "unknown"
                        if task.status == "needs_reconciliation"
                        else "blocked",
                        "evidence_digest": digest(receipt["payload"]) if receipt else None,
                        "receipt": reference(receipt) if receipt else None,
                    }
                )
            prepare, checkpoint, verify = (receipts.get(key) for key in ("prepare", "checkpoint", "verify"))
            branch = run.get("branch") or None
            if branch:
                require(prepare is not None and prepare["payload"].get("branch") == branch)
            commit, tree = run.get("checkpoint_revision"), run.get("source_tree")
            tree_receipts = (
                [item for item in receipts.values() if item["payload"].get("source_tree") == tree] if tree else []
            )
            require(not tree or tree_receipts)
            tree_receipt = max(tree_receipts, key=lambda item: item["sequence"]) if tree_receipts else None
            if commit:
                require(
                    checkpoint is not None
                    and checkpoint["payload"].get("checkpoint_revision") == commit
                    and checkpoint["payload"].get("source_tree") == tree
                )
            checks = run.get("final_verification") or []
            check_reference: Data = {}
            if checks:
                require(
                    verify is not None
                    and verify["payload"].get("checks") == checks
                    and verify["payload"].get("source_tree") == tree
                )
                assert verify is not None
                check_reference = reference(verify)
                require(
                    all(
                        isinstance(check.get("argv"), list)
                        and 1 <= len(check["argv"]) <= 64
                        and all(isinstance(arg, str) and 0 < len(arg) <= 8192 for arg in check["argv"])
                        and type(check.get("exit_code")) is int
                        for check in checks
                    )
                )
            state = "in_progress"
            reason = None
            verified = False
            pending = any(task.status in {"running", "needs_reconciliation"} for task in tasks)
            incomplete_plan = any(task.task_id in planned and task.status != "done" for task in tasks)
            if (
                run["status"] == "needs_reconciliation"
                or run.get("publication_status") == "publishing"
                or (run["status"] in {"succeeded", "failed", "cancelled", "interrupted"} and pending)
                or (run["status"] == "succeeded" and incomplete_plan)
            ):
                state = "unknown"
                reason = "The recorded run has an outcome that requires reconciliation."
            elif run["status"] == "succeeded":
                await self._rows(
                    session,
                    select(ForgeEventEntity)
                    .where(ForgeEventEntity.run_id == run_id)
                    .order_by(ForgeEventEntity.sequence)
                    .limit(MAX_JOURNAL + 1),
                    MAX_JOURNAL,
                    remaining,
                )
                reason = await self.campaigns._gate(run, gate)
                verified = reason is None
                state = "verified" if verified else "unknown"
            elif run["status"] in {"failed", "cancelled", "interrupted", "paused", "campaign_blocked"}:
                state = "blocked"
                reason = "The recorded run is blocked, stopped or awaiting an explicit operator decision."
            return {
                "run_id": run["id"],
                "run_digest": digest(run),
                "recorded_status": run["status"],
                "reason": reason,
                "state": state,
                "verification": "verified" if verified else "not_verified",
                "branch": branch,
                "branch_receipt": reference(prepare) if branch and prepare else None,
                "checkpoint_revision": commit,
                "source_tree": tree,
                "source_tree_receipt": reference(tree_receipt) if tree_receipt else None,
                "checkpoint_receipt": reference(checkpoint) if checkpoint else None,
                "plan_digest": plan.digest if plan else None,
                "journal": {
                    "sequence": len(entries),
                    "digest": entries[-1]["digest"] if entries else ZERO_HASH,
                    "head": entries[-1]["digest"] if entries else ZERO_HASH,
                },
                "tasks": task_reports,
                "checks": [
                    {
                        "command_digest": digest({"argv": check["argv"]}),
                        "argument_count": len(check["argv"]) - 1,
                        "exit_code": check["exit_code"],
                        "receipt": check_reference,
                    }
                    for check in checks
                ],
            }
