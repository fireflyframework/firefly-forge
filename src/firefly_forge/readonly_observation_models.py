# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Separate consent for an unavailable pure read, never a successful historical result."""

from typing import Any, Literal

from pydantic import Field, field_validator

from firefly_forge.bridge_completion_models import Digest, StrictRecord


class ReadonlyObservationRecoveryInputV1(StrictRecord):
    version: Literal[1]
    candidate_digest: Digest
    valid_for_seconds: int = Field(ge=1, le=7200)
    acknowledge_unavailable_observation: Literal[True]
    acknowledge_no_original_callback_replay: Literal[True]
    acknowledge_same_request_attempt_and_budgets: Literal[True]
    acknowledge_finite_ownership_lease: Literal[True]

    @field_validator(
        "acknowledge_unavailable_observation",
        "acknowledge_no_original_callback_replay",
        "acknowledge_same_request_attempt_and_budgets",
        "acknowledge_finite_ownership_lease",
        mode="before",
    )
    @classmethod
    def exact_acknowledgement(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicitly acknowledge the unavailable observation without callback replay")
        return value
