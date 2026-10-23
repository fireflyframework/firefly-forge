# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PyFly entities and typed repositories for the PostgreSQL ledger."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    and_,
    case,
    cast,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeRepositoryEntity(BaseEntity):
    __tablename__ = "forge_repositories"
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeRunEntity(BaseEntity):
    __tablename__ = "forge_runs"
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeEventEntity(BaseEntity):
    __tablename__ = "forge_events"
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeDeploymentEntity(BaseEntity):
    __tablename__ = "forge_deployments"
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), index=True)
    target_id: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    __table_args__ = (
        Index(
            "uq_forge_active_deployment_target",
            "target_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )


@repository
class ForgeRepositoryRepository(Repository[ForgeRepositoryEntity, UUID]):
    async def find_latest(self) -> list[ForgeRepositoryEntity]:
        result = await self._require_session().scalars(
            select(ForgeRepositoryEntity).order_by(ForgeRepositoryEntity.sequence.desc())
        )
        return list(result)


@repository
class ForgeRunRepository(Repository[ForgeRunEntity, UUID]):
    async def find_latest(self) -> list[ForgeRunEntity]:
        result = await self._require_session().scalars(
            select(ForgeRunEntity).order_by(ForgeRunEntity.sequence.desc()).limit(500)
        )
        return list(result)

    async def find_native_worker_questions(self) -> list[ForgeRunEntity]:
        from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity

        request = ForgeRunEntity.data["worker_request"]
        notification = ForgeWebhookInboxEntity.data["native_question"]
        reserved = (
            select(ForgeWebhookInboxEntity.id)
            .where(
                notification["run_id"].as_string() == ForgeRunEntity.data["id"].as_string(),
                notification["question_id"].as_string() == request["id"].as_string(),
            )
            .exists()
        )
        return list(
            await self._require_session().scalars(
                select(ForgeRunEntity)
                .where(
                    ForgeRunEntity.status == "paused",
                    ForgeRunEntity.data["active_task"].as_string() == "implement",
                    request["status"].as_string() == "pending",
                    request["role_id"].as_string() == "implementer",
                    ~reserved,
                )
                .order_by(ForgeRunEntity.sequence)
                .limit(50)
            )
        )

    async def find_queued(self) -> list[ForgeRunEntity]:
        result = await self._require_session().scalars(
            select(ForgeRunEntity).where(ForgeRunEntity.status == "queued").order_by(ForgeRunEntity.sequence)
        )
        return list(result)

    async def find_verification_handoffs(self, limit: int = 100) -> list[ForgeRunEntity]:
        state = ForgeRunEntity.data["verification_handoff"]
        request = ForgeRunEntity.data["worker_request"]
        snapshot = ForgeRunEntity.data["tool_policy_snapshot"]
        adoption = ForgeRunEntity.data["verification_adoption"]
        statement = (
            select(ForgeRunEntity)
            .where(
                ForgeRunEntity.status.in_(["paused", "needs_reconciliation"]),
                request["kind"].as_string() == "verification",
                or_(
                    func.coalesce(state["request_id"].as_string(), "") != request["id"].as_string(),
                    func.coalesce(state["status"].as_string(), "") != "held",
                ),
                func.coalesce(ForgeRunEntity.data["verification_refusal"]["request_id"].as_string(), "")
                != request["id"].as_string(),
                or_(
                    and_(
                        request["status"].as_string() == "answered",
                        state["status"].as_string() == "completed",
                        request["response_source"].as_string() == "factory_verification",
                    ),
                    and_(
                        request["status"].as_string() == "pending",
                        request["task_id"].as_string() == "implement",
                        request["attempt"].as_integer() < 4,
                        or_(
                            snapshot["settings"]["automatic_verification"]["enabled"].as_boolean().is_(True),
                            adoption["policy_snapshot"]["settings"]["automatic_verification"]["enabled"]
                            .as_boolean()
                            .is_(True),
                        ),
                    ),
                ),
            )
            .order_by(ForgeRunEntity.sequence)
            .limit(limit)
        )
        return list(await self._require_session().scalars(statement))

    async def find_ready_retries(self, current_time: float, limit: int = 100) -> list[ForgeRunEntity]:
        statement = (
            select(ForgeRunEntity)
            .where(
                ForgeRunEntity.status == "paused",
                ForgeRunEntity.data["pause"]["automatic"].as_boolean().is_(True),
                ForgeRunEntity.data["pause"]["retry_at"].as_float() <= current_time,
                func.coalesce(ForgeRunEntity.data["retry_count"].as_integer(), 0) < 3,
            )
            .order_by(ForgeRunEntity.sequence)
            .limit(limit)
        )
        return list(await self._require_session().scalars(statement))

    async def find_inflight(self) -> list[ForgeRunEntity]:
        result = await self._require_session().scalars(
            select(ForgeRunEntity)
            .where(
                ForgeRunEntity.status.not_in(
                    [
                        "queued",
                        "campaign_blocked",
                        "paused",
                        "needs_reconciliation",
                        "succeeded",
                        "failed",
                        "cancelled",
                        "interrupted",
                    ]
                )
            )
            .order_by(ForgeRunEntity.sequence)
            .with_for_update()
        )
        return list(result)


@repository
class ForgeEventRepository(Repository[ForgeEventEntity, UUID]):
    async def provider_budget_events(self, identifier: UUID) -> list[ForgeEventEntity]:
        from firefly_forge.activity_models import MAX_EVENTS, MAX_RUN_BYTES

        entity = ForgeEventEntity
        predicates = (entity.run_id == identifier, entity.data["stage"].as_string().in_(("stdout", "stderr")))
        session = self._require_session()
        count, size = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(func.octet_length(cast(entity.data, Text))), 0)).where(
                    *predicates
                )
            )
        ).one()
        if type(count) is not int or type(size) is not int or count > MAX_EVENTS or size > MAX_RUN_BYTES:
            raise ValueError("CLI command history exceeds the archive limit")
        return list(await session.scalars(select(entity).where(*predicates).order_by(entity.sequence)))

    async def find_for_run(self, identifier: UUID) -> list[ForgeEventEntity]:
        result = await self._require_session().scalars(
            select(ForgeEventEntity).where(ForgeEventEntity.run_id == identifier).order_by(ForgeEventEntity.sequence)
        )
        return list(result)


