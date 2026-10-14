# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Capacity is reserved durably before the single native OAuth token dispatch."""

import asyncio
from contextvars import Context
from datetime import timedelta
from uuid import uuid4

import pytest
from oauth_capacity_test_support import fill_history, history_size
from pyfly.oauth2 import OAuth2Tokens
from sqlalchemy import func, select, text
from test_connection_oauth_flows import complete, local_provider, setup
from test_connection_oauth_ledger import actor, configuration, service

from firefly_forge.connection_oauth_models import OAuthDisposition
from firefly_forge.connection_oauth_persistence import (
    OAuthConnectionEntity,
    OAuthEffectEntity,
    OAuthGrantEntity,
)


@pytest.mark.parametrize("kind", ["code", "refresh"])
async def test_full_history_refuses_token_rotation_before_remote_dispatch(store, kind):
    async with local_provider() as (origin, forms, *_):
        vault, user, challenge, callback, _, _ = await setup(store, origin)
        if kind == "refresh":
            await complete(vault, user, challenge, callback)
        await fill_history(vault, store, 1024)
        await store.admit_credentials("An ordinary reviewed customer journey.")
        expected_requests = len(forms)
        async with store._unit() as session:
            grant = await session.scalar(select(OAuthGrantEntity))
            original = (grant.revision, grant.encrypted_data, grant.status) if grant else None
        with pytest.raises(ValueError, match="history|capacity"):
            if kind == "code":
                await complete(vault, user, challenge, callback)
            else:
                await vault.refresh("design", uuid4().hex, user)
        assert len(forms) == expected_requests
        assert await history_size(store) == 1024
        async with store._unit() as session:
            current = await session.scalar(select(OAuthGrantEntity))
            assert ((current.revision, current.encrypted_data, current.status) if current else None) == original


async def test_representation_capacity_denies_remote_dispatch_even_with_free_history_rows(store):
    async with local_provider() as (origin, forms, *_):
        vault, user, challenge, callback, _, _ = await setup(store, origin)
        await fill_history(vault, store, 512, representations=32)
        # 511 authenticated packets with32 entries + the native registration's1
        # leave31 entries, insufficient for the conservative grant reservation.
        with pytest.raises(ValueError, match="representations|capacity"):
            await complete(vault, user, challenge, callback)
        assert not forms
        assert await history_size(store) == 512


async def test_configuration_validates_new_representation_packet_before_publication(store):
    vault, user = service(store), actor()
    await vault.configure("design", configuration(public_client=True), user)
    await fill_history(vault, store, 513, representations=32)
    await store.admit_credentials("An ordinary reviewed customer journey.")
    with pytest.raises(ValueError, match="representations|capacity"):
        await vault.configure("extra", configuration(client_secret="owned-extra-secret"), user)
    assert await history_size(store) == 513


async def named_complete(vault, user, challenge, callback, name):
    return await vault.complete(
        name, callback["state"][0], callback["code"][0], challenge.browser_nonce, user, issuer=callback["iss"][0]
    )


async def test_two_replicas_and_connections_compete_for_the_same_last_evidence_slot(store, store_factory, tmp_path):
    async with local_provider(delay=True) as first, local_provider(delay=True, connection_name="other") as second:
        a, user_a, challenge_a, callback_a, key, _ = await setup(store, first[0])
        replica = await store_factory(tmp_path / "replica" / "factory.db")
        b, user_b, challenge_b, callback_b, _, _ = await setup(replica, second[0], connection_name="other", key=key)
        await fill_history(a, store, 1023)
        pending = [
            asyncio.create_task(complete(a, user_a, challenge_a, callback_a)),
            asyncio.create_task(named_complete(b, user_b, challenge_b, callback_b, "other")),
        ]
        try:
            done, _ = await asyncio.wait(pending, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            assert len(done) == 1
            assert isinstance(next(iter(done)).exception(), OAuthDisposition)
            async with asyncio.timeout(5):
                while len(first[1]) + len(second[1]) != 1:
                    await asyncio.sleep(0.01)
            async with store._unit() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(OAuthEffectEntity)
                        .where(OAuthEffectEntity.state == "executing")
                    )
                    == 1
                )
            assert await history_size(store) == 1023
        finally:
            first[3].set()
            second[3].set()
            outcomes = await asyncio.gather(*pending, return_exceptions=True)
        assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
        assert len(first[1]) + len(second[1]) == 1
        assert await history_size(store) == 1024
        async with store._unit() as session:
            effects = list(await session.scalars(select(OAuthEffectEntity)))
            assert len(effects) == 1 and effects[0].state == "completed"
            assert effects[0].data["history_reservation"]["rows"] == 1


