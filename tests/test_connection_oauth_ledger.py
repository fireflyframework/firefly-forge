# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text

from firefly_forge.identity import Principal
from firefly_forge.migrations import SCHEMA_REVISION


def module():
    from firefly_forge import connection_oauth

    return connection_oauth


def actor():
    return Principal(uuid4(), "local", "operator", authentication_method="local_bootstrap", kind="local_bootstrap")


def provider():
    return module().OAuthProvider(
        id="fixture",
        issuer="https://provider.example",
        authorization_endpoint="https://provider.example/authorize",
        token_endpoint="https://provider.example/token",
        resource="https://provider.example/mcp",
        scopes=("read",),
    )


def service(store, key=None):
    return module().ConnectionOAuth(
        store,
        encryption_key=key or Fernet.generate_key().decode(),
        browser_origin="https://forge.example",
        catalog={"fixture": provider()},
    )


def configuration(**overrides):
    from firefly_forge.connection_oauth_models import OAuthConnectionInput

    return OAuthConnectionInput(provider="fixture", client_id="client", **overrides)


async def test_native_schema_additive_revision_and_encrypted_registration(store):
    svc, user = service(store), actor()
    result = await svc.configure("design", configuration(client_secret="private-secret"), user)
    assert result["revision"] == 1 and result["status"] == "authorization_required"
    async with store._unit() as session:
        assert await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations")) == SCHEMA_REVISION
        from firefly_forge.connection_oauth_persistence import OAuthRegistrationEntity

        row = await session.scalar(select(OAuthRegistrationEntity))
        assert "private-secret" not in row.encrypted_secret
        assert "private-secret" not in json.dumps(row.data)
    assert "private-secret" not in json.dumps(await svc.summary("design", user))


async def test_identical_registration_preserves_secret_but_new_client_requires_explicit_secret_choice(store):
    svc, user = service(store), actor()
    await svc.configure("design", configuration(client_secret="private-secret"), user)
    unchanged = await svc.configure("design", configuration(), user)
    assert unchanged["revision"] == 1
    from firefly_forge.connection_oauth_models import OAuthConnectionInput

    with pytest.raises(ValueError, match="secret choice"):
        await svc.configure("design", OAuthConnectionInput(provider="fixture", client_id="different"), user)
    changed = await svc.configure(
        "design", OAuthConnectionInput(provider="fixture", client_id="different", public_client=True), user
    )
    assert changed["revision"] == 2


async def test_ciphertext_cannot_move_between_schema_connection_revision_or_purpose(store, store_factory, tmp_path):
    key = Fernet.generate_key().decode()
    svc, user = service(store, key), actor()
    await svc.configure("first", configuration(client_secret="private-secret"), user)
    await svc.configure("second", configuration(client_secret="other-secret"), user)
    from firefly_forge.connection_oauth_persistence import OAuthRegistrationEntity

    async with store._unit() as session:
        rows = list(await session.scalars(select(OAuthRegistrationEntity).order_by(OAuthRegistrationEntity.created_at)))
        encoded = rows[0].encrypted_secret
        context = svc._client_context(rows[1].connection_id, rows[1].revision, rows[1].data)
    with pytest.raises(ValueError, match="context"):
        svc._open(encoded, context)


async def test_bearer_can_prepare_but_cannot_bind_browser_consent(store):
    svc, user = service(store), actor()
    await svc.configure("design", configuration(public_client=True), user)
    handoff = await svc.prepare("design", user)
    with pytest.raises(ValueError, match="browser"):
        await svc.begin("design", handoff["ticket"], user, browser_authenticated=False)
    challenge = await svc.begin("design", handoff["ticket"], user, browser_authenticated=True)
    assert "code_challenge_method=S256" in challenge.authorization_url
    with pytest.raises(ValueError, match="consumed"):
        await svc.begin("design", handoff["ticket"], user, browser_authenticated=True)


async def test_handoff_is_actor_bound_and_current_disabled_identity_is_denied(store):
    svc, user = service(store), actor()
    await svc.configure("design", configuration(public_client=True), user)
    handoff = await svc.prepare("design", user)
    with pytest.raises(ValueError, match="actor"):
        await svc.begin("design", handoff["ticket"], actor(), browser_authenticated=True)
    from firefly_forge.identity_persistence import ForgeIdentityUserEntity

    async with store._unit() as session:
        session.add(ForgeIdentityUserEntity(id=user.id, issuer=user.issuer, subject=user.subject, enabled=False))
    with pytest.raises(PermissionError):
        await svc.begin("design", handoff["ticket"], user, browser_authenticated=True)


async def test_configuration_change_invalidates_prepared_attempt_and_local_disconnect(store):
    svc, user = service(store), actor()
    await svc.configure("design", configuration(public_client=True), user)
    handoff = await svc.prepare("design", user)
    await svc.disconnect("design", user)
    with pytest.raises(ValueError, match="revision|disconnected"):
        await svc.begin("design", handoff["ticket"], user, browser_authenticated=True)


async def test_registration_cipher_is_bound_to_client_and_exact_policy(store):
    svc, user = service(store), actor()
    await svc.configure("design", configuration(client_secret="private-secret"), user)
    from firefly_forge.connection_oauth_persistence import OAuthRegistrationEntity

    async with store._unit() as session:
        row = await session.scalar(select(OAuthRegistrationEntity))
        altered_context = svc._client_context(row.connection_id, row.revision, {**row.data, "client_id": "different"})
        encoded = row.encrypted_secret
    with pytest.raises(ValueError, match="context"):
        svc._open(encoded, altered_context)


async def test_project_scope_must_exist_and_is_frozen_in_registration(store):
    svc, user = service(store), actor()
    from firefly_forge.connection_oauth_models import OAuthConnectionInput
    from firefly_forge.project_persistence import ForgeProjectEntity

    project_id = uuid4()
    request = OAuthConnectionInput(
        provider="fixture", client_id="client", public_client=True, project_id=project_id.hex
    )
    with pytest.raises(KeyError, match="project"):
        await svc.configure("design", request, user)
    async with store._unit() as session:
        session.add(ForgeProjectEntity(id=project_id, data={"id": project_id.hex}))
    assert (await svc.configure("design", request, user))["project_id"] == project_id.hex


async def test_unconfigured_optional_oauth_keeps_factory_available_but_cannot_authorize(store):
    svc = module().ConnectionOAuth(store, encryption_key=Fernet.generate_key().decode(), browser_origin="")
    assert svc.providers()[0]["status"] == "prerequisite_required"
    with pytest.raises(ValueError, match="origin|prerequisites"):
        await svc.configure("design", configuration(public_client=True), actor())


async def test_restart_hydrates_secret_redaction_without_async_calls_in_file_lock_callbacks(store):
    key = Fernet.generate_key().decode()
    first, user = service(store, key), actor()
    await first.configure("design", configuration(client_secret="restart-private-secret"), user)
    restarted = service(store, key)
    await restarted.start()
    assert "restart-private-secret" in restarted.redaction_settings().values()
