# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed Python client for factory operators and agent integrations."""

from __future__ import annotations

from typing import Any, Self

import httpx

from firefly_forge.models import RepositoryInput, RunInput


class ForgeClient:
    def __init__(self, base_url: str, token: str) -> None:
        if not token:
            raise ValueError("A factory operator token is required")
        self.http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1/", headers={"Authorization": f"Bearer {token}"}, timeout=60
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(method, path, **kwargs)
        if response.is_error:
            try:
                detail = response.json().get("detail", response.reason_phrase)
            except ValueError:
                detail = response.reason_phrase
            raise RuntimeError(f"Factory request failed (HTTP {response.status_code}): {detail}")
        return response.json()

    def repositories(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "repositories"))

    def add_repository(self, request: RepositoryInput) -> dict[str, Any]:
        return dict(self.request("POST", "repositories", json=request.model_dump()))

    def create_run(self, request: RunInput) -> dict[str, Any]:
        return dict(self.request("POST", "runs", json=request.model_dump()))

    def get_run(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{identifier}"))

    def action(self, identifier: str, action: str) -> dict[str, Any]:
        if action not in {"cancel", "retry", "publish"}:
            raise ValueError("Unknown run action")
        return dict(self.request("POST", f"runs/{identifier}/{action}"))

    def export(self, identifier: str) -> bytes:
        response = self.http.get(f"runs/{identifier}/artifacts/changes.zip")
        response.raise_for_status()
        return response.content
