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
    uvicorn.run(app, host=host, port=port)


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


@main.group()
def identity() -> None:
    """Read administrator-only identity catalogs and retained operation receipts."""


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
def connection_oauth_configure(
    name: str,
    provider: str,
    client_id: str,
    authentication: str,
    project_id: str | None,
    public_client: bool | None,
    client_secret_file: Path | None,
) -> None:
    request = {
        "provider": provider,
        "client_id": client_id,
        "authentication": authentication,
        "project_id": project_id,
        "public_client": public_client,
    }
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
def knowledge_bootstrap(data_dir: Path, service_dir: Path) -> None:
    """Create one-time private service credentials without resetting existing settings."""
    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.managed_knowledge import bootstrap_knowledge

    try:
        result = bootstrap_knowledge(ConfigurationStore(data_dir), service_dir)
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
