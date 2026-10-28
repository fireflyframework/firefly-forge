# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit consent to continue the original ticket from a retained checkpoint."""

from typing import Any, Literal

from pydantic import Field, field_validator

from firefly_forge.bridge_completion_models import Digest, StrictRecord


class CheckpointContinuationInputV1(StrictRecord):
    version: Literal[1]
    candidate_digest: Digest
    valid_for_seconds: int = Field(strict=True, ge=1, le=7200)
    acknowledge_retained_response_consumed_once: Literal[True]
    acknowledge_no_completed_effect_replay: Literal[True]
    acknowledge_same_request_attempt_and_budgets: Literal[True]
    acknowledge_finite_ownership_lease: Literal[True]

    @field_validator(
        "acknowledge_retained_response_consumed_once",
        "acknowledge_no_completed_effect_replay",
        "acknowledge_same_request_attempt_and_budgets",
        "acknowledge_finite_ownership_lease",
        mode="before",
    )
    @classmethod
    def exact_acknowledgement(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicitly acknowledge the same-ticket continuation and preserved effects")
        return value
