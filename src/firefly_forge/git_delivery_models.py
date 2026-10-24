# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit bounded factory-owned Git delivery policy and operator contracts."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from firefly_forge.git_diff_models import GitDiffBudget, GitDiffRefV1
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract

GitRevision = Annotated[str, Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")]
RemoteStatus = Literal["not_requested", "publishing", "draft", "unknown", "ready"]


class GitDeliveryContract(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class GitDeliveryPolicyV1(GitDeliveryContract):
    version: Literal[1] = 1
    enabled: bool = False
    every_units: int = Field(default=1, ge=1, le=20)
    on_pause: bool = True
    remote: Literal["local", "draft_pr"] = "local"
    max_checkpoints: int = Field(default=50, ge=1, le=50)
    diff_budget: GitDiffBudget | None = None

    @model_serializer(mode="wrap")
    def retained_policy(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value: dict[str, Any] = handler(self)
        if value.get("diff_budget") is None:
            value.pop("diff_budget", None)
        return value


class GitDeliverySourceEvidenceV2(GitDeliveryContract):
    version: Literal[2] = 2
    diff_artifact: GitDiffRefV1


class GitDeliveryPolicyInputV1(GitDeliveryContract):
    version: Literal[1]
    request_id: NativeID
    expected_previous_digest: Digest | None
    policy: GitDeliveryPolicyV1


class GitFailedUnitAdoptionV1(GitDeliveryContract):
    version: Literal[1]
    candidate_digest: Digest
    policy_digest: Digest
    failed_feedback_digest: Digest
    expected_run_digest: Digest
    journal_digest: Digest
    source_digest: Digest
    valid_for_seconds: int = Field(ge=60, le=1800)
    acknowledge_partial_unverified: Literal[True]
    acknowledge_preserve_failed_run: Literal[True]
    acknowledge_no_execution_grant: Literal[True]


class GitDeliveryInputV1(GitDeliveryContract):
    version: Literal[1]
    request_id: NativeID
    expected_head: GitRevision
    expected_source_tree: GitRevision | None = None
    expected_source_digest: Digest | None = None
    explicit_adoption: GitFailedUnitAdoptionV1 | None = None

    @model_validator(mode="after")
    def reviewed_source(self) -> GitDeliveryInputV1:
        if self.expected_source_tree is None and self.expected_source_digest is None and self.explicit_adoption is None:
            raise ValueError("Review the exact source tree or an explicit failed-unit source candidate")
        return self

    @model_serializer(mode="wrap")
    def retained_request(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Historical request digests predate these optional adoption fields.
        value: dict[str, Any] = handler(self)
        return {key: item for key, item in value.items() if item is not None}


class GitDeliveryCandidateV1(GitDeliveryContract):
    version: Literal[1] = 1
    run_id: NativeID
    expected_head: GitRevision
    expected_source_digest: Digest


class GitDeliveryPermissions(GitDeliveryContract):
    configure: bool
    publish: bool


class GitDeliveryRemoteCapability(GitDeliveryContract):
    repository_id: NativeID
    provider: str = Field(min_length=1, max_length=32)
    draft_updates: bool
    reason: str | None = Field(default=None, max_length=300)


class GitDeliveryPolicyViewV1(GitDeliveryContract):
    version: Literal[1] = 1
    project_id: NativeID
    policy: GitDeliveryPolicyV1
    digest: Digest | None
    request_id: NativeID | None
    permissions: GitDeliveryPermissions
    remote_capabilities: list[GitDeliveryRemoteCapability] = Field(max_length=50)


class GitDeliveryViewV1(GitDeliveryContract):
    version: Literal[1] = 1
    run_id: NativeID
    request_id: NativeID
    sequence: int = Field(ge=1, le=50)
    original_base_revision: GitRevision
    parent_revision: GitRevision
    revision: GitRevision
    source_tree: GitRevision
    task_id: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1, le=100)
    changed_file_count: int = Field(ge=1)
    patch_sha256: Digest
    branch: str = Field(min_length=1, max_length=200)
    verification_status: Literal["pending", "partial_unverified"] = "pending"
    local_status: Literal["checkpointed"] = "checkpointed"
    remote_status: RemoteStatus
    pr_url: str | None = Field(default=None, max_length=2048)
    created_at: str = Field(min_length=1, max_length=50)
    checkpoint_digest: Digest
    source_evidence: GitDeliverySourceEvidenceV2 | None = None

    @model_validator(mode="after")
    def artifact_binding(self) -> GitDeliveryViewV1:
        if self.source_evidence is not None and (
            self.source_evidence.diff_artifact.file_count != self.changed_file_count
            or self.source_evidence.diff_artifact.sha256 != self.patch_sha256
        ):
            raise ValueError("Checkpoint diff evidence differs from its recorded files or content hash")
        return self

    @model_serializer(mode="wrap")
    def retained_view(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value: dict[str, Any] = handler(self)
        if value.get("source_evidence") is None:
            value.pop("source_evidence", None)
        return value


class GitDeliveriesViewV1(GitDeliveryContract):
    version: Literal[1] = 1
    run_id: NativeID
    policy: GitDeliveryPolicyV1
    original_base_revision: GitRevision | None
    latest_checkpoint: GitRevision | None
    items: list[GitDeliveryViewV1] = Field(max_length=50)


class GitDeliveryPolicyReceiptV1(GitDeliveryContract):
    version: Literal[1] = 1
    project_id: NativeID
    request_id: NativeID
    previous_digest: Digest | None
    policy: GitDeliveryPolicyV1
    actor: dict[str, Any]
    created_at: str = Field(min_length=1, max_length=50)
    digest: Digest


class GitDeliveryCheckpointV1(GitDeliveryViewV1):
    changed_files: list[str] = Field(min_length=1, max_length=200)
    patch: str = Field(min_length=1, max_length=131072)

    @model_validator(mode="after")
    def bounded_source(self) -> GitDeliveryCheckpointV1:
        if len(self.patch.encode()) > 131072 or len(self.changed_files) != self.changed_file_count:
            raise ValueError("Git delivery source evidence exceeds its admitted bound")
        return self
