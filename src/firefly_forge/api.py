# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated PyFly API and Studio host for a single trusted factory operator."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
from firefly_forge.agent_role_controllers import AgentRoleController
from firefly_forge.agent_roles import RoleRegistry
from firefly_forge.analysis_context import ReviewedAnalysisContext
from firefly_forge.analysis_controllers import AnalysisController
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
from firefly_forge.browser_runner import BrowserRunner
from firefly_forge.channel_actions import RepositoryChannels
from firefly_forge.channel_controllers import ChannelController
from firefly_forge.cloud import CloudRecipes
from firefly_forge.cloud_controllers import CloudController
from firefly_forge.collaboration import Collaboration
from firefly_forge.collaboration_controllers import CollaborationController
from firefly_forge.collaboration_persistence import (
    ForgeChannelInboxRepository,
    ForgeCollaborationSettingsRepository,
    ForgeProductDefinitionRepository,
    ForgeWebhookInboxRepository,
)
from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration, redact_secrets
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
from firefly_forge.execution_request_persistence import (
    ForgeExecutionRequestEventRepository,
    ForgeExecutionRequestRepository,
)
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.identity import AUTH_PREFIX, SESSION_COOKIE, IdentityConfiguration, IdentityService
from firefly_forge.identity_controllers import IdentityController
from firefly_forge.identity_persistence import (
    ForgeIdentityAttemptRepository,
    ForgeIdentitySessionRepository,
    ForgeIdentityUserRepository,
)
from firefly_forge.journal import Journal
from firefly_forge.knowledge import (
    KnowledgeConfiguration,
    KnowledgeConnection,
    KnowledgeIndex,
    KnowledgeManager,
    KnowledgeSearch,
    KnowledgeTest,
)
from firefly_forge.knowledge_persistence import ForgeKnowledgeUploadRepository
from firefly_forge.knowledge_uploads import KnowledgeUploads, KnowledgeUploadsController
from firefly_forge.model_profile_controllers import ModelProfileController
from firefly_forge.model_profiles import ModelProfiles
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.operations import DeploymentManager, DeploymentRequest, DeploymentTarget
from firefly_forge.persistence import (
    ForgeDeploymentRepository,
    ForgeEventRepository,
    ForgeJournalRepository,
    ForgePlanRepository,
    ForgeRepositoryRepository,
    ForgeRunRepository,
    ForgeTaskRepository,
)
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
from firefly_forge.project_campaign_controllers import ProjectCampaignController
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
from firefly_forge.repository_role_controllers import RepositoryRoleController
from firefly_forge.request_limits import RequestLimits
from firefly_forge.reverse_engineering import ReverseEngineering
from firefly_forge.runner_configuration import RunnerConfiguration, RunnerController
from firefly_forge.runtime_role_persistence import ForgeRuntimeTopologyRepository
from firefly_forge.runtime_roles import Role, RuntimeRoleConfig, RuntimeRoles
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.sandbox_persistence import ForgeSandboxRepository
from firefly_forge.scm_connections import SCMConnections
from firefly_forge.search_tools import SearchTools
from firefly_forge.secrets import redact_environment
from firefly_forge.store import Store
from firefly_forge.task_routing import TaskRouting
from firefly_forge.user_administration import UserAdministration
from firefly_forge.user_administration_controllers import UserAdministrationController
from firefly_forge.user_administration_persistence import (
    ForgeAdminSessionRepository,
    ForgeIdentityAdminEventRepository,
    ForgeIdentityAdminOperationRepository,
)
from firefly_forge.worker_runtime import WorkerSupervision


