# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Factory-local, expiring Engine-stage observations; never execution authority."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from typing import cast as typing_cast
from uuid import UUID, uuid4

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    and_,
    cast,
    column,
    func,
    literal,
    or_,
    select,
    table,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, mapped_column

from firefly_forge.access_persistence import ForgeMembershipEntity
from firefly_forge.identity_persistence import ForgeIdentityUserEntity
from firefly_forge.persistence import ForgeJournalEntity, ForgePlanEntity, ForgeRunEntity, ForgeTaskEntity

SCOPE_FIELDS = (
    "project_id",
    "component_snapshot",
    "execution_actor",
    "sandbox_snapshot",
    "policy_snapshot",
    "role_snapshot",
    "work_duration",
    "config_revision",
    "knowledge_context_digest",
    "agent",
    "assurance",
    "commands",
    "tool_policy_snapshot",
    "mcp_snapshot",
    "attempt",
)


class ForgeEngineActivityEntity(BaseEntity):
    __tablename__ = "forge_engine_activity"
    __table_args__ = (UniqueConstraint("run_id", "task_id"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("forge_identity_users.id"), index=True)
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("forge_projects.id"), nullable=True, index=True)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("forge_plans.id"))
    current_plan_id: Mapped[UUID] = mapped_column(ForeignKey("forge_plans.id"))
    plan_digest: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str] = mapped_column(String(100))
    attempt: Mapped[int] = mapped_column()
    intent_digest: Mapped[str] = mapped_column(String(64))
    invocation_key: Mapped[str] = mapped_column(String(64))
    owner_epoch: Mapped[str] = mapped_column(String(32))
    owner_pid: Mapped[int] = mapped_column()
    owner_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


def scope_expression(run: Any = ForgeRunEntity) -> Any:
    return func.jsonb_build_object(
        "repository_id",
        func.replace(cast(run.repository_id, String), "-", ""),
        *[part for key in SCOPE_FIELDS for part in (key, run.data[key])],
    )


def factory_fence_predicate(schema: str, pid: Any, started: Any) -> Any:
    """A session lock is necessary, but cannot by itself prove any run is active."""
    backend = table("pg_stat_activity", column("pid"), column("backend_start"), schema="pg_catalog")
    locks = table(
        "pg_locks",
        *(column(key) for key in ("pid", "locktype", "granted", "objsubid", "classid", "objid")),
        schema="pg_catalog",
    )
    key = func.hashtextextended("forge:execution:" + schema, 0)
    held = (
        select(literal(1))
        .select_from(locks)
        .where(
            locks.c.pid == pid,
            locks.c.locktype == "advisory",
            locks.c.granted.is_(True),
            locks.c.objsubid == 1,
            cast(locks.c.classid, String) == cast(key.op(">>")(32).op("&")(4294967295), String),
            cast(locks.c.objid, String) == cast(key.op("&")(4294967295), String),
        )
        .correlate_except(locks)
        .exists()
    )
    return (
        select(literal(1))
        .select_from(backend)
        .where(
            backend.c.pid == pid,
            backend.c.backend_start == started,
            held,
        )
        .correlate_except(backend)
        .exists()
    )


def actor_predicate(run: Any = ForgeRunEntity, activity: Any = ForgeEngineActivityEntity) -> Any:
    """Only the explicitly supported canonical local/OIDC actor paths are observed."""
    user, membership = ForgeIdentityUserEntity, ForgeMembershipEntity
    actor = run.data["execution_actor"]
    method = actor["authentication_method"].as_string()
    admin = and_(
        actor["global_admin"].as_boolean().is_(True),
        actor["api_audience"].as_string() == user.api_roles_audience,
        user.api_roles_issued_at.is_not(None),
        user.api_roles_expires_at > func.clock_timestamp(),
        user.api_roles.contains(["forge-admin"]),
    )
    member = (
        select(membership.id)
        .where(
            membership.project_id == activity.project_id,
            membership.user_id == user.id,
            membership.active.is_(True),
            membership.role.in_(("owner", "maintainer", "developer")),
        )
        .correlate_except(membership)
        .exists()
    )
    isolated = and_(
        run.data["agent"].as_string().not_in(("codex", "claude")),
        func.coalesce(run.data["assurance"]["review_agent"].as_string(), "").not_in(("codex", "claude")),
        func.coalesce(run.data["sandbox_snapshot"]["mode"].as_string(), "") != "host",
    )
    return (
        select(user.id)
        .where(
            func.replace(cast(user.id, String), "-", "") == actor["user_id"].as_string(),
            user.id == activity.actor_id,
            user.enabled.is_(True),
            or_(
                method == "local_bootstrap",
                and_(method.in_(("oidc_session", "oidc_bearer")), or_(admin, and_(member, isolated))),
            ),
        )
        .correlate_except(user)
        .exists()
    )


