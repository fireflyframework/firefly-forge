# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated PyFly API and Studio host for a single trusted factory operator."""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel
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
from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration
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
from firefly_forge.journal import Journal
from firefly_forge.knowledge import (
    KnowledgeConfiguration,
    KnowledgeIndex,
    KnowledgeManager,
    KnowledgeSearch,
    KnowledgeTest,
)
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
from firefly_forge.scm_connections import SCMConnections
from firefly_forge.store import Store


@service
class OperatorAuth(OncePerRequestFilter):
    __pyfly_order__ = -1_000_000

    def __init__(self, token: str) -> None:
        self.token = token

    async def do_filter(self, request: Any, call_next: CallNext) -> Response:
        callbacks = {f"/api/v1/scm/connections/{provider}/callback" for provider in ("github", "gitlab", "bitbucket")}
        public_callback = request.method == "GET" and request.url.path in callbacks
        if request.url.path.startswith("/api/") and not public_callback:
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), f"Bearer {self.token}".encode()):
                return JSONResponse({"detail": "Connect using the factory operator token"}, status_code=401)
            try:
                size = int(request.headers.get("Content-Length", "0"))
            except ValueError:
                return JSONResponse({"detail": "Invalid content length"}, status_code=400)
            if size < 0 or size > 100_000:
                return JSONResponse({"detail": "Request exceeds 100 KB"}, status_code=413)
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
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
        "info": {"title": "Firefly Forge", "version": __version__},
        "paths": paths,
        "components": {"schemas": schemas, "securitySchemes": {"operator": {"type": "http", "scheme": "bearer"}}},
        "security": [{"operator": []}],
    }


@configuration
class FactoryConfiguration:
    def __init__(self, data_dir: Path, token: str) -> None:
        self.data_dir = data_dir
        self.token = token

    @bean
    def operator_filter(self) -> OperatorAuth:
        return OperatorAuth(self.token)

    @bean
    def ledger(
        self,
        session_provider: SessionProvider,
        async_engine: AsyncEngine,
        repository_records: ForgeRepositoryRepository,
        run_records: ForgeRunRepository,
        event_records: ForgeEventRepository,
        deployment_records: ForgeDeploymentRepository,
    ) -> Store:
        return Store(
            self.data_dir / "factory.db",
            session_provider,
            engine=async_engine,
            repository_records=repository_records,
            run_records=run_records,
            event_records=event_records,
            deployment_records=deployment_records,
        )

    @bean
    def coordinator(self, store: Store, connections: SCMConnections, journal: Journal) -> Engine:
        return Engine(
            store,
            self.data_dir / "workspaces",
            concurrency=int(os.environ.get("FORGE_CONCURRENCY", "2")),
            connections=connections,
            journal=journal,
        )

    @bean
    def scm_connections(self, store: Store) -> SCMConnections:
        return SCMConnections(store)

    @bean
    def execution_journal(
        self, store: Store, plans: ForgePlanRepository, tasks: ForgeTaskRepository, records: ForgeJournalRepository
    ) -> Journal:
        return Journal(store, plans=plans, tasks=tasks, records=records)

    @bean
    def provider_configuration(self, store: Store) -> ConfigurationStore:
        return store.configuration

    @bean
    def policy_catalog(self, store: Store) -> PolicyCatalog:
        return store.policies

    @bean
    def deployment_manager(self, store: Store, engine: Engine, journal: Journal) -> DeploymentManager:
        return DeploymentManager(store, engine, journal)

    @bean
    def knowledge_manager(self, store: Store, engine: Engine, journal: Journal) -> KnowledgeManager:
        manager = KnowledgeManager(store, engine)
        manager.journal = journal
        engine.knowledge = manager
        return manager


def create_application(data_dir: Path | None = None, token: str | None = None) -> Starlette:
    data_dir = data_dir or Path(os.environ.get("FORGE_DATA_DIR", ".forge")).resolve()
    token = token if token is not None else os.environ.get("FORGE_API_TOKEN", "")
    if len(token) < 24:
        raise ValueError("Set a factory operator token of at least 24 characters in FORGE_API_TOKEN")

    @pyfly_application(name="firefly-forge", version=__version__)
    class Application:
        pass

    runtime = PyFlyApplication(Application, config_path=Path(__file__).parent / "pyfly.yaml")
    container = runtime.context.container
    container.register_instance(FactoryConfiguration, FactoryConfiguration(data_dir, token))
    for component in [
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
    ]:
        container.register(component)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await runtime.startup()
        store = runtime.context.get_bean(Store)
        engine = runtime.context.get_bean(Engine)
        app.state.store, app.state.engine = store, engine
        try:
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
    source_static = Path(__file__).resolve().parents[2] / "studio" / "dist"
    packaged_static = Path(__file__).parent / "static"
    static = packaged_static if packaged_static.is_dir() else source_static
    if static.is_dir():
        app.routes.append(Mount("/", app=StaticFiles(directory=static, html=True), name="studio"))
    app.state.runtime = runtime
    return app
