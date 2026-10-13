# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable component DAG coordination; child workers retain their existing effect journals."""

from __future__ import annotations

import asyncio
import copy
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.kernel.lifecycle import CONSUMER_PHASE
from sqlalchemy import text

from firefly_forge.engine import Engine
from firefly_forge.journal import Journal, digest
from firefly_forge.knowledge import KnowledgeSettings
from firefly_forge.models import AssuranceInput, RunInput
from firefly_forge.persistence import ForgeRunEntity
from firefly_forge.project_campaign_persistence import (
    ForgeCampaignChildEntity,
    ForgeCampaignChildRepository,
    ForgeCampaignEntity,
    ForgeCampaignEventEntity,
    ForgeCampaignEventRepository,
    ForgeCampaignRepository,
)
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.projects import Projects
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]
SourceResolver = Callable[[Data], Awaitable[str]]
ZERO = "0" * 64


class CampaignLaunch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    brief: str = Field(min_length=1, max_length=40000)
    component_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)
    agent: Literal["codex", "claude", "bridge", "agentic"] = "codex"
    operation: Literal["build", "maintain", "test"] = "build"
    assurance: AssuranceInput = Field(default_factory=AssuranceInput)
    dependency_gate: Literal["verified", "published"] = "verified"
    publish: bool = False

    @model_validator(mode="after")
    def deliverable(self) -> CampaignLaunch:
        if self.operation == "test" and (self.publish or self.dependency_gate == "published"):
            raise ValueError("Test campaigns cannot require or request publication")
        if self.component_ids and len(set(self.component_ids)) != len(self.component_ids):
            raise ValueError("Choose each component only once")
        return self


class CampaignAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class CampaignRetry(CampaignAction):
    component_ids: list[str] = Field(min_length=1, max_length=50)


