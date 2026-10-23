# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current project operators recover one exact retained answer without another comment."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.collaboration import Collaboration
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_natural_effects import NaturalEffects, NaturalReadmission
from firefly_forge.scm_natural_product import NaturalProductContinuation
from firefly_forge.scm_natural_tasks import SCMNaturalTasks


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/repositories/{repository_id}/scm-polling/natural-messages")
class SCMNaturalController:
    def __init__(self, collaboration: Collaboration) -> None:
        self.collaboration = collaboration
        self.effects = NaturalEffects(SCMNaturalTasks(collaboration))

    async def _read(self, project: str, repository: str, receipt: str) -> dict[str, Any]:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None:
            raise AccessDenied("An authenticated current project reader is required")
        await ProjectAccess(self.collaboration.store).require_project(actor, project, "read")
        value = await self.collaboration.receipt(receipt)
        scope = (value.get("natural_turn") or {}).get("context", {}).get("scope", {})
        if value["repository_id"] != repository or scope.get("project_id") != project:
            raise AccessDenied("The retained natural message belongs to another exact project or repository")
        return value

    @get_mapping("/{receipt_id}")
    async def read(
        self, project_id: PathVar[str], repository_id: PathVar[str], receipt_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self._read(project_id, repository_id, receipt_id)

    @get_mapping("/{receipt_id}/admission-review")
    async def review(
        self, project_id: PathVar[str], repository_id: PathVar[str], receipt_id: PathVar[str]
    ) -> dict[str, Any]:
        parent = await self._read(project_id, repository_id, receipt_id)
        if parent["natural_turn"].get("resolution", {}).get("state") in {"product_answer", "product_request"}:
            return await NaturalProductContinuation(self.effects.natural).review(receipt_id)
        return await self.effects.review(receipt_id)

    @post_mapping("/{receipt_id}/admissions", status_code=201)
    async def admit(
        self,
        project_id: PathVar[str],
        repository_id: PathVar[str],
        receipt_id: PathVar[str],
        body: Body[NaturalReadmission],
    ) -> dict[str, Any]:
        parent = await self._read(project_id, repository_id, receipt_id)
        if parent["natural_turn"].get("resolution", {}).get("state") in {"product_answer", "product_request"}:
            return await NaturalProductContinuation(self.effects.natural).readmit(receipt_id, body)
        return await self.effects.readmit(receipt_id, body)

    @get_mapping("/{receipt_id}/admissions/{request_id}")
    async def admission(
        self, project_id: PathVar[str], repository_id: PathVar[str], receipt_id: PathVar[str], request_id: PathVar[str]
    ) -> dict[str, Any]:
        value = await self._read(project_id, repository_id, receipt_id)
        product = value["natural_turn"].get("resolution", {}).get("state") in {"product_answer", "product_request"}
        original = (value.get("natural_product_admissions" if product else "natural_admissions") or {}).get(request_id)
        if original is None:
            raise KeyError("The original answer admission receipt is unavailable; do not replace an uncertain request")
        return dict(original)
