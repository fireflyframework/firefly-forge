# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Set-based, paged observations over existing native PyFly ledger entities."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import Repository
from sqlalchemy import BigInteger, ColumnElement, Numeric, Select, String, and_, case, cast, func, literal, select
from sqlalchemy.dialects.postgresql import BIT, JSONB

from firefly_forge.access_persistence import ForgeMembershipEntity
from firefly_forge.factory_dashboard_models import RUN_BUCKETS, DashboardScope
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentitySessionEntity, ForgeIdentityUserEntity
from firefly_forge.persistence import ForgePlanEntity, ForgeRepositoryEntity, ForgeRunEntity, ForgeTaskEntity
from firefly_forge.project_access import ROLE_PERMISSIONS
from firefly_forge.project_persistence import ForgeProjectEntity


def visible_projects_statement(
    *, administrator: bool, actor: Principal | None = None, allowed_projects: tuple[str, ...] | None = None
) -> Select[Any]:
    user_id = actor.id if actor else UUID(int=0)
    membership = (
        select(ForgeMembershipEntity.id)
        .where(
            ForgeMembershipEntity.project_id == ForgeProjectEntity.id,
            ForgeMembershipEntity.user_id == user_id,
            ForgeMembershipEntity.active.is_(True),
            ForgeMembershipEntity.role.in_(
                [role for role, permissions in ROLE_PERMISSIONS.items() if "read" in permissions]
            ),
        )
        .exists()
    )
    enabled = (
        select(ForgeIdentityUserEntity.id)
        .where(
            ForgeIdentityUserEntity.id == user_id,
            ForgeIdentityUserEntity.enabled.is_(True),
            ForgeIdentityUserEntity.issuer == (actor.issuer if actor else ""),
            ForgeIdentityUserEntity.subject == (actor.subject if actor else ""),
        )
        .exists()
    )
    statement = select(ForgeProjectEntity).where(enabled, literal(administrator) | membership)
    if allowed_projects is not None:
        statement = statement.where(ForgeProjectEntity.id.in_([UUID(hex=value) for value in allowed_projects]))
    return statement


def visible_runs_statement(
    scope: DashboardScope,
    *,
    administrator: bool,
    actor: Principal | None = None,
    allowed_projects: tuple[str, ...] | None = None,
) -> Select[Any]:
    visible = (
        visible_projects_statement(administrator=administrator, actor=actor, allowed_projects=allowed_projects)
        .with_only_columns(ForgeProjectEntity.id)
        .subquery()
    )
    allowed = (
        select(visible.c.id)
        .where(func.replace(cast(visible.c.id, String), "-", "") == ForgeRunEntity.data["project_id"].as_string())
        .exists()
    )
    statement = select(ForgeRunEntity).where(literal(administrator) | allowed)
    if scope.kind == "project":
        statement = statement.where(ForgeRunEntity.data["project_id"].as_string() == scope.project_id)
    elif scope.kind == "standalone":
        statement = statement.where(ForgeRunEntity.data["project_id"].as_string().is_(None), literal(administrator))
    elif scope.kind == "run":
        statement = statement.where(ForgeRunEntity.id == UUID(hex=scope.run_id))
    return statement


def bucket_expression(status: ColumnElement[Any]) -> ColumnElement[str]:
    return case(
        *[(status.in_(values), literal(name)) for name, values in RUN_BUCKETS.items()], else_=literal("unknown")
    )


def project_summary_columns() -> tuple[Any, ...]:
    project = ForgeProjectEntity
    raw_components = project.data["definition"]["components"]
    components = case((func.jsonb_typeof(raw_components) == "array", raw_components), else_=literal([], type_=JSONB))
    elements = func.jsonb_array_elements(components).table_valued("value").lateral()
    repositories = (
        select(func.count(func.distinct(cast(elements.c.value, JSONB)["repository_id"].as_string())))
        .select_from(elements)
        .scalar_subquery()
    )
    return (
        project.id,
        func.coalesce(project.data["definition"]["name"].as_string(), literal("Unnamed product project")).label("name"),
        func.jsonb_array_length(components).label("component_count"),
        repositories.label("repository_count"),
    )


