# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""All public OAuth protocol packets observe current native credential custody."""

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from test_connection_oauth_ledger import actor, configuration, service

from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.connection_oauth_persistence import (
    OAuthAttemptEntity,
    OAuthConnectionEntity,
    OAuthEffectEntity,
    OAuthEventEntity,
    OAuthGrantEntity,
)


async def pair(store, store_factory, tmp_path):
    key = Fernet.generate_key().decode()
    replica = await store_factory(tmp_path / "replica" / "factory.db")
    return service(store, key), service(replica, key)


@pytest.mark.parametrize("operation", ["prepare", "begin", "disconnect"])
async def test_public_protocol_response_rejects_later_credentials_and_rolls_back_local_changes(
    store, store_factory, tmp_path, operation
):
    reader, writer = await pair(store, store_factory, tmp_path)
    user = actor()
    await reader.configure("design", configuration(public_client=True), user)
    ticket = await reader.prepare("design", user) if operation == "begin" else None
    credential = "disconnected" if operation == "disconnect" else "design"
    await writer.configure("legacy", configuration(client_secret=credential), user)
    async with store._unit() as session:
        before_attempts = len(list(await session.scalars(select(OAuthAttemptEntity))))
        before_events = len(list(await session.scalars(select(OAuthEventEntity))))
    with pytest.raises(ValueError, match="credentials"):
        if operation == "begin":
            await reader.begin("design", ticket["ticket"], user, browser_authenticated=True)
        else:
            await getattr(reader, operation)("design", user)
    async with store._unit() as session:
        row = await session.scalar(select(OAuthConnectionEntity).where(OAuthConnectionEntity.name == "design"))
        assert row.status == "authorization_required"
        attempts = list(await session.scalars(select(OAuthAttemptEntity)))
        assert len(attempts) == before_attempts and all(not entry.consumed for entry in attempts)
        assert len(list(await session.scalars(select(OAuthEventEntity)))) == before_events


async def local_pair(store, store_factory, tmp_path, origin):
    from test_connection_oauth_flows import setup

    reader, user, challenge, callback, key, policy = await setup(store, origin)
    replica = await store_factory(tmp_path / "replica" / "factory.db")
    writer = ConnectionOAuth(
        replica,
        encryption_key=key,
        browser_origin="https://forge.example",
        catalog={"fixture": policy},
        allow_loopback_http=True,
    )
    return reader, writer, user, challenge, callback


async def test_completed_refresh_replay_observation_is_admitted_without_provider_replay(store, store_factory, tmp_path):
    from test_connection_oauth_flows import complete, local_provider

    async with local_provider() as (origin, forms, *_):
        reader, writer, user, challenge, callback = await local_pair(store, store_factory, tmp_path, origin)
        await complete(reader, user, challenge, callback)
        request_id = uuid4().hex
        await reader.refresh("design", request_id, user)
        assert len(forms) == 2
        await writer.configure("legacy", configuration(client_secret="authorized_unverified"), user)
        with pytest.raises(ValueError, match="credentials"):
            await reader.refresh("design", request_id, user)
        assert len(forms) == 2
        async with store._unit() as session:
            retained = await session.scalar(select(OAuthEffectEntity).where(OAuthEffectEntity.request_id == request_id))
            assert retained.state == "completed"


async def test_token_exchange_publication_rejects_foreign_credentials_and_retains_unknown_without_replay(
    store, store_factory, tmp_path
):
    from test_connection_oauth_flows import complete, local_provider

    async with local_provider() as (origin, forms, *_):
        reader, writer, user, challenge, callback = await local_pair(store, store_factory, tmp_path, origin)
        await writer.configure("legacy", configuration(client_secret="authorized_unverified"), user)
        with pytest.raises(ValueError, match="credentials"):
            await complete(reader, user, challenge, callback)
        assert len(forms) == 1
        async with store._unit() as session:
            effect = await session.scalar(select(OAuthEffectEntity))
            assert effect.state == "unknown"
            assert list(await session.scalars(select(OAuthGrantEntity))) == []
        with pytest.raises(ValueError, match="consumed|effect"):
            await complete(reader, user, challenge, callback)
        assert len(forms) == 1
