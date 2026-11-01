# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A verified same-run resume delegation can settle only one already-consumed claim."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, model_validator

from firefly_forge.journal import digest
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract
from firefly_forge.scm_resume_models import SCMResumeActorContextV1


class SCMConsumedContinuationAuthorityV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    scope: SCMResumeActorContextV1
    request_id: NativeID
    recovery_digest: Digest
    claim_digest: Digest
    original_actor_digest: Digest
    intake_digest: Digest
    policy_digest: Digest
    local_postprocessing_only: Literal[True]
    digest: Digest

    @model_validator(mode="after")
    def exact_authority(self) -> SCMConsumedContinuationAuthorityV1:
        if (
            self.scope.transport != "github_rest_poll_resume_v3"
            or self.scope.action != "resume"
            or digest(self.model_dump(mode="json", exclude={"digest"})) != self.digest
        ):
            raise ValueError("Consumed continuation requires one sealed verified polling-resume authority")
        return self
