# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed learning evidence and pure rules for exact scoped reuse and bounded repair."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Purpose = Literal["verification", "regression", "diagnostic", "intentional_red", "repair_generation"]
State = Literal["candidate", "verified", "invalidated", "superseded"]
Reason = Literal[
    "verified_exact_match",
    "candidate_only",
    "context_drift",
    "version_drift",
    "source_drift",
    "expired",
    "invalidated",
    "superseded",
    "scope_denied",
    "missing_receipts",
    "unknown_effect",
    "no_candidate",
    "refresh_required",
    "independent_check_required",
    "repair_attempt_available",
    "identical_attempt_exhausted",
]


class LearningPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = Field(default=True, strict=True)
    projected_retrieval: bool = Field(default=False, strict=True)
    max_age_hours: int = Field(default=72, ge=1, le=720, strict=True)
    max_candidates: int = Field(default=8, ge=1, le=16, strict=True)
    max_repeated_attempts: int = Field(default=2, ge=1, le=3, strict=True)
    max_lookup_calls: int = Field(default=30, ge=1, le=100, strict=True)


class LearningContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    repository_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    component_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    framework: Literal["pyfly", "larafly", "none"]
    version: str = Field(min_length=1, max_length=100)
    dependency_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_tree: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    query_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class SourceAssociation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    base_revision: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    head: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    source_tree: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")


class LearningReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["receipt", "tool_receipt", "sandbox_receipt", "review_receipt", "checkpoint", "error"]
    key: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1, strict=True)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class LearningEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    type: Literal["candidate", "observed_failure", "local_outcome", "invalidation", "supersession", "search_decision"]
    correlation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    role: Literal["implementer", "docs", "qa", "tech-lead", "security", "architect", "product-manager"]
    context: LearningContext
    source: SourceAssociation
    purpose: Purpose
    question: str = Field(min_length=1, max_length=4000)
    statement: str = Field(min_length=1, max_length=6000)
    tool_invocation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    tool_request_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    citations: list[str] = Field(default_factory=list, max_length=16)
    receipts: list[LearningReceipt] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def exact_source(self) -> LearningEnvelope:
        if self.context.source_tree != self.source.source_tree:
            raise ValueError("Learning context and inspected source tree must match")
        if any(not value or len(value) > 200 for value in self.citations):
            raise ValueError("Learning citation identifiers must be bounded and nonempty")
        if (self.tool_invocation_id is None) != (self.tool_request_digest is None):
            raise ValueError("Tool evidence requires both invocation and request identity")
        return self


class LearningCandidate(BaseModel):
    """Internal ledger projection; receipt flags come from native evidence validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    state: State
    kind: Literal["local_outcome", "observed_failure", "web_reference"]
    context: LearningContext
    verified_at: datetime
    expires_at: datetime
    receipt_verified: bool = Field(strict=True)
    unknown_effect: bool = Field(strict=True)

    @model_validator(mode="after")
    def aware_freshness(self) -> LearningCandidate:
        if self.verified_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("Learning freshness requires timezone-aware database timestamps")
        if self.expires_at <= self.verified_at:
            raise ValueError("Learning evidence requires a positive freshness window")
        return self


class LearningDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    eligible: bool
    reason: Reason
    refresh_required: bool = False
    candidate_id: str | None = None


class RepeatDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    allowed: bool
    reason: Reason


def eligibility(candidate: LearningCandidate, observed: LearningContext, database_time: datetime) -> LearningDecision:
    if database_time.utcoffset() is None:
        raise ValueError("Learning eligibility requires timezone-aware database time")
    expected = candidate.context
    reason: Reason = "verified_exact_match"
    if (expected.repository_id, expected.project_id, expected.component_id) != (
        observed.repository_id,
        observed.project_id,
        observed.component_id,
    ):
        reason = "scope_denied"
    elif candidate.state in {"invalidated", "superseded"}:
        reason = "invalidated" if candidate.state == "invalidated" else "superseded"
    elif candidate.state != "verified" or candidate.kind != "local_outcome":
        reason = "candidate_only"
    elif candidate.unknown_effect:
        reason = "unknown_effect"
    elif not candidate.receipt_verified or candidate.verified_at > database_time:
        reason = "missing_receipts"
    elif (
        expected.version == "unknown"
        or observed.version == "unknown"
        or (expected.framework, expected.version) != (observed.framework, observed.version)
    ):
        reason = "version_drift"
    elif expected.dependency_digest != observed.dependency_digest or expected.query_digest != observed.query_digest:
        reason = "context_drift"
    elif expected.source_tree != observed.source_tree:
        reason = "source_drift"
    elif candidate.expires_at <= database_time:
        reason = "expired"
    return LearningDecision(
        eligible=reason == "verified_exact_match",
        reason=reason,
        refresh_required=reason == "expired",
        candidate_id=candidate.id,
    )


def repeat_decision(
    purpose: Purpose, *, identical_verified_failures: int, max_repeated_attempts: int
) -> RepeatDecision:
    if purpose not in {"verification", "regression", "diagnostic", "intentional_red", "repair_generation"}:
        raise ValueError("Choose an explicit supported execution purpose")
    if not 0 <= identical_verified_failures <= 1000 or not 1 <= max_repeated_attempts <= 3:
        raise ValueError("Repeated-attempt inputs exceed policy bounds")
    if purpose != "repair_generation":
        return RepeatDecision(allowed=True, reason="independent_check_required")
    blocked = identical_verified_failures >= max_repeated_attempts
    return RepeatDecision(
        allowed=not blocked, reason="identical_attempt_exhausted" if blocked else "repair_attempt_available"
    )


class ToolObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    origin: Literal["knowledge_first", "knowledge_lookup"]
    observed_at: str = Field(max_length=40)
    web_provider_dispatched: bool = Field(strict=True)
    classification: Literal[
        "lookup_started", "required_service_failure", "candidate_reference", "verified_outcome", "provider_dispatch"
    ]
    reason: Reason | None = None