@service
class OperatorAuth(OncePerRequestFilter):
    __pyfly_order__ = -1_000_000

    def __init__(self, token: str, identity: IdentityService, access: ProjectAccess) -> None:
        self.token, self.identity, self.access = token, identity, access
        self.routes = AccessRoutes(access, access.store)

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
        response.headers["Cache-Control"] = "no-store" if path.startswith("/api/") else "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def openapi_document(context: Any = None) -> dict[str, Any]:
    if context is not None:
        from pyfly.web.adapters.starlette.controller import ControllerRegistrar
        from pyfly.web.openapi import OpenAPIGenerator

        document = OpenAPIGenerator(
            title="firefly forge",
            version=__version__,
            security_schemes={"operator": {"type": "http", "scheme": "bearer"}},
            security=[{"operator": []}],
        ).generate(ControllerRegistrar().collect_route_metadata(context))
        document["paths"]["/api/v1/scm/connections/{id}/callback"]["get"]["security"] = []
        webhook = document["paths"].get("/api/v1/webhooks/{provider}/{repository_id}")
        if webhook:
            webhook["post"]["security"] = []
        webhook = document["paths"].get("/api/v1/webhooks/{provider}")
        if webhook:
            webhook["post"]["security"] = []
        return dict(document)
    paths: dict[str, Any] = {}
    specifications = [
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
    ]
    schemas: dict[str, Any] = {}
    models: list[type[BaseModel]] = [
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
        if "{id}" in path:
            operation["parameters"] = [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]
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
        "components": {"schemas": schemas, "securitySchemes": {"operator": {"type": "http", "scheme": "bearer"}}},
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
    def worker_supervision(
        self,
        engine: Engine,
        requests: ExecutionRequests,
        deployments: DeploymentManager,
        campaigns: ProjectCampaigns,
        collaboration: Collaboration,
        observability: ApplicationOperations,
    ) -> WorkerSupervision:
        return WorkerSupervision(
            engine,
            {
                "execution": engine,
                "delivery_requests": requests,
                "deployments": deployments,
                "campaigns": campaigns,
                "scm_feedback": collaboration,
                "observability": observability,
            },
        )

    @bean
    def operator_filter(self, identity: IdentityService, access: ProjectAccess) -> OperatorAuth:
        return OperatorAuth(self.token, identity, access)

    @bean
    def identity_service(
        self,
        store: Store,
        users: ForgeIdentityUserRepository,
        sessions: ForgeIdentitySessionRepository,
        attempts: ForgeIdentityAttemptRepository,
    ) -> IdentityService:
        return IdentityService(store, self.identity_configuration, users, sessions, attempts)

    @bean
    def project_access(self, store: Store, records: ForgeMembershipRepository) -> ProjectAccess:
        return ProjectAccess(store, records)

    @bean
    def ledger(
        self,
        session_provider: SessionProvider,
        async_engine: AsyncEngine,
        repository_records: ForgeRepositoryRepository,
        run_records: ForgeRunRepository,
        event_records: ForgeEventRepository,
        deployment_records: ForgeDeploymentRepository,
        product_records: ForgeProductDefinitionRepository,
    ) -> Store:
        return Store(
            self.data_dir / "factory.db",
            session_provider,
            engine=async_engine,
            repository_records=repository_records,
            run_records=run_records,
            event_records=event_records,
            deployment_records=deployment_records,
            product_records=product_records,
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
    def coordinator(
        self,
        store: Store,
        connections: SCMConnections,
        journal: Journal,
        assurance: Assurance,
        sandbox: SandboxExecutor,
        browser: BrowserRunner,
        search: SearchTools,
        runners: RunnerConfiguration,
        routing: TaskRouting,
        runtime_roles: RuntimeRoles,
    ) -> Engine:
        engine = Engine(
            store,
            self.data_dir / "workspaces",
            concurrency=int(os.environ.get("FORGE_CONCURRENCY", "2")),
            connections=connections,
            journal=journal,
        )
        engine.assurance = assurance
        engine.sandbox = sandbox
        engine.browser, engine.search = browser, search
        store.runners, store.task_router = runners, routing
        search.credential_registry = lambda: engine.credential_registry()
        journal.credential_registry = lambda: engine.credential_registry()
        store.credential_registry = lambda: engine.credential_registry()
        return engine

    @bean
    def assurance(self, store: Store, journal: Journal) -> Assurance:
        return Assurance(store, self.data_dir / "reviews", journal)

    @bean
    def product_projects(self, store: Store, records: ForgeProjectRepository) -> Projects:
        return Projects(store, records)

    @bean
    def reverse_engineering(self, store: Store, projects: Projects, connections: SCMConnections) -> ReverseEngineering:
        return ReverseEngineering(store, projects, self.data_dir / "analyses", connections)

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
    ) -> ProductAssistant:
        return ProductAssistant(
            store,
            self.data_dir / "assistant",
            knowledge,
            analysis_context=analysis_context,
            credential_registry=lambda: engine.credential_registry(),
            attachments=attachments,
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
    def sandbox_executor(self, store: Store, journal: Journal) -> SandboxExecutor:
        return SandboxExecutor(store, journal)

    @bean
    def search_tools(self) -> SearchTools:
        return SearchTools(self.data_dir / "tools" / "search")

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
    ) -> ProjectCampaigns:
        return ProjectCampaigns(store, engine, journal, records, children, events)

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
        auth: OperatorAuth,
        profiles: ModelProfiles,
        users: UserAdministration,
    ) -> DeploymentManager:
        manager = DeploymentManager(store, engine, journal)
        manager.secret_registries.append(search)
        manager.secret_registries.extend([auth, profiles, users])
        return manager

    @bean
    def knowledge_connection(self, private: ConfigurationStore) -> KnowledgeConnection:
        return KnowledgeConnection(private)

    @bean
    def knowledge_manager(
        self, store: Store, engine: Engine, journal: Journal, connection: KnowledgeConnection
    ) -> KnowledgeManager:
        manager = KnowledgeManager(store, engine, connection=connection)
        manager.journal = journal
        engine.knowledge = manager
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
    ) -> Collaboration:
        collaboration = Collaboration(store, scm, deployments, settings_records, inbox, products)
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
        ForgeMembershipRepository,
        ForgeAccessEventRepository,
        IdentityController,
        AccessController,
        ForgeRepositoryRepository,
        ForgeRunRepository,
        ForgeEventRepository,
        ForgeDeploymentRepository,
        ForgePlanRepository,
        ForgeTaskRepository,
        ForgeJournalRepository,
        ProviderService,
        RunService,
        OperatorErrors,
        CatalogController,
        RepositoryController,
        RunController,
        DeploymentController,
        SCMController,
        KnowledgeController,
        JournalController,
        ForgeCollaborationSettingsRepository,
        ForgeWebhookInboxRepository,
        ForgeProductDefinitionRepository,
        CollaborationController,
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
        RunnerController,
        ForgeCampaignRepository,
        ForgeCampaignChildRepository,
        ForgeCampaignEventRepository,
        ProjectCampaignController,
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
