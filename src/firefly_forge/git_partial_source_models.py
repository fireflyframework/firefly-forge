# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Finite source-only consent preserves failed graph custody and original budgets."""

from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, field_validator

from firefly_forge.git_diff_models import GitDiffRefV1
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


class GitPartialSourceConsentV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    version: Literal[1]
    candidate_digest: Digest
    policy_digest: Digest
    expected_run_digest: Digest
    journal_digest: Digest
    source_digest: Digest
    frontier_record_digest: Digest
    acknowledge_partial_unverified: Literal[True]
    acknowledge_preserve_failed_graph: Literal[True]
    acknowledge_no_execution_grant: Literal[True]

    @field_validator(
        "acknowledge_partial_unverified",
        "acknowledge_preserve_failed_graph",
        "acknowledge_no_execution_grant",
        mode="before",
    )
    @classmethod
    def explicit_acknowledgment(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicitly acknowledge partial source without graph execution")
        return True


Revision = Annotated[str, Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")]


class PartialSourceRecord(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class PartialSourceTaskV1(PartialSourceRecord):
    id: Literal["implement"]
    status: Literal["running", "needs_reconciliation"]
    attempt: int = Field(ge=1, le=100)
    idempotency_key: NativeID


class PartialSourceInvocationV1(PartialSourceRecord):
    invocation_id: NativeID
    ordinal: int = Field(ge=1, le=100)
    predecessor: Digest | None
    round: int = Field(ge=1, le=1000000)
    grants: list[str] = Field(min_length=1, max_length=9)
    contract: Digest
    run_cut: Digest
    workspace: dict[str, str] = Field(min_length=4, max_length=4)


class PartialSourceFrontierV1(PartialSourceRecord):
    version: Literal[1]
    kind: Literal["git_partial_source_checkpoint"]
    frontier_record_digest: Digest
    frontier_checkpoint_digest: Digest
    invocation: PartialSourceInvocationV1
    invocation_digest: Digest
    completion_count: int = Field(ge=1, le=100)
    closure_digest: Digest
    journal_head: Digest
    later_tool_names: list[Literal["list_files", "read_file", "search_text", "git_diff"]]
    task: PartialSourceTaskV1
    verification_status: Literal["partial_unverified"]


class PartialSourceCandidateV1(PartialSourceRecord):
    version: Literal[1]
    run_id: NativeID
    actor: dict[str, Any]
    policy_digest: Digest
    expected_run_digest: Digest
    journal_digest: Digest
    source_digest: Digest
    index_digest: Digest
    expected_head: Revision
    workspace: dict[str, str] = Field(min_length=4, max_length=4)
    frontier_record_digest: Digest
    proof: PartialSourceFrontierV1
    effects_digest: Digest
    original_checkpoint_revision: Revision | None
    digest: Digest


class PartialSourceAdmissionV1(PartialSourceRecord):
    version: Literal[1]
    request_id: NativeID
    actor: dict[str, Any]
    request: GitPartialSourceConsentV1
    candidate: PartialSourceCandidateV1
    staged_tree: Revision
    source_evidence: GitDiffRefV1
    verification_status: Literal["partial_unverified"]
