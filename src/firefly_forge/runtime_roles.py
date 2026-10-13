# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Server-owned lifecycle roles; API admission never grants code execution."""

from __future__ import annotations

import hashlib
import os
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text

from firefly_forge.runtime_role_persistence import ForgeRuntimeTopologyEntity, ForgeRuntimeTopologyRepository
from firefly_forge.store import Store, now

Role = Literal["combined", "api", "worker"]
TOPOLOGY_ID = uuid5(NAMESPACE_URL, "firefly-forge:runtime-topology")


class RuntimeRoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Role = "combined"

    @classmethod
    def from_environment(cls) -> RuntimeRoleConfig:
        return cls.model_validate({"role": os.getenv("FORGE_SERVICE_ROLE", "combined")})


@service
class RuntimeRoles:
    phase = 20

    def __init__(
        self, store: Store, role: Role = "combined", records: ForgeRuntimeTopologyRepository | None = None
    ) -> None:
        self.store = store
        self.config = RuntimeRoleConfig(role=role)
        self.records = records or ForgeRuntimeTopologyRepository()
        self.started = False

    @property
    def role(self) -> Role:
        return self.config.role

    @property
    def executes_jobs(self) -> bool:
        return self.role != "api"

    def require_execution(self) -> None:
        if not self.executes_jobs:
            raise RuntimeError("An API role can admit work but cannot execute project or delivery effects")
        if not self.started:
            raise RuntimeError("Runtime topology must be admitted before execution")

    async def start(self) -> None:
        mode = "combined" if self.role == "combined" else "split"
        storage = hashlib.sha256(str(self.store.path.parent.resolve()).encode()).hexdigest()
        owner_uid = os.getuid() if hasattr(os, "getuid") else None
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "forge:runtime-topology:" + self.store.schema},
            )
            row = await self.records.find_by_id(TOPOLOGY_ID, lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                if mode == "split":
                    held = await session.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted "
                            "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
                            "AND objsubid=1 AND classid::bigint=((hashtextextended(:key,0)>>32)&4294967295) "
                            "AND objid::bigint=(hashtextextended(:key,0)&4294967295))"
                        ),
                        {"key": "forge:execution:" + self.store.schema},
                    )
                    if held:
                        raise RuntimeError("Stop the existing execution owner before choosing split topology")
                await self.records.save(
                    ForgeRuntimeTopologyEntity(
                        id=TOPOLOGY_ID,
                        mode=mode,
                        data={
                            "revision": 1,
                            "mode": mode,
                            "storage_binding": storage,
                            "owner_uid": owner_uid,
                            "created_at": now(),
                        },
                    )
                )
            else:
                if row.mode != mode:
                    raise RuntimeError("This schema has another immutable topology; change it only through maintenance")
                if row.data.get("storage_binding") != storage or row.data.get("owner_uid") != owner_uid:
                    raise RuntimeError("All runtime nodes must use the same private storage root")
        self.started = True

    async def stop(self) -> None:
        self.started = False

    def health(self, *, execution_state: str = "stopped", max_parallel_jobs: int = 0) -> dict[str, Any]:
        state = "api_only" if not self.executes_jobs else execution_state
        ready = self.started and self.executes_jobs and state == "ready"
        return {
            "service_role": self.role,
            "admission_ready": self.started,
            "execution_ready": ready,
            "execution": {
                "local_enabled": self.executes_jobs,
                "state": state,
                "owner_scope": "postgres_schema",
                "single_owner": True,
                "max_parallel_jobs": max_parallel_jobs if ready else 0,
                "remote_worker_state": "unknown",
            },
        }
