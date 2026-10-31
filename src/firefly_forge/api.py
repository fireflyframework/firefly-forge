# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native pyfly API, shared sessions and project-scoped Studio host."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, SecretStr
from pyfly.container import bean, configuration, service
from pyfly.core import PyFlyApplication, pyfly_application
from pyfly.data.relational.sqlalchemy import SessionProvider
from pyfly.web import OncePerRequestFilter
from pyfly.web.adapters.starlette import create_app
from pyfly.web.ports.filter import CallNext
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from firefly_forge import __version__
from firefly_forge.access_controllers import AccessController
from firefly_forge.access_persistence import ForgeAccessEventRepository, ForgeMembershipRepository
from firefly_forge.access_routes import AccessRoutes
from firefly_forge.activity_controllers import ActivityContentController
from firefly_forge.activity_persistence import ForgeEventContentRepository
from firefly_forge.activity_service import ActivityArchive
from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.agent_restore_controllers import AgentCheckpointCandidateController, AgentRestoreController
from firefly_forge.agent_restores import AgentRestores
from firefly_forge.agent_role_controllers import AgentRoleController
from firefly_forge.agent_roles import RoleRegistry
from firefly_forge.analysis_context import ReviewedAnalysisContext
from firefly_forge.analysis_controllers import AnalysisController
from firefly_forge.analytics import AnalyticsConnection, ProjectAnalytics
from firefly_forge.analytics_controllers import ProjectAnalyticsController
from firefly_forge.analytics_persistence import ForgeAnalyticsBindingRepository
from firefly_forge.application_controllers import ApplicationController
from firefly_forge.application_operations import ApplicationOperations
from firefly_forge.application_persistence import ForgeIncidentRepository, ForgeObservationRepository
from firefly_forge.assistant_attachment_persistence import ForgeAssistantAttachmentRepository
from firefly_forge.assistant_attachments import AssistantAttachments
from firefly_forge.assistant_controllers import AssistantController
from firefly_forge.assistant_persistence import (
    ForgeAssistantEventRepository,
    ForgeAssistantSessionRepository,
    ForgeAssistantTurnRepository,
)
from firefly_forge.assurance import Assurance
from firefly_forge.bridge_completion_controllers import BridgeCompletionController
from firefly_forge.bridge_completions import BridgeCompletions
from firefly_forge.browser_runner import BrowserRunner
from firefly_forge.campaign_delivery_plan_controllers import CampaignDeliveryPlanController
from firefly_forge.campaign_delivery_plan_persistence import ForgeCampaignDeliveryPlanRepository
from firefly_forge.campaign_delivery_plans import CampaignDeliveryPlans
from firefly_forge.campaign_report_history import CampaignReportHistory
from firefly_forge.campaign_report_history_controllers import CampaignReportHistoryController
from firefly_forge.campaign_report_history_persistence import ForgeCampaignReportArchiveRepository
from firefly_forge.campaign_reports import CampaignReports
from firefly_forge.channel_actions import RepositoryChannels
from firefly_forge.channel_controllers import ChannelController
from firefly_forge.checkpoint_continuation_controllers import CheckpointContinuationController
from firefly_forge.checkpoint_mode_adoptions import CheckpointModeAdoptions
from firefly_forge.checkpoint_mode_controllers import CheckpointModeAdoptionController
from firefly_forge.checkpoint_test_sources import CheckpointTestSources
from firefly_forge.cloud import CloudRecipes
from firefly_forge.cloud_controllers import CloudController
from firefly_forge.coding_check_controllers import CodingCheckGrantController
from firefly_forge.coding_check_grants import CodingCheckGrants
from firefly_forge.collaboration import Collaboration
from firefly_forge.collaboration_controllers import CollaborationController
from firefly_forge.collaboration_persistence import (
    ForgeChannelInboxRepository,
    ForgeCollaborationSettingsRepository,
    ForgeProductDefinitionRepository,
    ForgeWebhookInboxRepository,
)
from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration, redact_secrets
from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.connection_oauth_controllers import ConnectionOAuthController, ConnectionOAuthPrivacy
from firefly_forge.connection_oauth_persistence import OAuthConnectionRepository
from firefly_forge.controllers import (
    CatalogController,
    DeploymentController,
    JournalController,
    KnowledgeController,
    OperatorErrors,
    ProviderService,
    RepositoryController,
    ResumeRequest,
    RunController,
    RunService,
    SCMConfiguration,
    SCMController,
    SCMRepositorySelection,
)
from firefly_forge.engine import Engine
from firefly_forge.engine_activity import EngineActivity, EngineActivitySettings
from firefly_forge.engine_activity_persistence import ForgeEngineActivityRepository
from firefly_forge.execution_request_persistence import (
    ForgeExecutionRequestEventRepository,
    ForgeExecutionRequestRepository,
)
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.factory_dashboard import FactoryDashboard
from firefly_forge.factory_dashboard_controllers import FactoryDashboardController
from firefly_forge.factory_dashboard_persistence import FactoryDashboardRepository
from firefly_forge.final_outcome_controllers import FinalOutcomeContinuationController
from firefly_forge.framework_knowledge import FrameworkKnowledge
from firefly_forge.framework_knowledge_controllers import FrameworkKnowledgeController
from firefly_forge.framework_knowledge_persistence import FrameworkKnowledgeRecords
from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.git_delivery_controllers import GitDeliveryController, GitDeliveryPolicyController
from firefly_forge.git_delivery_policy import GitDeliveryPolicies
from firefly_forge.git_delivery_requests import GitDeliveryRequests
from firefly_forge.git_diff_archive import GitDiffArchive
from firefly_forge.git_diff_controllers import GitDiffController
from firefly_forge.identity import AUTH_PREFIX, SESSION_COOKIE, IdentityConfiguration, IdentityService
from firefly_forge.identity_controllers import IdentityController
from firefly_forge.identity_persistence import (
    ForgeIdentityAttemptRepository,
    ForgeIdentitySessionRepository,
    ForgeIdentityUserRepository,
)
from firefly_forge.invalid_read_continuities import InvalidReadContinuities
from firefly_forge.invalid_read_continuity_controllers import (
    ContinuityLeasePolicyController,
    InvalidReadContinuityController,
)
from firefly_forge.invalid_read_lease import ContinuityLeasePolicies
from firefly_forge.journal import Journal
from firefly_forge.knowledge import (
    KnowledgeConfiguration,
    KnowledgeConnection,
    KnowledgeIndex,
    KnowledgeManager,
    KnowledgeSearch,
    KnowledgeTest,
)
from firefly_forge.knowledge_first import KnowledgeFirst
from firefly_forge.knowledge_persistence import ForgeKnowledgeUploadRepository
from firefly_forge.knowledge_uploads import KnowledgeUploads, KnowledgeUploadsController
from firefly_forge.known_worker_completion_controllers import KnownWorkerCompletionController
from firefly_forge.known_worker_completions import KnownWorkerCompletions
from firefly_forge.learning import LearningLedger
from firefly_forge.learning_controllers import LearningController
from firefly_forge.learning_observation_attempts import LearningObservationAttempts
from firefly_forge.learning_persistence import (
    ForgeLearningEventRepository,
    ForgeLearningProjectionRepository,
    ForgeLearningRepository,
)
from firefly_forge.learning_projection import LearningProjection
from firefly_forge.mcp_controllers import ExternalMCPController
from firefly_forge.mcp_registry import ExternalMCPRegistry
from firefly_forge.model_profile_controllers import ModelProfileController
from firefly_forge.model_profiles import ModelProfiles
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.oauth_provider_policies import OAuthProviderPolicies
from firefly_forge.oauth_provider_policy_controllers import OAuthProviderPolicyController
from firefly_forge.oauth_provider_policy_persistence import OAuthProviderPolicyRepository
from firefly_forge.oauth_resources import OAuthResources
from firefly_forge.operations import DeploymentManager, DeploymentRequest, DeploymentTarget
from firefly_forge.original_selection_controllers import OriginalSelectionReadmissionController
from firefly_forge.original_selection_readmissions import OriginalSelectionReadmissions
from firefly_forge.persistence import (
    ForgeDeploymentRepository,
    ForgeEventRepository,
    ForgeJournalRepository,
    ForgePlanRepository,
    ForgeRepositoryRepository,
    ForgeRunRepository,
    ForgeTaskRepository,
)
from firefly_forge.personal_token_controllers import PersonalTokenController
from firefly_forge.personal_token_models import PersonalTokenInput, PersonalTokenRotationInput
from firefly_forge.personal_token_persistence import ForgePersonalTokenEventRepository, ForgePersonalTokenRepository
from firefly_forge.personal_tokens import PersonalTokenAuthenticationProvider, PersonalTokens
from firefly_forge.policies import PolicyCatalog, PolicySync, ProjectPolicies
from firefly_forge.product_architecture import ProductArchitectureService
from firefly_forge.product_assistant import ProductAssistant
from firefly_forge.product_branding import ProductBranding
from firefly_forge.product_controllers import ProductController
from firefly_forge.product_design import ProductDesign
from firefly_forge.product_design_controllers import ProductDesignController
from firefly_forge.product_lifecycle import ProductLifecycle
from firefly_forge.product_lifecycle_controllers import ProductLifecycleController
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_campaign_controllers import CampaignExecutionPolicyController, ProjectCampaignController
from firefly_forge.project_campaign_persistence import (
    ForgeCampaignChildRepository,
    ForgeCampaignEventRepository,
    ForgeCampaignRepository,
)
from firefly_forge.project_campaigns import ProjectCampaigns
from firefly_forge.project_controllers import ProjectController
from firefly_forge.project_persistence import (
    ForgeAnalysisRepository,
    ForgeProjectRepository,
    ForgeProjectRevisionRepository,
)
from firefly_forge.projects import Projects
from firefly_forge.provider_recoveries import ProviderRecoveries
from firefly_forge.provider_recovery_allowance_controllers import ProviderRecoveryAllowanceController
from firefly_forge.provider_recovery_allowances import ProviderRecoveryAllowances
from firefly_forge.provider_recovery_controllers import ProviderRecoveryController
from firefly_forge.repository_role_controllers import RepositoryRoleController
from firefly_forge.request_limits import RequestLimits
from firefly_forge.reverse_engineering import ReverseEngineering
from firefly_forge.run_creation_controllers import RunCreationController
from firefly_forge.run_creation_persistence import (
    ForgeRunCreationCancellationRepository,
    ForgeRunCreationRequestRepository,
    ForgeRunCreationReservationRepository,
)
from firefly_forge.run_creation_requests import RunCreationRequests
from firefly_forge.run_evidence import RunEvidence
from firefly_forge.run_evidence_controllers import RunEvidenceController
from firefly_forge.runner_backends import RunnerBackends
from firefly_forge.runner_configuration import RunnerConfiguration, RunnerController
from firefly_forge.runner_continuation_controllers import RunnerContinuationController
from firefly_forge.runner_continuations import RunnerContinuations
from firefly_forge.runner_controller_client import RunnerControllerClient
from firefly_forge.runner_factory_authority import RunnerFactoryAuthority
from firefly_forge.runner_factory_models import load_factory_worker_configuration
from firefly_forge.runner_git_snapshot import RunnerGitSnapshot
from firefly_forge.runner_pool_worker import RunnerPoolWorker
from firefly_forge.runtime_role_persistence import ForgeRuntimeTopologyRepository
from firefly_forge.runtime_roles import Role, RuntimeRoleConfig, RuntimeRoles
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.sandbox_persistence import ForgeSandboxRepository
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_actor_authority_controllers import SCMActorAuthorityController
from firefly_forge.scm_connections import SCMConnections
from firefly_forge.scm_maintain_ticket_controllers import MaintainTicketController
from firefly_forge.scm_natural_controllers import SCMNaturalController
from firefly_forge.scm_poll_controllers import SCMPollController
from firefly_forge.scm_poll_functional import SCMPollFunctionalAuthority
from firefly_forge.scm_poll_functional_controllers import SCMPollFunctionalController
from firefly_forge.scm_poll_resume import SCMPollResumeAuthority
from firefly_forge.scm_poll_resume_controllers import SCMPollResumeController
from firefly_forge.scm_polling import SCMPolling
from firefly_forge.scm_product_change_controllers import SCMProductChangeController
from firefly_forge.scm_product_change_service import SCMProductChangeAuthority
from firefly_forge.scm_product_intake import SCMProductIntake
from firefly_forge.scm_product_intake_controllers import SCMProductIntakeController
from firefly_forge.scm_resume_controllers import SCMResumeAuthorityController
from firefly_forge.scm_resume_service import SCMResumeAuthority
from firefly_forge.scm_source_edit import SCMSourceEditAuthority
from firefly_forge.scm_source_edit_controllers import SCMSourceEditController
from firefly_forge.search_tools import SearchTools
from firefly_forge.secrets import redact_environment
from firefly_forge.source_budget_allowances import SourceBudgetPolicies
from firefly_forge.source_budget_controllers import SourceBudgetPolicyController
from firefly_forge.store import Store
from firefly_forge.task_routing import TaskRouting
from firefly_forge.tool_broker import ToolInvocationBroker
from firefly_forge.tool_controllers import ToolController
from firefly_forge.tool_persistence import ForgeToolInvocationRepository
from firefly_forge.unknown_command_acknowledgements import UnknownCommandAcknowledgements
from firefly_forge.unknown_command_controllers import UnknownCommandAcknowledgementController
from firefly_forge.user_administration import UserAdministration
from firefly_forge.user_administration_controllers import UserAdministrationController
from firefly_forge.user_administration_persistence import (
    ForgeAdminSessionRepository,
    ForgeIdentityAdminEventRepository,
    ForgeIdentityAdminOperationRepository,
)
from firefly_forge.verification_controllers import VerificationHandoffController
from firefly_forge.verification_handoffs import VerificationHandoffs
from firefly_forge.verification_models import VerificationAdoption
from firefly_forge.worker_evidence import WorkerEvidence
from firefly_forge.worker_evidence_controllers import WorkerEvidenceController
from firefly_forge.worker_runtime import WorkerSupervision
from firefly_forge.workspace_reconciliation_controllers import (
    SourceEditContinuationController,
    WorkspaceReconciliationController,
)
from firefly_forge.workspace_reconciliations import WorkspaceReconciliations


