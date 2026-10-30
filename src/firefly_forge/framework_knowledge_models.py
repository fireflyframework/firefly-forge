# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Official framework releases and durable refresh contracts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firefly_forge.product_provenance import content_digest as digest
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract

CollectorRevision = Literal[1, 2, 3]
CURRENT_COLLECTOR_REVISION: CollectorRevision = 3


def validate_collector_revision(value: object) -> CollectorRevision:
    if type(value) is int and value == 1:
        return 1
    if type(value) is int and value == 2:
        return 2
    if type(value) is int and value == 3:
        return 3
    raise ValueError("Unsupported official reference collector revision")


FrameworkName = Literal["pyfly", "larafly"]
FrameworkAction = Literal[
    "check_updates",
    "refresh",
    "observe",
    "search",
    "browse",
    "renew_budget",
    "cancel",
    "resume",
    "activate_generation_target",
]
OFFICIAL_REPOSITORIES = {
    "pyfly": "fireflyframework/fireflyframework-pyfly",
    "larafly": "fireflyframework/fireflyframework-php",
}


def framework_version(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("Choose an exact stable framework release version")
    return ".".join(str(int(part)) for part in value.removeprefix("v").split("."))


class FrameworkCheckUpdatesInput(StrictContract):
    version: Literal[1] = 1
    framework_version: str = "latest"

    @field_validator("framework_version")
    @classmethod
    def exact_or_latest(cls, value: str) -> str:
        return value if value == "latest" else framework_version(value)


class FrameworkRefreshInput(StrictContract):
    version: Literal[1]
    discovery_digest: Digest
    expected_status_revision: Digest
    policy_digest: Digest
    make_current: bool = Field(default=True, strict=True)


class FrameworkReleaseV1(StrictContract):
    framework: FrameworkName
    framework_version: str
    release_tag: str
    release_id: int = Field(ge=1, strict=True)
    repository: str
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    tree: str = Field(pattern=r"^[a-f0-9]{40}$")
    license_path: str
    license_sha256: Digest
    release_url: str
    published_at: str
    checked_at: str
    digest: Digest

    @model_validator(mode="after")
    def official_immutable_release(self) -> FrameworkReleaseV1:
        repository = OFFICIAL_REPOSITORIES[self.framework]
        if (
            self.repository != repository
            or framework_version(self.framework_version) != self.framework_version
            or framework_version(self.release_tag) != self.framework_version
            or self.release_url
            != "https://github.com/" + repository + "/releases/tag/" + quote(self.release_tag, safe="")
            or self.license_path.startswith("/")
            or ".." in self.license_path.split("/")
            or digest(self.model_dump(exclude={"digest"})) != self.digest
        ):
            raise ValueError("Framework release must bind its official repository, immutable reference and digest")
        for value in (self.published_at, self.checked_at):
            if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("Framework release observations require an aware timestamp")
        return self


class FrameworkCountsV1(StrictContract):
    files: int = Field(default=0, ge=0, strict=True)
    total_files: int = Field(default=0, ge=0, strict=True)
    documents: int = Field(default=0, ge=0, strict=True)
    examples: int = Field(default=0, ge=0, strict=True)
    api: int = Field(default=0, ge=0, strict=True)
    parts: int = Field(default=0, ge=0, strict=True)
    ready_parts: int = Field(default=0, ge=0, strict=True)
    bytes: int = Field(default=0, ge=0, strict=True)
    excluded_files: int = Field(default=0, ge=0, strict=True)
    excluded_parts: int = Field(default=0, ge=0, strict=True)


class FrameworkCorpusReferenceV1(StrictContract):
    collector_revision: CollectorRevision = 1
    corpus_ref: NativeID
    framework: FrameworkName
    framework_version: str
    release_tag: str
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    manifest_sha256: Digest | None
    license_sha256: Digest
    workspace_id: str
    state: Literal["queued", "fetching", "indexing", "verifying", "ready", "needs_reconciliation", "failed"]


class FrameworkWorkBudgetV1(StrictContract):
    requests: int = Field(gt=0, strict=True)
    bytes: int = Field(gt=0, strict=True)
    seconds: int = Field(gt=0, strict=True)


class FrameworkSpentBudgetV1(StrictContract):
    requests: int = Field(default=0, ge=0, strict=True)
    bytes: int = Field(default=0, ge=0, strict=True)
    seconds: float = Field(default=0.0, ge=0, strict=True, allow_inf_nan=False)


class FrameworkRefreshOperationV1(StrictContract):
    request_id: NativeID
    framework: FrameworkName
    corpus: FrameworkCorpusReferenceV1 | None
    state: Literal["queued", "running", "needs_reconciliation", "ready", "failed", "cancelled"]
    stage: Literal["discovery", "inventory", "fetching", "submitting", "verifying", "complete"]
    counts: FrameworkCountsV1
    spent_budget: FrameworkSpentBudgetV1
    remaining_budget: FrameworkSpentBudgetV1
    sanitized_error: str | None
    uncertain_sources: int = Field(default=0, ge=0, strict=True)
    allowed_actions: list[FrameworkAction]
    revision: Digest


class FrameworkGenerationTargetInput(StrictContract):
    version: Literal[1]
    corpus_ref: NativeID
    expected_status_revision: Digest
    validation_run_id: NativeID


class FrameworkGenerationTargetV1(StrictContract):
    version: Literal[1] = 1
    request_id: NativeID
    framework: FrameworkName
    corpus: FrameworkCorpusReferenceV1
    validation_run_id: NativeID
    validation_source_tree: str = Field(pattern=r"^[a-f0-9]{40}$")
    validation_digest: Digest
    activation_actor_digest: Digest
    activated_at: str
    revision: Digest


class FrameworkRuntimeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    runtime: Literal["python", "php"]
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class FrameworkGenerationCandidateV1(StrictContract):
    validation_run_id: NativeID
    repository_name: str = Field(min_length=1)
    project_id: NativeID | None
    component_id: str | None
    created_at: str
    source_tree: str = Field(pattern=r"^[a-f0-9]{40}$")
    runtime: FrameworkRuntimeObservation
    validation_digest: Digest


class FrameworkGenerationCandidatesV1(StrictContract):
    version: Literal[1] = 1
    framework: FrameworkName
    corpus_ref: NativeID
    items: list[FrameworkGenerationCandidateV1]
    next_cursor: NativeID | None


class FrameworkKnowledgeStatusV1(StrictContract):
    framework: FrameworkName
    official_repository: str
    current_corpus: FrameworkCorpusReferenceV1 | None
    latest_stable: FrameworkReleaseV1 | None
    generation_target: FrameworkGenerationTargetV1 | None = None
    discovery_retry_at: str | None = None
    discovery_error: str | None = None
    checked_at: str | None
    state: Literal[
        "missing", "checking", "update_available", "current", "indexed", "refreshing", "needs_reconciliation", "failed"
    ]
    counts: FrameworkCountsV1
    active_operation: FrameworkRefreshOperationV1 | None
    allowed_actions: list[FrameworkAction]
    revision: Digest


class FrameworkDiscoveryResultV1(StrictContract):
    version: Literal[1] = 1
    framework: FrameworkName
    release: FrameworkReleaseV1
    status: FrameworkKnowledgeStatusV1
    policy_digest: Digest


class FrameworkSourceV1(StrictContract):
    excluded_parts: int = Field(default=0, ge=0, strict=True)
    exclusions_sha256: Digest | None = None
    source_ref: NativeID
    corpus_ref: NativeID
    path: str
    kind: Literal["document", "example", "api", "license", "configuration"]
    blob_oid: str = Field(pattern=r"^[a-f0-9]{40}$")
    sha256: Digest | None
    bytes: int = Field(ge=0, strict=True)
    parts: int = Field(ge=0, strict=True)
    ready_parts: int = Field(ge=0, strict=True)
    state: Literal[
        "pending", "fetched", "submitting", "indexing", "ready", "needs_reconciliation", "failed", "excluded"
    ]
    source_uri: str
    license_sha256: Digest
    exclusion_reason: str | None


class FrameworkSearchInput(StrictContract):
    query: str = Field(min_length=1, max_length=8000)
    corpus_ref: NativeID | None = None
    framework_version: str | None = None

    @field_validator("framework_version")
    @classmethod
    def normalized_target_version(cls, value: str | None) -> str | None:
        return framework_version(value) if value is not None else None

    @model_validator(mode="after")
    def one_target(self) -> FrameworkSearchInput:
        if (self.corpus_ref is None) == (self.framework_version is None):
            raise ValueError("Select one exact corpus reference or framework version")
        return self


class FrameworkSearchHitV1(StrictContract):
    kind: Literal["document", "example", "api", "license", "configuration"] | None = None
    license_path: str | None = None
    source_ref: NativeID
    corpus_ref: NativeID
    framework: FrameworkName
    framework_version: str
    release_tag: str
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_id: str
    chunk_id: str | None
    path: str
    source_uri: str
    source_sha256: Digest
    license_sha256: Digest
    knowledge_version: int | None
    score: float = Field(allow_inf_nan=False)
    content: str


class FrameworkBudgetRenewalInput(StrictContract):
    version: Literal[1]
    expected_revision: Digest
    total_budget: FrameworkWorkBudgetV1


class FrameworkRefreshCancelInput(StrictContract):
    version: Literal[1]
    expected_revision: Digest


class FrameworkRunTargetInput(StrictContract):
    framework_version: str
    upgrade_from: str | None = None

    @field_validator("framework_version", "upgrade_from")
    @classmethod
    def normalize_target(cls, value: str | None) -> str | None:
        return framework_version(value) if value is not None else None

    @model_validator(mode="after")
    def explicit_upgrade(self) -> FrameworkRunTargetInput:
        if self.upgrade_from == self.framework_version:
            raise ValueError("An explicit framework upgrade must choose a different target release")
        return self


class FrameworkRefreshResumeInput(StrictContract):
    version: Literal[1]
    expected_revision: Digest
