# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Post-admission reporting plans do not authorize or orchestrate campaign work."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from typing import Any
from uuid import uuid4

from pyfly.container import service
from pyfly.data.transaction import Isolation, Propagation, TransactionTemplate, current_unit_of_work
from sqlalchemy import Text, cast, func, select, text

from firefly_forge.campaign_delivery_plan_models import (
    CampaignDeliveryPlanInput,
    CampaignDeliveryPlanReceipt,
    CampaignDeliveryPlanSummary,
    CampaignDeliveryProgress,
    DeliveryPlanIdentity,
)
from firefly_forge.campaign_delivery_plan_persistence import (
    ForgeCampaignDeliveryPlanEntity,
    ForgeCampaignDeliveryPlanRepository,
)
from firefly_forge.campaign_report_history import CampaignReportHistory, _signed, _source, _valid_identity
from firefly_forge.campaign_report_history_persistence import ForgeCampaignReportArchiveEntity
from firefly_forge.campaign_reports import CampaignReport, CampaignReports
from firefly_forge.collaboration import ProductDefinition
from firefly_forge.collaboration_persistence import ForgeProductDefinitionEntity
from firefly_forge.journal import ZERO_HASH, digest
from firefly_forge.project_access import Permission
from firefly_forge.project_campaign_persistence import ForgeCampaignChildEntity
from firefly_forge.store import Store, identifier

Data = dict[str, Any]
MAX_PLAN_BYTES, MAX_PROGRESS_BYTES, MAX_REVISIONS = 1_048_576, 16_777_216, 50


def _counts(states: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(states).items()))


def _aggregate(states: list[str]) -> str:
    if not states:
        return "unknown"
    return next(
        (
            state
            for state in ("unknown", "blocked", "dependency_blocked", "in_progress", "not_started")
            if state in states
        ),
        "satisfied",
    )


def _condition(condition: Data, components: dict[str, Data]) -> Data:
    component = components.get(condition["component_id"])
    result = {
        **condition,
        "state": "unknown",
        "reason": "component_unavailable",
        "run_id": None,
        "component_attempt": None,
        "task_attempt": None,
        "evidence_digest": None,
        "receipt": None,
    }
    if component is None:
        return result
    result.update(run_id=component["run_id"], component_attempt=component["attempt"])
    if condition["kind"] == "component_verified":
        if component["state"] == "unknown":
            return {**result, "reason": "component_evidence_unknown"}
        satisfied = component["state"] == "verified" and component["verification"] == "verified"
        return {
            **result,
            "state": "satisfied"
            if satisfied
            else component["state"]
            if component["state"] in {"blocked", "in_progress", "not_started"}
            else "unknown",
            "reason": None if satisfied else "component_not_verified",
        }
    task = next((item for item in component["tasks"] if item["id"] == condition["task_id"]), None)
    if task is None:
        return {
            **result,
            "state": "unknown" if component["plan_digest"] else "not_started",
            "reason": "required_task_absent" if component["plan_digest"] else "task_plan_not_started",
        }
    result.update(task_attempt=task["attempt"])
    if task["state"] == "receipted":
        if task["receipt"] is None or task["evidence_digest"] is None:
            return {**result, "reason": "task_receipt_incomplete"}
        return {
            **result,
            "state": "satisfied",
            "reason": None,
            "receipt": task["receipt"],
            "evidence_digest": task["evidence_digest"],
        }
    return {
        **result,
        "state": task["state"] if task["state"] in {"unknown", "blocked", "in_progress", "not_started"} else "unknown",
        "reason": "task_not_receipted",
    }