@pytest.mark.parametrize("name", ["extra", "capacity-seed"])
async def test_configuration_cannot_consume_the_last_reserved_slot(store, name):
    async with local_provider(delay=True) as (origin, forms, entered, release, _):
        vault, user, challenge, callback, _, _ = await setup(store, origin)
        await fill_history(vault, store, 1023)
        pending = asyncio.create_task(complete(vault, user, challenge, callback))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(OAuthDisposition, match="capacity"):
                await vault.configure(name, configuration(public_client=True), user)
            assert await history_size(store) == 1023
            async with store._unit() as session:
                row = await session.scalar(select(OAuthConnectionEntity).where(OAuthConnectionEntity.name == name))
                assert row is None if name == "extra" else row.revision == 1022
        finally:
            release.set()
            result = await pending
        assert result["status"] == "authorized_unverified" and len(forms) == 1
        assert await history_size(store) == 1024


async def test_token_http_wait_holds_no_native_connection_vault_or_effect_lock(store, store_factory, tmp_path):
    async with local_provider(delay=True) as (origin, _, entered, release, _):
        vault, user, challenge, callback, _, _ = await setup(store, origin)
        replica = await store_factory(tmp_path / "observer" / "factory.db")
        pending = asyncio.create_task(complete(vault, user, challenge, callback))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with asyncio.timeout(2), replica._unit() as session:
                for suffix in ("credential-admission", "design"):
                    assert await session.scalar(
                        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"),
                        {"key": store.schema + ":connection-oauth:" + suffix},
                    )
                assert await session.scalar(select(OAuthConnectionEntity).with_for_update(nowait=True))
                assert await session.scalar(select(OAuthEffectEntity).with_for_update(nowait=True))
        finally:
            release.set()
            await pending


async def test_expired_legacy_effect_keeps_capacity_until_a_durable_closed_outcome(store):
    async with local_provider() as (origin, forms, *_):
        vault, user, challenge, callback, _, _ = await setup(store, origin)
        await fill_history(vault, store, 1023)
        async with store._unit() as session:
            connection = await session.scalar(
                select(OAuthConnectionEntity).where(OAuthConnectionEntity.name == "capacity-seed")
            )
            effect = OAuthEffectEntity(
                id=uuid4(),
                connection_id=connection.id,
                request_id=uuid4().hex,
                kind="code",
                state="executing",
                lease_expires_at=await session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1),
                data={"registration_revision": connection.revision, "actor_id": user.id.hex},
            )
            session.add(effect)
            effect_id = effect.id
        with pytest.raises(OAuthDisposition, match="capacity"):
            await complete(vault, user, challenge, callback)
        assert not forms
        async with store._unit() as session:
            retained = await session.get(OAuthEffectEntity, effect_id)
            assert retained.state == "executing" and "history_reservation" not in retained.data
            retained.state = "unknown"
        await complete(vault, user, challenge, callback)
        assert len(forms) == 1 and await history_size(store) == 1024


