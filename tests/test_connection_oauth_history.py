# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PostgreSQL protects OAuth history and closed effects."""

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from test_connection_oauth_flows import complete, local_provider, setup
from test_connection_oauth_ledger import actor, configuration, service

from firefly_forge.connection_oauth_persistence import OAuthEffectEntity, OAuthEventEntity, OAuthRegistrationEntity


async def rejected_history_write(store, statement):
    with pytest.raises(DBAPIError, match="immutable|history"):
        async with store._unit() as session:
            await session.execute(statement)


@pytest.mark.parametrize("history", ["registration", "registration-delete", "event-update", "event-delete"])
@pytest.mark.asyncio
async def test_native_oauth_registration_and_event_history_rejects_mutation(store, history):
    svc, user = service(store), actor()
    await svc.configure("design", configuration(public_client=True), user)
    async with store._unit() as session:
        registration = await session.scalar(select(OAuthRegistrationEntity))
        if history == "registration":
            statement = (
                update(OAuthRegistrationEntity)
                .where(OAuthRegistrationEntity.id == registration.id)
                .values(data={**registration.data, "client_id": "rewritten-client"})
            )
        elif history == "registration-delete":
            statement = delete(OAuthRegistrationEntity)
        elif history == "event-update":
            statement = update(OAuthEventEntity).values(data={"kind": "rewritten-receipt"})
        else:
            statement = delete(OAuthEventEntity)
    await rejected_history_write(store, statement)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["state", "result", "request", "kind", "lease", "admission", "delete"])
async def test_completed_native_oauth_effect_cannot_be_rewritten(store, change):
    async with local_provider() as (origin, forms, *_):
        svc, user, challenge, callback, _, _ = await setup(store, origin)
        await complete(svc, user, challenge, callback)
        assert len(forms) == 1
        async with store._unit() as session:
            effect = await session.scalar(select(OAuthEffectEntity))
            assert effect.state == "completed"
            from datetime import timedelta

            values = {
                "state": {"state": "executing"},
                "result": {"data": {**effect.data, "result": {"agent_ready": True}}},
                "request": {"request_id": "a" * 32},
                "kind": {"kind": "refresh"},
                "lease": {"lease_expires_at": effect.lease_expires_at + timedelta(seconds=30)},
                "admission": {"data": {**effect.data, "actor_id": "a" * 32}},
            }
            statement = (
                delete(OAuthEffectEntity).where(OAuthEffectEntity.id == effect.id)
                if change == "delete"
                else update(OAuthEffectEntity).where(OAuthEffectEntity.id == effect.id).values(**values[change])
            )
        await rejected_history_write(store, statement)


@pytest.mark.asyncio
async def test_reserved_provider_catalog_name_is_rejected_without_registration_writes(store):
    from sqlalchemy import func

    from firefly_forge.connection_oauth_persistence import OAuthConnectionEntity

    svc = service(store)
    with pytest.raises(ValueError, match="name"):
        await svc.configure("providers", configuration(public_client=True), actor())
    async with store._unit() as session:
        assert await session.scalar(select(func.count()).select_from(OAuthConnectionEntity)) == 0
        assert await session.scalar(select(func.count()).select_from(OAuthRegistrationEntity)) == 0
        assert await session.scalar(select(func.count()).select_from(OAuthEventEntity)) == 0


@pytest.mark.parametrize("state", ["unknown", "rejected"])
@pytest.mark.asyncio
async def test_closed_uncertain_or_rejected_effect_cannot_reopen(store, state):
    import asyncio

    from pyfly.oauth2 import OAuth2ClientError

    async with local_provider(
        delay=state == "unknown", mutate=(lambda result: {"error": "invalid_grant"}) if state == "rejected" else None
    ) as (origin, forms, entered, _, _):
        svc, user, challenge, callback, _, _ = await setup(store, origin)
        if state == "unknown":
            pending = asyncio.create_task(complete(svc, user, challenge, callback))
            await asyncio.wait_for(entered.wait(), 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            with pytest.raises(OAuth2ClientError):
                await complete(svc, user, challenge, callback)
        async with store._unit() as session:
            effect = await session.scalar(select(OAuthEffectEntity))
            assert effect.state == state
            statement = update(OAuthEffectEntity).where(OAuthEffectEntity.id == effect.id).values(state="executing")
        await rejected_history_write(store, statement)
        assert len(forms) == 1


@pytest.mark.asyncio
async def test_reinitializing_schema21_keeps_registration_revisions_and_effect_controls(store):
    from firefly_forge.migrations import migrate

    svc, user = service(store), actor()
    first = await svc.configure("design", configuration(public_client=True), user)
    async with store._unit() as session:
        await migrate(session, store.schema)
    assert await svc.summary("design", user) == first
    second = await svc.configure("design", configuration(client_secret="fixture-replacement-secret"), user)
    assert second["revision"] == first["revision"] + 1
    async with store._unit() as session:
        rows = list(await session.scalars(select(OAuthRegistrationEntity).order_by(OAuthRegistrationEntity.revision)))
        assert [row.revision for row in rows] == [1, 2]
        assert rows[0].data == rows[1].data