@repository
class ForgeDeploymentRepository(Repository[ForgeDeploymentEntity, UUID]):
    async def find_latest(self, run_id: UUID | None = None) -> list[ForgeDeploymentEntity]:
        statement = select(ForgeDeploymentEntity).order_by(ForgeDeploymentEntity.sequence.desc()).limit(500)
        if run_id is not None:
            statement = statement.where(ForgeDeploymentEntity.run_id == run_id)
        return list(await self._require_session().scalars(statement))

    async def find_active_target(self, target: str) -> ForgeDeploymentEntity | None:
        return await self._require_session().scalar(
            select(ForgeDeploymentEntity).where(
                ForgeDeploymentEntity.target_id == target, ForgeDeploymentEntity.status.in_(["queued", "running"])
            )
        )

    async def find_queued(self, limit: int = 100) -> list[ForgeDeploymentEntity]:
        statement = (
            select(ForgeDeploymentEntity)
            .where(ForgeDeploymentEntity.status == "queued")
            .order_by(ForgeDeploymentEntity.sequence)
            .limit(limit)
        )
        return list(await self._require_session().scalars(statement))

    async def find_running(self) -> list[ForgeDeploymentEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeDeploymentEntity)
                .where(ForgeDeploymentEntity.status == "running")
                .order_by(ForgeDeploymentEntity.sequence)
                .with_for_update()
            )
        )


class ForgePlanEntity(BaseEntity):
    __tablename__ = "forge_plans"
    __table_args__ = (UniqueConstraint("run_id", "revision"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), index=True)
    revision: Mapped[int] = mapped_column()
    digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeTaskEntity(BaseEntity):
    __tablename__ = "forge_tasks"
    __table_args__ = (UniqueConstraint("run_id", "task_id"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), index=True)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("forge_plans.id"))
    task_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(default=1)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ForgeJournalEntity(BaseEntity):
    __tablename__ = "forge_journal"
    __table_args__ = (UniqueConstraint("run_id", "sequence"), UniqueConstraint("run_id", "idempotency_key", "kind"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    task_id: Mapped[str] = mapped_column(String(100))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    digest: Mapped[str] = mapped_column(String(64))
    previous_hash: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgePlanRepository(Repository[ForgePlanEntity, UUID]):
    async def find_current(self, run_id: UUID) -> ForgePlanEntity | None:
        return await self._require_session().scalar(
            select(ForgePlanEntity)
            .where(ForgePlanEntity.run_id == run_id)
            .order_by(ForgePlanEntity.revision.desc())
            .limit(1)
        )


@repository
class ForgeTaskRepository(Repository[ForgeTaskEntity, UUID]):
    async def find_for_run(self, run_id: UUID) -> list[ForgeTaskEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeTaskEntity)
                .where(ForgeTaskEntity.run_id == run_id)
                .order_by(ForgeTaskEntity.created_at, ForgeTaskEntity.task_id)
                .execution_options(populate_existing=True)
            )
        )

    async def find_pending_recovery(self) -> list[ForgeTaskEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeTaskEntity).where(ForgeTaskEntity.status == "running").order_by(ForgeTaskEntity.run_id)
            )
        )