@service
class OperatorAuth(OncePerRequestFilter):
    __pyfly_order__ = -1_000_000

    def __init__(self, token: str, identity: IdentityService, access: ProjectAccess, store: Store) -> None:
        self.token, self.identity, self.access = token, identity, access
        self.routes = AccessRoutes(access, store)

    def redaction_settings(self) -> dict[str, str]:
        return {"FORGE_API_TOKEN": self.token, **self.identity.redaction_settings()}

    async def do_filter(self, request: Any, call_next: CallNext) -> Response:
        path = request.url.path
        callbacks = {f"/api/v1/scm/connections/{provider}/callback" for provider in ("github", "gitlab", "bitbucket")}
        public_callback = request.method == "GET" and path in callbacks
        signed_webhook = request.method == "POST" and path.startswith("/api/v1/webhooks/")
        public_identity = (request.method, path) in {
            ("GET", AUTH_PREFIX + "/configuration"),
            ("POST", AUTH_PREFIX + "/login"),
            ("GET", AUTH_PREFIX + "/callback"),
            ("POST", AUTH_PREFIX + "/logout"),
        }
        context_token = None
        try:
            if public_identity and request.method == "POST":
                self.identity.check_cookie_origin(request)
            if path.startswith("/api/") and not (public_callback or signed_webhook or public_identity):
                principal = await self.identity.authenticate(request)
                if principal is None:
                    return JSONResponse({"detail": "Sign in to access the factory"}, status_code=401)
                if (
                    request.method not in {"GET", "HEAD", "OPTIONS"}
                    and request.cookies.get(SESSION_COOKIE)
                    and not request.headers.get("Authorization")
                ):
                    self.identity.check_cookie_origin(request)
                context_token = CURRENT_PRINCIPAL.set(principal)
                request.state.principal = principal
                await self.routes.authorize(principal, request)
            response: Response = await call_next(request)
        except AccessDenied as exc:
            response = JSONResponse({"detail": str(exc)}, status_code=403)
        except ValueError:
            response = JSONResponse({"detail": "Authentication or request validation failed"}, status_code=403)
        except KeyError:
            response = JSONResponse({"detail": "Resource not found"}, status_code=404)
        finally:
            if context_token is not None:
                CURRENT_PRINCIPAL.reset(context_token)
        privacy = {item.strip().lower() for item in response.headers.get("Cache-Control", "").split(",")}
        api_cache = "private, no-store" if "private" in privacy else "no-store"
        response.headers["Cache-Control"] = api_cache if path.startswith("/api/") else "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def openapi_document(context: Any = None) -> dict[str, Any]:
    security_schemes = {
        "operator": {"type": "http", "scheme": "bearer"},
        "browserSession": {"type": "apiKey", "in": "cookie", "name": SESSION_COOKIE},
    }
    if context is not None:
        from pyfly.web.adapters.starlette.controller import ControllerRegistrar
        from pyfly.web.openapi import OpenAPIGenerator

        document = OpenAPIGenerator(
            title="firefly forge",
            version=__version__,
            security_schemes=security_schemes,
            security=[{"operator": []}],
        ).generate(ControllerRegistrar().collect_route_metadata(context))
        document["paths"]["/api/v1/scm/connections/{id}/callback"]["get"]["security"] = []
        webhook = document["paths"].get("/api/v1/webhooks/{provider}/{repository_id}")
        if webhook:
            webhook["post"]["security"] = []
        webhook = document["paths"].get("/api/v1/webhooks/{provider}")
        if webhook:
            webhook["post"]["security"] = []
        for path, operations in document["paths"].items():
            if path.startswith("/api/v1/identity/tokens") or path.startswith("/api/v1/factory-dashboard"):
                for method in ("get", "post"):
                    if method in operations:
                        operations[method]["security"] = (
                            [{"browserSession": []}]
                            if path.startswith("/api/v1/identity/tokens")
                            else [{"operator": []}, {"browserSession": []}]
                        )
        return dict(document)
    paths: dict[str, Any] = {}
    specifications = [
        ("/factory-dashboard", "get", None),
        ("/factory-dashboard/projects", "get", None),
        ("/identity/tokens", "get", None),
        ("/identity/tokens", "post", "PersonalTokenInput"),
        ("/identity/tokens/{token_id}/revoke", "post", None),
        ("/identity/tokens/{token_id}/rotate", "post", "PersonalTokenRotationInput"),
        ("/repositories", "get", None),
        ("/repositories", "post", "RepositoryInput"),
        ("/repositories/{id}/policies", "get", None),
        ("/repositories/{id}/policies", "put", "ProjectPolicies"),
        ("/policies", "get", None),
        ("/policies/sync", "post", "PolicySync"),
        ("/runs", "get", None),
        ("/runs", "post", "RunInput"),
        ("/runs/{id}", "get", None),
        ("/runs/{id}/cancel", "post", None),
        ("/runs/{id}/retry", "post", None),
        ("/runs/{id}/publish", "post", None),
        ("/runs/{id}/artifacts/changes.zip", "get", None),
        ("/profiles", "get", None),
        ("/providers", "get", None),
        ("/providers/{id}/configuration", "put", "ProviderConfiguration"),
        ("/providers/{id}/test", "post", None),
        ("/skills", "get", None),
        ("/workflow", "get", None),
        ("/health", "get", None),
        ("/deployment-targets", "get", None),
        ("/deployment-targets/{id}", "put", "DeploymentTarget"),
        ("/deployments", "get", None),
        ("/runs/{id}/deploy", "post", "DeploymentRequest"),
        ("/deployments/{id}/rollback", "post", None),
        ("/scm/connections", "get", None),
        ("/scm/connections/{id}", "put", "SCMConfiguration"),
        ("/scm/connections/{id}/test", "post", None),
        ("/scm/connections/{id}/repositories", "get", None),
        ("/scm/connections/{id}/repositories/connect", "post", "SCMRepositorySelection"),
        ("/scm/connections/{id}/authorize", "post", None),
        ("/scm/connections/{id}/callback", "get", None),
        ("/knowledge/configuration", "get", None),
        ("/knowledge/configuration", "put", "KnowledgeConfiguration"),
        ("/knowledge/status", "get", None),
        ("/knowledge/test", "post", "KnowledgeTest"),
        ("/knowledge/search", "post", "KnowledgeSearch"),
        ("/runs/{id}/knowledge/index", "post", "KnowledgeIndex"),
        ("/runs/{id}/knowledge/remember", "post", None),
        ("/runs/{id}/plan", "get", None),
        ("/runs/{id}/journal", "get", None),
        ("/runs/{id}/resume", "post", "ResumeRequest"),
        ("/runs/{id}/handoff", "post", "ResumeRequest"),
        ("/runs/{id}/verification-handoffs/candidates", "get", None),
        ("/runs/{id}/verification-handoffs/adoptions", "post", "VerificationAdoption"),
        ("/runs/{id}/verification-handoffs/adoptions/{request_id}", "get", None),
    ]
    schemas: dict[str, Any] = {}
    models: list[type[BaseModel]] = [
        PersonalTokenInput,
        PersonalTokenRotationInput,
        RepositoryInput,
        RunInput,
        ProviderConfiguration,
        PolicySync,
        ProjectPolicies,
        DeploymentTarget,
        DeploymentRequest,
        SCMConfiguration,
        SCMRepositorySelection,
        KnowledgeConfiguration,
        KnowledgeSearch,
        KnowledgeIndex,
        KnowledgeTest,
        ResumeRequest,
        VerificationAdoption,
    ]
    for model in models:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema
    for path, method, model_name in specifications:
        operation: dict[str, Any] = {
            "summary": f"{method.upper()} {path}",
            "responses": {
                "200": {"description": "Successful operation"},
                "401": {"description": "Operator token required"},
                "422": {"description": "Invalid request"},
            },
        }
        if path.startswith("/identity/tokens"):
            operation["security"] = [{"browserSession": []}]
            if method == "post" and not path.endswith("/revoke"):
                operation["responses"]["201"] = operation["responses"].pop("200")
        if "{token_id}" in path:
            operation.setdefault("parameters", []).append(
                {
                    "name": "token_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
                }
            )
        if path.startswith("/factory-dashboard"):
            operation["security"] = [{"operator": []}, {"browserSession": []}]
            fields = [
                ("cursor", {"type": "string"}),
                ("limit", {"type": "integer", "minimum": 1, "maximum": 200, "default": 40}),
            ]
            if path.endswith("/projects"):
                fields.append(("search", {"type": "string", "maxLength": 200}))
            else:
                fields.extend(
                    [
                        (
                            "kind",
                            {
                                "type": "string",
                                "enum": ["factory", "project", "standalone", "run"],
                                "default": "factory",
                            },
                        ),
                        ("project_id", {"type": "string", "pattern": "^[a-f0-9]{32}$"}),
                        ("run_id", {"type": "string", "pattern": "^[a-f0-9]{32}$"}),
                        (
                            "bucket",
                            {
                                "type": "string",
                                "enum": [
                                    "active",
                                    "queued",
                                    "needs_action",
                                    "completed",
                                    "failed",
                                    "cancelled",
                                    "expired",
                                    "quarantined",
                                    "unknown",
                                ],
                            },
                        ),
                    ]
                )
            operation["parameters"] = [
                {"name": name, "in": "query", "required": False, "schema": schema} for name, schema in fields
            ]
        if "{id}" in path:
            operation["parameters"] = [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]
        if "{request_id}" in path:
            operation.setdefault("parameters", []).append(
                {
                    "name": "request_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
                }
            )
        if path == "/runs/{id}/verification-handoffs/candidates":
            operation["parameters"].append(
                {
                    "name": "agent",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "enum": ["codex", "claude", "bridge", "agentic"]},
                }
            )
        if model_name:
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{model_name}"}}},
            }
        paths.setdefault("/api/v1" + path, {})[method] = operation
    return {
        "openapi": "3.1.0",
        "info": {"title": "firefly forge", "version": __version__},
        "paths": paths,
        "components": {"schemas": schemas, "securitySchemes": security_schemes},
        "security": [{"operator": []}],
    }


