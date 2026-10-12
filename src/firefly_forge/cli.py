# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Operator CLI; the running control plane owns all jobs."""

from __future__ import annotations

import json
import os
import shlex
import time
from pathlib import Path
from typing import Any

import click

from firefly_forge import __version__
from firefly_forge.client import ForgeClient
from firefly_forge.models import Command, RepositoryInput, RunInput


def client() -> ForgeClient:
    return ForgeClient(os.environ.get("FORGE_URL", "http://127.0.0.1:8787"), os.environ.get("FORGE_API_TOKEN", ""))


def display(value: Any) -> None:
    click.echo(json.dumps(value, indent=2))


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Firefly Forge: turn product briefs into verified implementations."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8787, type=click.IntRange(1, 65535), show_default=True)
@click.option("--data-dir", type=click.Path(path_type=Path), default=None)
def serve(host: str, port: int, data_dir: Path | None) -> None:
    """Start the authenticated factory API and Studio."""
    import uvicorn

    from firefly_forge.api import create_application

    try:
        app = create_application(data_dir)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    uvicorn.run(app, host=host, port=port)


@main.group()
def repository() -> None:
    """Connect and list source repositories."""


@repository.command("add")
@click.argument("name")
@click.argument("url")
@click.option(
    "--provider", default="git", type=click.Choice(["git", "github", "gitlab", "bitbucket", "azure", "gitea"])
)
@click.option("--branch", default="main")
def add_repository(name: str, url: str, provider: Any, branch: str) -> None:
    with client() as connection:
        display(
            connection.add_repository(RepositoryInput(name=name, url=url, provider=provider, default_branch=branch))
        )


@repository.command("list")
def list_repositories() -> None:
    with client() as connection:
        display(connection.repositories())


@main.command("run")
@click.option("--repository", "repository_id", required=True)
@click.option("--brief", required=True)
@click.option("--backend", default="pyfly", type=click.Choice(["pyfly", "larafly", "java", "rust", "dotnet"]))
@click.option("--frontend", default="react", type=click.Choice(["react", "angular", "nextjs", "vue", "none"]))
@click.option("--agent", default="codex", type=click.Choice(["codex", "claude", "bridge", "agentic"]))
@click.option("--check", "checks", multiple=True, help="Verification command; may be repeated.")
@click.option("--wait", is_flag=True, help="Wait for a terminal result (up to one hour).")
@click.option("--publish", is_flag=True, help="Create a pull request after passing checks.")
def run(
    repository_id: str,
    brief: str,
    backend: Any,
    frontend: Any,
    agent: Any,
    checks: tuple[str, ...],
    wait: bool,
    publish: bool,
) -> None:
    request = RunInput(
        repository_id=repository_id,
        brief=brief,
        backend=backend,
        frontend=frontend,
        agent=agent,
        commands=[Command(argv=shlex.split(check)) for check in checks],
        publish=publish,
    )
    with client() as connection:
        result = connection.create_run(request)
        if wait:
            deadline = time.monotonic() + 3600
            while result["status"] not in {"succeeded", "failed", "cancelled", "interrupted"}:
                if time.monotonic() >= deadline:
                    raise click.ClickException(f"Run {result['id']} is still active; use forge status to reconnect")
                time.sleep(0.5)
                result = connection.get_run(result["id"])
        display(result)
        if result["status"] in {"failed", "cancelled", "interrupted"}:
            raise click.ClickException(f"Run {result['status']}: {result.get('error') or 'inspect run evidence'}")


@main.command()
@click.argument("identifier")
def status(identifier: str) -> None:
    with client() as connection:
        display(connection.get_run(identifier))


def register_action(name: str) -> None:
    @main.command(name)
    @click.argument("identifier")
    def action(identifier: str) -> None:
        with client() as connection:
            display(connection.action(identifier, name))


for _action in ["cancel", "retry", "publish"]:
    register_action(_action)


@main.command()
def doctor() -> None:
    """Inspect installed providers and profile configuration."""
    from firefly_forge.profiles import profile_catalog
    from firefly_forge.providers import provider_catalog

    display({"providers": provider_catalog(), "profiles": profile_catalog()})


@main.command("export")
@click.argument("identifier")
@click.option("--output", required=True, type=click.Path(path_type=Path))
def export_run(identifier: str, output: Path) -> None:
    """Download the changed-source artifact of a successful run."""
    if output.exists():
        raise click.ClickException("Output exists; choose a new path")
    with client() as connection:
        contents = connection.export(identifier)
    output.write_bytes(contents)
    click.echo(str(output))


@main.command("mcp")
def mcp_server() -> None:
    """Expose factory tools through the standard MCP stdio transport."""
    from firefly_forge.mcp_server import serve

    serve()
