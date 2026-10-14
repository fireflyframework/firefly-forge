# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed Python client for factory operators and agent integrations."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import UUID

import httpx

from firefly_forge.models import RepositoryInput, RunInput

if TYPE_CHECKING:
    from firefly_forge.agent_roles import ProjectRoles, RoleDocument, WorkerResponse
    from firefly_forge.assistant_attachments import AttachmentInput
    from firefly_forge.model_profiles import ModelProfileInput
    from firefly_forge.product_assistant import AssistantRecoveryInput, AssistantTurnInput
    from firefly_forge.product_lifecycle import FeatureChangeInput
    from firefly_forge.project_campaigns import CampaignAction, CampaignLaunch, CampaignRetry
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.reverse_engineering import AnalysisReview
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.task_routing import TaskRoutingInput


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

    def execution_requests(self, identifier: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.request("GET", f"runs/{self._id(identifier)}/execution-requests")]

    def reconcile_execution_request(self, identifier: str, request_id: str, reconciliation_id: str) -> dict[str, Any]:
        from firefly_forge.execution_requests import ExecutionRequestReconciliation

        body = ExecutionRequestReconciliation(reconciliation_id=reconciliation_id)
        return dict(
            self.request(
                "POST",
                f"runs/{self._id(identifier)}/execution-requests/{self._id(request_id)}/reconcile",
                json=body.model_dump(),
            )
        )

    def handoff(
        self, identifier: str, agent: str | None = None, *, model_profile_id: str | None = None
    ) -> dict[str, Any]:
        from firefly_forge.controllers import ResumeRequest

        request = ResumeRequest.model_validate({"agent": agent, "model_profile_id": model_profile_id})
        if request.agent is None and request.model_profile_id is None:
            raise ValueError("Choose a coding provider or named model profile")
        return dict(self.request("POST", f"runs/{identifier}/handoff", json=request.model_dump(exclude_none=True)))

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

    def product_definition(self, repository_id: str) -> dict[str, Any] | None:
        result = self.request("GET", f"repositories/{repository_id}/product")
        return dict(result) if result else None

    def save_product_definition(self, repository_id: str, definition: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.collaboration import ProductDefinition

        model = ProductDefinition.model_validate(definition)
        return dict(self.request("PUT", f"repositories/{repository_id}/product", json=model.model_dump()))

    def _product_change(
        self, repository_id: str, request: FeatureChangeInput | dict[str, Any], *, preview: bool
    ) -> dict[str, Any]:
        from firefly_forge.product_lifecycle import FeatureChangeInput

        model = FeatureChangeInput.model_validate(request)
        path = f"repositories/{self._id(repository_id)}/product/changes" + ("/preview" if preview else "")
        return dict(self.request("POST", path, json=model.model_dump(mode="json")))

    def preview_product_change(
        self, repository_id: str, request: FeatureChangeInput | dict[str, Any]
    ) -> dict[str, Any]:
        return self._product_change(repository_id, request, preview=True)

    def apply_product_change(self, repository_id: str, request: FeatureChangeInput | dict[str, Any]) -> dict[str, Any]:
        return self._product_change(repository_id, request, preview=False)

    @staticmethod
    def _model_profile_public(record: dict[str, Any], secrets: dict[str, str] | None = None) -> dict[str, Any]:
        from firefly_forge.configuration import FIELDS, SECRETS, redact_secrets

        fields = {"id", "name", "description", "provider", "revision", "tasks", "image_input", "generation_verified"}
        result = {key: value for key, value in record.items() if key in fields}
        public_config = {
            field + "_configured" if field in SECRETS else field for field in FIELDS.get(record.get("provider", ""), {})
        } | {"endpoint_invalid"}
        result["config"] = {key: value for key, value in record.get("config", {}).items() if key in public_config}

        def redact(value: Any) -> Any:
            if isinstance(value, str):
                return redact_secrets(value, secrets or {})
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        return dict(redact(result))

    def model_profiles(self) -> list[dict[str, Any]]:
        return [self._model_profile_public(dict(record)) for record in self.request("GET", "model-profiles")]

    def save_model_profile(self, name: str, request: ModelProfileInput | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.configuration import SECRETS
        from firefly_forge.model_profiles import ModelProfileInput, profile_id

        identifier = profile_id(name)
        try:
            model = ModelProfileInput.model_validate(request)
        except ValueError:
            raise ValueError("Invalid model profile configuration") from None
        try:
            result = self.request("PUT", f"model-profiles/{identifier}", json=model.model_dump())
        except RuntimeError:
            raise RuntimeError("Model profile configuration request failed; inspect server readiness") from None
        secrets = {"PROFILE_" + key.upper(): value for key, value in model.values.items() if key in SECRETS}
        return self._model_profile_public(dict(result), secrets)

    def test_model_profile(self, name: str) -> dict[str, Any]:
        from firefly_forge.model_profiles import profile_id

        return dict(self.request("POST", f"model-profiles/{profile_id(name)}/test", timeout=90))

    def task_routing(self) -> dict[str, Any]:
        return dict(self.request("GET", "task-routing"))

    def save_task_routing(self, request: TaskRoutingInput | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.task_routing import TaskRoutingInput

        model = TaskRoutingInput.model_validate(request)
        return dict(self.request("PUT", "task-routing", json=model.model_dump()))

    def repository_workflow(self, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"repositories/{repository_id}/collaboration"))

    def repository_receipts(self, repository_id: str | None = None) -> list[dict[str, Any]]:
        return list(
            self.request(
                "GET", "collaboration/receipts", params={"repository_id": repository_id} if repository_id else {}
            )
        )

    def cloud_providers(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "cloud/providers"))

    def render_cloud_recipe(self, recipe: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.cloud import CloudRecipeInput

        model = CloudRecipeInput.model_validate(recipe)
        return dict(self.request("POST", "cloud/recipes/render", json=model.model_dump()))

    def applications(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "applications"))

    def application(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("GET", f"applications/{identifier}"))

    def check_application(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("POST", f"applications/{identifier}/check"))

    def incidents(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "incidents"))

    def maintain_incident(
        self, identifier: str, brief: str, checks: list[list[str]], agent: str = "codex"
    ) -> dict[str, Any]:
        from firefly_forge.application_operations import MaintenanceRequest

        model = MaintenanceRequest.model_validate(
            {"brief": brief, "agent": agent, "commands": [{"argv": argv} for argv in checks]}
        )
        return dict(self.request("POST", f"incidents/{identifier}/maintenance", json=model.model_dump()))

    def resolve_incident(self, identifier: str, resolution: str) -> dict[str, Any]:
        from firefly_forge.application_operations import IncidentResolution

        model = IncidentResolution(resolution=resolution)
        return dict(self.request("POST", f"incidents/{identifier}/resolve", json=model.model_dump()))

    @staticmethod
    def _id(value: str) -> str:
        try:
            return UUID(value).hex
        except ValueError as exc:
            raise ValueError("Use a valid factory identifier") from exc

    def projects(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "projects"))

    def configure_project(
        self, definition: ProjectDefinition | dict[str, Any], project_id: str | None = None
    ) -> dict[str, Any]:
        from firefly_forge.project_models import ProjectDefinition

        model = ProjectDefinition.model_validate(definition)
        path = "projects" + ("/" + self._id(project_id) if project_id else "")
        return dict(self.request("PUT" if project_id else "POST", path, json=model.model_dump(mode="json")))

    def project(self, project_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"projects/{self._id(project_id)}"))

    def project_revision(self, project_id: str, revision: int) -> dict[str, Any]:
        if isinstance(revision, bool) or revision < 1:
            raise ValueError("Project revisions are positive integers")
        return dict(self.request("GET", f"projects/{self._id(project_id)}/revisions/{revision}"))

    def project_analyses(self, project_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"projects/{self._id(project_id)}/analyses"))

    def analyze_project(self, project_id: str, component_ids: list[str] | None = None) -> dict[str, Any]:
        from firefly_forge.reverse_engineering import AnalysisInput

        model = AnalysisInput(component_ids=component_ids or [])
        return dict(
            self.request("POST", f"projects/{self._id(project_id)}/analyses", json=model.model_dump(), timeout=360)
        )

    def project_analysis(self, project_id: str, analysis_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"projects/{self._id(project_id)}/analyses/{self._id(analysis_id)}"))

    def review_project_analysis(
        self, project_id: str, analysis_id: str, review: AnalysisReview | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.reverse_engineering import AnalysisReview

        model = AnalysisReview.model_validate(review)
        return dict(
            self.request(
                "POST",
                f"projects/{self._id(project_id)}/analyses/{self._id(analysis_id)}/review",
                json=model.model_dump(),
            )
        )

    def assistant_conversations(self, repository_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"repositories/{self._id(repository_id)}/assistant/conversations"))

    def create_assistant_conversation(
        self,
        repository_id: str,
        *,
        agent: str | None = None,
        project_id: str | None = None,
        task: Literal["product", "functions", "design", "architecture"] = "product",
        model_profile_id: str | None = None,
        analysis_id: str | None = None,
        analysis_digest: str | None = None,
        analysis_component_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        from firefly_forge.product_assistant import AssistantSessionInput

        values = {
            "task": task,
            **{
                key: value
                for key, value in {
                    "agent": agent,
                    "project_id": project_id,
                    "model_profile_id": model_profile_id,
                    "analysis_id": analysis_id,
                    "analysis_digest": analysis_digest,
                    "analysis_component_ids": analysis_component_ids,
                }.items()
                if value is not None
            },
        }
        model = AssistantSessionInput.model_validate(values)
        return dict(
            self.request(
                "POST",
                f"repositories/{self._id(repository_id)}/assistant/conversations",
                json=model.model_dump(exclude_unset=True),
            )
        )

    def assistant_conversation(self, repository_id: str, conversation_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET", f"repositories/{self._id(repository_id)}/assistant/conversations/" + self._id(conversation_id)
            )
        )

    def assistant_turn(
        self, repository_id: str, conversation_id: str, request: AssistantTurnInput | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.product_assistant import AssistantTurnInput

        model = AssistantTurnInput.model_validate(request)
        return dict(
            self.request(
                "POST",
                f"repositories/{self._id(repository_id)}/assistant/conversations/"
                + self._id(conversation_id)
                + "/turns",
                json=model.model_dump(),
                timeout=360,
            )
        )

    def assistant_capabilities(self, repository_id: str, conversation_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET",
                f"repositories/{self._id(repository_id)}/assistant/conversations/"
                + self._id(conversation_id)
                + "/capabilities",
            )
        )

    def recover_assistant_turn(
        self,
        repository_id: str,
        conversation_id: str,
        turn_id: str,
        request: AssistantRecoveryInput | dict[str, Any],
    ) -> dict[str, Any]:
        from firefly_forge.product_assistant import AssistantRecoveryInput

        model = AssistantRecoveryInput.model_validate(request)
        return dict(
            self.request(
                "POST",
                f"repositories/{self._id(repository_id)}/assistant/conversations/"
                + self._id(conversation_id)
                + f"/turns/{self._id(turn_id)}/recover",
                json=model.model_dump(),
            )
        )

    @staticmethod
    def _attachment_metadata(value: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "id",
            "request_id",
            "filename",
            "mime_type",
            "kind",
            "content_bytes",
            "sha256",
            "status",
            "text_chars",
            "truncated",
            "created_at",
            "detail",
        }
        return {key: item for key, item in value.items() if key in fields}

    def _attachment_path(self, repository_id: str, conversation_id: str) -> str:
        return f"repositories/{self._id(repository_id)}/assistant/conversations/{self._id(conversation_id)}/attachments"

    def assistant_attachments(self, repository_id: str, conversation_id: str) -> list[dict[str, Any]]:
        return [
            self._attachment_metadata(dict(item))
            for item in self.request("GET", self._attachment_path(repository_id, conversation_id))
        ]

    def assistant_attachment(self, repository_id: str, conversation_id: str, attachment_id: str) -> dict[str, Any]:
        return self._attachment_metadata(
            dict(
                self.request(
                    "GET", self._attachment_path(repository_id, conversation_id) + "/" + self._id(attachment_id)
                )
            )
        )

    def upload_assistant_attachment(
        self, repository_id: str, conversation_id: str, request: AttachmentInput | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.assistant_attachments import AttachmentInput

        try:
            model = AttachmentInput.model_validate(request)
            content = base64.b64decode(model.content_base64, validate=True)
            if not content or len(content) > 1_000_000:
                raise ValueError("Invalid attachment size")
            result = self.request(
                "POST", self._attachment_path(repository_id, conversation_id), json=model.model_dump()
            )
        except (ValueError, RuntimeError, httpx.HTTPError):
            raise ValueError(
                "Attachment upload failed. Check its name, format and 1 MB limit; inspect the conversation "
                "before retrying the same request ID."
            ) from None
        return self._attachment_metadata(dict(result))

    def agent_roles(self) -> dict[str, Any]:
        return dict(self.request("GET", "agent-roles"))

    def agent_role_lifecycle(self) -> dict[str, Any]:
        return dict(self.request("GET", "agent-roles/lifecycle"))

    def repository_role_lifecycle(self, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"repositories/{self._id(repository_id)}/agent-roles/lifecycle"))

    def identity_users(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "identity/users"))

    def project_eligible_users(self, project_id: str, query: str) -> list[dict[str, Any]]:
        query = query.strip()
        if not 3 <= len(query) <= 120:
            raise ValueError("Enter between 3 and 120 characters to find a registered user")
        return list(self.request("GET", f"projects/{self._id(project_id)}/eligible-users", params={"query": query}))

    def identity_administration(self) -> dict[str, Any]:
        return dict(self.request("GET", "identity/administration"))

    def identity_operations(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "identity/operations"))

    def save_agent_roles(self, document: RoleDocument | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.agent_roles import RoleDocument

        model = RoleDocument.model_validate(document)
        return dict(self.request("PUT", "agent-roles", json=model.model_dump(mode="json")))

    def repository_roles(self, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"repositories/{self._id(repository_id)}/agent-roles"))

    def save_repository_roles(self, repository_id: str, roles: ProjectRoles | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.agent_roles import ProjectRoles

        model = ProjectRoles.model_validate(roles)
        return dict(self.request("PUT", f"repositories/{self._id(repository_id)}/agent-roles", json=model.model_dump()))

    def worker_messages(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"runs/{self._id(run_id)}/worker-messages"))

    def respond_worker(self, run_id: str, request_id: str, response: WorkerResponse | dict[str, Any]) -> dict[str, Any]:
        import re

        from firefly_forge.agent_roles import WorkerResponse

        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", request_id):
            raise ValueError("Use the worker request's exact identifier")
        model = WorkerResponse.model_validate(response)
        return dict(
            self.request(
                "POST", f"runs/{self._id(run_id)}/worker-requests/{request_id}/respond", json=model.model_dump()
            )
        )

    def export(self, identifier: str) -> bytes:
        response = self.http.get(f"runs/{identifier}/artifacts/changes.zip")
        response.raise_for_status()
        return response.content

    def project_campaigns(self, project_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"projects/{self._id(project_id)}/campaigns"))

    def launch_campaign(self, project_id: str, request: CampaignLaunch | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.project_campaigns import CampaignLaunch

        model = CampaignLaunch.model_validate(request)
        return dict(
            self.request("POST", f"projects/{self._id(project_id)}/campaigns", json=model.model_dump(), timeout=360)
        )

    def project_campaign(self, project_id: str, campaign_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}"))

    def _campaign_action(self, project_id: str, campaign_id: str, operation: str, request: Any) -> dict[str, Any]:
        from firefly_forge.project_campaigns import CampaignAction, CampaignRetry

        model = (CampaignRetry if operation == "retry" else CampaignAction).model_validate(request)
        return dict(
            self.request(
                "POST",
                f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}/{operation}",
                json=model.model_dump(),
            )
        )

    def cancel_campaign(
        self, project_id: str, campaign_id: str, request: CampaignAction | dict[str, Any]
    ) -> dict[str, Any]:
        return self._campaign_action(project_id, campaign_id, "cancel", request)

    def resume_campaign(
        self, project_id: str, campaign_id: str, request: CampaignAction | dict[str, Any]
    ) -> dict[str, Any]:
        return self._campaign_action(project_id, campaign_id, "resume", request)

    def retry_campaign(
        self, project_id: str, campaign_id: str, request: CampaignRetry | dict[str, Any]
    ) -> dict[str, Any]:
        return self._campaign_action(project_id, campaign_id, "retry", request)

    def repository_runner(self, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"repositories/{self._id(repository_id)}/runner"))

    def save_repository_runner(self, repository_id: str, sandbox: SandboxInput | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.sandbox_models import SandboxInput

        model = SandboxInput.model_validate(sandbox)
        return dict(
            self.request("PUT", f"repositories/{self._id(repository_id)}/runner", json={"sandbox": model.model_dump()})
        )

    def runner_readiness(self, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"repositories/{self._id(repository_id)}/runner/readiness", timeout=90))

    def probe_runner(self, sandbox: SandboxInput | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.sandbox_models import SandboxInput

        model = SandboxInput.model_validate(sandbox)
        return dict(self.request("POST", "runners/probe", json=model.model_dump(), timeout=90))

    @staticmethod
    def _search_metadata(record: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "provider",
            "endpoint",
            "revision",
            "configured",
            "max_results",
            "live_search_verified",
            "include_domains",
        }
        return {key: value for key, value in record.items() if key in fields}

    def search_configuration(self) -> dict[str, Any]:
        return self._search_metadata(dict(self.request("GET", "search/configuration")))

    def external_mcp_catalog(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "tools/mcp"))

    @staticmethod
    def _oauth_name(name: str) -> str:
        if name == "providers" or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise ValueError("Invalid OAuth connection name")
        return name

    def oauth_provider_policies(self) -> dict[str, Any]:
        return dict(self.request("GET", "oauth-provider-policies"))

    def configure_local_oauth_provider_policy(self, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.oauth_provider_policy_models import OAuthProviderPolicyInput

        payload = OAuthProviderPolicyInput.model_validate(request).model_dump()
        return dict(self.request("PUT", "oauth-provider-policies/local-repository-tools", json=payload))

    def connection_oauth_providers(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "connection-oauth/providers"))

    def connection_oauth_connections(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id is not None and not re.fullmatch(r"[a-f0-9]{32}", project_id):
            raise ValueError("Select an exact project identifier")
        return list(self.request("GET", "connection-oauth", params={"project_id": project_id} if project_id else {}))

    def connection_oauth(self, name: str) -> dict[str, Any]:
        return dict(self.request("GET", f"connection-oauth/{self._oauth_name(name)}"))

    def connection_oauth_effect(self, name: str, request_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("Select an exact effect request identifier")
        return dict(self.request("GET", f"connection-oauth/{self._oauth_name(name)}/effects/{request_id}"))

    def supersede_connection_oauth(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.connection_oauth_models import OAuthSupersedeInput

        model = OAuthSupersedeInput.model_validate(request)
        payload = model.model_dump(exclude={"registration": {"client_secret"}})
        if model.registration.client_secret is not None:
            payload["registration"]["client_secret"] = model.registration.client_secret.get_secret_value()
        return dict(self.request("POST", f"connection-oauth/{self._oauth_name(name)}/supersede", json=payload))

    def configure_connection_oauth(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.connection_oauth_models import OAuthConnectionInput

        model = OAuthConnectionInput.model_validate(request)
        payload = model.model_dump(exclude={"client_secret"})
        if model.client_secret is not None:
            payload["client_secret"] = model.client_secret.get_secret_value()
        return dict(self.request("PUT", f"connection-oauth/{self._oauth_name(name)}", json=payload))

    def prepare_connection_oauth(self, name: str) -> dict[str, Any]:
        return dict(self.request("POST", f"connection-oauth/{self._oauth_name(name)}/prepare"))

    def refresh_connection_oauth(self, name: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.connection_oauth_controllers import OAuthRefreshInput

        body = OAuthRefreshInput(request_id=request_id)
        return dict(self.request("POST", f"connection-oauth/{self._oauth_name(name)}/refresh", json=body.model_dump()))

    def disconnect_connection_oauth(self, name: str) -> dict[str, Any]:
        return dict(self.request("POST", f"connection-oauth/{self._oauth_name(name)}/disconnect"))

    def configure_external_mcp(self, connector: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.mcp_registry import MCPConnectionInput, _connector

        _connector(connector)
        model = MCPConnectionInput.model_validate(request)
        return dict(self.request("PUT", f"tools/mcp/{connector}", json=model.model_dump()))

    def link_external_mcp_oauth(self, connector: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.mcp_registry import MCPOAuthConnectionInput, _connector

        _connector(connector)
        model = MCPOAuthConnectionInput.model_validate(request)
        return dict(self.request("PUT", f"tools/mcp/{connector}/oauth", json=model.model_dump()))

    def discover_external_mcp(self, connector: str) -> dict[str, Any]:
        from firefly_forge.mcp_registry import _connector

        _connector(connector)
        return dict(self.request("POST", f"tools/mcp/{connector}/discover"))

    def review_external_mcp(self, connector: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.mcp_registry import MCPReviewInput, _connector

        _connector(connector)
        model = MCPReviewInput.model_validate(request)
        return dict(self.request("POST", f"tools/mcp/{connector}/review", json=model.model_dump()))

    def external_mcp_grants(self, project_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"tools/mcp/projects/{self._id(project_id)}/grants"))

    def grant_external_mcp(self, project_id: str, connector: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.mcp_registry import MCPGrantInput, _connector

        _connector(connector)
        model = MCPGrantInput.model_validate(request)
        return dict(
            self.request(
                "POST",
                f"tools/mcp/projects/{self._id(project_id)}/connectors/{connector}/grants",
                json=model.model_dump(),
            )
        )

    def revoke_external_mcp(self, project_id: str, grant_id: str) -> dict[str, Any]:
        return dict(
            self.request("POST", f"tools/mcp/projects/{self._id(project_id)}/grants/{self._id(grant_id)}/revoke")
        )

    def tool_policy(self) -> dict[str, Any]:
        return dict(self.request("GET", "tools/policy"))

    def learning_policy(self) -> dict[str, Any]:
        return dict(self.request("GET", "learning/policy"))

    def configure_learning_policy(self, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.learning_models import LearningPolicy

        model = LearningPolicy.model_validate(request)
        return dict(self.request("PUT", "learning/policy", json=model.model_dump()))

    def learning_records(self, repository_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"repositories/{self._id(repository_id)}/learning"))

    def invalidate_learning(self, record_id: str, reason: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.learning_controllers import LearningInvalidation

        body = LearningInvalidation(reason=reason, request_id=request_id)
        return dict(self.request("POST", f"learning/records/{self._id(record_id)}/invalidate", json=body.model_dump()))

    def learning_projection(self, record_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"learning/records/{self._id(record_id)}/projection"))

    def submit_learning_projection(self, record_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.learning_controllers import LearningAction

        body = LearningAction(request_id=request_id)
        return dict(
            self.request("POST", f"learning/records/{self._id(record_id)}/projection/submit", json=body.model_dump())
        )

    def observe_learning_projection(
        self, record_id: str, request_id: str, source_id: str | None = None
    ) -> dict[str, Any]:
        from firefly_forge.learning_controllers import LearningObservation

        body = LearningObservation(request_id=request_id, source_id=source_id)
        return dict(
            self.request("POST", f"learning/records/{self._id(record_id)}/projection/observe", json=body.model_dump())
        )

    def configure_tool_policy(self, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.tool_broker import ToolPolicyInput

        model = ToolPolicyInput.model_validate(request)
        return dict(self.request("PUT", "tools/policy", json=model.model_dump()))

    def tool_invocations(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"runs/{self._id(run_id)}/tool-invocations"))

    def abandon_tool_invocation(self, run_id: str, correlation_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.tool_controllers import ToolReconciliationInput

        body = ToolReconciliationInput(request_id=request_id)
        return dict(
            self.request(
                "POST",
                f"runs/{self._id(run_id)}/tool-invocations/{self._id(correlation_id)}/reconcile",
                json=body.model_dump(),
            )
        )

    def configure_search(
        self,
        *,
        api_key: str | None = None,
        clear_secret: bool = False,
        provider: str | None = None,
        include_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        from firefly_forge.runner_configuration import SearchConfiguration

        if api_key is None and not clear_secret and provider is None and include_domains is None:
            raise ValueError("Supply a private key or a search configuration change")
        model = SearchConfiguration.model_validate(
            {"api_key": api_key, "clear_secret": clear_secret, "provider": provider, "include_domains": include_domains}
        )
        result = self.request(
            "PUT",
            "search/configuration",
            json=model.model_dump(exclude_none=True),
        )
        return self._search_metadata(dict(result))

    def test_search(self) -> dict[str, Any]:
        return dict(self.request("POST", "search/test", timeout=30))

    def internet_search(self, query: str, max_results: int = 5) -> dict[str, Any]:
        from firefly_forge.runner_configuration import SearchQuery

        model = SearchQuery(query=query, max_results=max_results)
        return dict(self.request("POST", "search/query", json=model.model_dump(), timeout=30))

    def search_citation(self, citation_id: str) -> dict[str, Any]:
        import re

        if not re.fullmatch(r"web-[a-f0-9]{24}", citation_id):
            raise ValueError("Use a valid search citation identifier")
        return dict(self.request("GET", "search/citations/" + citation_id))

    def browser_screenshot(self, evidence_id: str) -> bytes:
        with self.http.stream("GET", f"runner-evidence/{self._id(evidence_id)}/screenshot") as response:
            if response.is_error:
                raise RuntimeError(f"Factory screenshot request failed (HTTP {response.status_code})")
            if response.headers.get("content-type", "").split(";", 1)[0].lower() != "image/png":
                raise ValueError("Browser evidence must be a PNG image")
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > 2_000_000:
                    raise ValueError("Browser evidence exceeds 2 MB")
                data.extend(chunk)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Browser evidence contains an invalid PNG header")
        return bytes(data)

    def download_browser_screenshot(self, evidence_id: str, destination: Path) -> dict[str, Any]:
        import hashlib
        import os

        data = self.browser_screenshot(evidence_id)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        return {
            "evidence_id": self._id(evidence_id),
            "path": str(destination.absolute()),
            "media_type": "image/png",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
