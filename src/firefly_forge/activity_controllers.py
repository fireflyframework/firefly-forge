# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native authenticated reads of exact sanitized activity chunks."""

from pydantic import ValidationError
from pyfly.container import rest_controller
from pyfly.web import PathVar, QueryParam, get_mapping, request_mapping
from starlette.responses import JSONResponse, Response

from firefly_forge.activity_models import ActivityContentQuery, ActivityUnavailable
from firefly_forge.activity_service import ActivityArchive


@rest_controller
@request_mapping("/api/v1/runs")
class ActivityContentController:
    def __init__(self, archive: ActivityArchive) -> None:
        self.archive = archive

    @get_mapping("/{id}/activity/{event_id}/content")
    async def content(
        self,
        id: PathVar[str],
        event_id: PathVar[str],
        expected_descriptor: QueryParam[str],
        chunk: QueryParam[int] = 0,
    ) -> Response:
        try:
            query = ActivityContentQuery(expected_descriptor=expected_descriptor, chunk=chunk)
            body = await self.archive.content(id, event_id, query)
            return JSONResponse(body, headers={"Cache-Control": "private, no-store"})
        except ValidationError:
            return JSONResponse(
                {"state": "unavailable", "reason": "activity_query_invalid"},
                status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        except ActivityUnavailable as exc:
            return JSONResponse(
                {"state": "unavailable", "reason": str(exc)},
                status_code=409,
                headers={"Cache-Control": "private, no-store"},
            )
