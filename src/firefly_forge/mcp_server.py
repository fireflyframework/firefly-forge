# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""MCP tools let external coding agents operate the same authenticated API."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from firefly_forge.models import RepositoryInput, RunInput


def create_server() -> MCPServer:
    server = MCPServer("firefly forge")

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
        repository_id: str,
        brief: str,
        backend: str = "pyfly",
        frontend: str = "react",
        agent: str = "codex",
        operation: str = "build",
        checks: list[list[str]] | None = None,
        skills: list[str] | None = None,
    ) -> dict[str, Any]:
        """Queue a product implementation. Publication requires a separate explicit tool call."""
        from firefly_forge.cli import client

        request = RunInput.model_validate(
            {
                "repository_id": repository_id,
                "brief": brief,
                "backend": backend,
                "frontend": frontend,
                "agent": agent,
                "operation": operation,
                "commands": [{"argv": argv} for argv in checks or []],
                **({"skills": skills} if skills is not None else {}),
            }
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

    @server.tool()
    def get_development_plan(identifier: str) -> dict[str, Any]:
        """Read the immutable development plan, task attempts and source checkpoint."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.plan(identifier)

    @server.tool()
    def get_execution_journal(identifier: str) -> dict[str, Any]:
        """Read durable intents, receipts, errors and the journal chain validation."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.journal(identifier)

    @server.tool()
    def resume_run(identifier: str) -> dict[str, Any]:
        """Explicitly resume paused or interrupted work in its retained workspace."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.action(identifier, "resume")

    @server.tool()
    def handoff_run(identifier: str, agent: str) -> dict[str, Any]:
        """Hand paused work to a configured provider while retaining its source and policy snapshot."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.handoff(identifier, agent)

    @server.tool()
    def list_source_connections() -> list[dict[str, Any]]:
        """Read provider application setup and granted connection status without secrets."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.source_connections()

    @server.tool()
    def connect_granted_repository(provider: str, remote_id: str) -> dict[str, Any]:
        """Connect a repository already granted to an installed GitHub/GitLab/Bitbucket application."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.connect_granted_repository(provider, remote_id)

    @server.tool()
    def search_project_knowledge(repository_id: str, query: str) -> dict[str, Any]:
        """Retrieve cited project/framework references in the repository's flycanon scopes."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.search_knowledge(repository_id, query)

    @server.tool()
    def index_verified_source(identifier: str, paths: list[str]) -> dict[str, Any]:
        """Explicitly index selected verified tracked files; paths are bounded and checked for secrets."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.index_source(identifier, paths)

    @server.tool()
    def remember_verified_run(identifier: str) -> dict[str, Any]:
        """Save verified run evidence as cited, retrievable project memory."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.remember(identifier)

    @server.tool()
    def list_deployment_targets() -> list[dict[str, Any]]:
        """Read operator-configured target commands and verification requirements."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.deployment_targets()

    @server.tool()
    def deploy_verified_run(identifier: str, target_id: str) -> dict[str, Any]:
        """Explicitly deploy unchanged verified source to its project's configured target."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.deploy(identifier, target_id)

    @server.tool()
    def rollback_deployment(identifier: str) -> dict[str, Any]:
        """Explicitly execute a deployment's frozen rollback commands and health checks."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.rollback(identifier)

    return server


def serve() -> None:
    create_server().run(transport="stdio")