def derive_progress(plan: Data, archive: Data) -> Data:
    body = CampaignDeliveryPlanInput.model_validate(plan["request"])
    components = {item["component_id"]: item for item in archive["report"]["components"]}
    milestones = []
    for milestone in body.milestones:
        conditions = [_condition(condition.model_dump(), components) for condition in milestone.conditions]
        states = [item["state"] for item in conditions]
        milestones.append(
            {**milestone.model_dump(), "conditions": conditions, "state": _aggregate(states), "counts": _counts(states)}
        )
    waves: list[Data] = []
    for ordinal, wave in enumerate(body.waves, 1):
        selected = [item for item in milestones if item["wave_id"] == wave.id]
        component_states = {name: components.get(name, {}).get("state", "unknown") for name in wave.component_ids}
        states = [item["state"] for item in selected] + [
            "satisfied"
            if state == "verified" and components[name]["verification"] == "verified"
            else "unknown"
            if state == "verified"
            else state
            for name, state in component_states.items()
        ]
        observed = _aggregate(states)
        blocking = [item["id"] for item in waves if item["state"] != "satisfied"]
        waves.append(
            {
                **wave.model_dump(),
                "ordinal": ordinal,
                "component_states": component_states,
                "milestone_ids": [item["id"] for item in selected],
                "observed_state": observed,
                "state": "dependency_blocked" if blocking and observed == "satisfied" else observed,
                "blocking_wave_ids": blocking,
            }
        )
    result = CampaignDeliveryProgress.model_validate(
        dict(
            project_id=plan["project_id"],
            campaign_id=plan["campaign_id"],
            plan_request_id=plan["request_id"],
            plan_revision=plan["revision"],
            plan_digest=plan["digest"],
            report_request_id=archive["request_id"],
            report_archive_digest=archive["digest"],
            report_digest=archive["report"]["digest"],
            plan_prepared_at=plan["prepared_at"],
            report_prepared_at=archive["prepared_at"],
            source=archive["source"],
            state=_aggregate([item["state"] for item in waves]),
            counts={
                "conditions": _counts([c["state"] for item in milestones for c in item["conditions"]]),
                "milestones": _counts([item["state"] for item in milestones]),
                "waves": _counts([item["state"] for item in waves]),
            },
            milestones=milestones,
            waves=waves,
            digest=ZERO_HASH,
        )
    ).model_dump(mode="json")
    return _signed(result)


def _summary(data: Data) -> Data:
    return _signed(
        {
            **{key: data[key] for key in DeliveryPlanIdentity.model_fields if key != "digest"},
            "title": data["request"]["title"],
            "receipt_digest": data["digest"],
        }
    )