@pytest.mark.parametrize("phase", ["dispatch", "publication"])
async def test_legacy_effect_cannot_dispatch_or_publish_without_an_immutable_reservation(store, phase):
    vault, user = service(store), actor()
    await vault.configure("design", configuration(public_client=True), user)
    async with store._unit() as session:
        connection = await session.scalar(select(OAuthConnectionEntity))
        effect = OAuthEffectEntity(
            id=uuid4(),
            connection_id=connection.id,
            request_id=uuid4().hex,
            kind="code",
            state="executing",
            lease_expires_at=await session.scalar(select(func.clock_timestamp())) + timedelta(seconds=30),
            data={"registration_revision": 1, "actor_id": user.id.hex},
        )
        session.add(effect)
        effect_id = effect.id
    tokens = OAuth2Tokens("owned-legacy-token", "Bearer", expires_in=120, scope="read")
    with pytest.raises(OAuthDisposition, match="reservation") as failure:
        if phase == "dispatch":
            await vault._dispatch_fence("design", effect_id, user)
        else:
            await vault._publish("design", effect_id, tokens, user)
    await vault._retain_failure("design", effect_id, user, failure.value)
    async with store._unit() as session:
        retained = await session.get(OAuthEffectEntity, effect_id)
        assert retained.state == "unknown" and "history_reservation" not in retained.data
        assert await session.scalar(select(OAuthGrantEntity)) is None
    with pytest.raises(OAuthDisposition, match="changed"):
        await vault._publish("design", effect_id, tokens, user)
    assert await history_size(store) == 1


@pytest.mark.parametrize("change", ["boolean", "effect", "connection", "kind", "revision", "representations"])
async def test_reservation_requires_strict_exact_native_effect_context(store, change):
    vault, user = service(store), actor()
    await vault.configure("design", configuration(public_client=True), user)
    async with store._unit() as session:
        connection = await session.scalar(select(OAuthConnectionEntity))
        effect_id = uuid4()
        marker = {
            "version": 1,
            "effect_id": effect_id.hex,
            "connection_id": connection.id.hex,
            "kind": "code",
            "registration_revision": 1,
            "rows": 1,
            "representations": 32,
        }
        changes = {
            "boolean": {"rows": True},
            "effect": {"effect_id": uuid4().hex},
            "connection": {"connection_id": uuid4().hex},
            "kind": {"kind": "refresh"},
            "revision": {"registration_revision": 2},
            "representations": {"representations": 31},
        }
        session.add(
            OAuthEffectEntity(
                id=effect_id,
                connection_id=connection.id,
                request_id=uuid4().hex,
                kind="code",
                state="executing",
                lease_expires_at=await session.scalar(select(func.clock_timestamp())) + timedelta(seconds=30),
                data={
                    "registration_revision": 1,
                    "actor_id": user.id.hex,
                    "history_reservation": {**marker, **changes[change]},
                },
            )
        )
    with pytest.raises(OAuthDisposition, match="reservation"):
        await vault._dispatch_fence("design", effect_id, user)


