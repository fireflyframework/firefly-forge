# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Operator CLI; the running control plane owns all jobs."""

from __future__ import annotations

import base64
import json
import os
import shlex
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from firefly_forge import __version__
from firefly_forge.client import ForgeClient
from firefly_forge.models import Command, RepositoryInput, RunInput

if TYPE_CHECKING:
    from firefly_forge.model_profiles import ModelProfileInput


def client() -> ForgeClient:
    return ForgeClient(os.environ.get("FORGE_URL", "http://127.0.0.1:8787"), os.environ.get("FORGE_API_TOKEN", ""))


def display(value: Any) -> None:
    click.echo(json.dumps(value, indent=2))


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Firefly Forge: turn product briefs into verified implementations."""


@main.group("git-deliveries")
def git_deliveries() -> None:
    """Inspect factory checkpoints and request a quiescent intermediate delivery."""


@git_deliveries.command("policy")
@click.argument("project_id")
def git_delivery_policy(project_id: str) -> None:
    with client() as connection:
        display(connection.git_delivery_policy(project_id))


@git_deliveries.command("configure")
@click.argument("project_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def configure_git_deliveries(project_id: str, document: Path) -> None:
    with client() as connection:
        display(connection.configure_git_deliveries(project_id, json_document(document)))


@git_deliveries.command("list")
@click.argument("run_id")
def list_git_deliveries(run_id: str) -> None:
    with client() as connection:
        display(connection.git_deliveries(run_id))


@git_deliveries.command("request")
@click.argument("run_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def request_git_delivery(run_id: str, document: Path) -> None:
    with client() as connection:
        display(connection.request_git_delivery(run_id, json_document(document)))


@git_deliveries.command("receipt")
@click.argument("run_id")
@click.argument("request_id")
def git_delivery_request(run_id: str, request_id: str) -> None:
    with client() as connection:
        display(connection.git_delivery_request(run_id, request_id))


@git_deliveries.command("observe")
@click.argument("run_id")
@click.argument("request_id")
def git_delivery_observations(run_id: str, request_id: str) -> None:
    """Observe exact local/remote state without repeating any Git delivery effect."""
    with client() as connection:
        display(connection.git_delivery_observations(run_id, request_id))


@git_deliveries.command("diff-index")
@click.argument("run_id")
@click.argument("expected_manifest")
@click.option("--page", type=click.IntRange(min=0), default=0, show_default=True)
@click.option("--request-id", default=None, help="Read the exact checkpoint request instead of the current run diff.")
def git_diff_index(run_id: str, expected_manifest: str, page: int, request_id: str | None) -> None:
    """Read one manifest-pinned file-index page; this performs no Git operation."""
    with client() as connection:
        display(
            connection.run_diff_index(run_id, expected_manifest, page=page, request_id=request_id).model_dump(
                mode="json"
            )
        )


@git_deliveries.command("diff-file")
@click.argument("run_id")
@click.argument("expected_manifest")
@click.argument("file_index", type=click.IntRange(min=0))
@click.option("--offset", type=click.IntRange(min=0), default=0, show_default=True)
@click.option("--limit", type=click.IntRange(1, 1048576), default=32768, show_default=True)
@click.option("--request-id", default=None, help="Read the exact checkpoint request instead of the current run diff.")
def git_diff_file(
    run_id: str, expected_manifest: str, file_index: int, offset: int, limit: int, request_id: str | None
) -> None:
    """Read one verified file window as an operator JSON envelope with base64 content."""
    with client() as connection:
        display(
            connection.run_diff_file(
                run_id, expected_manifest, file_index=file_index, offset=offset, limit=limit, request_id=request_id
            ).model_dump(mode="json")
        )


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8787, type=click.IntRange(1, 65535), show_default=True)
@click.option("--data-dir", type=click.Path(path_type=Path), default=None)
@click.option("--role", type=click.Choice(["combined", "api"]), default=None)
def serve(host: str, port: int, data_dir: Path | None, role: str | None) -> None:
    """Start the authenticated factory API and Studio."""
    import uvicorn

    from firefly_forge.api import create_application
    from firefly_forge.runtime_roles import RuntimeRoleConfig

    try:
        app = create_application(data_dir, role=RuntimeRoleConfig.model_validate({"role": role}).role if role else None)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    uvicorn.run(app, host=host, port=port, loop="asyncio")


@main.command()
@click.option("--data-dir", type=click.Path(path_type=Path), default=None)
def worker(data_dir: Path | None) -> None:
    """Run the single execution worker without opening an HTTP listener."""
    import asyncio

    from firefly_forge.worker_runtime import run_worker

    try:
        asyncio.run(run_worker(data_dir))
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


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
@click.option("--project", "project_id", default=None, help="Logical project identifier.")
@click.option("--component", "component_id", default=None, help="Configured component identifier.")
@click.option("--roles-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--backend", default=None, type=click.Choice(["pyfly", "larafly", "java", "rust", "dotnet", "none"]))
@click.option("--frontend", default=None, type=click.Choice(["react", "angular", "nextjs", "vue", "none"]))
@click.option("--agent", default=None, type=click.Choice(["codex", "claude", "bridge", "agentic"]))
@click.option("--model-profile", "model_profile_id", default=None)
@click.option("--review-model-profile", "review_model_profile_id", default=None)
@click.option("--operation", default="build", type=click.Choice(["build", "maintain", "test"]))
@click.option("--check", "checks", multiple=True, help="Verification command; may be repeated.")
@click.option("--wait", is_flag=True, help="Wait for a terminal result (up to one hour).")
@click.option("--publish", is_flag=True, help="Create a pull request after passing checks.")
def run(
    repository_id: str,
    brief: str,
    project_id: str | None,
    component_id: str | None,
    roles_file: Path | None,
    backend: Any,
    frontend: Any,
    agent: Any,
    model_profile_id: str | None,
    review_model_profile_id: str | None,
    operation: Any,
    checks: tuple[str, ...],
    wait: bool,
    publish: bool,
) -> None:
    values: dict[str, Any] = {
        "repository_id": repository_id,
        "brief": brief,
        "operation": operation,
        "publish": publish,
    }
    values.update(
        {
            key: value
            for key, value in {
                "project_id": project_id,
                "component_id": component_id,
                "backend": backend,
                "frontend": frontend,
                "agent": agent,
                "model_profile_id": model_profile_id,
            }.items()
            if value is not None
        }
    )
    if checks:
        values["commands"] = [Command(argv=shlex.split(check)).model_dump() for check in checks]
    if roles_file:
        values["roles"] = json_document(roles_file)
    if review_model_profile_id is not None:
        values["assurance"] = {"review_model_profile_id": review_model_profile_id}
    request = RunInput.model_validate(values)
    with client() as connection:
        result = connection.create_run(request)
        if wait:
            deadline = time.monotonic() + 3600
            while result["status"] not in {
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
                "paused",
                "needs_reconciliation",
            }:
                if time.monotonic() >= deadline:
                    raise click.ClickException(f"Run {result['id']} is still active; use forge status to reconnect")
                time.sleep(0.5)
                result = connection.get_run(result["id"])
        display(result)
        if result["status"] in {"failed", "cancelled", "interrupted", "paused", "needs_reconciliation"}:
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


for _action in ["cancel", "retry", "publish", "resume"]:
    register_action(_action)


@main.command()
@click.argument("identifier")
@click.option("--agent", default=None, type=click.Choice(["codex", "claude", "bridge", "agentic"]))
@click.option("--model-profile", "model_profile_id", default=None)
def handoff(identifier: str, agent: str | None, model_profile_id: str | None) -> None:
    """Continue paused work with a configured coding provider."""
    if agent is None and model_profile_id is None:
        raise click.ClickException("Choose --agent or --model-profile for an explicit handoff")
    with client() as connection:
        display(connection.handoff(identifier, agent, model_profile_id=model_profile_id))


@main.command()
@click.argument("identifier")
def plan(identifier: str) -> None:
    """Inspect the recorded development plan and current task states."""
    with client() as connection:
        display(connection.plan(identifier))


@main.command()
@click.argument("identifier")
def journal(identifier: str) -> None:
    """Inspect the durable execution journal and chain validation."""
    with client() as connection:
        display(connection.journal(identifier))


@main.command("execution-requests")
@click.argument("identifier")
def execution_requests(identifier: str) -> None:
    """Inspect worker-mediated delivery and cancellation receipts."""
    with client() as connection:
        display(connection.execution_requests(identifier))


@main.command("reconcile-execution-request")
@click.argument("identifier")
@click.argument("request_id")
@click.option("--reconciliation-id", required=True)
def reconcile_execution_request(identifier: str, request_id: str, reconciliation_id: str) -> None:
    """Reconcile a request from recorded server receipts without repeating delivery."""
    with client() as connection:
        display(connection.reconcile_execution_request(identifier, request_id, reconciliation_id))


@main.command("request-run-creation")
@click.argument("request_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def request_run_creation(request_id: str, document: Path) -> None:
    """Submit the retained version-1 input under its original request ID once."""
    with client() as connection:
        display(connection.request_run_creation(request_id, json_document(document)))


@main.command("run-creation-request")
@click.argument("request_id")
def run_creation_request(request_id: str) -> None:
    """Read the original admission receipt without submitting work."""
    with client() as connection:
        display(connection.run_creation_request(request_id))


@main.command("cancel-run-creation-request")
@click.argument("request_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def cancel_run_creation_request(request_id: str, document: Path) -> None:
    """Settle a delayed submission using its exact version, input digest and scope."""
    with client() as connection:
        display(connection.cancel_run_creation_request(request_id, json_document(document)))


@main.command("run-creation-decision")
@click.argument("request_id")
def run_creation_decision(request_id: str) -> None:
    """Read the admitted or cancelled decision without choosing either action."""
    with client() as connection:
        display(connection.run_creation_decision(request_id))


@main.command("agent-checkpoint-candidates")
@click.argument("identifier")
def agent_checkpoint_candidates(identifier: str) -> None:
    """Inspect original-result references without observing or continuing work."""
    with client() as connection:
        display(connection.agent_checkpoint_candidates(identifier))


@main.command("request-agent-checkpoint-restore")
@click.argument("identifier")
@click.argument("completion_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def request_agent_checkpoint_restore(identifier: str, completion_id: str, document: Path) -> None:
    """Admit the explicitly selected original result with the retained restore ID."""
    with client() as connection:
        display(connection.request_agent_checkpoint_restore(identifier, completion_id, json_document(document)))


@main.command("agent-checkpoint-restore")
@click.argument("identifier")
@click.argument("completion_id")
@click.argument("request_id")
def agent_checkpoint_restore(identifier: str, completion_id: str, request_id: str) -> None:
    """Read the exact restore ticket without dispatching another request."""
    with client() as connection:
        display(connection.agent_checkpoint_restore(identifier, completion_id, request_id))


@main.command("request-runner-continuation")
@click.argument("identifier")
@click.option("--request-id", required=True)
@click.option("--execution-id", required=True)
def request_runner_continuation(identifier: str, request_id: str, execution_id: str) -> None:
    """Explicitly admit one original queued command; acceptance does not mean command success."""
    with client() as connection:
        display(
            connection.request_runner_continuation(identifier, {"request_id": request_id, "execution_id": execution_id})
        )


@main.command("runner-continuation")
@click.argument("identifier")
@click.argument("request_id")
def runner_continuation(identifier: str, request_id: str) -> None:
    """Read the exact retained operation; this command never dispatches work."""
    with client() as connection:
        display(connection.runner_continuation(identifier, request_id))


@main.command("runner-continuation-candidates")
@click.argument("identifier")
def runner_continuation_candidates(identifier: str) -> None:
    """Inspect original commands; local candidates still require leader confirmation."""
    with client() as connection:
        display(connection.runner_continuation_candidates(identifier))


@main.command()
@click.argument("identifier")
@click.option("--target", required=True)
def deploy(identifier: str, target: str) -> None:
    """Deploy unchanged verified source to an explicit project target."""
    with client() as connection:
        display(connection.deploy(identifier, target))


@main.command()
@click.argument("identifier")
def rollback(identifier: str) -> None:
    """Execute a deployment's configured rollback and health checks."""
    with client() as connection:
        display(connection.rollback(identifier))


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


