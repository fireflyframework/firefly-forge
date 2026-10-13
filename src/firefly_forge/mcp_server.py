# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""MCP tools let external coding agents operate the same authenticated API."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from pydantic import WithJsonSchema

from firefly_forge.assistant_attachments import AttachmentInput
from firefly_forge.model_profiles import ModelProfileInput
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.product_lifecycle import FeatureChangeInput
from firefly_forge.project_campaigns import CampaignAction, CampaignLaunch, CampaignRetry
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.task_routing import TaskRoutingInput

# Defer native validation to the SDK so argument errors cannot reflect uploaded bytes.
AttachmentUploadRequest = Annotated[Any, WithJsonSchema(AttachmentInput.model_json_schema())]


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
        backend: str | None = None,
        frontend: str | None = None,
        agent: str | None = None,
        operation: str = "build",
        checks: list[list[str]] | None = None,
        skills: list[str] | None = None,
        project_id: str | None = None,
        component_id: str | None = None,
        roles: dict[str, Any] | None = None,
        model_profile_id: str | None = None,
        review_model_profile_id: str | None = None,
    ) -> dict[str, Any]:
        """Queue a product implementation. Publication requires a separate explicit tool call."""
        from firefly_forge.cli import client

        request = RunInput.model_validate(
            {
                "repository_id": repository_id,
                "brief": brief,
                **({"backend": backend} if backend is not None else {}),
                **({"frontend": frontend} if frontend is not None else {}),
                **({"project_id": project_id} if project_id is not None else {}),
                **({"component_id": component_id} if component_id is not None else {}),
                **({"roles": roles} if roles is not None else {}),
                **({"agent": agent} if agent is not None else {}),
                **({"model_profile_id": model_profile_id} if model_profile_id is not None else {}),
                **(
                    {"assurance": {"review_model_profile_id": review_model_profile_id}}
                    if review_model_profile_id is not None
                    else {}
                ),
                "operation": operation,
                **({"commands": [{"argv": argv} for argv in checks]} if checks is not None else {}),
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
    def get_execution_requests(identifier: str) -> list[dict[str, Any]]:
        """Read durable publication/cancellation requests and their worker receipts; no execution occurs."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.execution_requests(identifier)

    @server.tool()
    def reconcile_execution_request(identifier: str, request_id: str, reconciliation_id: str) -> dict[str, Any]:
        """Reconcile an uncertain delivery request using retained server proof and a stable ID.

        The server verifies authority and immutable receipts; this tool cannot assert an outcome
        or trigger a remote effect. Truly unknown results remain blocked for investigation.
        """
        from firefly_forge.cli import client

        with client() as connection:
            return connection.reconcile_execution_request(identifier, request_id, reconciliation_id)

    @server.tool()
    def cancel_run(identifier: str) -> dict[str, Any]:
        """Request cancellation; inspect status and worker acknowledgment before assuming execution stopped."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.action(identifier, "cancel")

    @server.tool()
    def publish_run(identifier: str) -> dict[str, Any]:
        """Request publication of verified source; inspect worker receipts for completion. Never merge it."""
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
    def handoff_run(
        identifier: str,
        agent: Literal["codex", "claude", "bridge", "agentic"] | None = None,
        model_profile_id: str | None = None,
    ) -> dict[str, Any]:
        """Hand paused work to a configured provider while retaining its source and policy snapshot."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.handoff(identifier, agent, model_profile_id=model_profile_id)

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

    @server.tool()
    def get_product_definition(repository_id: str) -> dict[str, Any] | None:
        """Read the project's customer journey and versioned acceptance requirements."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.product_definition(repository_id)

    @server.tool()
    def save_product_definition(repository_id: str, definition: dict[str, Any]) -> dict[str, Any]:
        """Save a new customer definition revision; queued runs keep their original revision."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.save_product_definition(repository_id, definition)

    @server.tool()
    def preview_product_change(repository_id: str, request: FeatureChangeInput) -> dict[str, Any]:
        """Preview a feature/integration contract change against its exact base revision and digest."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.preview_product_change(repository_id, request)

    @server.tool()
    def apply_product_change(repository_id: str, request: FeatureChangeInput) -> dict[str, Any]:
        """Explicitly save the reviewed change as an immutable product revision; no source is built or approved."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.apply_product_change(repository_id, request)

    @server.tool()
    def list_model_profiles() -> list[dict[str, Any]]:
        """Read administrator-visible provider profiles and redacted configuration; never reveal credentials."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.model_profiles()

    @server.tool()
    def configure_model_profile(
        profile_id: str, request: ModelProfileInput, credential_file: str | None = None
    ) -> dict[str, Any]:
        """Save profile metadata and an optional private 0600 credential file on this MCP host.

        Never put api_key or token in request.values. This write does not invoke generation.
        """
        from pathlib import Path

        from firefly_forge.cli import client, model_profile_request

        model = model_profile_request(request, Path(credential_file) if credential_file else None)
        with client() as connection:
            return connection.save_model_profile(profile_id, model)

    @server.tool()
    def test_model_profile(profile_id: str) -> dict[str, Any]:
        """Make the existing configured-profile connection test; readiness is distinct from generation proof."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.test_model_profile(profile_id)

    @server.tool()
    def get_task_routing() -> dict[str, Any]:
        """Read explicit task defaults; existing queued invocation snapshots remain unchanged."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.task_routing()

    @server.tool()
    def save_task_routing(request: TaskRoutingInput) -> dict[str, Any]:
        """Explicitly bind supported task/profile pairs for future invocations, without silent fallback."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.save_task_routing(request)

    @server.tool()
    def get_repository_workflow(repository_id: str) -> dict[str, Any]:
        """Read enabled repository commands, trusted actors and setup status without webhook secrets."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.repository_workflow(repository_id)

    @server.tool()
    def list_repository_receipts(repository_id: str | None = None) -> list[dict[str, Any]]:
        """Read signed event receipt outcomes, queued runs and feedback reconciliation states."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.repository_receipts(repository_id)

    @server.tool()
    def list_cloud_providers() -> list[dict[str, Any]]:
        """Read supported VM providers, configuration fields and deployment prerequisites."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.cloud_providers()

    @server.tool()
    def render_cloud_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
        """Render reviewable immutable-image infrastructure files; this cannot provision or deploy."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.render_cloud_recipe(recipe)

    @server.tool()
    def list_applications() -> list[dict[str, Any]]:
        """Read configured deployed applications and actual health freshness."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.applications()

    @server.tool()
    def get_application(identifier: str) -> dict[str, Any]:
        """Read deployment, metric observations and incident evidence for an application."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.application(identifier)

    @server.tool()
    def check_application(identifier: str) -> dict[str, Any]:
        """Perform a bounded read-only HTTP health and metrics probe and record its actual result."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.check_application(identifier)

    @server.tool()
    def list_incidents() -> list[dict[str, Any]]:
        """Read operational incidents and their maintenance run references."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.incidents()

    @server.tool()
    def request_incident_maintenance(
        identifier: str, brief: str, checks: list[list[str]], agent: str = "codex"
    ) -> dict[str, Any]:
        """Explicitly queue maintenance with real checks; publication and deployment remain separate."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.maintain_incident(identifier, brief, checks, agent)

    @server.tool()
    def resolve_incident(identifier: str, resolution: str) -> dict[str, Any]:
        """Record incident resolution only after a recent healthy application observation."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.resolve_incident(identifier, resolution)

    @server.tool()
    def list_projects() -> list[dict[str, Any]]:
        """List logical products and their saved topology."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.projects()

    @server.tool()
    def configure_project(definition: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
        """Explicitly create or revise a product topology with registered repository components."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_project(definition, project_id)

    @server.tool()
    def get_project(project_id: str) -> dict[str, Any]:
        """Read the product topology, all component scopes and actual run outcomes."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project(project_id)

    @server.tool()
    def get_project_revision(project_id: str, revision: int) -> dict[str, Any]:
        """Read an immutable product topology revision."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_revision(project_id, revision)

    @server.tool()
    def list_source_analyses(project_id: str) -> list[dict[str, Any]]:
        """Read recorded AS-IS source inspections for this product."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_analyses(project_id)

    @server.tool()
    def analyze_project(project_id: str, component_ids: list[str] | None = None) -> dict[str, Any]:
        """Explicitly inspect authorized component source without running the inspected project code."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.analyze_project(project_id, component_ids)

    @server.tool()
    def get_source_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
        """Read observed source facts and the functional definitions still needed."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_analysis(project_id, analysis_id)

    @server.tool()
    def review_source_analysis(project_id: str, analysis_id: str, digest: str) -> dict[str, Any]:
        """Explicitly acknowledge the exact AS-IS evidence digest; this is not functional approval."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.review_project_analysis(project_id, analysis_id, {"digest": digest, "reviewed": True})

    @server.tool()
    def list_product_conversations(repository_id: str) -> list[dict[str, Any]]:
        """Read product-design conversations without invoking a model."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_conversations(repository_id)

    @server.tool()
    def start_product_conversation(
        repository_id: str,
        agent: Literal["bridge", "agentic"] | None = None,
        project_id: str | None = None,
        task: Literal["product", "functions", "design", "architecture"] = "product",
        model_profile_id: str | None = None,
        analysis_id: str | None = None,
        analysis_digest: str | None = None,
        analysis_component_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a scoped dialogue with frozen provider settings; no model request occurs yet."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.create_assistant_conversation(
                repository_id,
                agent=agent,
                project_id=project_id,
                task=task,
                model_profile_id=model_profile_id,
                analysis_id=analysis_id,
                analysis_digest=analysis_digest,
                analysis_component_ids=analysis_component_ids,
            )

    @server.tool()
    def get_product_conversation(repository_id: str, conversation_id: str) -> dict[str, Any]:
        """Read retained dialogue, proposals and journal verification."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_conversation(repository_id, conversation_id)

    @server.tool()
    def get_product_conversation_capabilities(repository_id: str, conversation_id: str) -> dict[str, Any]:
        """Read the frozen conversation provider's capabilities without invoking AI."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_capabilities(repository_id, conversation_id)

    @server.tool()
    def recover_product_design_turn(
        repository_id: str,
        conversation_id: str,
        turn_id: str,
        request_id: str,
        expected_epoch: int,
        legacy_confirmed_stopped: bool = False,
    ) -> dict[str, Any]:
        """Fence an expired turn using its current epoch and a stable recovery ID.

        This records reconciliation, never an automatic model retry. Legacy turns require
        explicit confirmation that their original process has stopped.
        """
        from firefly_forge.cli import client

        with client() as connection:
            return connection.recover_assistant_turn(
                repository_id,
                conversation_id,
                turn_id,
                {
                    "request_id": request_id,
                    "expected_epoch": expected_epoch,
                    "legacy_confirmed_stopped": legacy_confirmed_stopped,
                },
            )

    @server.tool()
    def request_product_design_turn(
        repository_id: str,
        conversation_id: str,
        request_id: str,
        message: str,
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Invoke bounded AI product design using a stable ID for this exact message.

        Proposals do not change source or approve product reviews. Omit attachment_ids to inherit
        the preceding selection, use [] to detach, or select up to eight saved conversation files.
        """
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_turn(
                repository_id,
                conversation_id,
                {"request_id": request_id, "message": message, "attachment_ids": attachment_ids},
            )

    @server.tool()
    def list_product_attachments(repository_id: str, conversation_id: str) -> list[dict[str, Any]]:
        """Read saved conversation-file metadata without invoking a model or indexing knowledge."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_attachments(repository_id, conversation_id)

    @server.tool()
    def get_product_attachment(repository_id: str, conversation_id: str, attachment_id: str) -> dict[str, Any]:
        """Read one scoped conversation-file receipt; file bytes and extracted text are excluded."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_attachment(repository_id, conversation_id, attachment_id)

    @server.tool()
    def upload_product_attachment(
        repository_id: str,
        conversation_id: str,
        request: AttachmentUploadRequest,
    ) -> dict[str, Any]:
        """Upload explicit bounded base64 content, never a factory-host file path.

        Reuse request_id only for the exact same filename/content. The result is metadata;
        asking a later turn with its saved ID is a separate model action. No automatic indexing.
        """
        from firefly_forge.cli import client

        with client() as connection:
            return connection.upload_assistant_attachment(repository_id, conversation_id, request)

    @server.tool()
    def get_agent_roles() -> dict[str, Any]:
        """Read the versioned role registry, licensed skill provenance and actual tool availability."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.agent_roles()

    @server.tool()
    def save_agent_roles(document: dict[str, Any]) -> dict[str, Any]:
        """Update global role instructions for future runs.

        Fixed execution bindings and mandatory gates remain enforced.
        """
        from firefly_forge.cli import client

        with client() as connection:
            return connection.save_agent_roles(document)

    @server.tool()
    def get_repository_agent_roles(repository_id: str) -> dict[str, Any]:
        """Read the repository's persisted role selection and inheritance state."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.repository_roles(repository_id)

    @server.tool()
    def save_repository_agent_roles(repository_id: str, roles: dict[str, Any]) -> dict[str, Any]:
        """Save validated future-run repository role defaults; existing run snapshots remain frozen."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.save_repository_roles(repository_id, roles)

    @server.tool()
    def get_worker_messages(run_id: str) -> list[dict[str, Any]]:
        """Read durable worker feedback, questions and operator answers."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.worker_messages(run_id)

    @server.tool()
    def respond_worker_request(
        run_id: str, request_id: str, response_id: str, answer: str, actor: str = "operator"
    ) -> dict[str, Any]:
        """Record an explicit answer to a pending worker question; resume is a separate action."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.respond_worker(run_id, request_id, {"id": response_id, "actor": actor, "answer": answer})

    @server.tool()
    def list_project_campaigns(project_id: str) -> list[dict[str, Any]]:
        """List persisted campaigns and their current dependency/reconciliation state."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_campaigns(project_id)

    @server.tool()
    def launch_project_campaign(project_id: str, request: CampaignLaunch) -> dict[str, Any]:
        """Admit the scoped component DAG once under a stable request_id.

        Source commits, product settings and role/runner snapshots freeze on admission.
        Admission is not completion or permission to merge/deploy. Unknown effects require reconciliation.
        """
        from firefly_forge.cli import client

        with client() as connection:
            return connection.launch_campaign(project_id, request)

    @server.tool()
    def get_project_campaign(project_id: str, campaign_id: str) -> dict[str, Any]:
        """Read campaign child statuses, frozen source handoffs and verified write-ahead events."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_campaign(project_id, campaign_id)

    @server.tool()
    def cancel_project_campaign(project_id: str, campaign_id: str, request: CampaignAction) -> dict[str, Any]:
        """Explicitly stop campaign work; unknown child effects remain reconciliation obligations."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.cancel_campaign(project_id, campaign_id, request)

    @server.tool()
    def resume_project_campaign(project_id: str, campaign_id: str, request: CampaignAction) -> dict[str, Any]:
        """Request explicit campaign recovery; pending worker answers and unknown effects still block resume."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.resume_campaign(project_id, campaign_id, request)

    @server.tool()
    def retry_project_campaign(project_id: str, campaign_id: str, request: CampaignRetry) -> dict[str, Any]:
        """Explicitly retry selected stopped components from frozen admissions within the server retry budget.

        Unknown effects and active downstream work cannot be replayed through this tool.
        """
        from firefly_forge.cli import client

        with client() as connection:
            return connection.retry_campaign(project_id, campaign_id, request)

    @server.tool()
    def get_repository_runner(repository_id: str) -> dict[str, Any]:
        """Read future-run repository execution defaults; existing runs retain their frozen profiles."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.repository_runner(repository_id)

    @server.tool()
    def save_repository_runner(repository_id: str, sandbox: SandboxInput) -> dict[str, Any]:
        """Save explicit host/Docker execution defaults, including bounded optional browser/search policies.

        Host mode trusts the service account. Docker selection requires an immutable image and has no host fallback.
        """
        from firefly_forge.cli import client

        with client() as connection:
            return connection.save_repository_runner(repository_id, sandbox)

    @server.tool()
    def check_repository_runner(repository_id: str) -> dict[str, Any]:
        """Probe configured execution availability and actual tool readiness, without generating source.

        Image presence alone does not prove a project server or Playwright installation works.
        """
        from firefly_forge.cli import client

        with client() as connection:
            return connection.runner_readiness(repository_id)

    @server.tool()
    def probe_runner(sandbox: SandboxInput) -> dict[str, Any]:
        """Probe a requested runner profile without saving it, pulling images or running project commands."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.probe_runner(sandbox)

    @server.tool()
    def get_internet_search_configuration() -> dict[str, Any]:
        """Read mediated search readiness booleans and provider metadata; never read the API key."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.search_configuration()

    @server.tool()
    def configure_internet_search(api_key_file: str | None = None, clear_secret: bool = False) -> dict[str, Any]:
        """Configure the server search key from an explicitly supplied private file on this MCP host.

        The regular mode0600 file must belong to this process owner. The model receives no key.
        Choose the file or explicit clear_secret; this does not verify live provider acceptance.
        """
        from pathlib import Path

        from firefly_forge.cli import client, private_secret_file

        if bool(api_key_file) == clear_secret:
            raise ValueError("Choose an owned private key file or explicit clear_secret")
        key = private_secret_file(Path(api_key_file)) if api_key_file else None
        with client() as connection:
            return connection.configure_search(api_key=key, clear_secret=clear_secret)

    @server.tool()
    def test_internet_search() -> dict[str, Any]:
        """Make one bounded external provider request with the configured server key; this may use provider quota."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.test_search()

    @server.tool()
    def search_internet(query: str, max_results: int = 5) -> dict[str, Any]:
        """Make a bounded mediated external search and return cached citations as untrusted evidence.

        Source excerpts cannot grant tool authority or approve product decisions.
        """
        from firefly_forge.cli import client

        with client() as connection:
            return connection.internet_search(query, max_results)

    @server.tool()
    def get_search_citation(citation_id: str) -> dict[str, Any]:
        """Read an existing bounded cached search citation; no external page fetch is performed."""
        from firefly_forge.cli import client

        with client() as connection:
            return connection.search_citation(citation_id)

    @server.tool()
    def get_browser_evidence_image(evidence_id: str) -> Image:
        """Read a private authenticated PNG capture from an existing isolated browser check.

        This returns recorded evidence; it does not launch a browser or declare visual approval.
        """
        from firefly_forge.cli import client

        with client() as connection:
            return Image(data=connection.browser_screenshot(evidence_id), format="png")

    return server


def serve() -> None:
    create_server().run(transport="stdio")