@service
class ProjectCampaigns:
    phase = CONSUMER_PHASE + 10

    def __init__(
        self,
        store: Store,
        engine: Engine,
        journal: Journal,
        records: ForgeCampaignRepository | None = None,
        children: ForgeCampaignChildRepository | None = None,
        events: ForgeCampaignEventRepository | None = None,
        *,
        source_resolver: SourceResolver | None = None,
    ) -> None:
        self.store, self.engine, self.journal = store, engine, journal
        self.records = records or ForgeCampaignRepository()
        self.children = children or ForgeCampaignChildRepository()
        self.events = events or ForgeCampaignEventRepository()
        self.source_resolver = source_resolver
        self.poller: asyncio.Task[None] | None = None
        self.last_error: str | None = None

    async def _source_base(self, repository: Data) -> str:
        resolver = self.source_resolver or getattr(self.engine, "resolve_campaign_source", None)
        if resolver is None:
            raise ValueError("Campaign source commit resolution is unavailable")
        value: str = await resolver(repository)
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value):
            raise ValueError("Campaign source resolution must return an actual Git commit")
        return value

    def _configuration_vector(self, product_repository_id: str, agents: set[str]) -> Data:
        private = self.store.configuration
        role = self.store.roles.current()
        knowledge = private._read(private.directory / "knowledge.json")
        connections = self.engine.connections
        return {
            "policy": self.store.policies.current()["digest"],
            "roles": role["digest"],
            "role_revision": role["revision"],
            "scm_applications": {
                provider: connections._record(provider).get("revision")
                for provider in ("github", "gitlab", "bitbucket")
            }
            if connections
            else {},
            "knowledge_settings": digest(KnowledgeSettings.model_validate(knowledge.get("settings", {})).model_dump()),
            "brand": digest(private._read(private.directory / f"product-brand-{product_repository_id}.json")),
            "knowledge": digest(private._read(private.directory / "knowledge.json")),
            "runners": digest(private._read(private.directory / "runners.json")),
            "search": digest(self.engine.search.snapshot()) if self.engine.search is not None else None,
            "credentials": {agent: digest(private.resolve(agent)) for agent in sorted(agents)},
        }

    def _validate_admission_snapshots(self, run: Data, configuration: Data) -> None:
        private = self.store.configuration
        if (
            run["policy_snapshot"]["digest"] != configuration["policy"]
            or run["role_snapshot"]["revision"] != configuration["role_revision"]
            or digest(run.get("branding_snapshot") or {}) != configuration["brand"]
            or digest(private.load_snapshot(run["config_revision"])) != configuration["credentials"][run["agent"]]
        ):
            raise ValueError("The child admission snapshot differs from the campaign's frozen settings")
        reviewer = run.get("assurance", {}).get("review_agent")
        if (
            reviewer
            and digest(private.load_snapshot(run["review_config_revision"])) != configuration["credentials"][reviewer]
        ):
            raise ValueError("The reviewer snapshot differs from the campaign's frozen settings")
        revision = run["knowledge_snapshot"]["revision"]
        if (
            digest(private._read(private.revisions / f"knowledge-{revision}.json"))
            != configuration["knowledge_settings"]
        ):
            raise ValueError("The knowledge snapshot differs from the campaign's frozen settings")
        if run.get("search_snapshot") and digest(run["search_snapshot"]) != configuration["search"]:
            raise ValueError("The search snapshot differs from the campaign's frozen settings")

    @staticmethod
    def _scope(definition: ProjectDefinition, requested: list[str] | None) -> list[str]:
        graph = {item.id: item.depends_on for item in definition.components}
        selected = set(requested or graph)
        if selected - graph.keys():
            raise ValueError("Campaign components must belong to the frozen project")
        pending = list(selected)
        while pending:
            for dependency in graph[pending.pop()]:
                if dependency not in selected:
                    selected.add(dependency)
                    pending.append(dependency)
        return [item.id for item in definition.components if item.id in selected]

    async def _locked(self, project_id: str, campaign_id: str) -> ForgeCampaignEntity:
        row = await self.records.find_by_id(identifier(campaign_id), lock=LockMode.PESSIMISTIC_WRITE)
        if row is None or row.project_id != identifier(project_id):
            raise KeyError("Delivery campaign not found")
        return row

    async def _append(self, row: ForgeCampaignEntity, key: str, kind: str, payload: Data) -> Data:
        entries = await self.events.events(row.id)
        previous = next((item for item in entries if item.key == key), None)
        if previous is not None:
            if previous.data["kind"] != kind or previous.data["payload"] != payload:
                raise ValueError("Campaign request identity cannot be reused for different content")
            return copy.deepcopy(previous.data)
        data = {
            "campaign_id": row.id.hex,
            "sequence": len(entries) + 1,
            "key": key,
            "kind": kind,
            "payload": copy.deepcopy(payload),
            "created_at": now(),
            "previous_hash": entries[-1].data["digest"] if entries else ZERO,
        }
        data["digest"] = digest(data)
        await self.events.save(
            ForgeCampaignEventEntity(
                id=uuid.uuid4(),
                campaign_id=row.id,
                sequence=data["sequence"],
                key=key,
                data=data,
            )
        )
        return data

    @staticmethod
    def _verify(entries: list[Data]) -> bool:
        previous = ZERO
        for sequence, entry in enumerate(entries, 1):
            signed = {key: value for key, value in entry.items() if key != "digest"}
            if entry["sequence"] != sequence or entry["previous_hash"] != previous or digest(signed) != entry["digest"]:
                return False
            previous = entry["digest"]
        return True

    async def launch(self, project_id: str, request: CampaignLaunch) -> Data:
        self.store.reject_credentials(request.model_dump())
        if not self.store.runtime_roles or self.store.runtime_roles.executes_jobs:
            await self.engine.assert_ownership()
        request_digest = digest(request.model_dump())
        async with self.store._unit():
            existing = await self.records.request(identifier(project_id), request.request_id)
            if existing is not None:
                if existing.data["request_digest"] != request_digest:
                    raise ValueError("Campaign request identity already has another launch definition")
                return await self.campaign(project_id, existing.id.hex)
        project = await Projects(self.store).project(project_id)
        definition = ProjectDefinition.model_validate(project["definition"])
        selected = self._scope(definition, request.component_ids)
        repositories = {item.repository_id for item in definition.components if item.id in selected}
        agents: set[str] = {request.agent}
        if request.assurance.review_agent:
            agents.add(request.assurance.review_agent)
        configuration = self._configuration_vector(str(definition.product_repository_id), agents)
        prepared: dict[str, tuple[Data, list[Data]]] = {}
        if self.store.runners is not None:
            for repo_id in sorted(repositories):
                runner = await self.store.runners.capture_run(repo_id)
                tools = await self.store.runners.capabilities(runner)
                prepared[repo_id] = (runner, tools)
        source_bases = {
            repo_id: await self._source_base(await self.store.repository(repo_id)) for repo_id in sorted(repositories)
        }
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"forge:campaign:{self.store.schema}:{project_id}:{request.request_id}"},
            )
            existing = await self.records.request(identifier(project_id), request.request_id)
            if existing is not None:
                if existing.data["request_digest"] != request_digest:
                    raise ValueError("Campaign request identity already has another launch definition")
                return await self.campaign(project_id, existing.id.hex)
            current = await ForgeProjectRepository().find_by_id(identifier(project_id), lock=LockMode.PESSIMISTIC_WRITE)
            if current is None or current.data["digest"] != project["digest"]:
                raise ValueError("The project changed during campaign admission; launch again with a new request")
            for repo_id in sorted(repositories | {str(definition.product_repository_id)}):
                await self.store.repository_records.find_by_id(identifier(repo_id), lock=LockMode.PESSIMISTIC_WRITE)
            if self._configuration_vector(str(definition.product_repository_id), agents) != configuration:
                raise ValueError("Campaign settings changed during source and runner preflight; launch again")
            row = ForgeCampaignEntity(
                id=uuid.uuid4(),
                project_id=identifier(project_id),
                request_id=request.request_id,
                status="running",
                data={
                    "request_digest": request_digest,
                    "launch": request.model_dump(),
                    "project_snapshot": copy.deepcopy(project),
                    "current_attempts": {item: 1 for item in selected},
                    "created_at": now(),
                    "updated_at": now(),
                },
            )
            await self.records.save(row)
            first: Data | None = None
            for component in definition.components:
                if component.id not in selected:
                    continue
                input_run = RunInput(
                    repository_id=component.repository_id,
                    project_id=project_id,
                    component_id=component.id,
                    brief=request.brief,
                    agent=request.agent,
                    operation=request.operation,
                    assurance=request.assurance,
                    publish=request.publish,
                )
                if component.repository_id in prepared:
                    runner, tools = prepared[component.repository_id]
                    run = await self.store.create_run(
                        input_run, prepared_runner=copy.deepcopy(runner), prepared_tools=copy.deepcopy(tools)
                    )
                else:
                    run = await self.store.create_run(input_run)
                self._validate_admission_snapshots(run, configuration)
                if first is not None:
                    for field in ("product_definition", "branding_snapshot", "design_snapshot"):
                        if run.get(field) != first.get(field):
                            raise ValueError("Product or brand snapshots changed during campaign admission")
                    for field in (
                        "config_revision",
                        "execution_config",
                        "review_config_revision",
                        "review_execution_config",
                        "knowledge_snapshot",
                    ):
                        run[field] = copy.deepcopy(first.get(field))
                else:
                    first = run
                if self._configuration_vector(str(definition.product_repository_id), agents) != configuration:
                    raise ValueError("Policy, role, brand or credential settings changed during campaign admission")
                run.pop("events", None)
                run.update(
                    status="campaign_blocked",
                    campaign_context={
                        "campaign_id": row.id.hex,
                        "component_id": component.id,
                        "attempt": 1,
                        "source_base": source_bases[component.repository_id],
                        "dependency_gate": request.dependency_gate,
                        "upstream_receipts": [],
                        "integration": "Frozen source base; upstream artifacts are references, not merged code.",
                    },
                )
                await self.store.update(run["id"], **{key: value for key, value in run.items() if key != "id"})
                await self.children.save(
                    ForgeCampaignChildEntity(
                        id=uuid.uuid4(),
                        campaign_id=row.id,
                        component_id=component.id,
                        attempt=1,
                        run_id=identifier(run["id"]),
                        data={
                            "admission": copy.deepcopy(run),
                            "depends_on": component.depends_on,
                            "admission_digest": digest(run),
                        },
                    )
                )
            await self._append(
                row,
                "launch",
                "launch",
                {
                    "request_digest": request_digest,
                    "project_digest": project["digest"],
                    "component_ids": selected,
                    "source_bases": source_bases,
                },
            )
            await self._release(row)
            return await self.campaign(project_id, row.id.hex)

    async def campaigns(self, project_id: str) -> list[Data]:
        await Projects(self.store).project(project_id)
        async with self.store._unit():
            return [
                await self.campaign(project_id, row.id.hex) for row in await self.records.latest(identifier(project_id))
            ]

    async def campaign(self, project_id: str, campaign_id: str) -> Data:
        async with self.store._unit():
            row = await self.records.find_by_id(identifier(campaign_id))
            if row is None or row.project_id != identifier(project_id):
                raise KeyError("Delivery campaign not found")
            children = []
            for child in await self.children.children(row.id):
                run = await self.store.get_run(child.run_id.hex)
                children.append(
                    {
                        "component_id": child.component_id,
                        "attempt": child.attempt,
                        "run_id": child.run_id.hex,
                        "current": row.data["current_attempts"][child.component_id] == child.attempt,
                        "depends_on": child.data["depends_on"],
                        "admission_digest": child.data["admission_digest"],
                        "status": run["status"],
                        "branch": run.get("branch"),
                        "error": run.get("error"),
                        "source_base": run["campaign_context"]["source_base"],
                        "blocked_reason": run["campaign_context"].get("blocked_reason"),
                        "upstream_receipts": run["campaign_context"]["upstream_receipts"],
                    }
                )
            events = [copy.deepcopy(item.data) for item in await self.events.events(row.id)]
            return {
                "id": row.id.hex,
                "project_id": row.project_id.hex,
                "status": row.status,
                **copy.deepcopy(row.data),
                "children": children,
                "journal": {
                    "verified": self._verify(events),
                    "head": events[-1]["digest"] if events else ZERO,
                    "entries": events,
                },
            }

    async def _unknown(self, run: Data) -> bool:
        if run["status"] == "needs_reconciliation" or run.get("publication_status") == "publishing":
            return True
        if run["status"] not in {"succeeded", "failed", "cancelled", "interrupted"}:
            return False
        try:
            plan = await self.journal.plan(run["id"])
        except KeyError:
            return False
        return any(task["status"] in {"running", "needs_reconciliation"} for task in plan["tasks"])

    async def _gate(self, run: Data, gate: str = "verified") -> str | None:
        if run["status"] != "succeeded":
            return "The upstream run has not succeeded"
        checks = run.get("final_verification", [])
        if not checks or any(not check.get("argv") or check.get("exit_code") != 0 for check in checks):
            return "The upstream run has no successful executable verification receipt"
        tree, commit = run.get("source_tree"), run.get("checkpoint_revision")
        if not tree or not commit:
            return "The upstream verified source checkpoint is unavailable"
        if run.get("assurance", {}).get("review_agent") and (
            run.get("review", {}).get("status") != "approved" or run["review"].get("source_tree") != tree
        ):
            return "The upstream source still requires its requested independent review"
        try:
            plan = await self.journal.plan(run["id"])
        except KeyError:
            return "The upstream execution journal is unavailable"
        if not Journal.verify_chain(await self.journal.entries(run["id"])):
            return "The upstream execution journal does not verify"
        tasks = {task["id"]: task for task in plan["tasks"]}
        verify = tasks.get("verify", {})
        checkpoint = tasks.get("checkpoint", {})
        if (
            verify.get("status") != "done"
            or verify.get("evidence", {}).get("checks") != checks
            or verify.get("evidence", {}).get("source_tree") != tree
            or checkpoint.get("status") != "done"
            or checkpoint.get("evidence", {}).get("source_tree") != tree
            or checkpoint.get("evidence", {}).get("checkpoint_revision") != commit
        ):
            return "The upstream verification and checkpoint receipts do not match the recorded source"
        if run.get("assurance", {}).get("review_agent"):
            review = tasks.get("review", {})
            if (
                review.get("status") != "done"
                or review.get("evidence", {}).get("status") != "approved"
                or review.get("evidence", {}).get("source_tree") != tree
            ):
                return "The upstream independent review does not have a matching durable receipt"
        if gate == "published" or run.get("publish"):
            publication = tasks.get("publish", {})
            if (
                run.get("publication_status") != "published"
                or not run.get("pr_url")
                or publication.get("status") != "done"
                or publication.get("evidence", {}).get("pr_url") != run["pr_url"]
                or publication.get("evidence", {}).get("source_tree") != tree
            ):
                return "The upstream run requires an explicit, receipted publication"
        return None

    @staticmethod
    def _upstream_receipt(component_id: str, run: Data) -> Data:
        return {
            "component_id": component_id,
            "run_id": run["id"],
            "repository_id": run["repository_id"],
            "source_base": run["campaign_context"]["source_base"],
            "checkpoint_revision": run["checkpoint_revision"],
            "source_tree": run["source_tree"],
            "branch": run.get("branch"),
            "pr_url": run.get("pr_url"),
            "publication_status": run.get("publication_status"),
            "artifacts": copy.deepcopy(run.get("artifacts", [])),
            "checks": [{"argv": check["argv"], "exit_code": check["exit_code"]} for check in run["final_verification"]],
            "review_status": run.get("review", {}).get("status", "not_requested"),
        }

    async def _set_state(self, row: ForgeCampaignEntity, status: str) -> None:
        if row.status != status:
            row.status = status
            row.data = {**row.data, "updated_at": now()}
            await self.records.save(row)
            await self._append(row, uuid.uuid4().hex, "state", {"status": status})

    async def _release(self, row: ForgeCampaignEntity) -> None:
        if row.data.get("control") == "cancelled":
            cancelled_runs = [
                await self.store.get_run(child.run_id.hex)
                for child in await self.children.children(row.id)
                if row.data["current_attempts"][child.component_id] == child.attempt
            ]
            if any([await self._unknown(run) for run in cancelled_runs]):
                await self._set_state(row, "needs_reconciliation")
            elif all(run["status"] in {"succeeded", "failed", "cancelled", "interrupted"} for run in cancelled_runs):
                await self._set_state(row, "cancelled")
            return
        if row.data.get("pending_action"):
            return
        children = [
            child
            for child in await self.children.children(row.id)
            if row.data["current_attempts"][child.component_id] == child.attempt
        ]
        runs = {child.component_id: await self.store.get_run(child.run_id.hex) for child in children}
        if any([await self._unknown(run) for run in runs.values()]):
            await self._set_state(row, "needs_reconciliation")
            return
        for child in children:
            run = runs[child.component_id]
            if run["status"] != "campaign_blocked":
                continue
            reasons = []
            for component_id in child.data["depends_on"]:
                reason = await self._gate(runs[component_id], row.data["launch"]["dependency_gate"])
                if reason:
                    reasons.append(f"{component_id}: {reason}")
            if reasons:
                context = {**run["campaign_context"], "blocked_reason": "; ".join(reasons)}
                if context != run["campaign_context"]:
                    await self.store.update(run["id"], campaign_context=context)
                continue
            receipts = [
                self._upstream_receipt(component_id, runs[component_id]) for component_id in child.data["depends_on"]
            ]
            context = {**run["campaign_context"], "upstream_receipts": receipts, "blocked_reason": None}
            await self.store.update(run["id"], status="queued", campaign_context=context)
            runs[child.component_id] = {**run, "status": "queued", "campaign_context": context}
            await self._append(
                row, "release:" + run["id"], "released", {"run_id": run["id"], "upstream_receipts": receipts}
            )
        statuses = {run["status"] for run in runs.values()}
        if statuses == {"succeeded"}:
            status = "succeeded" if all([await self._gate(run) is None for run in runs.values()]) else "paused"
        elif statuses & {"failed", "cancelled"}:
            status = "failed"
        elif statuses & {"paused", "interrupted"} or statuses <= {"succeeded", "campaign_blocked"}:
            status = "paused"
        else:
            status = "running"
        await self._set_state(row, status)

    async def tick(self, project_id: str, campaign_id: str) -> Data:
        await self.engine.assert_ownership()
        async with self.store._unit():
            row = await self._locked(project_id, campaign_id)
            events = [item.data for item in await self.events.events(row.id)]
            if not self._verify(events):
                raise ValueError("Campaign journal integrity verification failed")
            await self._release(row)
            return await self.campaign(project_id, campaign_id)

    async def start(self) -> None:
        if self.store.runtime_roles and not self.store.runtime_roles.executes_jobs:
            return
        await self.engine.assert_ownership()
        if self.poller is None or self.poller.done():
            self.poller = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self.poller:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)
            self.poller = None

    async def _poll(self) -> None:
        while True:
            try:
                await self.engine.assert_ownership()
                async with self.store._unit():
                    candidates = [
                        (row.project_id.hex, row.id.hex)
                        for row in await self.records.latest()
                        if row.status not in {"succeeded", "cancelled"}
                    ]
                for project_id, campaign_id in candidates:
                    await self.tick(project_id, campaign_id)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = "Campaign coordination stopped; inspect execution ownership and retained evidence"
                return
            await asyncio.sleep(0.5)

    async def _action_existing(self, row: ForgeCampaignEntity, action: CampaignAction, kind: str) -> bool:
        payload = {"action": kind, "request": action.model_dump()}
        entries = await self.events.events(row.id)
        event = next((item for item in entries if item.key == "action:" + action.request_id), None)
        if event:
            if event.data["payload"] != payload:
                raise ValueError("Campaign action identity already has another definition")
            if any(item.key == "receipt:" + action.request_id for item in entries):
                return True
            raise ValueError("The previous action requires reconciliation; it is not replayed automatically")
        if row.data.get("pending_action"):
            raise ValueError("The previous campaign action requires reconciliation before another action")
        return False

    async def _action_begin(self, row: ForgeCampaignEntity, action: CampaignAction, kind: str) -> None:
        await self._append(
            row, "action:" + action.request_id, "action_intent", {"action": kind, "request": action.model_dump()}
        )
        row.data = {
            **row.data,
            "pending_action": {"action": kind, "request_id": action.request_id},
            "updated_at": now(),
        }
        await self.records.save(row)

    async def _action_finish(self, row: ForgeCampaignEntity, action: CampaignAction, evidence: Data) -> None:
        await self._append(row, "receipt:" + action.request_id, "action_receipt", evidence)
        row.data = {**row.data, "pending_action": None, "updated_at": now()}
        await self.records.save(row)

    async def _current(self, row: ForgeCampaignEntity) -> dict[str, tuple[ForgeCampaignChildEntity, Data]]:
        return {
            child.component_id: (child, await self.store.get_run(child.run_id.hex))
            for child in await self.children.children(row.id)
            if row.data["current_attempts"][child.component_id] == child.attempt
        }

    async def cancel(self, project_id: str, campaign_id: str, action: CampaignAction) -> Data:
        if not self.store.runtime_roles or self.store.runtime_roles.executes_jobs:
            await self.engine.assert_ownership()
        async with self.store._unit():
            row = await self._locked(project_id, campaign_id)
            if await self._action_existing(row, action, "cancel"):
                return await self.campaign(project_id, campaign_id)
            await self._action_begin(row, action, "cancel")
            row.status = "cancelling"
            row.data = {**row.data, "control": "cancelled"}
            await self.records.save(row)
            run_ids = [
                run["id"]
                for _, run in (await self._current(row)).values()
                if run["status"] not in {"succeeded", "failed", "cancelled", "interrupted"}
            ]
        try:
            for run_id in run_ids:
                await self.engine.cancel(run_id)
        except Exception:
            async with self.store._unit():
                row = await self._locked(project_id, campaign_id)
                row.status = "needs_reconciliation"
                await self.records.save(row)
            raise ValueError(
                "Campaign cancellation outcome needs reconciliation; inspect recorded child runs"
            ) from None
        async with self.store._unit():
            row = await self._locked(project_id, campaign_id)
            runs = [run for _, run in (await self._current(row)).values()]
            row.status = (
                "needs_reconciliation"
                if any([await self._unknown(run) for run in runs])
                else "cancelling"
                if any(run.get("cancel_request", {}).get("status") == "requested" for run in runs)
                else "cancelled"
            )
            await self._action_finish(row, action, {"run_ids": run_ids, "status": row.status})
            return await self.campaign(project_id, campaign_id)

    async def resume(self, project_id: str, campaign_id: str, action: CampaignAction) -> Data:
        if not self.store.runtime_roles or self.store.runtime_roles.executes_jobs:
            await self.engine.assert_ownership()
        async with self.store._unit():
            row = await self._locked(project_id, campaign_id)
            current = await self._current(row)
            if any([await self._unknown(run) for _, run in current.values()]):
                raise ValueError("Child execution requires reconciliation before campaign resume")
            pending = row.data.get("pending_action")
            if pending:
                if pending["action"] != "cancel" or any(
                    run["status"] not in {"succeeded", "failed", "cancelled", "interrupted"}
                    for _, run in current.values()
                ):
                    raise ValueError("The previous action requires reconciliation against stopped child runs")
                await self._append(
                    row,
                    "receipt:" + pending["request_id"],
                    "action_reconciliation",
                    {
                        "observed_statuses": {key: run["status"] for key, (_, run) in current.items()},
                        "outcome": "stopped",
                    },
                )
                row.data = {**row.data, "pending_action": None}
                await self.records.save(row)
            if await self._action_existing(row, action, "resume"):
                return await self.campaign(project_id, campaign_id)
            await self._action_begin(row, action, "resume")
            resumed = []
            for _, run in current.values():
                if run["status"] in {"paused", "interrupted"}:
                    await self.journal.resume(run["id"], campaign_action_id=action.request_id)
                    resumed.append(run["id"])
            row.data = {**row.data, "control": "active"}
            await self._action_finish(row, action, {"resumed_run_ids": resumed})
            await self._release(row)
            return await self.campaign(project_id, campaign_id)

    async def retry(self, project_id: str, campaign_id: str, action: CampaignRetry) -> Data:
        if not self.store.runtime_roles or self.store.runtime_roles.executes_jobs:
            await self.engine.assert_ownership()
        async with self.store._unit():
            row = await self._locked(project_id, campaign_id)
            if await self._action_existing(row, action, "retry"):
                return await self.campaign(project_id, campaign_id)
            current = await self._current(row)
            selected = set(action.component_ids)
            if len(selected) != len(action.component_ids) or selected - current.keys():
                raise ValueError("Choose distinct components belonging to this campaign")
            for component_id in selected:
                child, run = current[component_id]
                if await self._unknown(run):
                    raise ValueError("Child execution requires reconciliation; retry would duplicate an unknown effect")
                if run["status"] not in {"failed", "cancelled", "interrupted"}:
                    raise ValueError("Only failed, cancelled or interrupted campaign children can retry")
                if child.attempt >= 4:
                    raise ValueError("The campaign component has exhausted its three explicit retries")
                descendants: set[str] = set()
                pending = [component_id]
                while pending:
                    upstream = pending.pop()
                    for dependent, (binding, _) in current.items():
                        if upstream in binding.data["depends_on"] and dependent not in descendants:
                            descendants.add(dependent)
                            pending.append(dependent)
                for dependent in descendants:
                    if current[dependent][1]["status"] not in {
                        "campaign_blocked",
                        "failed",
                        "cancelled",
                        "interrupted",
                    }:
                        raise ValueError("An already dispatched dependent must stop before its upstream can retry")
            await self._action_begin(row, action, "retry")
            attempts = copy.deepcopy(row.data["current_attempts"])
            new_run_ids = []
            for component_id in sorted(selected):
                child, _ = current[component_id]
                attempt = child.attempt + 1
                run = copy.deepcopy(child.data["admission"])
                run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{campaign_id}:{component_id}:{attempt}").hex
                run.update(id=run_id, status="campaign_blocked", created_at=now(), updated_at=now())
                run["campaign_context"] = {**run["campaign_context"], "attempt": attempt, "upstream_receipts": []}
                await self.store.run_records.save(
                    ForgeRunEntity(
                        id=identifier(run_id),
                        repository_id=identifier(run["repository_id"]),
                        status="campaign_blocked",
                        data=run,
                    )
                )
                await self.store.event(run_id, "campaign_blocked", "Frozen campaign admission explicitly retried")
                await self.children.save(
                    ForgeCampaignChildEntity(
                        id=uuid.uuid4(),
                        campaign_id=row.id,
                        component_id=component_id,
                        attempt=attempt,
                        run_id=identifier(run_id),
                        data={
                            "admission": copy.deepcopy(run),
                            "depends_on": child.data["depends_on"],
                            "admission_digest": digest(run),
                        },
                    )
                )
                attempts[component_id] = attempt
                new_run_ids.append(run_id)
            row.data = {**row.data, "current_attempts": attempts, "control": "active"}
            await self._action_finish(row, action, {"run_ids": new_run_ids})
            await self._release(row)
            return await self.campaign(project_id, campaign_id)
