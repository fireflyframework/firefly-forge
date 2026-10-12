# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PostgreSQL ledger using native PyFly repositories and transaction units."""

from __future__ import annotations

import asyncio
import copy
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode, SessionProvider
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction.registry import TransactionManagerRegistry
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

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

Data = dict[str, Any]
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise KeyError("Record not found") from exc


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
    ) -> None:
        self.path = path or Path(os.environ.get("FORGE_DATA_DIR", ".forge")) / "factory.db"
        self.configuration = ConfigurationStore(self.path.parent)
        self.policies = PolicyCatalog(self.path.parent)
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
                raise RuntimeError("Another factory server already owns this PostgreSQL schema")
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

    async def create_run(self, request: RunInput) -> Data:
        from firefly_forge.knowledge import snapshot_knowledge

        async with self._unit():
            repo = await self.repository(request.repository_id)
            selection = request.skills if "skills" in request.model_fields_set else repo.get("skills")
            snapshot = self.policies.selection(selection)
            data = {
                **request.model_dump(),
                "skills": [item["id"] for item in snapshot["skills"]],
                "policy_snapshot": snapshot,
                **self.configuration.snapshot(request.agent),
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
        async with self._unit():
            row = await self.run_records.find_by_id(identifier(value), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.status != "queued":
                return False
            row.status = "preparing"
            row.data = {**row.data, "status": row.status, "updated_at": now()}
            await self.run_records.save(row)
            return True

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
