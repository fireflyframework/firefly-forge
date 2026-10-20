# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native administrator self-consent and original read-only intake recovery."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.scm_product_intake import SCMProductIntake
from firefly_forge.scm_product_intake_models import SCMProductIntakeConsentInputV1


@rest_controller
@request_mapping("/api/v1/repositories/{repository_id}/scm-product-intake")
class SCMProductIntakeController:
    def __init__(self, intake: SCMProductIntake) -> None:
        self.intake = intake

    @post_mapping("/consents", status_code=201)
    async def configure(
        self, repository_id: PathVar[str], body: Body[SCMProductIntakeConsentInputV1]
    ) -> dict[str, Any]:
        return await self.intake.configure(repository_id, body)

    @get_mapping("/consents/{request_id}")
    async def consent(self, repository_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.intake.consent(repository_id, request_id)

    @get_mapping("/requests/{request_id}")
    async def operation(self, repository_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.intake.operation(repository_id, request_id)