@configuration
class FactoryConfiguration:
    def __init__(self, data_dir: Path, token: str, identity: IdentityConfiguration, role: Role = "combined") -> None:
        self.data_dir, self.token, self.identity_configuration = data_dir, token, identity
        self.role = role

    @bean
    def runtime_roles(self, store: Store, records: ForgeRuntimeTopologyRepository) -> RuntimeRoles:
        runtime = RuntimeRoles(store, self.role, records)
        store.runtime_roles = runtime
        return runtime

    @bean
    def execution_requests(
        self,
        store: Store,
        roles: RuntimeRoles,
        engine: Engine,
        records: ForgeExecutionRequestRepository,
        events: ForgeExecutionRequestEventRepository,
    ) -> ExecutionRequests:
        requests = ExecutionRequests(store, roles, records, events)

        async def publish(run_id: str, actor: dict[str, Any] | None) -> str:
            return await engine.publish(run_id, actor=actor)

        requests.publish_effect = publish
        requests.cancel_effect = engine.cancel_effect
        store.execution_requests = requests
        return requests

    @bean
    def agent_restores(self, store: Store, requests: ExecutionRequests, engine: Engine) -> AgentRestores:
        restores = AgentRestores(store, requests, engine)
        requests.agent_restores = restores
        return restores

    @bean
    def invalid_read_continuities(
        self, store: Store, journal: Journal, requests: ExecutionRequests, engine: Engine
    ) -> InvalidReadContinuities:
        service = InvalidReadContinuities(store, journal, engine, requests)
        requests.invalid_read_continuities = service
        return service

    @bean
    def continuity_lease_policies(self, store: Store) -> ContinuityLeasePolicies:
        return ContinuityLeasePolicies(store)

    @bean
    def git_delivery_policies(self, store: Store) -> GitDeliveryPolicies:
        return GitDeliveryPolicies(store)

    @bean
    def git_deliveries(self, store: Store, engine: Engine, journal: Journal) -> GitDeliveries:
        deliveries = GitDeliveries(store, engine, journal)
        engine.git_deliveries = deliveries
        return deliveries

    @bean
    def checkpoint_test_sources(self, deliveries: GitDeliveries) -> CheckpointTestSources:
        sources = CheckpointTestSources(deliveries)
        deliveries.store.checkpoint_sources = sources
        return sources

    @bean
    def git_delivery_requests(self, deliveries: GitDeliveries, requests: ExecutionRequests) -> GitDeliveryRequests:
        tickets = GitDeliveryRequests(deliveries, requests)
        requests.git_deliveries = tickets
        return tickets

    @bean
    def runner_continuations(
        self, store: Store, roles: RuntimeRoles, requests: ExecutionRequests, sandbox: SandboxExecutor
    ) -> RunnerContinuations:
        continuations = RunnerContinuations(store, roles, sandbox.pool_worker)
        requests.runner_continuations = continuations
        return continuations

    @bean
    def worker_supervision(
        self,
        engine: Engine,
        requests: ExecutionRequests,
        deployments: DeploymentManager,
        campaigns: ProjectCampaigns,
        collaboration: Collaboration,
        polling: SCMPolling,
        observability: ApplicationOperations,
        frameworks: FrameworkKnowledge,
    ) -> WorkerSupervision:
        return WorkerSupervision(
            engine,
            {
                "execution": engine,
                "delivery_requests": requests,
                "deployments": deployments,
                "campaigns": campaigns,
                "scm_feedback": collaboration,
                "scm_ingress": polling,
                "observability": observability,
                "framework_knowledge": frameworks,
            },
        )

    @bean
    def operator_filter(self, identity: IdentityService, access: ProjectAccess, store: Store) -> OperatorAuth:
        return OperatorAuth(self.token, identity, access, store)

    @bean
    def identity_service(
        self,
        store: Store,
        users: ForgeIdentityUserRepository,
        sessions: ForgeIdentitySessionRepository,
        attempts: ForgeIdentityAttemptRepository,
        tokens: PersonalTokens,
    ) -> IdentityService:
        return IdentityService(
            store,
            self.identity_configuration,
            users,
            sessions,
            attempts,
            personal_token_provider=PersonalTokenAuthenticationProvider(tokens),
        )

    @bean
    def personal_tokens(
        self,
        store: Store,
        records: ForgePersonalTokenRepository,
        events: ForgePersonalTokenEventRepository,
        users: ForgeIdentityUserRepository,
        memberships: ForgeMembershipRepository,
    ) -> PersonalTokens:
        tokens = PersonalTokens(store, records, events, users, memberships)
        store.personal_tokens = tokens
        return tokens

    @bean
    def project_access(self, store: Store, records: ForgeMembershipRepository, tokens: PersonalTokens) -> ProjectAccess:
        return ProjectAccess(store, records, personal_tokens=tokens)

    @bean
    def factory_dashboard(
        self, store: Store, access: ProjectAccess, records: FactoryDashboardRepository
    ) -> FactoryDashboard:
        return FactoryDashboard(store, access, records)

    @bean
    def oauth_provider_policies(self, store: Store, records: OAuthProviderPolicyRepository) -> OAuthProviderPolicies:
        available = (
            os.getenv("FORGE_OAUTH_LOCAL_DEVELOPMENT") == "1"
            and self.identity_configuration.allow_loopback_http
            and urlsplit(self.identity_configuration.browser_origin).hostname in {"127.0.0.1", "::1"}
        )
        return OAuthProviderPolicies(
            store,
            records,
            local_development_available=available,
            browser_origin=self.identity_configuration.browser_origin,
        )

    @bean
    def connection_oauth(
        self,
        store: Store,
        identity: IdentityService,
        records: OAuthConnectionRepository,
        policies: OAuthProviderPolicies,
    ) -> ConnectionOAuth:
        return ConnectionOAuth(
            store,
            encryption_key=identity.encryption_key,
            browser_origin=identity.configuration()["browser_origin"] or "",
            records=records,
            allow_loopback_http=identity.config.allow_loopback_http,
            policy_lookup=policies.lookup,
            policy_directory=policies.directory,
        )

    @bean
    def ledger(
        self,
        session_provider: SessionProvider,
        async_engine: AsyncEngine,
        repository_records: ForgeRepositoryRepository,
        run_records: ForgeRunRepository,
        event_records: ForgeEventRepository,
        event_content_records: ForgeEventContentRepository,
        deployment_records: ForgeDeploymentRepository,
        product_records: ForgeProductDefinitionRepository,
        campaign_records: ForgeCampaignRepository,
        campaign_children: ForgeCampaignChildRepository,
    ) -> Store:
        return Store(
            self.data_dir / "factory.db",
            session_provider,
            engine=async_engine,
            repository_records=repository_records,
            run_records=run_records,
            event_records=event_records,
            event_content_records=event_content_records,
            deployment_records=deployment_records,
            product_records=product_records,
            campaign_records=campaign_records,
            campaign_children=campaign_children,
        )

    @bean
    def model_profiles(self, store: Store) -> ModelProfiles:
        return ModelProfiles(store.configuration, credential_registry=lambda: store.credential_registry())

    @bean
    def task_routing(self, profiles: ModelProfiles) -> TaskRouting:
        return TaskRouting(profiles)

    @bean
    def user_administration(
        self,
        store: Store,
        identity: IdentityService,
        operations: ForgeIdentityAdminOperationRepository,
        events: ForgeIdentityAdminEventRepository,
    ) -> UserAdministration:
        return UserAdministration(store, identity, operations, events)

    @bean
    def engine_activity(self, store: Store, records: ForgeEngineActivityRepository) -> EngineActivity:
        return EngineActivity(
            store,
            records,
            EngineActivitySettings(
                heartbeat_seconds=float(os.environ.get("FORGE_ACTIVITY_HEARTBEAT_SECONDS", "5")),
                stale_seconds=float(os.environ.get("FORGE_ACTIVITY_STALE_SECONDS", "20")),
                sample_timeout_seconds=float(os.environ.get("FORGE_ACTIVITY_SAMPLE_TIMEOUT_SECONDS", "1")),
            ),
        )

    @bean
    def coordinator(
        self,
        store: Store,
        connections: SCMConnections,
        journal: Journal,
        assurance: Assurance,
        sandbox: SandboxExecutor,
        browser: BrowserRunner,
        search: SearchTools,
        tool_broker: ToolInvocationBroker,
        external_mcp: ExternalMCPRegistry,
        learning: LearningLedger,
        knowledge_first: KnowledgeFirst,
        runners: RunnerConfiguration,
        routing: TaskRouting,
        runtime_roles: RuntimeRoles,
        worker_evidence: WorkerEvidence,
        stage_activity: EngineActivity,
    ) -> Engine:
        engine = Engine(
            store,
            self.data_dir / "workspaces",
            concurrency=int(os.environ.get("FORGE_CONCURRENCY", "2")),
            connections=connections,
            journal=journal,
            stage_activity=stage_activity,
        )
        engine.assurance = assurance
        engine.worker_evidence = worker_evidence
        engine.sandbox = sandbox
        engine.browser, engine.search = browser, search
        engine.tool_broker = tool_broker
        engine.external_mcp = tool_broker.external_mcp = external_mcp
        engine.learning, engine.knowledge_first = learning, knowledge_first
        learning.engine = engine
        external_mcp.credential_registry = lambda: engine.credential_registry()
        store.runners, store.task_router = runners, routing
        search.credential_registry = lambda: engine.credential_registry()
        search.credential_admission = store.admit_credentials
        tool_broker.credential_registry = lambda: engine.credential_registry()
        journal.credential_registry = lambda: engine.credential_registry()
        store.credential_registry = lambda: engine.credential_registry()
        return engine

    @bean
    def verification_handoffs(self, store: Store, journal: Journal, engine: Engine) -> VerificationHandoffs:
        handoffs = VerificationHandoffs(store, journal, engine)
        engine.verification_handoffs = handoffs
        return handoffs

    @bean
    def provider_recoveries(self, store: Store, journal: Journal, engine: Engine) -> ProviderRecoveries:
        recoveries = ProviderRecoveries(store, journal, engine)
        engine.provider_recoveries = recoveries
        return recoveries

    @bean
    def provider_recovery_allowances(self, recoveries: ProviderRecoveries) -> ProviderRecoveryAllowances:
        return ProviderRecoveryAllowances(recoveries)

    @bean
    def workspace_reconciliations(self, recoveries: ProviderRecoveries) -> WorkspaceReconciliations:
        return WorkspaceReconciliations(recoveries)

    @bean
    def coding_check_grants(self, recoveries: ProviderRecoveries) -> CodingCheckGrants:
        grants = CodingCheckGrants(recoveries)
        recoveries.engine.coding_checks = grants
        return grants

    @bean
    def assurance(self, store: Store, journal: Journal) -> Assurance:
        return Assurance(store, self.data_dir / "reviews", journal)

    @bean
    def product_projects(
        self, store: Store, records: ForgeProjectRepository, revisions: ForgeProjectRevisionRepository
    ) -> Projects:
        return Projects(store, records, revisions)

    @bean
    def reverse_engineering(
        self, store: Store, projects: Projects, connections: SCMConnections, records: ForgeAnalysisRepository
    ) -> ReverseEngineering:
        return ReverseEngineering(store, projects, self.data_dir / "analyses", connections, records=records)

    @bean
    def reviewed_analysis_context(
        self, analysis: ReverseEngineering, projects: Projects, uploads: KnowledgeUploads
    ) -> ReviewedAnalysisContext:
        return ReviewedAnalysisContext(analysis, projects, uploads)

    @bean
    def assistant_attachments(
        self, store: Store, sessions: ForgeAssistantSessionRepository, records: ForgeAssistantAttachmentRepository
    ) -> AssistantAttachments:
        return AssistantAttachments(store, sessions, records)

    @bean
    def product_assistant(
        self,
        store: Store,
        knowledge: KnowledgeManager,
        engine: Engine,
        analysis_context: ReviewedAnalysisContext,
        attachments: AssistantAttachments,
        projects: Projects,
        records: ForgeAssistantSessionRepository,
        turns: ForgeAssistantTurnRepository,
        events: ForgeAssistantEventRepository,
    ) -> ProductAssistant:
        return ProductAssistant(
            store,
            self.data_dir / "assistant",
            knowledge,
            analysis_context=analysis_context,
            credential_registry=lambda: engine.credential_registry(),
            attachments=attachments,
            projects=projects,
            records=records,
            turns=turns,
            events=events,
        )

    @bean
    def role_registry(self, store: Store) -> RoleRegistry:
        store.roles.credential_registry = lambda: store.credential_registry()
        return store.roles

    @bean
    def scm_connections(self, store: Store) -> SCMConnections:
        connections = SCMConnections(store)
        store.connections = connections
        return connections

    @bean
    def scm_polling(
        self,
        store: Store,
        scm: SCMConnections,
        settings_records: ForgeCollaborationSettingsRepository,
        inbox: ForgeWebhookInboxRepository,
        events: ForgeAccessEventRepository,
    ) -> SCMPolling:
        polling = SCMPolling(store, scm, settings_records, inbox, events)
        store.scm_polling = polling
        return polling

    @bean
    def scm_poll_functional_authority(self, store: Store, polling: SCMPolling) -> SCMPollFunctionalAuthority:
        authority = SCMPollFunctionalAuthority(store, polling)
        store.scm_poll_functional = authority
        return authority

    @bean
    def scm_source_edit_authority(
        self, store: Store, polling: SCMPolling, workspaces: WorkspaceReconciliations
    ) -> SCMSourceEditAuthority:
        return SCMSourceEditAuthority(store, polling, workspaces)

    @bean
    def scm_actor_authority(self, store: Store, connections: SCMConnections) -> SCMActorAuthority:
        return SCMActorAuthority(store, connections)

    @bean
    def scm_resume_authority(
        self,
        store: Store,
        authority: SCMActorAuthority,
        journal: Journal,
        settings_records: ForgeCollaborationSettingsRepository,
        inbox: ForgeWebhookInboxRepository,
        events: ForgeAccessEventRepository,
    ) -> SCMResumeAuthority:
        service = SCMResumeAuthority(store, authority, journal, settings_records, inbox, events)
        store.scm_resume_authorities[service.actor_transport] = service
        return service

    @bean
    def scm_poll_resume_authority(
        self,
        store: Store,
        authority: SCMActorAuthority,
        journal: Journal,
        settings_records: ForgeCollaborationSettingsRepository,
        inbox: ForgeWebhookInboxRepository,
        events: ForgeAccessEventRepository,
        polling: SCMPolling,
    ) -> SCMPollResumeAuthority:
        service = SCMPollResumeAuthority(store, authority, journal, settings_records, inbox, events)
        service.polling = polling
        store.scm_resume_authorities[service.actor_transport] = service
        return service

    @bean
    def scm_product_change_authority(
        self,
        store: Store,
        authority: SCMActorAuthority,
        settings_records: ForgeCollaborationSettingsRepository,
        inbox: ForgeWebhookInboxRepository,
        events: ForgeAccessEventRepository,
    ) -> SCMProductChangeAuthority:
        return SCMProductChangeAuthority(store, authority, settings_records, inbox, events)

    @bean
    def scm_product_intake(
        self,
        store: Store,
        scm: SCMConnections,
        settings_records: ForgeCollaborationSettingsRepository,
        inbox: ForgeWebhookInboxRepository,
        events: ForgeAccessEventRepository,
    ) -> SCMProductIntake:
        return SCMProductIntake(store, scm, settings_records, inbox, events)

    @bean
    def execution_journal(
        self,
        store: Store,
        plans: ForgePlanRepository,
        tasks: ForgeTaskRepository,
        records: ForgeJournalRepository,
        sandboxes: ForgeSandboxRepository,
    ) -> Journal:
        return Journal(store, plans=plans, tasks=tasks, records=records, sandboxes=sandboxes)

    @bean
    def bridge_completions(self, store: Store, journal: Journal) -> BridgeCompletions:
        return BridgeCompletions(store, journal)

    @bean
    def known_worker_completions(self, store: Store, journal: Journal, engine: Engine) -> KnownWorkerCompletions:
        return KnownWorkerCompletions(store, journal, engine)

    @bean
    def unknown_command_acknowledgements(
        self, store: Store, journal: Journal, handoffs: VerificationHandoffs
    ) -> UnknownCommandAcknowledgements:
        return UnknownCommandAcknowledgements(store, journal, handoffs)

    @bean
    def checkpoint_mode_adoptions(self, store: Store, journal: Journal, engine: Engine) -> CheckpointModeAdoptions:
        return CheckpointModeAdoptions(store, journal, engine)

    @bean
    def original_selection_readmissions(
        self, store: Store, journal: Journal, engine: Engine
    ) -> OriginalSelectionReadmissions:
        return OriginalSelectionReadmissions(store, journal, engine)

    @bean
    def runner_backends(self) -> RunnerBackends:
        return RunnerBackends()

    @bean
    def sandbox_executor(self, store: Store, journal: Journal, backends: RunnerBackends) -> SandboxExecutor:
        executor = SandboxExecutor(store, journal, backends)
        configuration_path = os.environ.get("FORGE_RUNNER_WORKER_CONFIGURATION")
        if configuration_path:
            admin = load_factory_worker_configuration(Path(configuration_path))
            if admin.transport.active:
                workspaces = self.data_dir / "workspaces"
                workspaces.mkdir(parents=True, exist_ok=True)
                directory = self.data_dir / ".forge-pool-artifacts"
                directory.mkdir(mode=0o700, exist_ok=True)
                executor.pool_worker = RunnerPoolWorker(
                    store,
                    journal,
                    RunnerFactoryAuthority(store, journal),
                    RunnerControllerClient(admin.transport),
                    RunnerGitSnapshot(admin.git_binary),
                    workspaces,
                    directory,
                )
        return executor

    @bean
    def search_tools(self) -> SearchTools:
        return SearchTools(self.data_dir / "tools" / "search")

    @bean
    def learning_ledger(
        self,
        store: Store,
        journal: Journal,
        records: ForgeLearningRepository,
        events: ForgeLearningEventRepository,
        projections: ForgeLearningProjectionRepository,
    ) -> LearningLedger:
        return LearningLedger(store, journal, records, events, projections)

    @bean
    def learning_projection(
        self,
        store: Store,
        journal: Journal,
        ledger: LearningLedger,
        knowledge: KnowledgeManager,
        knowledge_first: KnowledgeFirst,
    ) -> LearningProjection:
        projection = LearningProjection(store, journal, ledger, knowledge)
        knowledge_first.projection = projection
        return projection

    @bean
    def knowledge_first(
        self,
        store: Store,
        journal: Journal,
        ledger: LearningLedger,
        broker: ToolInvocationBroker,
        search: SearchTools,
    ) -> KnowledgeFirst:
        return KnowledgeFirst(store, journal, ledger, broker, search)

    @bean
    def oauth_resources(self, connections: ConnectionOAuth, access: ProjectAccess) -> OAuthResources:
        return OAuthResources(connections, access)

    @bean
    def analytics_connection(self, private: ConfigurationStore) -> AnalyticsConnection:
        return AnalyticsConnection(private)

    @bean
    def project_analytics(
        self,
        store: Store,
        access: ProjectAccess,
        connection: AnalyticsConnection,
        records: ForgeAnalyticsBindingRepository,
    ) -> ProjectAnalytics:
        return ProjectAnalytics(store, access, connection, records)

    @bean
    def external_mcp_registry(self, private: ConfigurationStore, resources: OAuthResources) -> ExternalMCPRegistry:
        return ExternalMCPRegistry(private, resources=resources)

    @bean
    def tool_invocation_broker(
        self, store: Store, journal: Journal, records: ForgeToolInvocationRepository
    ) -> ToolInvocationBroker:
        return ToolInvocationBroker(store, journal, self.data_dir, records)

    @bean
    def browser_runner(self, sandbox: SandboxExecutor) -> BrowserRunner:
        return BrowserRunner(sandbox, self.data_dir)

    @bean
    def runner_configuration(
        self,
        store: Store,
        sandbox: SandboxExecutor,
        search: SearchTools,
        browser: BrowserRunner,
    ) -> RunnerConfiguration:
        return RunnerConfiguration(store, sandbox, search, browser)

    @bean
    def project_campaigns(
        self,
        store: Store,
        engine: Engine,
        journal: Journal,
        records: ForgeCampaignRepository,
        children: ForgeCampaignChildRepository,
        events: ForgeCampaignEventRepository,
        projects: Projects,
        project_records: ForgeProjectRepository,
    ) -> ProjectCampaigns:
        return ProjectCampaigns(
            store, engine, journal, records, children, events, projects=projects, project_records=project_records
        )

    @bean
    def run_evidence(self, store: Store) -> RunEvidence:
        return RunEvidence(store)

    @bean
    def activity_archive(
        self, store: Store, records: ForgeEventContentRepository, evidence: RunEvidence
    ) -> ActivityArchive:
        return ActivityArchive(store, records, evidence)

    @bean
    def campaign_reports(self, store: Store, campaigns: ProjectCampaigns, journal: Journal) -> CampaignReports:
        return CampaignReports(store, campaigns, journal)

    @bean
    def campaign_report_history(
        self, store: Store, reports: CampaignReports, records: ForgeCampaignReportArchiveRepository
    ) -> CampaignReportHistory:
        return CampaignReportHistory(store, reports, records)

    @bean
    def campaign_delivery_plans(
        self,
        store: Store,
        reports: CampaignReports,
        history: CampaignReportHistory,
        records: ForgeCampaignDeliveryPlanRepository,
    ) -> CampaignDeliveryPlans:
        return CampaignDeliveryPlans(store, reports, history, records)

    @bean
    def provider_configuration(self, store: Store) -> ConfigurationStore:
        return store.configuration

    @bean
    def policy_catalog(self, store: Store) -> PolicyCatalog:
        return store.policies

    @bean
    def deployment_manager(
        self,
        store: Store,
        engine: Engine,
        journal: Journal,
        search: SearchTools,
        external_mcp: ExternalMCPRegistry,
        auth: OperatorAuth,
        profiles: ModelProfiles,
        users: UserAdministration,
        oauth: ConnectionOAuth,
        analytics: AnalyticsConnection,
    ) -> DeploymentManager:
        manager = DeploymentManager(store, engine, journal)
        manager.secret_registries.extend([search, external_mcp])
        manager.secret_registries.extend([auth, profiles, users, oauth, analytics])
        return manager

    @bean
    def knowledge_connection(self, private: ConfigurationStore) -> KnowledgeConnection:
        return KnowledgeConnection(private)

    @bean
    def knowledge_manager(
        self,
        store: Store,
        engine: Engine,
        journal: Journal,
        connection: KnowledgeConnection,
        knowledge_first: KnowledgeFirst,
    ) -> KnowledgeManager:
        manager = KnowledgeManager(store, engine, connection=connection)
        manager.journal = journal
        engine.knowledge = manager
        knowledge_first.knowledge = manager
        return manager

    @bean
    def framework_knowledge(
        self, store: Store, knowledge: KnowledgeManager, records: FrameworkKnowledgeRecords
    ) -> FrameworkKnowledge:
        manager = FrameworkKnowledge(store, knowledge, records)
        knowledge.frameworks = manager
        store.framework_knowledge = manager
        return manager

    @bean
    def knowledge_uploads(
        self,
        store: Store,
        knowledge: KnowledgeManager,
        records: ForgeKnowledgeUploadRepository,
        deployments: DeploymentManager,
    ) -> KnowledgeUploads:
        return KnowledgeUploads(
            store,
            knowledge,
            records,
            redact=lambda value: redact_environment(redact_secrets(value, deployments._redaction_settings())),
        )

    @bean
    def collaboration_manager(
        self,
        store: Store,
        scm: SCMConnections,
        deployments: DeploymentManager,
        settings_records: ForgeCollaborationSettingsRepository,
        inbox: ForgeWebhookInboxRepository,
        products: ForgeProductDefinitionRepository,
        analysis: ReverseEngineering,
        assistant: ProductAssistant,
        resume_authority: SCMResumeAuthority,
        poll_resume_authority: SCMPollResumeAuthority,
        poll_functional_authority: SCMPollFunctionalAuthority,
        source_edit_authority: SCMSourceEditAuthority,
        product_change_authority: SCMProductChangeAuthority,
        intake: SCMProductIntake,
        polling: SCMPolling,
    ) -> Collaboration:
        collaboration = Collaboration(
            store,
            scm,
            deployments,
            settings_records,
            inbox,
            products,
            resume_authority,
            product_change_authority,
            intake,
            browser_origin=self.identity_configuration.browser_origin,
        )
        collaboration.poll_resume_authority = poll_resume_authority
        collaboration.poll_functional_authority = poll_functional_authority
        collaboration.source_edit_authority = source_edit_authority
        collaboration.polling = polling
        polling.collaboration = collaboration
        collaboration.analysis_manager = analysis
        collaboration.product_assistant = assistant
        deployments.secret_registries.append(collaboration)
        return collaboration

    @bean
    def repository_channels(
        self,
        store: Store,
        collaboration: Collaboration,
        scm: SCMConnections,
        inbox: ForgeChannelInboxRepository,
        deployments: DeploymentManager,
    ) -> RepositoryChannels:
        channels = RepositoryChannels(store, collaboration, scm, inbox)
        deployments.secret_registries.append(channels)
        return channels

    @bean
    def product_lifecycle(self, collaboration: Collaboration) -> ProductLifecycle:
        return ProductLifecycle(collaboration)

    @bean
    def application_operations(
        self,
        store: Store,
        engine: Engine,
        deployments: DeploymentManager,
        observations: ForgeObservationRepository,
        incidents: ForgeIncidentRepository,
    ) -> ApplicationOperations:
        return ApplicationOperations(store, engine, deployments, observations, incidents)

    @bean
    def cloud_recipes(self, store: Store, deployments: DeploymentManager) -> CloudRecipes:
        return CloudRecipes(store, deployments)


