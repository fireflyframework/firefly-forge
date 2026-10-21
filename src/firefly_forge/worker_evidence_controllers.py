# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current-project authenticated reads of immutable worker decision previews."""

from pyfly.container import rest_controller
from pyfly.web import PathVar, QueryParam, get_mapping, request_mapping
from starlette.responses import JSONResponse, Response

from firefly_forge.worker_evidence import WorkerEvidence


@rest_controller
@request_mapping("/api/v1/runs")
class WorkerEvidenceController:
    def __init__(self, evidence: WorkerEvidence) -> None:
        self.evidence = evidence

    @get_mapping("/{id}/worker-evidence")
    async def list(self, id: PathVar[str], request_id: QueryParam[str]) -> Response:
        result = await self.evidence.list(id, request_id)
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"})

    @get_mapping("/{id}/worker-evidence/{image_id}")
    async def image(self, id: PathVar[str], image_id: PathVar[str]) -> Response:
        record, raw = await self.evidence.image(id, image_id)
        return Response(
            raw,
            media_type=record["mime_type"],
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Content-SHA256": record["sha256"],
                "Content-Disposition": 'inline; filename="worker-preview"',
            },
        )
