# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact retained checkpoint references do not carry execution authority."""

from typing import Any, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from firefly_forge.git_delivery_models import GitDeliveryContract, GitRevision
from firefly_forge.scm_authority_models import Digest, NativeID


class SourceCheckpointV1(GitDeliveryContract):
    version: Literal[1]
    run_id: NativeID
    request_id: NativeID
    expected_checkpoint_digest: Digest
    expected_revision: GitRevision


class ResolvedCheckpointV1(GitDeliveryContract):
    version: Literal[1] = 1
    operation: Literal["test", "maintain"] = "test"
    reference: SourceCheckpointV1
    repository_id: NativeID
    project_id: NativeID
    component_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    revision: GitRevision
    source_tree: GitRevision
    branch: str = Field(min_length=1, max_length=200)
    remote_receipt_digest: Digest
    journal_head: Digest
    parent_scope_digest: Digest
    digest: Digest

    @model_serializer(mode="wrap")
    def retained_operation(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value: dict[str, Any] = handler(self)
        if self.operation == "test":
            value.pop("operation", None)
        return value


def frozen_checkpoint_source(
    run: dict[str, Any], repository: dict[str, Any]
) -> tuple[dict[str, Any], str | None, str | None]:
    from firefly_forge.git_deliveries import delivery_digest

    reference = run.get("source_checkpoint")
    snapshot = run.get("source_checkpoint_snapshot")
    if reference is None and snapshot is None:
        return repository, None, None
    value = ResolvedCheckpointV1.model_validate(snapshot).model_dump(mode="json")
    if (
        run.get("operation") != value.get("operation", "test")
        or run.get("publish") is not False
        or repository["id"] != run.get("repository_id")
        or any(run.get(key) != value[key] for key in ("repository_id", "project_id", "component_id"))
        or reference != value["reference"]
        or value["revision"] != value["reference"]["expected_revision"]
        or value["digest"] != delivery_digest({key: item for key, item in value.items() if key != "digest"})
        or run.get("campaign_context")
    ):
        raise ValueError("The frozen checkpoint source no longer matches this run's operation and scope")
    return {**repository, "default_branch": value["branch"]}, value["revision"], value["source_tree"]