def json_document(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 1_000_000:
        raise click.ClickException("JSON document exceeds 1 MB")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise click.ClickException("Use a JSON object document")
    return value


@main.group()
def project() -> None:
    """Manage logical products and inspect their existing source."""


@project.command("list")
def list_projects() -> None:
    with client() as connection:
        display(connection.projects())


@project.command("eligible-users")
@click.argument("identifier")
@click.option("--query", required=True, help="Find an enabled registered user using 3 to 120 characters.")
def find_project_eligible_users(identifier: str, query: str) -> None:
    with client() as connection:
        display(connection.project_eligible_users(identifier, query))


@project.group("scm-actors")
def scm_actors() -> None:
    """Review verified repository accounts and explicitly delegate their project actions."""


@scm_actors.command("candidates")
@click.argument("project_id")
@click.argument("repository_id")
@click.option("--cursor", help="Continue from the exact retained event cursor.")
def scm_actor_candidates(project_id: str, repository_id: str, cursor: str | None) -> None:
    """Read retained verified accounts without contacting the provider."""
    with client() as connection:
        display(connection.scm_actor_binding_candidates(project_id, repository_id, cursor))


@scm_actors.command("configure")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def configure_scm_actor(project_id: str, repository_id: str, document: Path) -> None:
    """Submit one explicit delegation JSON; preserve its request ID after any uncertain result."""
    with client() as connection:
        display(connection.configure_scm_actor_binding(project_id, repository_id, json_document(document)))


@scm_actors.command("list")
@click.argument("project_id")
@click.argument("repository_id")
@click.option("--cursor", help="Continue from the exact retained event cursor.")
def list_scm_actors(project_id: str, repository_id: str, cursor: str | None) -> None:
    """Read immutable delegation summaries, including revoked revisions."""
    with client() as connection:
        display(connection.scm_actor_bindings(project_id, repository_id, cursor))


@scm_actors.command("get")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("request_id")
def get_scm_actor(project_id: str, repository_id: str, request_id: str) -> None:
    """Read the original receipt; a missing result never authorizes another write."""
    with client() as connection:
        display(connection.scm_actor_binding(project_id, repository_id, request_id))


@project.group("scm-polling")
def scm_polling() -> None:
    """Configure local GitHub issue polling and separate API-observed account consent."""


@scm_polling.command("setup")
@click.argument("project_id")
@click.argument("repository_id")
def read_scm_polling_setup(project_id: str, repository_id: str) -> None:
    with client() as connection:
        display(connection.scm_polling_setup(project_id, repository_id))


@scm_polling.command("configure")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def configure_scm_polling(project_id: str, repository_id: str, document: Path) -> None:
    with client() as connection:
        display(connection.configure_scm_polling(project_id, repository_id, json_document(document)))


@scm_polling.command("configuration")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("request_id")
def read_scm_polling_configuration(project_id: str, repository_id: str, request_id: str) -> None:
    with client() as connection:
        display(connection.scm_polling_configuration(project_id, repository_id, request_id))


@scm_polling.command("status")
@click.argument("project_id")
@click.argument("repository_id")
def read_scm_polling_status(project_id: str, repository_id: str) -> None:
    with client() as connection:
        display(connection.scm_polling_status(project_id, repository_id))


@scm_polling.command("candidates")
@click.argument("project_id")
@click.argument("repository_id")
def read_scm_polling_candidates(project_id: str, repository_id: str) -> None:
    with client() as connection:
        display(connection.scm_polling_candidates(project_id, repository_id))


@scm_polling.command("consent")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def configure_scm_polling_binding(project_id: str, repository_id: str, document: Path) -> None:
    with client() as connection:
        display(connection.configure_scm_polling_binding(project_id, repository_id, json_document(document)))


@scm_polling.command("binding")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("request_id")
def read_scm_polling_binding(project_id: str, repository_id: str, request_id: str) -> None:
    with client() as connection:
        display(connection.scm_polling_binding(project_id, repository_id, request_id))


@scm_polling.command("heads")
@click.argument("project_id")
@click.argument("repository_id")
@click.option("--after", help="Continue after the exact retained binding key.")
def read_scm_polling_heads(project_id: str, repository_id: str, after: str | None) -> None:
    with client() as connection:
        display(connection.scm_polling_heads(project_id, repository_id, after))


@project.group("scm-resume-grants")
def scm_resume_grants() -> None:
    """Review and save explicit consent for signed same-run resume requests."""


@scm_resume_grants.command("configure")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def configure_scm_resume_grant(project_id: str, repository_id: str, document: Path) -> None:
    """Submit one reviewed consent JSON; retain its request ID after an uncertain response."""
    with client() as connection:
        display(connection.configure_scm_resume_grant(project_id, repository_id, json_document(document)))


@scm_resume_grants.command("get")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("request_id")
def get_scm_resume_grant(project_id: str, repository_id: str, request_id: str) -> None:
    """Read the original consent receipt without resending or replacing its request."""
    with client() as connection:
        display(connection.scm_resume_grant(project_id, repository_id, request_id))


@scm_resume_grants.command("current")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("binding_request_id")
def get_current_scm_resume_grant(project_id: str, repository_id: str, binding_request_id: str) -> None:
    """Read current consent for an exact V1 binding; this does not resume a run."""
    with client() as connection:
        display(connection.current_scm_resume_grant(project_id, repository_id, binding_request_id))


@project.group("scm-polling-resume-grants")
def scm_polling_resume_grants() -> None:
    """Review and save explicit consent for API-observed GitHub same-run resume requests."""


@scm_polling_resume_grants.command("configure")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def configure_scm_polling_resume_grant(project_id: str, repository_id: str, document: Path) -> None:
    """Submit one reviewed consent JSON; retain its request ID after an uncertain response."""
    with client() as connection:
        display(connection.configure_scm_polling_resume_grant(project_id, repository_id, json_document(document)))


@scm_polling_resume_grants.command("get")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("request_id")
def get_scm_polling_resume_grant(project_id: str, repository_id: str, request_id: str) -> None:
    """Read the original consent receipt without resending or replacing its request."""
    with client() as connection:
        display(connection.scm_polling_resume_grant(project_id, repository_id, request_id))


@scm_polling_resume_grants.command("current")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("binding_request_id")
def get_current_scm_polling_resume_grant(project_id: str, repository_id: str, binding_request_id: str) -> None:
    """Read current consent for an exact polling binding; this does not resume a run."""
    with client() as connection:
        display(connection.current_scm_polling_resume_grant(project_id, repository_id, binding_request_id))


@scm_polling_resume_grants.command("review")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("binding_request_id")
@click.argument("run_id")
def review_scm_polling_resume(project_id: str, repository_id: str, binding_request_id: str, run_id: str) -> None:
    """Read the exact current answered clarification cut without resuming it."""
    with client() as connection:
        display(connection.review_scm_polling_resume(project_id, repository_id, binding_request_id, run_id))


@project.group("scm-polling-functional-grants")
def scm_polling_functional_grants() -> None:
    """Review and save explicit consent for API-observed GitHub bounded functional assistant actions."""


@scm_polling_functional_grants.command("configure")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def configure_scm_polling_functional_grant(project_id: str, repository_id: str, document: Path) -> None:
    """Submit one reviewed consent JSON; retain its request ID after an uncertain response."""
    with client() as connection:
        display(connection.configure_scm_polling_functional_grant(project_id, repository_id, json_document(document)))


@scm_polling_functional_grants.command("get")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("request_id")
def get_scm_polling_functional_grant(project_id: str, repository_id: str, request_id: str) -> None:
    """Read the original consent receipt without resending or replacing its request."""
    with client() as connection:
        display(connection.scm_polling_functional_grant(project_id, repository_id, request_id))


@scm_polling_functional_grants.command("current")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("binding_request_id")
def get_current_scm_polling_functional_grant(project_id: str, repository_id: str, binding_request_id: str) -> None:
    """Read current consent for an exact polling binding; this does not resume a run."""
    with client() as connection:
        display(connection.current_scm_polling_functional_grant(project_id, repository_id, binding_request_id))


@scm_polling_functional_grants.command("review")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("binding_request_id")
def review_scm_polling_functional(project_id: str, repository_id: str, binding_request_id: str) -> None:
    """Read the exact current functional assistant scope without invoking a model."""
    with client() as connection:
        display(connection.review_scm_polling_functional(project_id, repository_id, binding_request_id))


@project.group("scm-product-change-grants")
def scm_product_change_grants() -> None:
    """Review and save explicit consent for signed whole-product edits."""


@scm_product_change_grants.command("configure")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def configure_scm_product_change_grant(project_id: str, repository_id: str, document: Path) -> None:
    """Submit one reviewed consent JSON; retain its request ID after an uncertain response."""
    with client() as connection:
        display(connection.configure_scm_product_change_grant(project_id, repository_id, json_document(document)))


@scm_product_change_grants.command("get")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("request_id")
def get_scm_product_change_grant(project_id: str, repository_id: str, request_id: str) -> None:
    """Read the original consent receipt without resending or replacing its request."""
    with client() as connection:
        display(connection.scm_product_change_grant(project_id, repository_id, request_id))


@scm_product_change_grants.command("current")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("binding_request_id")
def get_current_scm_product_change_grant(project_id: str, repository_id: str, binding_request_id: str) -> None:
    """Read current consent for an exact V1 binding; this does not edit a product."""
    with client() as connection:
        display(connection.current_scm_product_change_grant(project_id, repository_id, binding_request_id))


@project.group("scm-product-changes")
def scm_product_changes() -> None:
    """Read retained signed product edits without sending another command."""


@scm_product_changes.command("get")
@click.argument("project_id")
@click.argument("repository_id")
@click.argument("request_id")
def get_scm_product_change(project_id: str, repository_id: str, request_id: str) -> None:
    """Read the exact original product-edit result; missing evidence does not permit another write."""
    with client() as connection:
        display(connection.scm_product_change(project_id, repository_id, request_id))


@main.group()
def identity() -> None:
    """Inspect identity receipts and request explicit administrator-only changes."""


@identity.command("configuration")
def identity_configuration() -> None:
    """Read public authentication configuration; no credentials are returned."""
    with client() as connection:
        display(connection.identity_configuration())


@identity.command("session")
def identity_session() -> None:
    """Read the authenticated caller's current identity and memberships."""
    with client() as connection:
        display(connection.identity_session())


@project.command("members")
@click.argument("project_id")
def project_members(project_id: str) -> None:
    """Read members with current owner or administrator permission."""
    with client() as connection:
        display(connection.project_members(project_id))


@project.command("member")
@click.argument("project_id")
@click.argument("user_id")
@click.option("--role", type=click.Choice(["owner", "maintainer", "developer", "reviewer", "viewer"]))
@click.option("--revoke", is_flag=True)
def set_project_membership(project_id: str, user_id: str, role: str | None, revoke: bool) -> None:
    """Set an existing user's project role or explicitly revoke membership."""
    if (role is None) == (not revoke):
        raise click.ClickException("Choose exactly one of --role or --revoke")
    with client() as connection:
        display(connection.set_project_membership(project_id, user_id, {"role": role}))


@identity.command("users")
def list_identity_users() -> None:
    with client() as connection:
        display(connection.identity_users())


@identity.command("administration")
def identity_administration() -> None:
    with client() as connection:
        display(connection.identity_administration())


@identity.command("operations")
def list_identity_operations() -> None:
    with client() as connection:
        display(connection.identity_operations())


@identity.command("set-enabled")
@click.argument("user_id")
@click.option("--enable/--disable", "enabled", default=None, required=True)
def set_identity_user_enabled(user_id: str, enabled: bool | None) -> None:
    """Enable or suspend a registered local identity; revoked sessions stay revoked."""
    if enabled is None:
        raise click.ClickException("Choose explicit --enable or --disable")
    with client() as connection:
        display(connection.set_identity_user_enabled(user_id, enabled))


@identity.command("provision")
@click.argument("request", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def provision_identity_user(request: Path) -> None:
    """Request provider-managed creation from nonsecret JSON with a stable request ID."""
    with client() as connection:
        display(connection.provision_identity_user(json_document(request)))


@identity.command("invite")
@click.argument("user_id")
@click.option("--request-id", required=True)
def invite_identity_user(user_id: str, request_id: str) -> None:
    """Request a separate invitation; provider acceptance does not prove delivery."""
    with client() as connection:
        display(connection.invite_identity_user(user_id, {"request_id": request_id}))


@identity.command("reconcile")
@click.argument("operation_id")
@click.option(
    "--outcome", type=click.Choice(["created", "not_created", "provider_accepted", "not_accepted"]), required=True
)
@click.option("--subject", default=None, help="Exact provider subject required for a created account.")
@click.option("--note", required=True)
def reconcile_identity_operation(operation_id: str, outcome: str, subject: str | None, note: str) -> None:
    """Record an inspected uncertain outcome using the server's existing checks."""
    with client() as connection:
        display(
            connection.reconcile_identity_operation(
                operation_id, {"outcome": outcome, "subject": subject, "note": note}
            )
        )


@identity.command("recover")
@click.argument("operation_id")
@click.option("--request-id", required=True)
@click.option("--owner-stopped", is_flag=True, help="Confirm the original execution owner has stopped.")
@click.option("--note", required=True)
def recover_identity_operation(operation_id: str, request_id: str, owner_stopped: bool, note: str) -> None:
    """Fence an interrupted operation for reconciliation; never repeat its effect."""
    if not owner_stopped:
        raise click.ClickException("Explicit --owner-stopped confirmation is required")
    with client() as connection:
        display(
            connection.recover_identity_operation(
                operation_id, {"request_id": request_id, "owner_stopped": owner_stopped, "note": note}
            )
        )


@project.command("save")
@click.argument("definition", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--id", "project_id", default=None)
def save_project(definition: Path, project_id: str | None) -> None:
    with client() as connection:
        display(connection.configure_project(json_document(definition), project_id))


@project.command("get")
@click.argument("identifier")
@click.option("--revision", type=click.IntRange(1), default=None)
def get_project(identifier: str, revision: int | None) -> None:
    with client() as connection:
        display(connection.project_revision(identifier, revision) if revision else connection.project(identifier))


@project.command("analyze")
@click.argument("identifier")
@click.option("--component", "components", multiple=True)
def analyze_project(identifier: str, components: tuple[str, ...]) -> None:
    with client() as connection:
        display(connection.analyze_project(identifier, list(components)))


@project.command("analyses")
@click.argument("identifier")
def list_analyses(identifier: str) -> None:
    with client() as connection:
        display(connection.project_analyses(identifier))


@project.command("analysis")
@click.argument("identifier")
@click.argument("analysis_id")
def get_analysis(identifier: str, analysis_id: str) -> None:
    with client() as connection:
        display(connection.project_analysis(identifier, analysis_id))


@project.command("review")
@click.argument("identifier")
@click.argument("analysis_id")
@click.option("--digest", required=True)
def review_analysis(identifier: str, analysis_id: str, digest: str) -> None:
    with client() as connection:
        display(connection.review_project_analysis(identifier, analysis_id, {"digest": digest, "reviewed": True}))


@main.group()
def assistant() -> None:
    """Keep a product-design conversation and inspect reviewable proposals."""


@assistant.command("list")
@click.argument("repository_id")
def list_conversations(repository_id: str) -> None:
    with client() as connection:
        display(connection.assistant_conversations(repository_id))


@assistant.command("start")
@click.argument("repository_id")
@click.option("--agent", default=None, type=click.Choice(["bridge", "agentic"]))
@click.option("--project", "project_id", default=None)
@click.option("--task", default="product", type=click.Choice(["product", "functions", "design", "architecture"]))
@click.option("--model-profile", "model_profile_id", default=None)
@click.option("--analysis", "analysis_id", default=None)
@click.option("--analysis-digest", default=None)
@click.option("--analysis-component", "analysis_component_ids", multiple=True)
def start_conversation(
    repository_id: str,
    agent: str | None,
    project_id: str | None,
    task: Any,
    model_profile_id: str | None,
    analysis_id: str | None,
    analysis_digest: str | None,
    analysis_component_ids: tuple[str, ...],
) -> None:
    with client() as connection:
        display(
            connection.create_assistant_conversation(
                repository_id,
                agent=agent,
                project_id=project_id,
                task=task,
                model_profile_id=model_profile_id,
                analysis_id=analysis_id,
                analysis_digest=analysis_digest,
                analysis_component_ids=list(analysis_component_ids) if analysis_component_ids else None,
            )
        )


@assistant.command("get")
@click.argument("repository_id")
@click.argument("conversation_id")
def get_conversation(repository_id: str, conversation_id: str) -> None:
    with client() as connection:
        display(connection.assistant_conversation(repository_id, conversation_id))


@assistant.command("ask")
@click.argument("repository_id")
@click.argument("conversation_id")
@click.option("--request-id", required=True, help="Stable 32-character delivery ID; reuse only for this exact message.")
@click.option("--message", required=True)
@click.option("--attachment", "attachment_ids", multiple=True, help="Select a saved conversation file; up to eight.")
@click.option("--detach-attachments", is_flag=True, help="Do not inherit files selected in the preceding turn.")
def ask_product(
    repository_id: str,
    conversation_id: str,
    request_id: str,
    message: str,
    attachment_ids: tuple[str, ...],
    detach_attachments: bool,
) -> None:
    if attachment_ids and detach_attachments:
        raise click.ClickException("Choose saved files or detach them, not both")
    with client() as connection:
        display(
            connection.assistant_turn(
                repository_id,
                conversation_id,
                {
                    "request_id": request_id,
                    "message": message,
                    "attachment_ids": list(attachment_ids) if attachment_ids else [] if detach_attachments else None,
                },
            )
        )


@assistant.command("capabilities")
@click.argument("repository_id")
@click.argument("conversation_id")
def conversation_capabilities(repository_id: str, conversation_id: str) -> None:
    """Read capabilities of the conversation's frozen provider without invoking AI."""
    with client() as connection:
        display(connection.assistant_capabilities(repository_id, conversation_id))


@assistant.command("recover")
@click.argument("repository_id")
@click.argument("conversation_id")
@click.argument("turn_id")
@click.option("--request-id", required=True, help="Stable 32-character recovery ID; reuse for this exact request.")
@click.option("--expected-epoch", required=True, type=click.IntRange(0, 2**63 - 2))
@click.option("--legacy-confirmed-stopped", is_flag=True, help="Confirm a legacy turn's original process has stopped.")
def recover_conversation_turn(
    repository_id: str,
    conversation_id: str,
    turn_id: str,
    request_id: str,
    expected_epoch: int,
    legacy_confirmed_stopped: bool,
) -> None:
    """Fence an expired turn for reconciliation; never retry a model automatically."""
    with client() as connection:
        display(
            connection.recover_assistant_turn(
                repository_id,
                conversation_id,
                turn_id,
                {
                    "request_id": request_id,
                    "expected_epoch": expected_epoch,
                    "legacy_confirmed_stopped": legacy_confirmed_stopped,
                },
            )
        )


@assistant.command("files")
@click.argument("repository_id")
@click.argument("conversation_id")
def list_conversation_files(repository_id: str, conversation_id: str) -> None:
    with client() as connection:
        display(connection.assistant_attachments(repository_id, conversation_id))


@assistant.command("file")
@click.argument("repository_id")
@click.argument("conversation_id")
@click.argument("attachment_id")
def get_conversation_file(repository_id: str, conversation_id: str, attachment_id: str) -> None:
    with client() as connection:
        display(connection.assistant_attachment(repository_id, conversation_id, attachment_id))


@assistant.command("upload")
@click.argument("repository_id")
@click.argument("conversation_id")
@click.option("--file", "file_path", type=click.Path(path_type=Path), required=True)
@click.option("--request-id", required=True, help="Stable 32-character upload ID; reuse only for this exact file.")
def upload_conversation_file(repository_id: str, conversation_id: str, file_path: Path, request_id: str) -> None:
    try:
        selected = file_path.lstat()
        if not stat.S_ISREG(selected.st_mode) or not 0 < selected.st_size <= 1_000_000:
            raise ValueError("Invalid selected file")
        descriptor = os.open(file_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or not 0 < info.st_size <= 1_000_000
                or (selected.st_dev, selected.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise ValueError("Invalid selected file")
            content = source.read(1_000_001)
        if not content or len(content) > 1_000_000:
            raise ValueError("Invalid selected file")
        with client() as connection:
            display(
                connection.upload_assistant_attachment(
                    repository_id,
                    conversation_id,
                    {
                        "request_id": request_id,
                        "filename": file_path.name,
                        "content_base64": base64.b64encode(content).decode("ascii"),
                    },
                )
            )
    except (OSError, ValueError):
        raise click.ClickException(
            "Upload failed. Select a regular file without links, at most 1 MB, and a stable request ID. "
            "Inspect conversation files before retrying an uncertain response."
        ) from None


@main.group()
def roles() -> None:
    """Read or edit versioned factory role instructions."""


@roles.command("get")
def get_roles() -> None:
    with client() as connection:
        display(connection.agent_roles())


@roles.command("lifecycle")
def get_role_lifecycle() -> None:
    with client() as connection:
        display(connection.agent_role_lifecycle())


@roles.command("repository-lifecycle")
@click.argument("repository_id")
def get_repository_role_lifecycle(repository_id: str) -> None:
    with client() as connection:
        display(connection.repository_role_lifecycle(repository_id))


@roles.command("save")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def save_roles(document: Path) -> None:
    with client() as connection:
        display(connection.save_agent_roles(json_document(document)))


@roles.command("repository-get")
@click.argument("repository_id")
def get_repository_roles(repository_id: str) -> None:
    with client() as connection:
        display(connection.repository_roles(repository_id))


@roles.command("repository-save")
@click.argument("repository_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def save_repository_roles(repository_id: str, document: Path) -> None:
    with client() as connection:
        display(connection.save_repository_roles(repository_id, json_document(document)))


@main.group()
def feedback() -> None:
    """Read worker questions and record explicit owner answers."""


@feedback.command("list")
@click.argument("run_id")
def get_feedback(run_id: str) -> None:
    with client() as connection:
        display(connection.worker_messages(run_id))


@feedback.command("answer")
@click.argument("run_id")
@click.argument("request_id")
@click.option("--response-id", required=True)
@click.option("--actor", default="operator")
@click.option("--answer", required=True)
def answer_worker(run_id: str, request_id: str, response_id: str, actor: str, answer: str) -> None:
    with client() as connection:
        display(connection.respond_worker(run_id, request_id, {"id": response_id, "actor": actor, "answer": answer}))


def private_secret_file(path: Path) -> str:
    import stat

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_uid != os.getuid()
            ):
                raise click.ClickException("Use an owned regular secret file with mode 0600; links are unavailable")
            data = source.read(4097)
    except OSError as exc:
        raise click.ClickException("Cannot read the private secret file") from exc
    if len(data) > 4096:
        raise click.ClickException("Secret file exceeds 4096 bytes")
    try:
        value = data.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError as exc:
        raise click.ClickException("Secret file must contain UTF-8 text") from exc
    if not value or "\x00" in value:
        raise click.ClickException("Secret file must contain a nonempty key")
    return value


def model_profile_request(document: Any, credential_file: Path | None = None) -> ModelProfileInput:
    from firefly_forge.configuration import SECRETS
    from firefly_forge.model_profiles import ModelProfileInput

    try:
        request = ModelProfileInput.model_validate(document)
    except ValueError:
        raise click.ClickException("Use a valid model profile metadata document") from None
    if SECRETS.intersection(request.values):
        raise click.ClickException("Supply credentials through a private credential file, not document values")
    if credential_file:
        field = {"agentic": "api_key", "bridge": "token"}.get(request.provider)
        if field is None:
            raise click.ClickException("Official CLI profiles use their own login and do not accept a credential file")
        request = request.model_copy(update={"values": {**request.values, field: private_secret_file(credential_file)}})
    return request


@main.group()
def models() -> None:
    """Manage named provider profiles with write-only credentials."""


@models.command("list")
def list_model_profiles() -> None:
    with client() as connection:
        display(connection.model_profiles())


@models.command("save")
@click.argument("profile_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--credential-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
def save_model_profile(profile_id: str, document: Path, credential_file: Path | None) -> None:
    request = model_profile_request(json_document(document), credential_file)
    with client() as connection:
        display(connection.save_model_profile(profile_id, request))


@models.command("test")
@click.argument("profile_id")
def test_model_profile(profile_id: str) -> None:
    with client() as connection:
        display(connection.test_model_profile(profile_id))


@main.group()
def routing() -> None:
    """Read or explicitly change future task-to-profile bindings."""


@routing.command("get")
def get_task_routing() -> None:
    with client() as connection:
        display(connection.task_routing())


@routing.command("save")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def save_task_routing(document: Path) -> None:
    with client() as connection:
        display(connection.save_task_routing(json_document(document)))


@main.group()
def product() -> None:
    """Preview and save versioned product-contract changes."""


@product.command("preview-change")
@click.argument("repository_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def preview_product_change(repository_id: str, document: Path) -> None:
    with client() as connection:
        display(connection.preview_product_change(repository_id, json_document(document)))


@product.command("apply-change")
@click.argument("repository_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def apply_product_change(repository_id: str, document: Path) -> None:
    with client() as connection:
        display(connection.apply_product_change(repository_id, json_document(document)))


@main.group()
def campaign() -> None:
    """Admit product component campaigns and inspect explicit recovery outcomes."""


@campaign.command("list")
@click.argument("project_id")
def list_campaigns(project_id: str) -> None:
    with client() as connection:
        display(connection.project_campaigns(project_id))


@campaign.command("launch")
@click.argument("project_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def launch_campaign(project_id: str, document: Path) -> None:
    """Admit a validated launch JSON containing its stable request_id."""
    with client() as connection:
        display(connection.launch_campaign(project_id, json_document(document)))


@campaign.command("get")
@click.argument("project_id")
@click.argument("campaign_id")
def get_campaign(project_id: str, campaign_id: str) -> None:
    with client() as connection:
        display(connection.project_campaign(project_id, campaign_id))


@campaign.command("report")
@click.argument("project_id")
@click.argument("campaign_id")
def report_campaign(project_id: str, campaign_id: str) -> None:
    """Read a deterministic campaign evidence snapshot, without contacting source control."""
    with client() as connection:
        display(connection.project_campaign_report(project_id, campaign_id))


@campaign.command("archive-report")
@click.argument("project_id")
@click.argument("campaign_id")
@click.option("--request-id", required=True)
@click.option("--expected-digest", required=True)
def archive_campaign_report(project_id: str, campaign_id: str, request_id: str, expected_digest: str) -> None:
    """Save a selected report; keep the request ID after any uncertain response."""
    with client() as connection:
        display(
            connection.archive_campaign_report(
                project_id,
                campaign_id,
                {
                    "request_id": request_id,
                    "expected_report_digest": expected_digest,
                },
            )
        )


@campaign.command("report-history")
@click.argument("project_id")
@click.argument("campaign_id")
@click.option("--after", type=click.IntRange(0, 100), default=0)
def campaign_report_history(project_id: str, campaign_id: str, after: int) -> None:
    """Read bounded saved-report metadata without regenerating live evidence."""
    with client() as connection:
        display(connection.campaign_report_archives(project_id, campaign_id, after))


@campaign.command("report-archive")
@click.argument("project_id")
@click.argument("campaign_id")
@click.argument("request_id")
def campaign_report_archive(project_id: str, campaign_id: str, request_id: str) -> None:
    """Read the exact immutable report receipt for the original request ID."""
    with client() as connection:
        display(connection.campaign_report_archive(project_id, campaign_id, request_id))


@campaign.command("plan-save")
@click.argument("project_id")
@click.argument("campaign_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def save_campaign_delivery_plan(project_id: str, campaign_id: str, document: Path) -> None:
    """Save a reporting plan JSON with its stable request ID and expected previous digest."""
    with client() as connection:
        display(connection.save_campaign_delivery_plan(project_id, campaign_id, json_document(document)))


@campaign.command("plan-list")
@click.argument("project_id")
@click.argument("campaign_id")
def list_campaign_delivery_plans(project_id: str, campaign_id: str) -> None:
    """Read bounded immutable reporting-plan revision summaries."""
    with client() as connection:
        display(connection.campaign_delivery_plans(project_id, campaign_id))


@campaign.command("plan-get")
@click.argument("project_id")
@click.argument("campaign_id")
@click.argument("request_id")
def get_campaign_delivery_plan(project_id: str, campaign_id: str, request_id: str) -> None:
    """Read the exact post-admission plan receipt for the original request."""
    with client() as connection:
        display(connection.campaign_delivery_plan(project_id, campaign_id, request_id))


@campaign.command("plan-progress")
@click.argument("project_id")
@click.argument("campaign_id")
@click.argument("request_id")
@click.option("--report-request-id", required=True)
def get_campaign_delivery_progress(project_id: str, campaign_id: str, request_id: str, report_request_id: str) -> None:
    """Read deterministic criteria states from one explicitly selected saved report."""
    with client() as connection:
        display(connection.campaign_delivery_progress(project_id, campaign_id, request_id, report_request_id))


@campaign.command("cancel")
@click.argument("project_id")
@click.argument("campaign_id")
@click.option("--request-id", required=True)
def cancel_campaign(project_id: str, campaign_id: str, request_id: str) -> None:
    with client() as connection:
        display(connection.cancel_campaign(project_id, campaign_id, {"request_id": request_id}))


@campaign.command("resume")
@click.argument("project_id")
@click.argument("campaign_id")
@click.option("--request-id", required=True)
def resume_campaign(project_id: str, campaign_id: str, request_id: str) -> None:
    with client() as connection:
        display(connection.resume_campaign(project_id, campaign_id, {"request_id": request_id}))


@campaign.command("retry")
@click.argument("project_id")
@click.argument("campaign_id")
@click.option("--request-id", required=True)
@click.option("--component", multiple=True, required=True)
def retry_campaign(project_id: str, campaign_id: str, request_id: str, component: tuple[str, ...]) -> None:
    with client() as connection:
        display(
            connection.retry_campaign(
                project_id, campaign_id, {"request_id": request_id, "component_ids": list(component)}
            )
        )


@main.group()
def runner() -> None:
    """Configure repository execution profiles and download private browser evidence."""


@runner.command("get")
@click.argument("repository_id")
def get_runner(repository_id: str) -> None:
    with client() as connection:
        display(connection.repository_runner(repository_id))


@runner.command("save")
@click.argument("repository_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def save_runner(repository_id: str, document: Path) -> None:
    with client() as connection:
        display(connection.save_repository_runner(repository_id, json_document(document)))


@runner.command("readiness")
@click.argument("repository_id")
def runner_readiness(repository_id: str) -> None:
    with client() as connection:
        display(connection.runner_readiness(repository_id))


@runner.command("probe")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def probe_runner(document: Path) -> None:
    with client() as connection:
        display(connection.probe_runner(json_document(document)))


@runner.command("screenshot")
@click.argument("evidence_id")
@click.option("--output", required=True, type=click.Path(path_type=Path))
def download_screenshot(evidence_id: str, output: Path) -> None:
    with client() as connection:
        display(connection.download_browser_screenshot(evidence_id, output))


@main.group()
def search() -> None:
    """Configure mediated internet search and inspect cached source citations."""


@main.group()
def tools() -> None:
    """Manage mediated tool budgets and inspect recorded execution outcomes."""


@tools.group("mcp")
def external_mcp() -> None:
    """Review external tool connections and project grants as a factory administrator."""


@main.group("connections")
def connections() -> None:
    """Manage external connection authorization in the factory control plane."""


@connections.group("oauth")
def connection_oauth() -> None:
    """Prepare browser consent and inspect durable OAuth connection state."""


@connection_oauth.command("policy-list")
def oauth_policy_list() -> None:
    """Read the current server-owned provider admission policy."""
    with client() as connection:
        display(connection.oauth_provider_policies())


@connection_oauth.command("local-policy")
@click.option("--origin", required=True)
@click.option("--enable/--disable", default=True)
@click.option("--expected-revision", type=click.IntRange(0, 2_147_483_646), required=True)
@click.option("--request-id", required=True)
def local_oauth_policy(origin: str, enable: bool, expected_revision: int, request_id: str) -> None:
    """Admit the fixed local repository service under the server's development gates."""
    with client() as connection:
        display(
            connection.configure_local_oauth_provider_policy(
                {
                    "enabled": enable,
                    "local_origin": origin,
                    "expected_revision": expected_revision,
                    "request_id": request_id,
                }
            )
        )


@connection_oauth.command("providers")
def connection_oauth_providers() -> None:
    with client() as connection:
        display(connection.connection_oauth_providers())


@connection_oauth.command("show")
@click.argument("name")
def connection_oauth_show(name: str) -> None:
    with client() as connection:
        display(connection.connection_oauth(name))


@connection_oauth.command("list")
@click.option("--project-id")
def connection_oauth_list(project_id: str | None) -> None:
    """List the server-owned connection directory and available actions."""
    with client() as connection:
        display(connection.connection_oauth_connections(project_id))


@connection_oauth.command("effect")
@click.argument("name")
@click.option("--request-id", required=True)
def connection_oauth_effect(name: str, request_id: str) -> None:
    """Observe one exact request receipt without repeating its provider call."""
    with client() as connection:
        display(connection.connection_oauth_effect(name, request_id))


@connection_oauth.command("configure")
@click.argument("name")
@click.option("--provider", required=True)
@click.option("--client-id", required=True)
@click.option(
    "--authentication", type=click.Choice(["client_secret_post", "client_secret_basic"]), default="client_secret_post"
)
@click.option("--project-id")
@click.option("--public-client", is_flag=True, default=None)
@click.option("--client-secret-file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--expected-revision", type=click.IntRange(0, 2147483647), default=None)
def connection_oauth_configure(
    name: str,
    provider: str,
    client_id: str,
    authentication: str,
    project_id: str | None,
    public_client: bool | None,
    client_secret_file: Path | None,
    expected_revision: int | None,
) -> None:
    request: dict[str, Any] = {
        "provider": provider,
        "client_id": client_id,
        "authentication": authentication,
        "project_id": project_id,
        "public_client": public_client,
    }
    if expected_revision is not None:
        request["expected_revision"] = expected_revision
    if client_secret_file:
        request["client_secret"] = private_secret_file(client_secret_file)
    with client() as connection:
        display(connection.configure_connection_oauth(name, request))


@connection_oauth.command("supersede")
@click.argument("name")
@click.option("--expected-revision", type=click.IntRange(min=1), required=True)
@click.option("--unknown-effect-id", multiple=True, required=True)
@click.option("--reason", required=True)
@click.option("--acknowledge-unknown", is_flag=True, required=True)
@click.option("--provider", required=True)
@click.option("--client-id", required=True)
@click.option(
    "--authentication", type=click.Choice(["client_secret_post", "client_secret_basic"]), default="client_secret_post"
)
@click.option("--project-id")
@click.option("--public-client", is_flag=True, default=None)
@click.option("--client-secret-file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def connection_oauth_supersede(
    name: str,
    expected_revision: int,
    unknown_effect_id: tuple[str, ...],
    reason: str,
    acknowledge_unknown: bool,
    provider: str,
    client_id: str,
    authentication: str,
    project_id: str | None,
    public_client: bool | None,
    client_secret_file: Path | None,
) -> None:
    """Start a new admission while preserving the explicitly acknowledged unknown history."""
    registration = {
        "provider": provider,
        "client_id": client_id,
        "authentication": authentication,
        "project_id": project_id,
        "public_client": public_client,
    }
    if client_secret_file:
        registration["client_secret"] = private_secret_file(client_secret_file)
    with client() as connection:
        display(
            connection.supersede_connection_oauth(
                name,
                {
                    "expected_revision": expected_revision,
                    "unknown_effect_ids": list(unknown_effect_id),
                    "reason": reason,
                    "acknowledge_unknown": acknowledge_unknown,
                    "registration": registration,
                },
            )
        )


@connection_oauth.command("prepare")
@click.argument("name")
def connection_oauth_prepare(name: str) -> None:
    """Return a short-lived link to open in your authenticated factory browser."""
    with client() as connection:
        display(connection.prepare_connection_oauth(name))


@connection_oauth.command("refresh")
@click.argument("name")
@click.option("--request-id", required=True)
def connection_oauth_refresh(name: str, request_id: str) -> None:
    with client() as connection:
        display(connection.refresh_connection_oauth(name, request_id))


@connection_oauth.command("disconnect")
@click.argument("name")
def connection_oauth_disconnect(name: str) -> None:
    """Disconnect locally; remote provider revocation remains a separate action."""
    with client() as connection:
        display(connection.disconnect_connection_oauth(name))


@external_mcp.command("catalog")
def external_mcp_catalog() -> None:
    with client() as connection:
        display(connection.external_mcp_catalog())


@external_mcp.command("configure")
@click.argument("connector", type=click.Choice(["github", "sentry", "figma", "playwright", "grafana"]))
@click.option("--enable/--disable", default=False)
@click.option("--key-file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--clear-secret", is_flag=True)
def configure_external_mcp(connector: str, enable: bool, key_file: Path | None, clear_secret: bool) -> None:
    key = private_secret_file(key_file) if key_file else None
    with client() as connection:
        display(
            connection.configure_external_mcp(
                connector, {"enabled": enable, "api_key": key, "clear_secret": clear_secret}
            )
        )


@external_mcp.command("oauth")
@click.argument("connector", type=click.Choice(["github", "local-repository-tools"]))
@click.option("--connection", "connection_name", required=True)
@click.option("--project-id", required=True)
@click.option("--enable/--disable", default=True)
def link_external_mcp_oauth(connector: str, connection_name: str, project_id: str, enable: bool) -> None:
    """Link an already authorized native connection; no token is exported."""
    with client() as connection:
        display(
            connection.link_external_mcp_oauth(
                connector, {"connection_name": connection_name, "project_id": project_id, "enabled": enable}
            )
        )


@external_mcp.command("discover")
@click.argument("connector")
def discover_external_mcp(connector: str) -> None:
    """Make an explicit remote schema request; this does not grant a tool."""
    with client() as connection:
        display(connection.discover_external_mcp(connector))


@external_mcp.command("review")
@click.argument("connector")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
def review_external_mcp(connector: str, file_path: Path) -> None:
    with client() as connection:
        display(connection.review_external_mcp(connector, json_document(file_path)))


@external_mcp.command("grants")
@click.argument("project_id")
def external_mcp_grants(project_id: str) -> None:
    with client() as connection:
        display(connection.external_mcp_grants(project_id))


@external_mcp.command("grant")
@click.argument("project_id")
@click.argument("connector")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
def grant_external_mcp(project_id: str, connector: str, file_path: Path) -> None:
    with client() as connection:
        display(connection.grant_external_mcp(project_id, connector, json_document(file_path)))


@external_mcp.command("revoke")
@click.argument("project_id")
@click.argument("grant_id")
def revoke_external_mcp(project_id: str, grant_id: str) -> None:
    with client() as connection:
        display(connection.revoke_external_mcp(project_id, grant_id))


@tools.command("policy")
def tool_policy() -> None:
    with client() as connection:
        display(connection.tool_policy())


@tools.command("configure-policy")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
def configure_tool_policy(file_path: Path) -> None:
    with client() as connection:
        display(connection.configure_tool_policy(json_document(file_path)))


@tools.command("invocations")
@click.argument("run_id")
def tool_invocations(run_id: str) -> None:
    with client() as connection:
        display(connection.tool_invocations(run_id))


@tools.command("abandon")
@click.argument("run_id")
@click.argument("correlation_id")
@click.option("--request-id", required=True, help="Stable ID for this explicit reconciliation decision.")
def abandon_tool_invocation(run_id: str, correlation_id: str, request_id: str) -> None:
    with client() as connection:
        display(connection.abandon_tool_invocation(run_id, correlation_id, request_id))


@search.command("get")
def search_configuration() -> None:
    with client() as connection:
        display(connection.search_configuration())


@search.command("configure")
@click.option("--key-file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--clear-secret", is_flag=True)
@click.option("--provider", type=click.Choice(["brave", "tavily"]))
@click.option(
    "--domain", multiple=True, help="Restrict admitted citations to this public hostname; repeat up to 10 times."
)
@click.option("--clear-domains", is_flag=True, help="Remove the saved hostname restriction.")
def configure_search(
    key_file: Path | None, clear_secret: bool, provider: str | None, domain: tuple[str, ...], clear_domains: bool
) -> None:
    if key_file and clear_secret or domain and clear_domains:
        raise click.ClickException("Choose either a key file or --clear-secret, and either domains or --clear-domains")
    if not (key_file or clear_secret or provider or domain or clear_domains):
        raise click.ClickException("Supply a private key file or a search configuration change")
    key = private_secret_file(key_file) if key_file else None
    selection: dict[str, Any] = {}
    if provider is not None:
        selection["provider"] = provider
    if domain or clear_domains:
        selection["include_domains"] = list(domain)
    with client() as connection:
        display(connection.configure_search(api_key=key, clear_secret=clear_secret, **selection))


@search.command("test")
def test_search() -> None:
    """Make one real bounded provider request with the configured server key."""
    with client() as connection:
        display(connection.test_search())


@search.command("query")
@click.argument("query")
@click.option("--count", default=5, type=click.IntRange(1, 10))
def internet_search(query: str, count: int) -> None:
    with client() as connection:
        display(connection.internet_search(query, count))


@search.command("citation")
@click.argument("citation_id")
def get_citation(citation_id: str) -> None:
    with client() as connection:
        display(connection.search_citation(citation_id))


@main.command("mcp")
def mcp_server() -> None:
    """Expose factory tools through the standard MCP stdio transport."""
    from firefly_forge.mcp_server import serve

    serve()


@main.group()
def knowledge() -> None:
    """Install and verify the managed knowledge service."""


@knowledge.command("bootstrap")
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
@click.option("--service-dir", type=click.Path(path_type=Path), required=True)
@click.option("--endpoint", help="New installation only: HTTPS loopback URL for the local knowledge service")
def knowledge_bootstrap(data_dir: Path, service_dir: Path, endpoint: str | None) -> None:
    """Create one-time private service credentials without resetting existing settings."""
    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.managed_knowledge import bootstrap_knowledge

    try:
        result = bootstrap_knowledge(ConfigurationStore(data_dir), service_dir, endpoint=endpoint)
        if result["state"] == "external":
            raise ValueError(
                "Existing knowledge settings retained; use the existing service instead of the managed stack"
            )
        display(result)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@knowledge.command("initialize")
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
def knowledge_initialize(data_dir: Path) -> None:
    """Initialize authenticated framework knowledge and require real cited retrieval."""
    import asyncio

    from pyfly.container import Container

    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.knowledge import KnowledgeConnection
    from firefly_forge.managed_knowledge import ManagedKnowledgeProvisioner

    container = Container()
    container.register_instance(ConfigurationStore, ConfigurationStore(data_dir))
    container.register(KnowledgeConnection)
    container.register(ManagedKnowledgeProvisioner)
    manager = container.resolve(ManagedKnowledgeProvisioner)
    try:
        result = asyncio.run(manager.initialize())
        display(
            {
                "readiness": result["readiness"],
                "tenant_id": result["tenant_id"],
                "framework_workspace": result["framework_workspace"],
            }
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@main.group()
def learning() -> None:
    """Inspect scoped learning evidence and review its knowledge projection."""


@learning.command("policy")
def learning_policy() -> None:
    with client() as connection:
        display(connection.learning_policy())


@learning.command("configure")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
def learning_configure(file_path: Path) -> None:
    with client() as connection:
        display(connection.configure_learning_policy(json_document(file_path)))


@learning.command("records")
@click.argument("repository_id")
def learning_records(repository_id: str) -> None:
    with client() as connection:
        display(connection.learning_records(repository_id))


@learning.command("invalidate")
@click.argument("record_id")
@click.option("--reason", required=True)
@click.option("--request-id", required=True)
def learning_invalidate(record_id: str, reason: str, request_id: str) -> None:
    with client() as connection:
        display(connection.invalidate_learning(record_id, reason, request_id))


@learning.command("invalidation-receipt")
@click.argument("record_id")
@click.option("--request-id", required=True)
def learning_invalidation_receipt(record_id: str, request_id: str) -> None:
    """Read one committed request. A missing receipt never authorizes another write."""
    with client() as connection:
        display(connection.learning_invalidation_receipt(record_id, request_id))


@learning.command("observation-receipt")
@click.argument("record_id")
@click.option("--request-id", required=True)
def learning_observation_receipt(record_id: str, request_id: str) -> None:
    """Read exact observation evidence. A missing receipt never authorizes resubmission."""
    with client() as connection:
        display(connection.learning_projection_observation_receipt(record_id, request_id))


@learning.command("projection")
@click.argument("record_id")
def learning_projection(record_id: str) -> None:
    with client() as connection:
        display(connection.learning_projection(record_id))


@learning.command("submit")
@click.argument("record_id")
@click.option("--request-id", required=True)
def learning_submit(record_id: str, request_id: str) -> None:
    """Submit once. Unknown outcomes require observation, never another submission."""
    with client() as connection:
        display(connection.submit_learning_projection(record_id, request_id))


@learning.command("observe")
@click.argument("record_id")
@click.option("--request-id", required=True)
@click.option("--source-id", default=None)
def learning_observe(record_id: str, request_id: str, source_id: str | None) -> None:
    with client() as connection:
        display(connection.observe_learning_projection(record_id, request_id, source_id))


@learning.command("observe-attempt")
@click.argument("record_id")
@click.argument("request", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def learning_observe_attempt(record_id: str, request: Path) -> None:
    """Send one nonsecret v2 JSON request. Read its exact receipt after any uncertain response."""
    with client() as connection:
        display(connection.observe_learning_projection_attempt(record_id, json_document(request)))


@learning.command("observation-attempt")
@click.argument("record_id")
@click.option("--attempt-id", required=True)
def learning_observation_attempt(record_id: str, attempt_id: str) -> None:
    """Read exact local evidence. Only not_dispatched permits an explicit fresh request."""
    with client() as connection:
        display(connection.learning_projection_observation_attempt(record_id, attempt_id))


@main.group()
def evidence() -> None:
    """Read retained run evidence without executing or resuming work."""


@evidence.command("query")
@click.argument("run_id")
@click.option("--expected-cut")
@click.option("--expected-filter")
@click.option("--after", type=click.IntRange(0, 8192), default=0)
@click.option("--limit", type=click.IntRange(1, 50), default=50)
@click.option("--kind", type=click.Choice(["all", "journal", "activity", "tool", "artifact"]), default="all")
@click.option("--task-id")
@click.option("--text", default="")
@click.option("--record-id")
def query_run_evidence(run_id: str, **values: Any) -> None:
    with client() as connection:
        display(connection.run_evidence(run_id, {k: v for k, v in values.items() if v is not None}))


@evidence.command("export")
@click.argument("run_id")
@click.option("--expected-cut", required=True)
@click.option("--output", type=click.Path(path_type=Path), required=True)
def export_run_evidence(run_id: str, expected_cut: str, output: Path) -> None:
    with client() as connection:
        content = connection.export_run_evidence(run_id, expected_cut)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(content)
    click.echo(str(output))


@evidence.command("activity")
@click.argument("run_id")
@click.argument("event_id")
@click.option("--expected-descriptor", required=True)
@click.option("--chunk", type=click.IntRange(0, 255), default=0)
def read_activity_content(run_id: str, event_id: str, expected_descriptor: str, chunk: int) -> None:
    """Read one verified fragment of the complete sanitized provider message."""
    with client() as connection:
        display(
            connection.run_activity_content(
                run_id, event_id, {"expected_descriptor": expected_descriptor, "chunk": chunk}
            )
        )


@main.group("coding-check-grants")
def coding_check_grants() -> None:
    """Review explicit configured-check authority, separate from a recovery allowance."""


@main.group("workspace-reconciliations")
def workspace_reconciliations() -> None:
    """Review an environment-only transition for the existing queued task and unchanged budgets."""


@main.group("source-edit-continuations")
def source_edit_continuations() -> None:
    """Explicitly activate existing readers while preserving the original command budget."""


@source_edit_continuations.command("candidate")
@click.argument("run_id")
def source_edit_candidate(run_id: str) -> None:
    with client() as connection:
        display(connection.source_edit_candidate(run_id))


@source_edit_continuations.command("activate")
@click.argument("run_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def activate_source_edit(run_id: str, document: Path) -> None:
    with client() as connection:
        display(connection.activate_source_edit(run_id, json_document(document)))


@source_edit_continuations.command("get")
@click.argument("run_id")
@click.argument("request_id")
def source_edit_receipt(run_id: str, request_id: str) -> None:
    with client() as connection:
        display(connection.source_edit_continuation(run_id, request_id))


@main.group("source-budget")
def source_budget() -> None:
    """Review finite source-read and continuation allowances for an existing run."""


@source_budget.command("policy")
def source_budget_policy() -> None:
    with client() as connection:
        display(connection.source_budget_policy())


@source_budget.command("configure-policy")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def configure_source_budget_policy(document: Path) -> None:
    with client() as connection:
        display(connection.configure_source_budget_policy(json_document(document)))


@source_budget.command("candidate")
@click.option("--basis", type=click.Choice(["known_completed_output"]))
@click.argument("run_id")
@click.argument("parent_request_id")
@click.option("--previous-catalog-path", required=True)
@click.option("--read-file-lifetime-limit", required=True, type=click.IntRange(11, 30))
@click.option("--activation-ceiling", required=True, type=click.IntRange(5, 32))
@click.option("--additional-activations", required=True, type=click.IntRange(1, 1))
def source_budget_candidate(
    run_id: str,
    parent_request_id: str,
    previous_catalog_path: str,
    read_file_lifetime_limit: int,
    activation_ceiling: int,
    additional_activations: int,
    basis: str | None = None,
) -> None:
    with client() as connection:
        display(
            connection.source_budget_candidate(
                run_id,
                parent_request_id,
                previous_catalog_path,
                {
                    "version": 1,
                    "read_file_lifetime_limit": read_file_lifetime_limit,
                    "activation_ceiling": activation_ceiling,
                    "additional_activations": additional_activations,
                    **({"basis": basis} if basis is not None else {}),
                },
            )
        )


@source_budget.command("admit")
@click.argument("run_id")
@click.argument("parent_request_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def admit_source_budget(run_id: str, parent_request_id: str, document: Path) -> None:
    with client() as connection:
        display(connection.admit_source_budget(run_id, parent_request_id, json_document(document)))


@source_budget.command("get")
@click.argument("run_id")
@click.argument("parent_request_id")
@click.argument("request_id")
def source_budget_receipt(run_id: str, parent_request_id: str, request_id: str) -> None:
    with client() as connection:
        display(connection.source_budget_receipt(run_id, parent_request_id, request_id))


@workspace_reconciliations.command("candidate")
@click.argument("run_id")
def workspace_reconciliation_candidate(run_id: str) -> None:
    with client() as connection:
        display(connection.workspace_reconciliation_candidate(run_id))


@workspace_reconciliations.command("admit")
@click.argument("run_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def reconcile_workspace(run_id: str, document: Path) -> None:
    """Submit the reviewed original request once; uncertain delivery requires exact receipt readback."""
    with client() as connection:
        display(connection.reconcile_workspace(run_id, json_document(document)))


@workspace_reconciliations.command("get")
@click.argument("run_id")
@click.argument("request_id")
def workspace_reconciliation_receipt(run_id: str, request_id: str) -> None:
    with client() as connection:
        display(connection.workspace_reconciliation(run_id, request_id))


@coding_check_grants.command("candidate")
@click.argument("run_id")
@click.argument("recovery_request_id")
@click.option("--bind-port", type=click.IntRange(0, 65535), default=0)
def coding_check_candidate(run_id: str, recovery_request_id: str, bind_port: int) -> None:
    with client() as connection:
        display(connection.coding_check_candidate(run_id, recovery_request_id, bind_port=bind_port))


@coding_check_grants.command("grant")
@click.argument("run_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def grant_coding_checks(run_id: str, document: Path) -> None:
    """Submit the reviewed original grant once; a lost reply requires exact readback."""
    with client() as connection:
        display(connection.grant_coding_checks(run_id, json_document(document)))


@coding_check_grants.command("get")
@click.argument("run_id")
@click.argument("request_id")
def coding_check_receipt(run_id: str, request_id: str) -> None:
    with client() as connection:
        display(connection.coding_check_receipt(run_id, request_id))


@coding_check_grants.command("current")
@click.argument("run_id")
def coding_check_current(run_id: str) -> None:
    with client() as connection:
        display(connection.coding_check_current(run_id))


@coding_check_grants.command("revoke")
@click.argument("run_id")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def revoke_coding_checks(run_id: str, document: Path) -> None:
    with client() as connection:
        display(connection.revoke_coding_checks(run_id, json_document(document)))