def authorization_witness_statement(actor: Principal, session_hash: str | None = None) -> Select[Any]:
    """A fixed-size change detector, never an alternative source of grants."""
    user, membership, session = ForgeIdentityUserEntity, ForgeMembershipEntity, ForgeIdentitySessionEntity
    material = cast(
        func.jsonb_build_array(
            membership.id, membership.project_id, membership.role, membership.active, membership.revision
        ),
        String,
    )
    fingerprint = func.encode(func.sha256(func.convert_to(material, "UTF8")), "hex")
    # Four exact NUMERIC sums retain a 256-bit multiset fingerprint without
    # loading or concatenating the user's potentially numerous memberships.
    membership_state = (
        select(
            func.count().label("count"),
            func.coalesce(func.sum(membership.revision), 0).label("revision"),
            *[
                func.coalesce(
                    func.sum(
                        cast(
                            cast(cast(literal("x") + func.substr(fingerprint, offset, 16), BIT(64)), BigInteger),
                            Numeric,
                        )
                    ),
                    0,
                ).label(f"fingerprint_{offset}")
                for offset in (1, 17, 33, 49)
            ],
        )
        .where(membership.user_id == actor.id)
        .subquery()
    )
    session_state = (
        select(
            func.jsonb_build_array(
                session.id, session.status, session.revision, session.expires_at, session.access_expires_at
            )
        )
        .where(
            session.user_id == actor.id,
            session.cookie_hash == session_hash,
            session.status == "active",
            session.expires_at > func.clock_timestamp(),
            session.access_expires_at > func.clock_timestamp(),
        )
        .scalar_subquery()
        if session_hash is not None
        else literal(None)
    )
    statement = (
        select(
            user.id,
            user.issuer,
            user.subject,
            user.enabled,
            cast(user.api_roles, String),
            user.api_roles_audience,
            user.api_roles_issued_at,
            user.api_roles_expires_at,
            session_state.label("session_state"),
            *membership_state.c,
        )
        .select_from(user)
        .join(membership_state, literal(True))
        .where(user.id == actor.id, user.issuer == actor.issuer, user.subject == actor.subject, user.enabled.is_(True))
    )
    if actor.authentication_method in {"oidc_bearer", "oidc_session"}:
        statement = statement.where(
            user.api_roles_audience == actor.api_audience,
            user.api_roles_expires_at > func.clock_timestamp(),
        )
    if session_hash is not None:
        statement = statement.where(session_state.is_not(None))
    return statement


