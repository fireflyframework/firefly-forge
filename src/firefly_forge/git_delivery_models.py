# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit bounded factory-owned Git delivery policy and operator contracts."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, model_validator

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


class GitDeliveryPolicyInputV1(GitDeliveryContract):
    version: Literal[1]
    request_id: NativeID
    expected_previous_digest: Digest | None
    policy: GitDeliveryPolicyV1


class GitDeliveryInputV1(GitDeliveryContract):
    version: Literal[1]
    request_id: NativeID
    expected_head: GitRevision
    expected_source_tree: GitRevision


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
    changed_file_count: int = Field(ge=1, le=200)
    patch_sha256: Digest
    branch: str = Field(min_length=1, max_length=200)
    local_status: Literal["checkpointed"] = "checkpointed"
    remote_status: RemoteStatus
    pr_url: str | None = Field(default=None, max_length=2048)
    created_at: str = Field(min_length=1, max_length=50)
    checkpoint_digest: Digest


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
