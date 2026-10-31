# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated read-only factory observations with explicit scope and paging."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import QueryParam, get_mapping, request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_forge.factory_dashboard import FactoryDashboard
from firefly_forge.factory_dashboard_models import DashboardPageInput, DashboardScope
from firefly_forge.identity import SESSION_COOKIE, digest
from firefly_forge.project_access import CURRENT_PRINCIPAL


@rest_controller
@request_mapping("/api/v1/factory-dashboard")
class FactoryDashboardController:
    def __init__(self, dashboard: FactoryDashboard) -> None:
        self.dashboard = dashboard

    @staticmethod
    def _query(request: Request, allowed: set[str]) -> None:
        if set(request.query_params) - allowed or any(
            len(request.query_params.getlist(name)) != 1 for name in request.query_params
        ):
            raise ValueError("Choose one value for each supported dashboard filter")

    @staticmethod
    def _response(value: dict[str, Any]) -> JSONResponse:
        return JSONResponse(value, headers={"Cache-Control": "private, no-store"})

    @staticmethod
    def _session_hash(request: Request) -> str | None:
        cookie = request.cookies.get(SESSION_COOKIE, "")
        return digest(cookie) if cookie and not request.headers.get("authorization") else None

    @get_mapping("")
    async def observe(
        self,
        request: Request,
        kind: QueryParam[str] = "factory",
        project_id: QueryParam[str | None] = None,
        run_id: QueryParam[str | None] = None,
        cursor: QueryParam[str | None] = None,
        limit: QueryParam[int] = 40,
        bucket: QueryParam[str | None] = None,
    ) -> JSONResponse:
        self._query(request, {"kind", "project_id", "run_id", "cursor", "limit", "bucket"})
        scope = DashboardScope.model_validate({"kind": kind, "project_id": project_id, "run_id": run_id})
        page = DashboardPageInput.model_validate({"cursor": cursor, "limit": limit, "bucket": bucket})
        return self._response(
            await self.dashboard.observe(
                self.dashboard._reader(CURRENT_PRINCIPAL.get()), scope, page, session_hash=self._session_hash(request)
            )
        )

    @get_mapping("/projects")
    async def projects(
        self,
        request: Request,
        cursor: QueryParam[str | None] = None,
        limit: QueryParam[int] = 40,
        search: QueryParam[str] = "",
    ) -> JSONResponse:
        self._query(request, {"cursor", "limit", "search"})
        page = DashboardPageInput.model_validate({"cursor": cursor, "limit": limit})
        return self._response(
            await self.dashboard.projects(
                self.dashboard._reader(CURRENT_PRINCIPAL.get()), page, search, session_hash=self._session_hash(request)
            )
        )