@pytest.mark.parametrize("cancel", [False, True])
async def test_publication_converts_reservation_and_evidence_in_one_native_commit(
    store, store_factory, tmp_path, monkeypatch, cancel
):
    async with local_provider() as (origin, forms, _, _, observed):
        vault, user, challenge, callback, _, _ = await setup(store, origin)
        observer = await store_factory(tmp_path / "observer" / "factory.db")
        await fill_history(vault, store, 1023)
        original = vault._event
        inspected = []

        async def inspect_before_commit(session, row, kind, actor, **data):
            await original(session, row, kind, actor, **data)
            if kind != "effect_completed":
                return
            publisher_pid = await session.scalar(text("SELECT pg_backend_pid()"))

            async def observe_committed_state():
                async with observer._unit() as view:
                    assert await view.scalar(text("SELECT pg_backend_pid()")) != publisher_pid
                    assert await view.scalar(select(OAuthGrantEntity)) is None
                    retained = await view.scalar(select(OAuthEffectEntity))
                    assert retained.state == "executing" and "result" not in retained.data
                    inspected.append(retained.id)
                assert await history_size(observer) == 1023

            await asyncio.create_task(observe_committed_state(), context=Context())
            if cancel:
                raise asyncio.CancelledError

        monkeypatch.setattr(vault, "_event", inspect_before_commit)
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await complete(vault, user, challenge, callback)
        else:
            await complete(vault, user, challenge, callback)
        assert len(inspected) == 1 and len(forms) == 1
        async with observer._unit() as session:
            effect = await session.get(OAuthEffectEntity, inspected[0])
            assert effect.state == ("unknown" if cancel else "completed")
            assert ("result" in effect.data) == (not cancel)
            grant = await session.scalar(select(OAuthGrantEntity))
            assert (grant is None) == cancel
        assert await history_size(observer) == (1023 if cancel else 1024)
        with pytest.raises(ValueError, match="consumed"):
            await complete(vault, user, challenge, callback)
        if cancel:
            await vault.configure("extra", configuration(public_client=True), user)
            payload = observed[0]
            tokens = OAuth2Tokens(
                payload["access_token"],
                payload["token_type"],
                expires_in=payload["expires_in"],
                refresh_token=payload.get("refresh_token"),
                scope=payload.get("scope"),
            )
            with pytest.raises(OAuthDisposition, match="changed"):
                await vault._publish("design", inspected[0], tokens, user)
        assert len(forms) == 1 and await history_size(observer) == 1024


@pytest.mark.parametrize("kind", ["code", "refresh"])
async def test_new_token_effect_refuses_a_callers_ambient_native_unit(store, kind):
    async with local_provider() as (origin, forms, *_):
        vault, user, challenge, callback, _, _ = await setup(store, origin)
        if kind == "refresh":
            await complete(vault, user, challenge, callback)
        expected = len(forms)
        async with store._unit():
            with pytest.raises(OAuthDisposition, match="independent.*unit"):
                if kind == "code":
                    await complete(vault, user, challenge, callback)
                else:
                    await vault.refresh("design", uuid4().hex, user)
        assert len(forms) == expected


async def test_configuration_cannot_consume_reserved_representation_capacity(store):
    async with local_provider(delay=True) as (origin, forms, entered, release, _):
        vault, user, challenge, callback, _, _ = await setup(store, origin)
        await fill_history(vault, store, 512, representations=32, last_representations=31)
        pending = asyncio.create_task(complete(vault, user, challenge, callback))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(OAuthDisposition, match="representation_capacity"):
                await vault.configure("extra", configuration(client_secret="owned-extra-secret"), user)
            assert await history_size(store) == 512
        finally:
            release.set()
            await pending
        assert len(forms) == 1 and await history_size(store) == 513
        await vault.configure("extra", configuration(client_secret="owned-extra-secret"), user)
        assert await history_size(store) == 514


@pytest.mark.parametrize("state", ["completed", "unknown"])
async def test_legacy_closed_refresh_receipt_is_preserved_inside_an_ambient_unit(store, state):
    async with local_provider() as (origin, forms, *_):
        vault, user, challenge, callback, _, _ = await setup(store, origin)
        await complete(vault, user, challenge, callback)
        original_id = uuid4().hex
        result = await vault.refresh("design", original_id, user)
        request_id = uuid4().hex
        async with store._unit() as session:
            original = await session.scalar(
                select(OAuthEffectEntity).where(OAuthEffectEntity.request_id == original_id)
            )
            data = {key: value for key, value in original.data.items() if key != "history_reservation"}
            if state == "unknown":
                data = {key: value for key, value in data.items() if key != "result"}
            session.add(
                OAuthEffectEntity(
                    id=uuid4(),
                    connection_id=original.connection_id,
                    request_id=request_id,
                    kind="refresh",
                    state=state,
                    lease_expires_at=original.lease_expires_at,
                    data=data,
                )
            )
        async with store._unit():
            if state == "completed":
                assert await vault.refresh("design", request_id, user) == result
            else:
                with pytest.raises(OAuthDisposition, match="effect_unknown"):
                    await vault.refresh("design", request_id, user)
        assert len(forms) == 2
