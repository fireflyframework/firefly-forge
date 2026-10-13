# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One immutable topology choice per PostgreSQL control-plane schema."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeRuntimeTopologyEntity(BaseEntity):
    __tablename__ = "forge_runtime_topology"
    mode: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeRuntimeTopologyRepository(Repository[ForgeRuntimeTopologyEntity, UUID]):
    pass
