# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit reporting criteria are separate from the campaign's execution contract."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_forge.campaign_report_history import ArchiveSource
from firefly_forge.campaign_reports import ReportReference

Phase = Literal["prepare", "implement", "verify", "checkpoint", "review"]
ProgressState = Literal["satisfied", "unknown", "blocked", "in_progress", "not_started", "dependency_blocked"]


class DeliveryCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["component_verified", "task_receipted"]
    component_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    task_id: Phase | None = None

    @model_validator(mode="after")
    def task_binding(self) -> Self:
        if (self.kind == "task_receipted") != (self.task_id is not None):
            raise ValueError("A task condition requires exactly one supported task phase")
        return self


class DeliveryWave(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    component_ids: list[str] = Field(min_length=1, max_length=50)


class DeliveryMilestone(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    wave_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    conditions: list[DeliveryCondition] = Field(min_length=1, max_length=20)
    feature_ids: list[str] = Field(default_factory=list, max_length=100)
    journey_refs: list[str] = Field(default_factory=list, max_length=50)
    acceptance_refs: list[str] = Field(default_factory=list, max_length=100)


class CampaignDeliveryPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_previous_digest: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    expected_campaign_request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_project_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    title: str = Field(min_length=1, max_length=200)
    waves: list[DeliveryWave] = Field(min_length=1, max_length=20)
    milestones: list[DeliveryMilestone] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def coherent_reporting_structure(self) -> Self:
        waves = {wave.id: wave for wave in self.waves}
        components = [component for wave in self.waves for component in wave.component_ids]
        if (
            len(waves) != len(self.waves)
            or len(set(components)) != len(components)
            or len(components) > 50
            or len({item.id for item in self.milestones}) != len(self.milestones)
            or set(waves) != {item.wave_id for item in self.milestones}
            or sum(len(item.conditions) for item in self.milestones) > 200
        ):
            raise ValueError("Use unique bounded waves, components and milestones, with a milestone for every wave")
        for milestone in self.milestones:
            conditions = [condition.model_dump_json() for condition in milestone.conditions]
            if len(set(conditions)) != len(conditions) or any(
                condition.component_id not in waves[milestone.wave_id].component_ids
                for condition in milestone.conditions
            ):
                raise ValueError("Milestone conditions must be unique and belong to their wave")
            for refs in (milestone.feature_ids, milestone.journey_refs, milestone.acceptance_refs):
                if len(set(refs)) != len(refs) or any(not ref.strip() or len(ref) > 4000 for ref in refs):
                    raise ValueError("Use unique bounded references from the frozen product definition")
        return self


class ProductDefinitionReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    repository_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: int = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class DeliveryPlanSource(ArchiveSource):
    report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    product_definitions: list[ProductDefinitionReference]


class DeliveryPlanIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1] = 1
    criteria_binding: Literal["reporting_only_post_admission"] = "reporting_only_post_admission"
    enforced_at_admission: Literal[False] = False
    project_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    campaign_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    actor_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: int = Field(ge=1, le=50)
    previous_digest: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    prepared_at: str = Field(min_length=1, max_length=64)
    repositories: list[str] = Field(min_length=1, max_length=200)
    source: DeliveryPlanSource
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CampaignDeliveryPlanReceipt(DeliveryPlanIdentity):
    request: CampaignDeliveryPlanInput


class CampaignDeliveryPlanSummary(DeliveryPlanIdentity):
    title: str = Field(min_length=1, max_length=200)
    receipt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CampaignDeliveryPlanPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    project_id: str
    campaign_id: str
    items: list[CampaignDeliveryPlanSummary]


class ConditionProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["component_verified", "task_receipted"]
    component_id: str
    task_id: Phase | None
    state: ProgressState
    reason: str | None
    run_id: str | None
    component_attempt: int | None
    task_attempt: int | None
    evidence_digest: str | None
    receipt: ReportReference | None


class MilestoneProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str
    name: str
    wave_id: str
    state: ProgressState
    counts: dict[str, int]
    conditions: list[ConditionProgress]
    feature_ids: list[str]
    journey_refs: list[str]
    acceptance_refs: list[str]


class WaveProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str
    name: str
    ordinal: int
    component_ids: list[str]
    component_states: dict[str, str]
    milestone_ids: list[str]
    observed_state: ProgressState
    state: ProgressState
    blocking_wave_ids: list[str]


class DeliveryProgressCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    conditions: dict[str, int]
    milestones: dict[str, int]
    waves: dict[str, int]


class CampaignDeliveryProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1] = 1
    criteria_binding: Literal["reporting_only_post_admission"] = "reporting_only_post_admission"
    enforced_at_admission: Literal[False] = False
    project_id: str
    campaign_id: str
    plan_request_id: str
    plan_revision: int
    plan_digest: str
    report_request_id: str
    report_archive_digest: str
    report_digest: str
    plan_prepared_at: str
    report_prepared_at: str
    source: ArchiveSource
    scm_freshness: Literal["not_observed"] = "not_observed"
    state: ProgressState
    counts: DeliveryProgressCounts
    milestones: list[MilestoneProgress]
    waves: list[WaveProgress]
    digest: str
