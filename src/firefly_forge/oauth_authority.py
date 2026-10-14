# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current native administrator authority for OAuth control-plane changes."""

from datetime import datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity
from firefly_forge.project_access import AccessDenied, ProjectAccess


class OAuthAccessDenied(PermissionError, AccessDenied):
    pass


async def require_oauth_administrator(session: AsyncSession, actor: Principal) -> None:
    row = await session.scalar(
        select(ForgeIdentityUserEntity).where(ForgeIdentityUserEntity.id == actor.id).with_for_update()
    )
    if actor.authentication_method == "local_bootstrap":
        if row is None or row.enabled and row.issuer == actor.issuer and row.subject == actor.subject:
            return
    elif (
        row is not None
        and row.enabled
        and row.issuer == actor.issuer
        and row.subject == actor.subject
        and ProjectAccess.is_admin(actor)
        and bool(actor.api_audience)
        and actor.api_audience == row.api_roles_audience
        and "forge-admin" in (row.api_roles or ())
        and row.api_roles_issued_at is not None
        and row.api_roles_expires_at is not None
        and row.api_roles_expires_at > cast(datetime, await session.scalar(select(func.clock_timestamp())))
    ):
        return
    raise OAuthAccessDenied("Current factory administrator authority is required")
