# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native authenticated, private paged Git diff endpoints."""

from pyfly.container import rest_controller
from pyfly.web import PathVar, QueryParam, get_mapping, request_mapping
from starlette.responses import JSONResponse, Response

from firefly_forge.git_diff_archive import GitDiffArchive


@rest_controller
@request_mapping("/api/v1/runs/{run_id}")
class GitDiffController:
    def __init__(self, archive: GitDiffArchive) -> None:
        self.archive = archive

    @get_mapping("/diff/index")
    async def index(
        self, run_id: PathVar[str], expected_manifest: QueryParam[str], page: QueryParam[int] = 0
    ) -> Response:
        body = await self.archive.index(run_id, expected_manifest, page=page)
        return JSONResponse(body, headers={"Cache-Control": "private, no-store"})

    @get_mapping("/diff/files/{file_index}")
    async def file(
        self,
        run_id: PathVar[str],
        file_index: PathVar[int],
        expected_manifest: QueryParam[str],
        offset: QueryParam[int] = 0,
        limit: QueryParam[int] = 32768,
    ) -> Response:
        body = await self.archive.file(run_id, expected_manifest, file_index=file_index, offset=offset, limit=limit)
        return JSONResponse(body, headers={"Cache-Control": "private, no-store"})

    @get_mapping("/git-deliveries/requests/{request_id}/diff")
    async def checkpoint_index(
        self,
        run_id: PathVar[str],
        request_id: PathVar[str],
        expected_manifest: QueryParam[str],
        page: QueryParam[int] = 0,
    ) -> Response:
        body = await self.archive.index(run_id, expected_manifest, page=page, request_id=request_id)
        return JSONResponse(body, headers={"Cache-Control": "private, no-store"})

    @get_mapping("/git-deliveries/requests/{request_id}/diff/files/{file_index}")
    async def checkpoint_file(
        self,
        run_id: PathVar[str],
        request_id: PathVar[str],
        file_index: PathVar[int],
        expected_manifest: QueryParam[str],
        offset: QueryParam[int] = 0,
        limit: QueryParam[int] = 32768,
    ) -> Response:
        body = await self.archive.file(
            run_id, expected_manifest, file_index=file_index, offset=offset, limit=limit, request_id=request_id
        )
        return JSONResponse(body, headers={"Cache-Control": "private, no-store"})