def current_activity_predicate(
    schema: str,
    run: Any = ForgeRunEntity,
    task: Any = ForgeTaskEntity,
    activity: Any = ForgeEngineActivityEntity,
) -> Any:
    latest = (
        select(func.max(ForgePlanEntity.revision))
        .where(ForgePlanEntity.run_id == run.id)
        .correlate_except(ForgePlanEntity)
        .scalar_subquery()
    )
    plan = (
        select(ForgePlanEntity.id)
        .where(
            ForgePlanEntity.run_id == run.id,
            ForgePlanEntity.id == activity.current_plan_id,
            ForgePlanEntity.digest == activity.plan_digest,
            ForgePlanEntity.revision == latest,
        )
        .correlate_except(ForgePlanEntity)
        .exists()
    )
    return and_(
        activity.run_id == run.id,
        task.run_id == run.id,
        activity.task_id == task.task_id,
        activity.plan_id == task.plan_id,
        activity.attempt == task.attempt,
        activity.invocation_key == task.data["idempotency_key"].as_string(),
        activity.status == "active",
        activity.expires_at > func.clock_timestamp(),
        task.status == "running",
        run.data["active_task"].as_string() == task.task_id,
        run.status.in_(("preparing", "developing", "validating", "running")),
        func.coalesce(run.data["cancel_request"]["status"].as_string(), "") != "requested",
        activity.scope == scope_expression(run),
        func.coalesce(func.replace(cast(activity.project_id, String), "-", ""), "")
        == func.coalesce(run.data["project_id"].as_string(), ""),
        plan,
        actor_predicate(run, activity),
        select(ForgeJournalEntity.id)
        .where(
            ForgeJournalEntity.run_id == run.id,
            ForgeJournalEntity.task_id == task.task_id,
            ForgeJournalEntity.idempotency_key == activity.invocation_key,
            ForgeJournalEntity.kind == "intent",
            ForgeJournalEntity.digest == activity.intent_digest,
        )
        .correlate_except(ForgeJournalEntity)
        .exists(),
        factory_fence_predicate(schema, activity.owner_pid, activity.owner_started_at),
    )


@repository
class ForgeEngineActivityRepository(Repository[ForgeEngineActivityEntity, UUID]):
    async def begin(self, values: dict[str, Any], seconds: float, schema: str) -> UUID | None:
        current = ForgeEngineActivityEntity
        stamp = func.clock_timestamp()
        statement = insert(current).from_select(
            ["id", *values, "status", "observed_at", "expires_at", "created_at", "updated_at"],
            select(
                literal(uuid4()),
                *[literal(value, type_=current.__table__.c[key].type) for key, value in values.items()],
                literal("active"),
                stamp,
                stamp + timedelta(seconds=seconds),
                stamp,
                stamp,
            )
            .select_from(ForgeRunEntity)
            .join(ForgeTaskEntity, ForgeTaskEntity.run_id == ForgeRunEntity.id)
            .where(
                ForgeRunEntity.id == values["run_id"],
                current_activity_predicate(
                    schema,
                    activity=SimpleNamespace(
                        **{key: literal(value, type_=current.__table__.c[key].type) for key, value in values.items()},
                        status=literal("active"),
                        expires_at=stamp + timedelta(seconds=seconds),
                    ),
                ),
            ),
        )
        returning = statement.on_conflict_do_update(
            index_elements=[current.run_id, current.task_id],
            set_={key: statement.excluded[key] for key in (*values, "id", "status", "observed_at", "expires_at")},
            where=or_(current.owner_epoch != values["owner_epoch"], current.invocation_key != values["invocation_key"]),
        ).returning(current.id)
        return typing_cast(UUID | None, await self._require_session().scalar(returning))

    async def renew(self, identifiers: list[UUID], epoch: str, seconds: float, schema: str) -> list[UUID]:
        row = ForgeEngineActivityEntity
        stamp = func.clock_timestamp()
        return list(
            await self._require_session().scalars(
                update(row)
                .where(
                    row.id.in_(identifiers),
                    row.owner_epoch == epoch,
                    select(ForgeRunEntity.id)
                    .join(ForgeTaskEntity, ForgeTaskEntity.run_id == ForgeRunEntity.id)
                    .where(current_activity_predicate(schema))
                    .correlate(row)
                    .exists(),
                )
                .values(observed_at=stamp, expires_at=stamp + timedelta(seconds=seconds))
                .returning(row.id)
            )
        )

    async def end(self, identifiers: list[UUID], epoch: str, schema: str) -> None:
        row = ForgeEngineActivityEntity
        await self._require_session().execute(
            update(row)
            .where(
                row.id.in_(identifiers),
                row.owner_epoch == epoch,
                row.status == "active",
                factory_fence_predicate(schema, row.owner_pid, row.owner_started_at),
            )
            .values(status="ended", expires_at=func.clock_timestamp())
        )