@repository
class ForgeJournalRepository(Repository[ForgeJournalEntity, UUID]):
    async def bridge_budget(self, run_id: UUID) -> dict[str, int]:
        row = (
            await self._require_session().execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(func.octet_length(cast(ForgeJournalEntity.data, Text))), 0),
                    func.coalesce(func.sum(case((ForgeJournalEntity.kind == "bridge_request_intent", 1), else_=0)), 0),
                    func.coalesce(
                        func.max(
                            case(
                                (
                                    ForgeJournalEntity.kind == "bridge_request_intent",
                                    func.octet_length(cast(ForgeJournalEntity.data, Text)),
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(case((ForgeJournalEntity.kind == "bridge_request_response", 1), else_=0)), 0
                    ),
                    func.coalesce(func.sum(case((ForgeJournalEntity.kind == "agent_checkpoint", 1), else_=0)), 0),
                    func.coalesce(func.sum(case((ForgeJournalEntity.kind == "agent_consume", 1), else_=0)), 0),
                ).where(ForgeJournalEntity.run_id == run_id)
            )
        ).one()
        return {
            "rows": int(row[0]),
            "bytes": int(row[1]),
            "intents": int(row[2]),
            "max_intent_bytes": int(row[3]),
            "responses": int(row[4]),
            "checkpoints": int(row[5]),
            "checkpoint_consumes": int(row[6]),
        }

    async def checkpoint_budget(self, run_id: UUID) -> dict[str, int]:
        size = func.octet_length(cast(ForgeJournalEntity.data, Text))
        row = (
            await self._require_session().execute(
                select(
                    func.coalesce(func.sum(case((ForgeJournalEntity.kind == "agent_checkpoint", 1), else_=0)), 0),
                    func.coalesce(func.sum(case((ForgeJournalEntity.kind == "agent_consume", 1), else_=0)), 0),
                    func.coalesce(func.sum(size), 0),
                    func.coalesce(func.max(size), 0),
                ).where(
                    ForgeJournalEntity.run_id == run_id,
                    ForgeJournalEntity.kind.in_(["agent_checkpoint", "agent_consume"]),
                )
            )
        ).one()
        return dict(zip(("count", "consumes", "bytes", "max_bytes"), (int(value) for value in row), strict=True))

    async def checkpoint_record(self, run_id: UUID, key: str, kind: str) -> ForgeJournalEntity | None:
        if kind not in {"agent_checkpoint", "agent_consume"}:
            raise ValueError("Unsupported checkpoint record kind")
        predicate = (
            ForgeJournalEntity.run_id == run_id,
            ForgeJournalEntity.idempotency_key == key,
            ForgeJournalEntity.kind == kind,
        )
        size = await self._require_session().scalar(
            select(func.octet_length(cast(ForgeJournalEntity.data, Text))).where(*predicate)
        )
        if size is None:
            return None
        if size > (4096 if kind == "agent_consume" else 262144):
            raise ValueError("Agent checkpoint record exceeds its bounded read")
        return await self._require_session().scalar(select(ForgeJournalEntity).where(*predicate))

    async def bridge_record(self, run_id: UUID, key: str, kind: str) -> ForgeJournalEntity | None:
        predicate = (
            ForgeJournalEntity.run_id == run_id,
            ForgeJournalEntity.idempotency_key == key,
            ForgeJournalEntity.kind == kind,
        )
        size = await self._require_session().scalar(
            select(func.octet_length(cast(ForgeJournalEntity.data, Text))).where(*predicate)
        )
        if size is None:
            return None
        if size > 532480:
            raise ValueError("Bridge journal record exceeds its bounded read")
        return await self._require_session().scalar(select(ForgeJournalEntity).where(*predicate))

    async def bridge_intents(self, run_id: UUID, after: int, limit: int) -> list[dict[str, Any]]:
        rows = await self._require_session().execute(
            select(
                ForgeJournalEntity.sequence,
                ForgeJournalEntity.data,
                ForgeJournalEntity.run_id,
                ForgeJournalEntity.task_id,
                ForgeJournalEntity.idempotency_key,
                ForgeJournalEntity.kind,
                ForgeJournalEntity.digest,
            )
            .where(
                ForgeJournalEntity.run_id == run_id,
                ForgeJournalEntity.kind == "bridge_request_intent",
                ForgeJournalEntity.sequence > after,
            )
            .order_by(ForgeJournalEntity.sequence)
            .limit(limit)
        )
        return [
            {
                "sequence": row[0],
                "data": row[1],
                "run_id": row[2].hex,
                "task_id": row[3],
                "idempotency_key": row[4],
                "kind": row[5],
                "digest": row[6],
            }
            for row in rows
        ]

    async def checkpoint_candidates(self, run_id: UUID) -> list[str]:
        return list(
            await self._require_session().scalars(
                select(ForgeJournalEntity.idempotency_key)
                .where(ForgeJournalEntity.run_id == run_id, ForgeJournalEntity.kind == "agent_checkpoint")
                .order_by(ForgeJournalEntity.sequence.desc())
                .limit(16)
            )
        )

    async def checkpoint_observations(self, run_id: UUID, completion_id: str) -> list[str]:
        return list(
            await self._require_session().scalars(
                select(ForgeJournalEntity.idempotency_key)
                .where(
                    ForgeJournalEntity.run_id == run_id,
                    ForgeJournalEntity.kind == "bridge_request_observation",
                    ForgeJournalEntity.data["payload"]["request_id"].astext == completion_id,
                )
                .order_by(ForgeJournalEntity.sequence.desc())
                .limit(1)
            )
        )

    async def checkpoint_restore_tickets(self, run_id: UUID) -> list[dict[str, str]]:
        from firefly_forge.execution_request_persistence import ForgeExecutionRequestEntity as Ticket

        session = self._require_session()
        budget = (
            await session.execute(
                select(func.count(), func.coalesce(func.max(func.octet_length(cast(Ticket.data, Text))), 0)).where(
                    Ticket.run_id == run_id, Ticket.kind == "agent_restore"
                )
            )
        ).one()
        if int(budget[0]) > 50 or int(budget[1]) > 65536:
            raise ValueError("Checkpoint restore tickets exceed their bounded read")
        rows = await session.execute(
            select(Ticket.id, Ticket.request_key, Ticket.status)
            .where(Ticket.run_id == run_id, Ticket.kind == "agent_restore")
            .limit(50)
        )
        return [{"id": row[0].hex, "completion_id": row[1], "status": row[2]} for row in rows]

    async def find_question_context(self, run_id: UUID, request_id: str) -> list[ForgeJournalEntity]:
        """Read exact typed question ancestry without loading unrelated provider/tool output."""
        if not isinstance(request_id, str) or not request_id or len(request_id) > 100:
            raise ValueError("Select one bounded native worker question identity")
        event = ForgeJournalEntity
        payload = event.data["payload"]
        matching = and_(
            event.run_id == run_id,
            or_(
                and_(event.kind == "worker_feedback", payload["request"]["id"].astext == request_id),
                and_(event.kind == "worker_response", payload["request_id"].astext == request_id),
                and_(
                    event.kind.in_(("resume", "handoff")),
                    payload["human_clarification"]["request_id"].astext == request_id,
                ),
            ),
        )
        budget = (
            select(
                func.count().label("rows"),
                func.coalesce(func.sum(func.octet_length(cast(event.data, Text))), 0).label("bytes"),
            )
            .where(matching)
            .cte("question_budget")
        )
        # One statement snapshot prevents a concurrent append from invalidating a prior byte probe.
        rows = (
            await self._require_session().execute(
                select(budget.c.rows, budget.c.bytes, event)
                .select_from(
                    budget.outerjoin(event, and_(matching, budget.c.rows <= 2000, budget.c.bytes <= 4 * 1024 * 1024))
                )
                .order_by(event.sequence)
                .limit(2001)
            )
        ).all()
        if not rows or int(rows[0][0]) > 2000 or int(rows[0][1]) > 4 * 1024 * 1024:
            raise ValueError("The selected question ancestry exceeds its bounded native lookup")
        return [row[2] for row in rows if row[2] is not None]

    async def find_for_run(self, run_id: UUID) -> list[ForgeJournalEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeJournalEntity)
                .where(ForgeJournalEntity.run_id == run_id)
                .order_by(ForgeJournalEntity.sequence)
            )
        )