@service
class CampaignDeliveryPlans:
    def __init__(
        self,
        store: Store,
        reports: CampaignReports,
        history: CampaignReportHistory,
        records: ForgeCampaignDeliveryPlanRepository,
    ) -> None:
        self.store, self.reports, self.history, self.records = store, reports, history, records

    async def _capture(
        self, project_id: str, campaign_id: str, body: CampaignDeliveryPlanInput
    ) -> tuple[Data, set[str], list[Data]]:
        await self.reports._authorize(project_id, set())
        async with self.store._unit():
            unit = current_unit_of_work()
            assert unit is not None
            manager = unit.manager
        async with TransactionTemplate(
            manager,
            propagation=Propagation.REQUIRES_NEW,
            isolation=Isolation.REPEATABLE_READ,
            read_only=True,
            timeout=10,
        ).transaction():
            raw, repositories = await self.reports._snapshot(project_id, campaign_id)
            report = _signed(CampaignReport(**raw, digest=ZERO_HASH).model_dump(mode="json"))
            remaining = [MAX_PROGRESS_BYTES - len(json.dumps(report).encode())]
            async with self.store._unit() as session:
                children = await self.reports._rows(
                    session,
                    select(ForgeCampaignChildEntity)
                    .where(
                        ForgeCampaignChildEntity.campaign_id == identifier(campaign_id),
                        ForgeCampaignChildEntity.run_id.in_(
                            [identifier(item["run_id"]) for item in report["components"]]
                        ),
                    )
                    .limit(51),
                    50,
                    remaining,
                )
            admissions = {row.component_id: row.data for row in children}
            if len(admissions) != len(report["components"]) or any(
                digest(admissions[item["component_id"]]["admission"]) != item["admission_digest"]
                for item in report["components"]
            ):
                raise ValueError("Frozen campaign admission integrity failed")
            products = await self._validate(body, report, admissions, remaining)
            repositories.update(item["repository_id"] for item in products)
        return report, repositories, products

    async def _validate(
        self, body: CampaignDeliveryPlanInput, report: Data, admissions: dict[str, Data], remaining: list[int]
    ) -> list[Data]:
        if (
            body.expected_project_digest != report["campaign"]["project_digest"]
            or body.expected_campaign_request_digest != report["campaign"]["request_digest"]
        ):
            raise ValueError("Select the frozen campaign launch and project digests")
        wave_index = {name: index for index, wave in enumerate(body.waves) for name in wave.component_ids}
        if set(wave_index) != set(admissions) or any(
            wave_index[dependency] > wave_index[item["component_id"]]
            for item in report["components"]
            for dependency in item["depends_on"]
        ):
            raise ValueError("Waves must cover the frozen component graph without reversing dependencies")
        products: dict[str, Data] = {}
        validated: dict[str, ProductDefinition] = {}
        for milestone in body.milestones:
            definitions = []
            for condition in milestone.conditions:
                admission = admissions[condition.component_id]["admission"]
                if (
                    condition.task_id == "implement"
                    and admission.get("operation") == "test"
                    or condition.task_id == "review"
                    and not admission.get("assurance", {}).get("review_agent")
                ):
                    raise ValueError("The selected task phase is not configured in this frozen admission")
                definitions.append(admission.get("product_definition"))
            if milestone.feature_ids or milestone.journey_refs or milestone.acceptance_refs:
                if (
                    not definitions
                    or definitions[0] is None
                    or any(product != definitions[0] for product in definitions)
                ):
                    raise ValueError("Milestone references require one exact frozen product definition")
                product = definitions[0]
                key = digest({name: product[name] for name in ("repository_id", "revision", "digest")})
                if key not in validated:
                    async with self.store._unit() as session:
                        entity = ForgeProductDefinitionEntity
                        rows = await self.reports._rows(
                            session,
                            select(entity)
                            .where(
                                entity.repository_id == identifier(product["repository_id"]),
                                entity.revision == product["revision"],
                            )
                            .limit(1),
                            1,
                            remaining,
                        )
                    if not rows or rows[0].data != product or rows[0].digest != product["digest"]:
                        raise ValueError("Frozen product definition receipt is unavailable")
                    expected = hashlib.sha256(
                        json.dumps(
                            {k: v for k, v in product.items() if k != "digest"}, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest()
                    if expected != product["digest"]:
                        raise ValueError("Frozen product definition integrity failed")
                    validated[key] = ProductDefinition.model_validate(product["definition"])
                definition = validated[key]
                features = {item.id: item for item in definition.features}
                if (
                    set(milestone.feature_ids) - features.keys()
                    or set(milestone.journey_refs) - set(definition.journey)
                    or set(milestone.acceptance_refs)
                    - {f"acceptance-{i + 1}" for i in range(len(definition.acceptance))}
                    or any(set(features[name].component_refs) - set(wave_index) for name in milestone.feature_ids)
                ):
                    raise ValueError("Use references from the frozen product and campaign component scope")
                ref = {key: product[key] for key in ("repository_id", "revision", "digest")}
                products[digest(ref)] = ref
        return [products[key] for key in sorted(products)]

    async def _chain(self, project_id: str, campaign_id: str, through: int = MAX_REVISIONS) -> list[Data]:
        entity = ForgeCampaignDeliveryPlanEntity
        async with self.store._unit() as session:
            statement = (
                select(
                    entity.project_id,
                    entity.campaign_id,
                    entity.request_id,
                    entity.revision,
                    entity.previous_digest,
                    entity.digest,
                    entity.summary,
                )
                .where(
                    entity.campaign_id == identifier(campaign_id),
                    entity.revision <= through,
                )
                .order_by(entity.revision)
                .limit(MAX_REVISIONS + 1)
            )
            sample = statement.subquery()
            size = await session.scalar(
                select(func.coalesce(func.sum(func.octet_length(cast(sample.c.summary, Text))), 0))
            )
            if size is None or size > MAX_PLAN_BYTES:
                raise ValueError("Reporting-plan history exceeds its byte bound")
            rows = (await session.execute(statement)).all()
        if len(rows) > MAX_REVISIONS:
            raise ValueError("Reporting-plan history exceeds its revision bound")
        result, previous = [], None
        for index, row in enumerate(rows, 1):
            data = CampaignDeliveryPlanSummary.model_validate(row.summary).model_dump(mode="json")
            _valid_identity(data)
            if (
                row.project_id.hex != project_id
                or row.campaign_id.hex != campaign_id
                or row.request_id != data["request_id"]
                or row.revision != index
                or data["revision"] != index
                or data["project_id"] != project_id
                or data["campaign_id"] != campaign_id
                or row.digest != data["receipt_digest"]
                or row.previous_digest != previous
                or data["previous_digest"] != previous
            ):
                raise ValueError("Reporting-plan revision history integrity failed")
            previous = row.digest
            result.append(data)
        return result

    async def _row(self, campaign_id: str, request_id: str) -> ForgeCampaignDeliveryPlanEntity | None:
        entity = ForgeCampaignDeliveryPlanEntity
        async with self.store._unit() as session:
            size = await session.scalar(
                select(
                    func.octet_length(cast(entity.data, Text)) + func.octet_length(cast(entity.summary, Text))
                ).where(
                    entity.campaign_id == identifier(campaign_id),
                    entity.request_id == request_id,
                )
            )
            if size is not None and size > MAX_PLAN_BYTES:
                raise ValueError("Reporting-plan receipt exceeds its byte bound")
            return await self.records.request(identifier(campaign_id), request_id)

    def _view(self, row: ForgeCampaignDeliveryPlanEntity, project_id: str, campaign_id: str) -> Data:
        data = CampaignDeliveryPlanReceipt.model_validate(row.data).model_dump(mode="json")
        _valid_identity(data)
        body = data["request"]
        if (
            row.project_id.hex != project_id
            or row.campaign_id.hex != campaign_id
            or data["project_id"] != project_id
            or data["campaign_id"] != campaign_id
            or row.request_id != data["request_id"]
            or body["request_id"] != row.request_id
            or row.digest != data["digest"]
            or row.revision != data["revision"]
            or row.previous_digest != data["previous_digest"]
            or body["expected_previous_digest"] != row.previous_digest
            or data["request_digest"] != digest(body)
            or row.summary != _summary(data)
            or body["expected_project_digest"] != data["source"]["project_digest"]
            or body["expected_campaign_request_digest"] != data["source"]["request_digest"]
        ):
            raise ValueError("Reporting-plan receipt integrity failed")
        return data

    async def get(self, project_id: str, campaign_id: str, request_id: str) -> Data:
        return await self._get(project_id, campaign_id, request_id, "read")

    async def _get(self, project_id: str, campaign_id: str, request_id: str, permission: Permission) -> Data:
        project_id, campaign_id, request_id = (identifier(value).hex for value in (project_id, campaign_id, request_id))
        actor = self.history._actor()
        await self.history._authorize(project_id, set(), permission, actor)
        row = await self._row(campaign_id, request_id)
        if row is None or row.project_id.hex != project_id:
            raise KeyError("Reporting-plan receipt not found")
        data = self._view(row, project_id, campaign_id)
        chain = await self._chain(project_id, campaign_id, data["revision"])
        if len(chain) != data["revision"] or chain[-1]["receipt_digest"] != data["digest"]:
            raise ValueError("Reporting-plan receipt does not match its revision chain")
        repositories = {repo for item in chain for repo in item["repositories"]}
        await self.history._authorize(project_id, repositories, permission, actor)
        await self.store.admit_credentials({"receipt": data, "history": chain})
        async with self.store._unit():
            await self.history._lock_actor(actor)
            await self.history._authorize(project_id, repositories, permission, actor)
            self.store.reject_credentials(data)
        return data

    async def list(self, project_id: str, campaign_id: str) -> Data:
        project_id, campaign_id = identifier(project_id).hex, identifier(campaign_id).hex
        actor = self.history._actor()
        await self.history._authorize(project_id, set(), "read", actor)
        await self.history._campaign(project_id, campaign_id)
        items = await self._chain(project_id, campaign_id)
        repositories = {repo for item in items for repo in item["repositories"]}
        result = {"project_id": project_id, "campaign_id": campaign_id, "items": items}
        await self.history._authorize(project_id, repositories, "read", actor)
        await self.store.admit_credentials(result)
        async with self.store._unit():
            await self.history._lock_actor(actor)
            await self.history._authorize(project_id, repositories, "read", actor)
            self.store.reject_credentials(result)
        return result

    async def save(self, project_id: str, campaign_id: str, body: CampaignDeliveryPlanInput) -> Data:
        project_id, campaign_id = identifier(project_id).hex, identifier(campaign_id).hex
        actor = self.history._actor()
        await self.history._authorize(project_id, set(), "execute", actor)
        await self.store.admit_credentials(body.model_dump())
        previous = await self._row(campaign_id, body.request_id)
        if previous is not None:
            if previous.project_id.hex != project_id:
                raise KeyError("Reporting-plan receipt not found")
            data = self._view(previous, project_id, campaign_id)
            self._same(data, body, actor)
            return await self._get(project_id, campaign_id, body.request_id, "execute")
        report, repositories, products = await self._capture(project_id, campaign_id, body)
        chain = await self._chain(project_id, campaign_id)
        previous_digest = chain[-1]["receipt_digest"] if chain else None
        if len(chain) >= MAX_REVISIONS or previous_digest != body.expected_previous_digest:
            raise ValueError("Select the current reporting-plan revision within its bound")
        repositories.update(repo for item in chain for repo in item["repositories"])
        await self.history._authorize(project_id, repositories, "execute", actor)
        async with self.store._unit() as session:
            at = await session.scalar(select(func.clock_timestamp()))
        if not isinstance(at, datetime):
            raise ValueError("Database plan preparation time is unavailable")
        data = CampaignDeliveryPlanReceipt.model_validate(
            dict(
                project_id=project_id,
                campaign_id=campaign_id,
                request_id=body.request_id,
                actor_id=actor,
                revision=len(chain) + 1,
                previous_digest=previous_digest,
                prepared_at=at.isoformat(),
                repositories=sorted(repositories),
                source={**_source(report), "report_digest": report["digest"], "product_definitions": products},
                request=body,
                request_digest=digest(body.model_dump()),
                digest=ZERO_HASH,
            )
        ).model_dump(mode="json")
        data = _signed(data)
        summary = _summary(data)
        if len(json.dumps(data).encode()) + len(json.dumps(summary).encode()) > MAX_PLAN_BYTES:
            raise ValueError("Reporting-plan receipt exceeds its byte bound")
        await self.store.admit_credentials({"receipt": data, "summary": summary, "history": chain})
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": self.store.schema + ":delivery-plans:" + campaign_id},
            )
            await self.history._lock_actor(actor)
            await self.reports.campaigns._locked(project_id, campaign_id)
            await self.history._authorize(project_id, repositories, "execute", actor)
            previous = await self.records.request(identifier(campaign_id), body.request_id)
            if previous is not None:
                self._same(self._view(previous, project_id, campaign_id), body, actor)
            else:
                current = await self._chain(project_id, campaign_id)
                if (
                    len(current) >= MAX_REVISIONS
                    or len(current) != len(chain)
                    or (current[-1]["receipt_digest"] if current else None) != previous_digest
                ):
                    raise ValueError("Reporting-plan history advanced; retain this request identity")
                self.store.reject_credentials({"receipt": data, "summary": summary})
                await self.records.save(
                    ForgeCampaignDeliveryPlanEntity(
                        id=uuid4(),
                        project_id=identifier(project_id),
                        campaign_id=identifier(campaign_id),
                        request_id=body.request_id,
                        revision=data["revision"],
                        previous_digest=previous_digest,
                        digest=data["digest"],
                        data=data,
                        summary=summary,
                    )
                )
        return await self._get(project_id, campaign_id, body.request_id, "execute")

    @staticmethod
    def _same(data: Data, body: CampaignDeliveryPlanInput, actor: str) -> None:
        if data["actor_id"] != actor or data["request_digest"] != digest(body.model_dump()):
            raise ValueError("Reporting-plan request identity belongs to another actor or input")

    async def progress(self, project_id: str, campaign_id: str, request_id: str, report_request_id: str) -> Data:
        project_id, campaign_id, request_id, report_request_id = (
            identifier(value).hex for value in (project_id, campaign_id, request_id, report_request_id)
        )
        actor = self.history._actor()
        await self.history._authorize(project_id, set(), "read", actor)
        async with self.store._unit() as session:
            sizes = []
            for entity, identity in (
                (ForgeCampaignDeliveryPlanEntity, request_id),
                (ForgeCampaignReportArchiveEntity, report_request_id),
            ):
                size = await session.scalar(
                    select(
                        func.octet_length(cast(entity.data, Text)) + func.octet_length(cast(entity.summary, Text))
                    ).where(
                        entity.project_id == identifier(project_id),
                        entity.campaign_id == identifier(campaign_id),
                        entity.request_id == identity,
                    )
                )
                if size is None:
                    raise KeyError("Selected reporting plan or report archive not found")
                sizes.append(size)
            if sum(sizes) > MAX_PROGRESS_BYTES:
                raise ValueError("Reporting progress exceeds its aggregate byte bound")
        plan = await self.get(project_id, campaign_id, request_id)
        archive = await self.history.get(project_id, campaign_id, report_request_id)
        if any(plan["source"][key] != archive["source"][key] for key in ("project_digest", "request_digest")):
            raise ValueError("Select a report from the plan's frozen campaign and project")
        result = derive_progress(plan, archive)
        if len(json.dumps(result).encode()) > MAX_PROGRESS_BYTES:
            raise ValueError("Reporting progress exceeds its output byte bound")
        repositories = set(plan["repositories"]) | set(archive["repositories"])
        await self.history._authorize(project_id, repositories, "read", actor)
        await self.store.admit_credentials(result)
        async with self.store._unit():
            await self.history._lock_actor(actor)
            await self.history._authorize(project_id, repositories, "read", actor)
            self.store.reject_credentials(result)
        return result