def create_runtime(
    data_dir: Path | None = None, token: str | None = None, role: Role | None = None
) -> PyFlyApplication:
    role = RuntimeRoleConfig(role=role).role if role is not None else RuntimeRoleConfig.from_environment().role
    data_dir = data_dir or Path(os.environ.get("FORGE_DATA_DIR", ".forge")).resolve()
    explicit_bootstrap = token is not None and "FORGE_AUTH_MODE" not in os.environ
    token = token if token is not None else os.environ.get("FORGE_API_TOKEN", "")
    if explicit_bootstrap or os.getenv("FORGE_AUTH_MODE") == "local_bootstrap":
        if len(token) < 24:
            raise ValueError("Set a local bootstrap token of at least 24 characters")
        identity = IdentityConfiguration(
            mode="local_bootstrap",
            bootstrap_token=SecretStr(token),
            browser_origin=os.getenv("FORGE_BROWSER_ORIGIN", ""),
            allow_loopback_http=os.getenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP") == "true",
        )
    else:
        identity = IdentityConfiguration.from_environment()

    @pyfly_application(name="firefly-forge", version=__version__)
    class Application:
        pass

    runtime = PyFlyApplication(Application, config_path=Path(__file__).parent / "pyfly.yaml")
    container = runtime.context.container
    container.register_instance(FactoryConfiguration, FactoryConfiguration(data_dir, token, identity, role))
    for component in [
        ForgeAnalyticsBindingRepository,
        ProjectAnalyticsController,
        OAuthProviderPolicyRepository,
        OAuthProviderPolicyController,
        OAuthConnectionRepository,
        ConnectionOAuthController,
        ConnectionOAuthPrivacy,
        ForgeRuntimeTopologyRepository,
        ForgeExecutionRequestRepository,
        ForgeExecutionRequestEventRepository,
        ModelProfileController,
        UserAdministrationController,
        ForgeIdentityAdminOperationRepository,
        ForgeIdentityAdminEventRepository,
        ForgeAdminSessionRepository,
        ForgeIdentityUserRepository,
        ForgeIdentitySessionRepository,
        ForgeIdentityAttemptRepository,
        ForgePersonalTokenRepository,
        ForgePersonalTokenEventRepository,
        PersonalTokenController,
        FactoryDashboardRepository,
        ForgeEngineActivityRepository,
        FactoryDashboardController,
        ForgeMembershipRepository,
        ForgeAccessEventRepository,
        IdentityController,
        AccessController,
        ForgeRepositoryRepository,
        ForgeRunRepository,
        ForgeEventRepository,
        ForgeEventContentRepository,
        ForgeDeploymentRepository,
        ForgePlanRepository,
        ForgeTaskRepository,
        ForgeJournalRepository,
        ProviderService,
        RunService,
        RunCreationRequests,
        ForgeRunCreationRequestRepository,
        ForgeRunCreationReservationRepository,
        ForgeRunCreationCancellationRepository,
        RunCreationController,
        OperatorErrors,
        CatalogController,
        RepositoryController,
        RunController,
        RunnerContinuationController,
        DeploymentController,
        SCMController,
        KnowledgeController,
        LearningController,
        LearningObservationAttempts,
        AgentCheckpoints,
        AgentRestoreController,
        AgentCheckpointCandidateController,
        InvalidReadContinuityController,
        CheckpointContinuationController,
        FinalOutcomeContinuationController,
        ContinuityLeasePolicyController,
        GitDeliveryPolicyController,
        GitDeliveryController,
        KnownWorkerCompletionController,
        UnknownCommandAcknowledgementController,
        CheckpointModeAdoptionController,
        OriginalSelectionReadmissionController,
        GitDiffArchive,
        GitDiffController,
        JournalController,
        VerificationHandoffController,
        ProviderRecoveryController,
        ProviderRecoveryAllowanceController,
        WorkspaceReconciliationController,
        SourceEditContinuationController,
        SourceBudgetPolicies,
        SourceBudgetPolicyController,
        CodingCheckGrantController,
        BridgeCompletionController,
        RunEvidenceController,
        ActivityContentController,
        ForgeCollaborationSettingsRepository,
        ForgeWebhookInboxRepository,
        ForgeProductDefinitionRepository,
        CollaborationController,
        SCMActorAuthorityController,
        SCMResumeAuthorityController,
        SCMPollResumeController,
        SCMPollFunctionalController,
        SCMNaturalController,
        MaintainTicketController,
        SCMSourceEditController,
        SCMPollController,
        SCMProductChangeController,
        SCMProductIntakeController,
        ForgeObservationRepository,
        ForgeIncidentRepository,
        ApplicationController,
        CloudController,
        ProductController,
        ProductBranding,
        ProductArchitectureService,
        ProductDesign,
        ProductDesignController,
        ProductLifecycleController,
        ForgeChannelInboxRepository,
        ChannelController,
        ForgeKnowledgeUploadRepository,
        KnowledgeUploadsController,
        FrameworkKnowledgeController,
        FrameworkKnowledgeRecords,
        ForgeProjectRepository,
        ForgeProjectRevisionRepository,
        ForgeAnalysisRepository,
        ProjectController,
        AnalysisController,
        ForgeAssistantSessionRepository,
        ForgeAssistantAttachmentRepository,
        ForgeAssistantTurnRepository,
        ForgeAssistantEventRepository,
        AssistantController,
        AgentRoleController,
        RepositoryRoleController,
        ForgeSandboxRepository,
        ForgeToolInvocationRepository,
        ForgeLearningRepository,
        ForgeLearningEventRepository,
        ForgeLearningProjectionRepository,
        RunnerController,
        WorkerEvidence,
        WorkerEvidenceController,
        ToolController,
        ExternalMCPController,
        ForgeCampaignRepository,
        ForgeCampaignChildRepository,
        ForgeCampaignEventRepository,
        ForgeCampaignReportArchiveRepository,
        CampaignReportHistoryController,
        ForgeCampaignDeliveryPlanRepository,
        CampaignDeliveryPlanController,
        ProjectCampaignController,
        CampaignExecutionPolicyController,
    ]:
        container.register(component)

    return runtime


def create_application(data_dir: Path | None = None, token: str | None = None, role: Role | None = None) -> Starlette:
    selected = RuntimeRoleConfig(role=role).role if role is not None else RuntimeRoleConfig.from_environment().role
    if selected == "worker":
        raise ValueError("The worker role is headless; start it with forge worker")
    runtime = create_runtime(data_dir, token, selected)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            await runtime.startup()
            store = runtime.context.get_bean(Store)
            engine = runtime.context.get_bean(Engine)
            app.state.store, app.state.engine = store, engine
            yield
        finally:
            await runtime.shutdown()

    app = create_app(
        title="firefly forge",
        version=__version__,
        context=runtime.context,
        docs_enabled=False,
        actuator_enabled=False,
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=os.environ.get("FORGE_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(","),
    )
    app.add_middleware(RequestLimits)
    source_static = Path(__file__).resolve().parents[2] / "studio" / "dist"
    packaged_static = Path(__file__).parent / "static"
    static = packaged_static if packaged_static.is_dir() else source_static
    if static.is_dir():
        app.routes.append(Mount("/", app=StaticFiles(directory=static, html=True), name="studio"))
    app.state.runtime = runtime
    return app
