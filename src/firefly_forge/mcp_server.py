# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""MCP tools let external coding agents operate the same authenticated API."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from firefly_forge.models import RepositoryInput, RunInput


def create_server() -> MCPServer:
    server = MCPServer("Firefly Forge")

    @server.tool()
    def list_repositories() -> list[dict[str, Any]]:
        """List connected factory repositories."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.repositories()

    @server.tool()
    def connect_repository(name: str, url: str, provider: str = "git", branch: str = "main") -> dict[str, Any]:
        """Connect a Git repository; credentials use server-side environment configuration."""
        from firefly_forge.cli import client

        request = RepositoryInput.model_validate(
            {"name": name, "url": url, "provider": provider, "default_branch": branch}
        )
        with client() as connection:
            return connection.add_repository(request)

    @server.tool()
    def create_run(
        repository_id: str, brief: str, backend: str = "pyfly", frontend: str = "react", agent: str = "codex"
    ) -> dict[str, Any]:
        """Queue a product implementation. Publication requires a separate explicit tool call."""
        from firefly_forge.cli import client

        request = RunInput.model_validate(
            {"repository_id": repository_id, "brief": brief, "backend": backend, "frontend": frontend, "agent": agent}
        )
        with client() as connection:
            return connection.create_run(request)

    @server.tool()
    def get_run(identifier: str) -> dict[str, Any]:
        """Read run status, diff and verification evidence."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.get_run(identifier)

    @server.tool()
    def cancel_run(identifier: str) -> dict[str, Any]:
        """Cancel a queued or active implementation."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.action(identifier, "cancel")

    @server.tool()
    def publish_run(identifier: str) -> dict[str, Any]:
        """Publish a verified implementation as a reviewable pull request; never merge it."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.action(identifier, "publish")

    return server


def serve() -> None:
    create_server().run(transport="stdio")