@repository
class FactoryDashboardRepository(Repository[ForgeRunEntity, UUID]):
    async def authorization_witness(self, actor: Principal, session_hash: str | None = None) -> tuple[Any, ...] | None:
        row = (
            await self._require_session().execute(authorization_witness_statement(actor, session_hash))
        ).one_or_none()
        return tuple(row) if row else None

    async def projects(
        self, actor: Principal, administrator: bool, *, after: str | None, search: str, limit: int
    ) -> list[dict[str, Any]]:
        project = ForgeProjectEntity
        statement = visible_projects_statement(administrator=administrator, actor=actor).with_only_columns(
            *project_summary_columns()
        )
        if search:
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            statement = statement.where(project.data["definition"]["name"].as_string().ilike(pattern, escape="\\"))
        if after:
            statement = statement.where(project.id < UUID(hex=after))
        values = (
            (await self._require_session().execute(statement.order_by(project.id.desc()).limit(limit + 1)))
            .mappings()
            .all()
        )
        return [dict(row) for row in values]

    async def project(
        self, actor: Principal, administrator: bool, project_id: str, *, allowed_projects: tuple[str, ...] | None = None
    ) -> dict[str, Any] | None:
        statement = (
            visible_projects_statement(administrator=administrator, actor=actor, allowed_projects=allowed_projects)
            .with_only_columns(*project_summary_columns())
            .where(ForgeProjectEntity.id == UUID(hex=project_id))
        )
        row = (await self._require_session().execute(statement)).mappings().one_or_none()
        return {**dict(row), "id": row["id"].hex} if row else None

    async def snapshot(
        self,
        actor: Principal,
        administrator: bool,
        scope: DashboardScope,
        *,
        after: int | None,
        limit: int,
        bucket: str | None,
        allowed_projects: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        session = self._require_session()
        scoped = visible_runs_statement(
            scope, administrator=administrator, actor=actor, allowed_projects=allowed_projects
        ).subquery()
        scope_project_id = (
            await session.scalar(select(scoped.c.data["project_id"].as_string()).limit(1))
            if scope.kind == "run"
            else None
        )
        category = bucket_expression(scoped.c.status)
        counts = {
            row.category: row.count
            for row in await session.execute(
                select(category.label("category"), func.count().label("count")).select_from(scoped).group_by(category)
            )
        }
        latest_plans = select(
            ForgePlanEntity.id,
            ForgePlanEntity.run_id,
            func.row_number()
            .over(partition_by=ForgePlanEntity.run_id, order_by=ForgePlanEntity.revision.desc())
            .label("position"),
        ).subquery()
        tasks = ForgeTaskEntity
        stage = case((tasks.task_id.like("publish%"), literal("publish")), else_=tasks.task_id)
        # The controller ledger lives in its separate private schema. A running task
        # is retained progress, not a per-run heartbeat; never infer current motion.
        task_state = case(
            (tasks.status.in_(("done", "succeeded")), literal("done")),
            (tasks.status.in_(("queued", "pending", "waiting")), literal("waiting")),
            (tasks.status == "skipped", literal("skipped")),
            (tasks.status == "failed", literal("failed")),
            (tasks.status.in_(("paused", "interrupted", "needs_reconciliation")), literal("needs_action")),
            else_=literal("unknown"),
        )
        stage_query = (
            select(
                stage.label("stage"),
                task_state.label("state"),
                func.count().label("count"),
                func.count().filter(tasks.status == "running").label("recorded_running"),
            )
            .select_from(tasks)
            .join(scoped, scoped.c.id == tasks.run_id)
            .join(latest_plans, and_(latest_plans.c.run_id == tasks.run_id, latest_plans.c.position == 1))
            .group_by(stage, task_state)
        )
        stages = [dict(row) for row in (await session.execute(stage_query)).mappings()]
        project = ForgeProjectEntity
        page = (
            select(
                scoped.c.id,
                scoped.c.sequence,
                scoped.c.repository_id,
                scoped.c.status,
                scoped.c.data["project_id"].as_string().label("project_id"),
                func.left(scoped.c.data["brief"].as_string(), 512).label("brief"),
                (func.length(scoped.c.data["brief"].as_string()) > 512).label("brief_truncated"),
                scoped.c.data["created_at"].as_string().label("created_at"),
                scoped.c.data["branch"].as_string().label("branch"),
                scoped.c.data["active_task"].as_string().label("active_task"),
                scoped.c.data["pr_url"].as_string().label("pr_url"),
                scoped.c.data["component_snapshot"]["id"].as_string().label("component_id"),
                scoped.c.data["component_snapshot"]["name"].as_string().label("component_name"),
                scoped.c.data["issue_context"]["url"].as_string().label("issue_url"),
                scoped.c.data["issue_context"]["number"].as_integer().label("issue_number"),
                scoped.c.data["issue_context"]["title"].as_string().label("issue_title"),
                project.data["definition"]["name"].as_string().label("project_name"),
                ForgeRepositoryEntity.data["name"].as_string().label("repository_name"),
            )
            .select_from(scoped)
            .outerjoin(
                project, func.replace(cast(project.id, String), "-", "") == scoped.c.data["project_id"].as_string()
            )
            .join(ForgeRepositoryEntity, ForgeRepositoryEntity.id == scoped.c.repository_id)
        )
        if after is not None:
            page = page.where(scoped.c.sequence < after)
        if bucket is not None:
            page = page.where(category == bucket)
        rows = [
            dict(row)
            for row in (await session.execute(page.order_by(scoped.c.sequence.desc()).limit(limit + 1))).mappings()
        ]
        projects = visible_projects_statement(
            administrator=administrator, actor=actor, allowed_projects=allowed_projects
        ).with_only_columns(ForgeProjectEntity.id)
        if scope.kind == "project":
            projects = projects.where(ForgeProjectEntity.id == UUID(hex=scope.project_id))
        elif scope.kind == "run":
            projects = projects.where(
                select(scoped.c.id)
                .correlate(ForgeProjectEntity)
                .where(
                    scoped.c.data["project_id"].as_string()
                    == func.replace(cast(ForgeProjectEntity.id, String), "-", "")
                )
                .exists()
            )
        elif scope.kind == "standalone":
            projects = projects.where(literal(False))
        project_count = select(func.count()).select_from(projects.subquery()).scalar_subquery()
        associations = (
            (
                await session.execute(
                    select(
                        project_count.label("projects"),
                        func.count().filter(scoped.c.data["project_id"].as_string().is_(None)).label("standalone_runs"),
                        func.count(func.distinct(scoped.c.repository_id)).label("repositories_with_runs"),
                    ).select_from(scoped)
                )
            )
            .mappings()
            .one()
        )
        return {
            "counts": counts,
            "stages": stages,
            "rows": rows,
            "associations": dict(associations),
            "scope_project_id": scope_project_id,
        }
