# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PostgreSQL ledger using native PyFly repositories and transaction units."""

from __future__ import annotations

import asyncio
import copy
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode, SessionProvider
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction.registry import TransactionManagerRegistry
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from firefly_forge.agent_roles import ProjectRoles, RoleRegistry
from firefly_forge.collaboration_persistence import ForgeProductDefinitionRepository
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.migrations import migrate
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.persistence import (
    ForgeDeploymentEntity,
    ForgeDeploymentRepository,
    ForgeEventEntity,
    ForgeEventRepository,
    ForgeRepositoryEntity,
    ForgeRepositoryRepository,
    ForgeRunEntity,
    ForgeRunRepository,
)
from firefly_forge.policies import PolicyCatalog
from firefly_forge.sandbox_models import capture_sandbox
from firefly_forge.secrets import contains_known_credential

Data = dict[str, Any]
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise KeyError("Record not found") from exc


class ExecutionOwnerBusy(RuntimeError):
    """Another live database session holds the schema execution fence."""


@service
class Store:
    phase = 0

    def __init__(
        self,
        path: Path | None = None,
        session_provider: SessionProvider | None = None,
        *,
        database_url: str | None = None,
        schema: str | None = None,
        engine: AsyncEngine | None = None,
        repository_records: ForgeRepositoryRepository | None = None,
        run_records: ForgeRunRepository | None = None,
        event_records: ForgeEventRepository | None = None,
        deployment_records: ForgeDeploymentRepository | None = None,
        product_records: ForgeProductDefinitionRepository | None = None,
    ) -> None:
        self.path = path or Path(os.environ.get("FORGE_DATA_DIR", ".forge")) / "factory.db"
        self.configuration = ConfigurationStore(self.path.parent)
        self.policies = PolicyCatalog(self.path.parent)
        self.roles = RoleRegistry(self.path.parent)
        self.credential_registry: Callable[[], dict[str, str]] = dict
        self.task_router: Any = None
        self.runtime_roles: Any = None
        self.execution_requests: Any = None
        self.runners: Any = None
        self.connections: Any = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.schema = schema or os.environ.get("FORGE_DATABASE_SCHEMA", "forge")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.schema):
            raise ValueError("PostgreSQL schema must be a lowercase identifier of at most 63 characters")
        self.database_url = database_url or os.environ.get("FORGE_DATABASE_URL") or os.environ.get("PYFLY_DATA_URL")
        self.session_provider = session_provider
        self._engine = engine
        self._owns_engine = False
        self._initialized = False
        self._ownership_connection: AsyncConnection | None = None
        self._ownership_pid: int | None = None
        self._ownership_lock = asyncio.Lock()
        self.repository_records = repository_records or ForgeRepositoryRepository()
        self.run_records = run_records or ForgeRunRepository()
        self.event_records = event_records or ForgeEventRepository()
        self.deployment_records = deployment_records or ForgeDeploymentRepository()
        self.product_records = product_records or ForgeProductDefinitionRepository()

    async def start(self) -> None:
        await self.initialize()

    async def stop(self) -> None:
        await self.close()

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self.session_provider is None:
            if not self.database_url:
                raise ValueError("Set FORGE_DATABASE_URL to a PostgreSQL connection URL")
            url = make_url(self.database_url)
            if url.get_backend_name() != "postgresql":
                raise ValueError("Forge requires PostgreSQL; SQLite persistence is no longer supported")
            url = url.set(drivername="postgresql+asyncpg")
            self._owns_engine = self._engine is None
            self._engine = self._engine or create_async_engine(url, pool_pre_ping=True)
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            registry = TransactionManagerRegistry()
            registry.register(SqlAlchemyTransactionManager(sessionmaker=factory, name="primary"), default=True)
            self.session_provider = SessionProvider(registry)
        assert self.session_provider is not None
        async with self.session_provider.unit() as session:
            await migrate(session, self.schema)
        self._initialized = True

    async def close(self) -> None:
        await self.release_ownership()
        if self._owns_engine and self._engine is not None:
            await self._engine.dispose()
        self._initialized = False

    async def acquire_ownership(self) -> None:
        async with self._ownership_lock:
            await self._acquire_ownership()

    async def _acquire_ownership(self) -> None:
        if not self._initialized or self._engine is None:
            raise RuntimeError("Await Store.initialize() before acquiring execution ownership")
        if self._ownership_connection is not None:
            await self._assert_ownership()
            return
        connection = await self._engine.connect()
        try:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                {"key": "forge:execution:" + self.schema},
            )
            if not acquired:
                raise ExecutionOwnerBusy("Another factory server already owns this PostgreSQL schema")
            pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            await connection.commit()
            self._ownership_connection, self._ownership_pid = connection, pid
        except BaseException:
            await connection.invalidate()
            await connection.close()
            raise

    async def assert_ownership(self) -> None:
        async with self._ownership_lock:
            await self._assert_ownership()

    async def _assert_ownership(self) -> None:
        connection = self._ownership_connection
        if connection is None:
            raise RuntimeError("PostgreSQL execution ownership was lost")
        try:
            held = await connection.scalar(
                text(
                    "SELECT pg_backend_pid() = :pid AND EXISTS ("
                    "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
                    "AND pid = pg_backend_pid() AND objsubid = 1 "
                    "AND classid::bigint = ((hashtextextended(:key, 0) >> 32) & 4294967295) "
                    "AND objid::bigint = (hashtextextended(:key, 0) & 4294967295))"
                ),
                {"key": "forge:execution:" + self.schema, "pid": self._ownership_pid},
            )
            await connection.commit()
        except Exception:
            raise RuntimeError("PostgreSQL execution ownership was lost") from None
        if not held:
            raise RuntimeError("PostgreSQL execution ownership was lost")

    async def release_ownership(self) -> None:
        async with self._ownership_lock:
            await self._release_ownership()

    async def _release_ownership(self) -> None:
        connection, self._ownership_connection = self._ownership_connection, None
        self._ownership_pid = None
        if connection is None:
            return
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                {"key": "forge:execution:" + self.schema},
            )
            await connection.commit()
        except BaseException:
            await connection.invalidate()
            raise
        finally:
            await connection.close()

    @asynccontextmanager
    async def _unit(self) -> AsyncIterator[AsyncSession]:
        if not self._initialized or self.session_provider is None:
            raise RuntimeError("Await Store.initialize() before accessing the ledger")
        async with self.session_provider.unit() as session:
            await session.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            await session.execute(text("SET LOCAL synchronous_commit TO on"))
            yield session

    async def add_repository(self, request: RepositoryInput) -> Data:
        data = {**request.model_dump(), "id": uuid.uuid4().hex, "created_at": now()}
        async with self._unit():
            await self.repository_records.save(ForgeRepositoryEntity(id=identifier(data["id"]), data=data))
        return data

    async def repositories(self) -> list[Data]:
        async with self._unit():
            return [copy.deepcopy(row.data) for row in await self.repository_records.find_latest()]

    async def repository(self, value: str) -> Data:
        async with self._unit():
            row = await self.repository_records.find_by_id(identifier(value))
            if row is None:
                raise KeyError("Repository not found")
            return copy.deepcopy(row.data)

    async def create_run(
        self,
        request: RunInput,
        *,
        prepared_runner: Data | None = None,
        prepared_tools: list[Data] | None = None,
    ) -> Data:
        from firefly_forge.collaboration import ProductDefinition
        from firefly_forge.knowledge import snapshot_knowledge
        from firefly_forge.product_design import ProductDesign
        from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
        from firefly_forge.projects import Projects

        if request.operation == "test" and (request.model_profile_id or request.assurance.review_model_profile_id):
            raise ValueError("Test operations check existing source without a coding model")
        if self.task_router is None and (request.model_profile_id or request.assurance.review_model_profile_id):
            raise ValueError("Named model profiles are not configured")
        selection = review_selection = None
        if self.task_router is not None and request.operation != "test":
            selection = await self.task_router.resolve("implementation", request.model_profile_id)
            review_selection = await self.task_router.resolve("review", request.assurance.review_model_profile_id)
        if selection:
            requested_agent = "agentic" if request.agent == "api" else request.agent
            if "agent" in request.model_fields_set and requested_agent != selection["provider"]:
                raise ValueError("The chosen agent conflicts with this task's model profile")
            request = request.model_copy(update={"agent": selection["provider"]})
        if review_selection:
            if (
                request.assurance.review_agent is not None
                and request.assurance.review_agent != review_selection["provider"]
            ):
                raise ValueError("The chosen reviewer conflicts with this task's model profile")
            request = request.model_copy(
                update={
                    "assurance": request.assurance.model_copy(update={"review_agent": review_selection["provider"]})
                }
            )
        frozen_configuration = (
            {"config_revision": selection["config_revision"], "execution_config": selection["execution_config"]}
            if selection
            else self.configuration.snapshot(request.agent)
        )
        actor = CURRENT_PRINCIPAL.get()
        if actor is not None and not await ProjectAccess(self).current_administrator(actor):
            if not request.project_id:
                raise ValueError("Select an authorized project before starting work")
            await ProjectAccess(self).require_project(
                actor, request.project_id, "release" if request.publish else "execute"
            )
        self.reject_credentials(request.model_dump())
        runner = (
            prepared_runner
            if prepared_runner is not None
            else (
                await self.runners.capture_run(request.repository_id, request.sandbox)
                if self.runners is not None
                else {
                    "sandbox_snapshot": capture_sandbox(request.sandbox),
                    "search_snapshot": None,
                    "runner_config_revision": None,
                }
            )
        )
        if runner is None:
            raise ValueError("The selected runner configuration is unavailable")
        sandbox_snapshot = runner["sandbox_snapshot"]
        if not isinstance(sandbox_snapshot, dict):
            raise ValueError("The selected runner requires a frozen sandbox configuration")
        if actor is not None and not await ProjectAccess(self).current_administrator(actor):
            if sandbox_snapshot["mode"] != "docker":
                raise ValueError(
                    "Project users require an isolated runner; host execution requires a factory administrator"
                )
            if request.operation != "test" and (
                request.agent in {"codex", "claude"} or request.assurance.review_agent in {"codex", "claude"}
            ):
                raise ValueError(
                    "Direct host coding CLIs require a factory administrator; choose a configured API agent"
                )
        tools = (
            prepared_tools
            if prepared_tools is not None
            else (await self.runners.capabilities(runner) if self.runners is not None else None)
        )

        async with self._unit():
            repo = await self.repository(request.repository_id)
            policy_selection = request.skills if "skills" in request.model_fields_set else repo.get("skills")
            snapshot = self.policies.selection(policy_selection)
            component = await Projects(self).component_snapshot(request)
            role_defaults = (component or {}).get("project_definition", {}).get("roles") or repo.get("roles")
            role_selection = (
                request.roles
                if request.roles is not None
                else ProjectRoles.model_validate(role_defaults)
                if role_defaults is not None
                else None
            )
            role_source = (
                "run"
                if request.roles is not None
                else "project"
                if (component or {}).get("project_definition", {}).get("roles")
                else "repository"
                if repo.get("roles")
                else "global"
            )
            product_repository_id = component["product_repository_id"] if component else repo["id"]
            product_definition = request.product_definition
            if product_definition is None:
                product = await self.product_records.find_current(identifier(product_repository_id))
                product_definition = copy.deepcopy(product.data) if product else None
            if actor is not None and request.product_definition is not None:
                recorded = await self.product_records.find_revision(
                    identifier(product_repository_id), cast(int, request.product_definition["revision"])
                )
                if recorded is None or recorded.data != request.product_definition:
                    raise ValueError(
                        "Select an existing immutable product revision; client-supplied approval is not authority"
                    )
            if product_definition and product_definition["repository_id"] != product_repository_id:
                raise ValueError("Product snapshot must match the selected product's definition and design source")
            fields = request.model_dump()
            if component:
                fields["backend"], fields["frontend"] = component["backend"], component["frontend"]
                if "commands" not in request.model_fields_set:
                    fields["commands"] = component["verification"]
            fields["product_definition"] = product_definition
            definition = (product_definition or {}).get("definition")
            architecture = definition.get("architecture") if isinstance(definition, dict) else None
            if architecture and not component:
                for field in ("backend", "frontend"):
                    if field not in request.model_fields_set:
                        fields[field] = architecture[field]
            RunInput.model_validate(fields)
            branding = self.configuration._read(
                self.configuration.directory / f"product-brand-{product_repository_id}.json"
            )
            design = (
                await ProductDesign(self).validate_review(
                    product_repository_id, ProductDefinition.model_validate(definition)
                )
                if definition and request.operation != "test"
                else None
            )
            budget = max(0, min(3, int(os.environ.get("FORGE_REPAIR_ATTEMPTS", "2"))))
            if design and design["brand_digest"] != (branding or {}).get("digest"):
                raise ValueError(
                    "The product brand revision changed while freezing its reviewed design; retry admission"
                )
            role_snapshot = self.roles.snapshot(role_selection, snapshot, tool_catalog=tools)
            self.reject_credentials([definition, branding, snapshot, role_snapshot, component, runner, design])
            review_configuration = (
                {
                    "config_revision": review_selection["config_revision"],
                    "execution_config": review_selection["execution_config"],
                }
                if review_selection
                else self.configuration.snapshot(request.assurance.review_agent)
                if request.assurance.review_agent
                else {}
            )
            data = {
                **fields,
                "product_definition": product_definition,
                "branding_snapshot": copy.deepcopy(branding) if branding else None,
                "design_snapshot": design,
                "execution_actor": ProjectAccess.actor_snapshot(actor) if actor else None,
                "component_snapshot": component,
                "scm_snapshot": self.connections.snapshot(repo) if self.connections is not None else None,
                "repair_budget": budget,
                "model_selection": selection,
                "review_model_selection": review_selection,
                "review_config_revision": review_configuration.get("config_revision"),
                "review_execution_config": review_configuration.get("execution_config", {}),
                "skills": [item["id"] for item in snapshot["skills"]],
                "policy_snapshot": snapshot,
                "role_snapshot": role_snapshot,
                "role_selection_source": role_source,
                **runner,
                **frozen_configuration,
                "knowledge_snapshot": snapshot_knowledge(self.configuration),
                "id": uuid.uuid4().hex,
                "repository_name": repo["name"],
                "status": "queued",
                "created_at": now(),
                "updated_at": now(),
                "verification": [],
                "changed_files": [],
                "diff": "",
                "artifacts": [],
                "error": None,
                "pr_url": None,
                "summary": "",
                "branch": "",
                "base_revision": "",
            }
            await self.run_records.save(
                ForgeRunEntity(
                    id=identifier(data["id"]),
                    repository_id=identifier(request.repository_id),
                    status="queued",
                    data=data,
                )
            )
            await self.event(data["id"], "queued", "Run queued")
            return await self.get_run(data["id"])

    def reject_credentials(self, value: Any) -> None:
        registry = {**self.credential_registry()}
        for provider in ("codex", "claude", "bridge", "agentic"):
            registry.update(self.configuration.resolve(provider))
        if contains_known_credential(value, registry):
            raise ValueError(
                "Do not place configured credentials in product briefs, definitions or worker instructions"
            )

    async def repository_roles(self, value: str) -> Data:
        repo = await self.repository(value)
        return {"roles": repo.get("roles"), "inherited": "roles" not in repo}

    async def set_repository_roles(self, value: str, request: ProjectRoles) -> Data:
        self.roles.snapshot(request, self.policies.selection())
        self.reject_credentials(request.model_dump())
        async with self._unit():
            row = await self.repository_records.find_by_id(identifier(value), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Repository not found")
            row.data = {**row.data, "roles": request.model_dump()}
            await self.repository_records.save(row)
            return await self.repository_roles(value)

    async def get_project_policies(self, value: str) -> Data:
        repo = await self.repository(value)
        selection = self.policies.selection(repo.get("skills"))
        return {
            "skills": [item["id"] for item in selection["skills"]],
            "revision": selection["revision"],
            "digest": selection["digest"],
            "inherited": "skills" not in repo,
        }

    async def set_project_policies(self, value: str, request: Any) -> Data:
        self.policies.selection(request.skills)
        async with self._unit():
            row = await self.repository_records.find_by_id(identifier(value), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Repository not found")
            row.data = {**row.data, "skills": request.skills}
            await self.repository_records.save(row)
            return await self.get_project_policies(value)

    async def get_run(self, value: str) -> Data:
        async with self._unit():
            row = await self.run_records.find_by_id(identifier(value))
            if row is None:
                raise KeyError("Run not found")
            data = copy.deepcopy(row.data)
            data["events"] = [copy.deepcopy(event.data) for event in await self.event_records.find_for_run(row.id)]
            return data

    async def runs(self) -> list[Data]:
        async with self._unit():
            return [await self.get_run(row.id.hex) for row in await self.run_records.find_latest()]

    async def queued(self) -> list[str]:
        async with self._unit():
            return [row.id.hex for row in await self.run_records.find_queued()]

    async def update(self, value: str, **fields: Any) -> None:
        async with self._unit():
            row = await self.run_records.find_by_id(identifier(value), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Run not found")
            if row.status in TERMINAL and fields.get("status", row.status) != row.status:
                return
            row.data = {**row.data, **fields, "updated_at": now()}
            row.status = row.data["status"]
            await self.run_records.save(row)

    async def claim(self, value: str) -> bool:
        current = await self.get_run(value)
        async with self._unit():
            await self.assert_campaign_current(current)
            row = await self.run_records.find_by_id(identifier(value), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.status != "queued" or row.data.get("cancel_request", {}).get("status") == "requested":
                return False
            row.status = "preparing"
            row.data = {**row.data, "status": row.status, "updated_at": now()}
            await self.run_records.save(row)
            return True

    async def assert_campaign_current(self, run: Data, *, action_id: str | None = None) -> None:
        from firefly_forge.project_campaign_persistence import ForgeCampaignChildRepository, ForgeCampaignRepository

        context = run.get("campaign_context") or {}
        if not context.get("campaign_id"):
            return
        header = await ForgeCampaignRepository().find_by_id(
            identifier(context["campaign_id"]),
            lock=LockMode.PESSIMISTIC_WRITE,
        )
        if header is None or header.data["current_attempts"].get(context["component_id"]) != context["attempt"]:
            raise ValueError("This component attempt is no longer current in its project campaign")
        children = await ForgeCampaignChildRepository().children(header.id)
        if not any(
            child.run_id.hex == run["id"]
            and child.component_id == context["component_id"]
            and child.attempt == context["attempt"]
            for child in children
        ):
            raise ValueError("The run does not belong to this campaign attempt")
        pending = header.data.get("pending_action")
        coordinated_resume = bool(action_id and pending == {"action": "resume", "request_id": action_id})
        if not coordinated_resume and (header.data.get("control") == "cancelled" or pending):
            raise ValueError(
                "The project campaign is stopped or requires reconciliation; resume it through the campaign"
            )

    async def event(self, value: str, stage: str, message: str) -> None:
        data = {"id": uuid.uuid4().hex, "stage": stage, "message": message[:8000], "created_at": now()}
        async with self._unit():
            await self.event_records.save(
                ForgeEventEntity(id=identifier(data["id"]), run_id=identifier(value), data=data)
            )

    async def finish(self, value: str, status: str, *, error: str | None = None) -> None:
        if status not in TERMINAL:
            raise ValueError("Not a terminal run status")
        async with self._unit():
            row = await self.run_records.find_by_id(identifier(value), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Run not found")
            if row.status in TERMINAL:
                if row.status == status and error and not row.data.get("error"):
                    row.data = {**row.data, "error": error, "updated_at": now()}
                    await self.run_records.save(row)
                    await self.event(value, status, error)
                return
            row.status = status
            row.data = {**row.data, "status": status, "error": error, "updated_at": now()}
            await self.run_records.save(row)
            await self.event(value, status, error or f"Run {status}")

    async def recover(self) -> int:
        async with self._unit():
            identifiers = [row.id.hex for row in await self.run_records.find_inflight()]
            for value in identifiers:
                await self.finish(
                    value, "interrupted", error="Worker stopped before completion; retry to start a fresh clone"
                )
            return len(identifiers)

    async def create_deployment(self, data: Data) -> Data:
        target = data.get("target_id") or data.get("target")
        if not isinstance(target, str) or not target or len(target) > 200:
            raise ValueError("Deployment target must be a nonempty identifier of at most 200 characters")
        run_id = identifier(data["run_id"])
        async with self._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "forge:deployment:" + self.schema + ":" + target},
            )
            if await self.deployment_records.find_active_target(target) is not None:
                raise ValueError("A deployment is already active for this target")
            if await self.run_records.find_by_id(run_id) is None:
                raise KeyError("Run not found")
            value = {
                **copy.deepcopy(data),
                "id": data.get("id") or uuid.uuid4().hex,
                "status": "queued",
                "target_id": target,
                "created_at": now(),
                "updated_at": now(),
                "error": None,
            }
            await self.deployment_records.save(
                ForgeDeploymentEntity(
                    id=identifier(value["id"]), run_id=run_id, target_id=target, status="queued", data=value
                )
            )
            return value

    async def get_deployment(self, value: str) -> Data:
        async with self._unit():
            row = await self.deployment_records.find_by_id(identifier(value))
            if row is None:
                raise KeyError("Deployment not found")
            return copy.deepcopy(row.data)

    async def deployments(self, run_id: str | None = None) -> list[Data]:
        async with self._unit():
            return [
                copy.deepcopy(row.data)
                for row in await self.deployment_records.find_latest(identifier(run_id) if run_id is not None else None)
            ]

    async def claim_deployment(self, value: str) -> bool:
        async with self._unit():
            row = await self.deployment_records.find_by_id(identifier(value), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.status != "queued":
                return False
            row.status = "running"
            row.data = {**row.data, "status": row.status, "updated_at": now()}
            await self.deployment_records.save(row)
            return True

    async def update_deployment(self, value: str, **fields: Any) -> None:
        async with self._unit():
            row = await self.deployment_records.find_by_id(identifier(value), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Deployment not found")
            if row.status in TERMINAL and fields.get("status", row.status) != row.status:
                return
            if any(
                key in fields and fields[key] != row.data.get(key) for key in ["id", "run_id", "target", "target_id"]
            ):
                raise ValueError("Deployment identity and target cannot change")
            row.data = {**row.data, **fields, "updated_at": now()}
            row.status = row.data["status"]
            await self.deployment_records.save(row)

    async def queued_deployments(self, limit: int = 100) -> list[Data]:
        if not 1 <= limit <= 1000:
            raise ValueError("Deployment dispatch limit must be between 1 and 1000")
        async with self._unit():
            return [copy.deepcopy(row.data) for row in await self.deployment_records.find_queued(limit)]

    async def recover_deployments(self) -> int:
        async with self._unit():
            running = await self.deployment_records.find_running()
            for row in running:
                await self.update_deployment(
                    row.id.hex,
                    status="interrupted",
                    error="Deployment worker stopped; inspect the target before starting a new deployment",
                )
            return len(running)
