# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated reads and deterministic downloads of retained run evidence."""

import hashlib
from typing import Any

from pyfly.container import rest_controller
from pyfly.web import PathVar, QueryParam, get_mapping, request_mapping
from starlette.responses import JSONResponse, Response

from firefly_forge.run_evidence import EvidenceCutChanged, EvidenceQuery, EvidenceUnavailable, RunEvidence


@rest_controller
@request_mapping("/api/v1/runs")
class RunEvidenceController:
    def __init__(self, evidence: RunEvidence) -> None:
        self.evidence = evidence

    @get_mapping("/{id}/evidence")
    async def query(
        self,
        id: PathVar[str],
        after: QueryParam[int] = 0,
        limit: QueryParam[int] = 50,
        kind: QueryParam[str] = "all",
        task_id: QueryParam[str | None] = None,
        text: QueryParam[str] = "",
        record_id: QueryParam[str | None] = None,
        expected_cut: QueryParam[str | None] = None,
        expected_filter: QueryParam[str | None] = None,
    ) -> Response:
        try:
            query = EvidenceQuery.model_validate(
                dict(
                    after=after,
                    limit=limit,
                    kind=kind,
                    task_id=task_id,
                    text=text,
                    record_id=record_id,
                    expected_cut=expected_cut,
                    expected_filter=expected_filter,
                )
            )
            result: dict[str, Any] = await self.evidence.query(id, query)
            return JSONResponse(result, headers={"Cache-Control": "private, no-store"})
        except (EvidenceCutChanged, EvidenceUnavailable) as exc:
            return JSONResponse(
                {"state": "unavailable", "reason": str(exc)}, status_code=409, headers={"Cache-Control": "no-store"}
            )

    @get_mapping("/{id}/evidence.zip")
    async def export(self, id: PathVar[str], expected_cut: QueryParam[str]) -> Response:
        try:
            body = await self.evidence.export(id, expected_cut)
            return Response(
                body,
                media_type="application/zip",
                headers={
                    "Cache-Control": "private, no-store",
                    "Content-Disposition": 'attachment; filename="run-evidence.zip"',
                    "X-Content-SHA256": hashlib.sha256(body).hexdigest(),
                },
            )
        except (EvidenceCutChanged, EvidenceUnavailable) as exc:
            return JSONResponse(
                {"state": "unavailable", "reason": str(exc)}, status_code=409, headers={"Cache-Control": "no-store"}
            )
