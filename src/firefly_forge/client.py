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
        return dict(self.request("POST", "runs", json=request.model_dump(exclude_unset=True)))

    def get_run(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{identifier}"))

    def action(self, identifier: str, action: str) -> dict[str, Any]:
        if action not in {"cancel", "retry", "publish", "resume"}:
            raise ValueError("Unknown run action")
        return dict(self.request("POST", f"runs/{identifier}/{action}", **({"json": {}} if action == "resume" else {})))

    def plan(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{identifier}/plan"))

    def journal(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{identifier}/journal"))

    def handoff(self, identifier: str, agent: str) -> dict[str, Any]:
        if agent not in {"codex", "claude", "bridge", "agentic"}:
            raise ValueError("Unknown coding provider")
        return dict(self.request("POST", f"runs/{identifier}/handoff", json={"agent": agent}))

    def source_connections(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "scm/connections"))

    def connect_granted_repository(self, provider: str, remote_id: str) -> dict[str, Any]:
        if provider not in {"github", "gitlab", "bitbucket"}:
            raise ValueError("Unknown application provider")
        return dict(
            self.request("POST", f"scm/connections/{provider}/repositories/connect", json={"repository_id": remote_id})
        )

    def search_knowledge(self, repository_id: str, query: str) -> dict[str, Any]:
        return dict(self.request("POST", "knowledge/search", json={"repository_id": repository_id, "query": query}))

    def index_source(self, identifier: str, paths: list[str]) -> dict[str, Any]:
        return dict(self.request("POST", f"runs/{identifier}/knowledge/index", json={"paths": paths}))

    def remember(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("POST", f"runs/{identifier}/knowledge/remember"))

    def deployment_targets(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "deployment-targets"))

    def deploy(self, identifier: str, target_id: str) -> dict[str, Any]:
        return dict(self.request("POST", f"runs/{identifier}/deploy", json={"target_id": target_id}))

    def rollback(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("POST", f"deployments/{identifier}/rollback"))

    def export(self, identifier: str) -> bytes:
        response = self.http.get(f"runs/{identifier}/artifacts/changes.zip")
        response.raise_for_status()
        return response.content
