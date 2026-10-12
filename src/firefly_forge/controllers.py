# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PyFly REST controllers and constructor-injected application services."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pyfly.container import controller_advice, rest_controller, service
from pyfly.kernel.exceptions import InvalidRequestException, ValidationException
from pyfly.web import (
    Body,
    Cookie,
    PathVar,
    QueryParam,
    exception_handler,
    get_mapping,
    post_mapping,
    put_mapping,
    request_mapping,
)
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from firefly_forge import __version__
from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration, normalize_provider
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
from firefly_forge.policies import PolicyCatalog, PolicySync, ProjectPolicies
from firefly_forge.profiles import profile_catalog
from firefly_forge.providers import provider_catalog, test_provider_connection
from firefly_forge.scm_connections import SCMConnections
from firefly_forge.skills import workflow_catalog
from firefly_forge.store import Store


class SCMConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(default_factory=dict, max_length=10)
    clear_secrets: list[str] = Field(default_factory=list, max_length=2)


class SCMRepositorySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: str = Field(min_length=1, max_length=200)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: Literal["codex", "claude", "bridge", "agentic"] | None = None


@service
class ProviderService:
    def __init__(self, configuration: ConfigurationStore) -> None:
        self.configuration = configuration

    async def catalog(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(provider_catalog, self.configuration)

    async def configure(self, provider_id: str, request: ProviderConfiguration) -> dict[str, Any]:
        provider_id = normalize_provider(provider_id)
        await asyncio.to_thread(self.configuration.update, provider_id, request)
        return next(item for item in await self.catalog() if item["id"] == provider_id)

    async def test(self, provider_id: str) -> dict[str, Any]:
        return await test_provider_connection(provider_id, self.configuration)


@service
class RunService:
    def __init__(self, store: Store, engine: Engine) -> None:
        self.store = store
        self.engine = engine

    async def retry(self, identifier: str) -> dict[str, Any]:
        run = await self.store.get_run(identifier)
        if run["status"] not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("Only failed, cancelled or interrupted runs can be retried")
        request = RunInput.model_validate({key: run[key] for key in RunInput.model_fields if key in run})
        return await self.store.create_run(request)

    async def export(self, identifier: str) -> FileResponse:
        run = await self.store.get_run(identifier)
        if not run["artifacts"]:
            raise KeyError("Artifact not found")
        artifact = self.engine.workspaces / f"{identifier}.zip"
        return FileResponse(artifact, filename="changes.zip", media_type="application/zip")


@controller_advice
class OperatorErrors:
    @exception_handler(Exception)
    def handle(self, exc: Exception) -> Response:
        if isinstance(exc, ValidationError):
            return JSONResponse({"detail": exc.errors(include_input=False, include_context=False)}, status_code=422)
        if isinstance(exc, (json.JSONDecodeError, InvalidRequestException, ValidationException)):
            return JSONResponse({"detail": "Invalid request body or parameters"}, status_code=422)
        if isinstance(exc, KeyError):
            return JSONResponse({"detail": str(exc)}, status_code=404)
        if isinstance(exc, ValueError):
            return JSONResponse({"detail": str(exc)}, status_code=409)
        # Provider exceptions can contain a persisted secret not present in the process environment.
        return JSONResponse(
            {"detail": "Factory operation failed; inspect server configuration and run evidence"}, status_code=500
        )


@rest_controller
@request_mapping("/api/v1")
class CatalogController:
    def __init__(self, providers: ProviderService, policies: PolicyCatalog, engine: Engine) -> None:
        self.providers = providers
        self.policies = policies
        self.engine = engine

    @get_mapping("/health")
    def health(self) -> dict[str, Any]:
        detail = self.engine.ownership_failure
        if not detail and self.engine.poller and self.engine.poller.done() and not self.engine.stopping:
            detail = "Execution scheduler stopped; inspect interrupted work before restarting"
        return {
            "status": "degraded" if detail else "ok",
            "version": __version__,
            "runtime": "PyFly",
            "storage": "PostgreSQL",
            "detail": detail,
            "execution_ready": not bool(detail),
        }

    @get_mapping("/openapi.json")
    def openapi(self, request: Request) -> dict[str, Any]:
        from firefly_forge.api import openapi_document

        return openapi_document(request.app.state.runtime.context)

    @get_mapping("/profiles")
    def profiles(self) -> dict[str, Any]:
        return profile_catalog()

    @get_mapping("/providers")
    async def provider_list(self) -> list[dict[str, Any]]:
        return await self.providers.catalog()

    @put_mapping("/providers/{id}/configuration")
    async def configure_provider(self, id: PathVar[str], body: Body[ProviderConfiguration]) -> dict[str, Any]:
        return await self.providers.configure(id, body)

    @post_mapping("/providers/{id}/test")
    async def test_provider(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.providers.test(id)

    @get_mapping("/skills")
    def skills(self) -> dict[str, Any]:
        return self.policies.current()

    @get_mapping("/policies")
    def policy_catalog(self) -> dict[str, Any]:
        return self.policies.current()

    @post_mapping("/policies/sync")
    async def sync_policies(self, body: Body[PolicySync]) -> dict[str, Any]:
        return await self.policies.sync(body)

    @get_mapping("/workflow")
    def workflow(self) -> dict[str, Any]:
        return workflow_catalog()


@rest_controller
@request_mapping("/api/v1/repositories")
class RepositoryController:
    def __init__(self, store: Store) -> None:
        self.store = store

    @get_mapping("")
    async def list_repositories(self) -> list[dict[str, Any]]:
        return await self.store.repositories()

    @post_mapping("", status_code=201)
    async def connect(self, body: Body[RepositoryInput]) -> dict[str, Any]:
        return await self.store.add_repository(body)

    @get_mapping("/{id}/policies")
    async def policies(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.store.get_project_policies(id)

    @put_mapping("/{id}/policies")
    async def configure_policies(self, id: PathVar[str], body: Body[ProjectPolicies]) -> dict[str, Any]:
        return await self.store.set_project_policies(id, body)


@rest_controller
@request_mapping("/api/v1/runs")
class RunController:
    def __init__(self, runs: RunService, store: Store, engine: Engine) -> None:
        self.runs = runs
        self.store = store
        self.engine = engine

    @get_mapping("")
    async def list_runs(self) -> list[dict[str, Any]]:
        return await self.store.runs()

    @post_mapping("", status_code=201)
    async def create(self, body: Body[RunInput]) -> dict[str, Any]:
        return await self.store.create_run(body)

    @get_mapping("/{id}")
    async def get(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.store.get_run(id)

    @post_mapping("/{id}/cancel")
    async def cancel(self, id: PathVar[str]) -> dict[str, Any]:
        await self.engine.cancel(id)
        return await self.store.get_run(id)

    @post_mapping("/{id}/retry", status_code=201)
    async def retry(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.runs.retry(id)

    @post_mapping("/{id}/publish")
    async def publish(self, id: PathVar[str]) -> dict[str, Any]:
        await self.engine.publish(id)
        return await self.store.get_run(id)

    @get_mapping("/{id}/artifacts/changes.zip")
    async def export(self, id: PathVar[str]) -> FileResponse:
        return await self.runs.export(id)


@rest_controller
@request_mapping("/api/v1")
class DeploymentController:
    def __init__(self, deployments: DeploymentManager) -> None:
        self.deployments = deployments

    @get_mapping("/deployment-targets")
    def targets(self) -> list[dict[str, Any]]:
        return self.deployments.targets()

    @put_mapping("/deployment-targets/{id}")
    async def configure_target(self, id: PathVar[str], body: Body[DeploymentTarget]) -> dict[str, Any]:
        return await self.deployments.configure_target(id, body)

    @get_mapping("/deployments")
    async def list_deployments(self) -> list[dict[str, Any]]:
        return await self.deployments.store.deployments()

    @post_mapping("/runs/{id}/deploy", status_code=201)
    async def deploy(self, id: PathVar[str], body: Body[DeploymentRequest]) -> dict[str, Any]:
        return await self.deployments.create(id, body.target_id)

    @post_mapping("/deployments/{id}/rollback", status_code=201)
    async def rollback(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.deployments.rollback(id)


@rest_controller
@request_mapping("/api/v1/scm/connections")
class SCMController:
    def __init__(self, connections: SCMConnections) -> None:
        self.connections = connections

    @get_mapping("")
    def catalog(self) -> list[dict[str, Any]]:
        return self.connections.catalog()

    @put_mapping("/{id}")
    async def configure(self, id: PathVar[str], body: Body[SCMConfiguration]) -> dict[str, Any]:
        return await self.connections.configure(id, body.model_dump())

    @post_mapping("/{id}/test")
    async def test(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.connections.test(id)

    @get_mapping("/{id}/repositories")
    async def repositories(self, id: PathVar[str]) -> list[dict[str, Any]]:
        return await self.connections.repositories(id)

    @post_mapping("/{id}/repositories/connect", status_code=201)
    async def connect(self, id: PathVar[str], body: Body[SCMRepositorySelection]) -> dict[str, Any]:
        return await self.connections.connect(id, body.repository_id)

    @post_mapping("/{id}/authorize")
    async def authorize(self, id: PathVar[str], request: Request) -> Response:
        result = await self.connections.authorize(id)
        nonce = result.pop("browser_nonce")
        response = JSONResponse(result)
        response.set_cookie(
            "forge_scm_oauth",
            nonce,
            max_age=600,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            path="/api/v1/scm/connections",
        )
        return response

    @get_mapping("/{id}/callback")
    async def callback(
        self, id: PathVar[str], state: QueryParam[str], code: QueryParam[str], forge_scm_oauth: Cookie[str] = ""
    ) -> Response:
        await self.connections.callback(id, state, code, browser_nonce=forge_scm_oauth)
        response = RedirectResponse("/?scm=connected", status_code=303)
        response.delete_cookie("forge_scm_oauth", path="/api/v1/scm/connections", httponly=True, samesite="lax")
        return response


@rest_controller
@request_mapping("/api/v1")
class KnowledgeController:
    def __init__(self, knowledge: KnowledgeManager, store: Store) -> None:
        self.knowledge = knowledge
        self.store = store

    @get_mapping("/knowledge/configuration")
    def configuration(self) -> dict[str, Any]:
        return self.knowledge.public()

    @get_mapping("/knowledge/status")
    def status(self) -> dict[str, Any]:
        return self.knowledge.public()

    @put_mapping("/knowledge/configuration")
    def configure(self, body: Body[KnowledgeConfiguration]) -> dict[str, Any]:
        return self.knowledge.configure(body)

    @post_mapping("/knowledge/test")
    async def test(self, body: Body[KnowledgeTest]) -> dict[str, Any]:
        return await self.knowledge.test(body)

    @post_mapping("/knowledge/search")
    async def search(self, body: Body[KnowledgeSearch]) -> dict[str, Any]:
        return await self.knowledge.search(body.repository_id, body.query)

    @post_mapping("/runs/{id}/knowledge/index")
    async def index(self, id: PathVar[str], body: Body[KnowledgeIndex]) -> dict[str, Any]:
        return await self.knowledge.index(id, body.paths)

    @post_mapping("/runs/{id}/knowledge/remember")
    async def remember(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.knowledge.remember(await self.store.get_run(id))


@rest_controller
@request_mapping("/api/v1/runs")
class JournalController:
    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    @get_mapping("/{id}/plan")
    async def plan(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.journal.ensure_plan(id)

    @get_mapping("/{id}/journal")
    async def entries(self, id: PathVar[str]) -> dict[str, Any]:
        entries = await self.journal.entries(id)
        return {"entries": entries, "valid": Journal.verify_chain(entries)}

    @post_mapping("/{id}/resume")
    async def resume(self, id: PathVar[str], body: Body[ResumeRequest]) -> dict[str, Any]:
        return await self.journal.resume(id, body.agent)

    @post_mapping("/{id}/handoff")
    async def handoff(self, id: PathVar[str], body: Body[ResumeRequest]) -> dict[str, Any]:
        if body.agent is None:
            raise ValueError("Choose a coding provider for the explicit handoff")
        return await self.journal.resume(id, body.agent)
