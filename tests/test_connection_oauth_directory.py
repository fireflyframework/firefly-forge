# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Server-owned OAuth directory and explicit uncertain-revision recovery."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from test_connection_oauth_ledger import actor, configuration, service

from firefly_forge.connection_oauth_persistence import OAuthConnectionEntity, OAuthEffectEntity, OAuthEventEntity


async def effect(store, *, state="unknown", expired=False):
    async with store._unit() as session:
        connection = await session.scalar(select(OAuthConnectionEntity))
        now = await session.scalar(text("SELECT clock_timestamp()"))
        row = OAuthEffectEntity(
            id=uuid4(),
            connection_id=connection.id,
            request_id=uuid4().hex,
            kind="refresh",
            state=state,
            lease_expires_at=now + timedelta(seconds=-1 if expired else 300),
            data={"registration_revision": connection.revision},
        )
        session.add(row)
        return row.id.hex


async def test_native_directory_lists_safe_server_owned_connection_summaries(store):
    vault, user = service(store), actor()
    await vault.configure("design", configuration(client_secret="owned-directory-secret"), user)
    result = await vault.directory(user)
    assert [entry["name"] for entry in result] == ["design"]
    item = result[0]
    assert item["authentication"] == "client_secret_post"
    assert item["secret_configured"] and not item["public_client"]
    assert item["callback_uri"] == "https://forge.example/api/v1/connection-oauth/design/callback"
    assert item["effect_state"] == "none" and not item["reconciliation_required"]
    assert item["allowed_actions"] == {
        "configure": True,
        "authorize": True,
        "refresh": False,
        "disconnect": True,
        "supersede": False,
    }
    assert "owned-directory-secret" not in str(result)
    assert not {"access_token", "refresh_token", "client_secret", "code", "ticket"}.intersection(item)


async def test_directory_rechecks_current_native_administrator_and_project_scope(store):
    from firefly_forge.identity import Principal

    vault = service(store)
    await vault.configure("design", configuration(public_client=True), actor())
    outsider = Principal(uuid4(), "https://id.example", "unprivileged")
    with pytest.raises(PermissionError, match="administrator"):
        await vault.directory(outsider)
    with pytest.raises(KeyError, match="project"):
        await vault.directory(actor(), project_id=uuid4().hex)


@pytest.mark.parametrize("state", ["executing", "unknown"])
async def test_ordinary_configuration_cannot_silently_supersede_an_unresolved_effect(store, state):
    vault, user = service(store), actor()
    await vault.configure("design", configuration(public_client=True), user)
    await effect(store, state=state)
    with pytest.raises(ValueError, match="effect"):
        await vault.configure("design", configuration(public_client=True), user)
    assert (await vault.summary("design", user))["revision"] == 1


@pytest.mark.parametrize("state,expired", [("unknown", False), ("executing", False), ("executing", True)])
async def test_summary_and_browser_handoff_share_current_effect_fences(store, state, expired):
    vault, user = service(store), actor()
    await vault.configure("design", configuration(public_client=True), user)
    handoff = await vault.prepare("design", user)
    identifier = await effect(store, state=state, expired=expired)
    summary = await vault.summary("design", user)
    expected = "unknown" if state == "unknown" or expired else "executing"
    assert summary["effect_state"] == expected
    assert summary["unresolved_effects"][0]["id"] == identifier
    assert not summary["allowed_actions"]["authorize"]
    assert summary["allowed_actions"]["supersede"] == (expected == "unknown")
    with pytest.raises(ValueError, match="effect"):
        await vault.prepare("design", user)
    with pytest.raises(ValueError, match="effect"):
        await vault.begin("design", handoff["ticket"], user, browser_authenticated=True)


async def test_explicit_supersession_keeps_exact_unknown_history_and_audits_new_admission(store):
    from firefly_forge import connection_oauth_models

    vault, user = service(store), actor()
    await vault.configure("design", configuration(public_client=True), user)
    unknown = await effect(store)
    request = connection_oauth_models.OAuthSupersedeInput(
        expected_revision=1,
        unknown_effect_ids=[unknown],
        reason="Start a new admission while the interrupted provider outcome stays unknown.",
        acknowledge_unknown=True,
        registration=configuration(public_client=True),
    )
    result = await vault.supersede("design", request, user)
    assert result["revision"] == 2 and result["effect_state"] == "none"
    assert result["archived_unknown_effects"][0]["id"] == unknown
    assert result["allowed_actions"]["authorize"]
    with pytest.raises(ValueError, match="revision|effect"):
        await vault.supersede("design", request, user)
    async with store._unit() as session:
        retained = await session.get(OAuthEffectEntity, UUID(unknown))
        assert retained.state == "unknown"
        events = list(await session.scalars(select(OAuthEventEntity)))
        receipt = next(row.data for row in events if row.data["kind"] == "uncertain_revision_superseded")
        assert receipt["unknown_effect_ids"] == [unknown] and receipt["previous_revision"] == 1


async def test_exact_effect_receipt_resolves_a_lost_response_without_replaying_provider_or_claiming_fresh_actions(
    store,
):
    from test_connection_oauth_flows import complete, local_provider, setup

    async with local_provider() as (origin, forms, *_):
        vault, user, challenge, callback, _, _ = await setup(store, origin)
        await complete(vault, user, challenge, callback)
        async with store._unit() as session:
            recorded = await session.scalar(select(OAuthEffectEntity))
            request_id = recorded.request_id
        result = await vault.effect_receipt("design", request_id, user)
        assert result["state"] == "completed" and result["kind"] == "code"
        assert result["request_id"] == request_id and result["current_context"]
        assert result["observed_at"]
        assert not {"result", "allowed_actions", "access_token", "refresh_token", "code", "ticket"}.intersection(result)
        assert len(forms) == 1
        await vault.configure("other", configuration(public_client=True), user)
        with pytest.raises(KeyError, match="effect"):
            await vault.effect_receipt("other", request_id, user)
        with pytest.raises(ValueError, match="request"):
            await vault.effect_receipt("design", "../private", user)


@pytest.mark.parametrize(
    "overrides", [{"expected_revision": True}, {"expected_revision": "1"}, {"acknowledge_unknown": 1}]
)
def test_recovery_requires_integer_revision_and_an_explicit_boolean_acknowledgment(overrides):
    from pydantic import ValidationError

    from firefly_forge.connection_oauth_models import OAuthSupersedeInput

    with pytest.raises(ValidationError):
        OAuthSupersedeInput.model_validate(
            {
                "expected_revision": 1,
                "unknown_effect_ids": ["a" * 32],
                "reason": "Start a separately admitted authorization.",
                "acknowledge_unknown": True,
                "registration": {"provider": "fixture", "client_id": "client", "public_client": True},
                **overrides,
            }
        )


@pytest.mark.parametrize("failure", ["stale_revision", "wrong_effect", "live_effect"])
async def test_explicit_supersession_rejects_stale_wrong_or_live_effects_without_mutation(store, failure):
    from firefly_forge.connection_oauth_models import OAuthSupersedeInput

    vault, user = service(store), actor()
    await vault.configure("design", configuration(public_client=True), user)
    unknown = await effect(store)
    if failure == "live_effect":
        await effect(store, state="executing")
    request = OAuthSupersedeInput(
        expected_revision=2 if failure == "stale_revision" else 1,
        unknown_effect_ids=[uuid4().hex if failure == "wrong_effect" else unknown],
        reason="Start a separately admitted authorization.",
        acknowledge_unknown=True,
        registration=configuration(public_client=True),
    )
    with pytest.raises(ValueError, match="revision|effect"):
        await vault.supersede("design", request, user)
    assert (await vault.summary("design", user))["revision"] == 1
    async with store._unit() as session:
        events = list(await session.scalars(select(OAuthEventEntity)))
        assert all(event.data["kind"] != "uncertain_revision_superseded" for event in events)


@pytest.mark.parametrize("credential", ["false", "true", "null", "authorization_required"])
async def test_public_registration_rejects_prospective_response_credentials_before_any_publication(store, credential):
    from firefly_forge.connection_oauth_persistence import OAuthCredentialEvidenceEntity, OAuthRegistrationEntity

    vault = service(store)
    with pytest.raises(ValueError, match="credentials"):
        await vault.configure("design", configuration(client_secret=credential), actor())
    async with store._unit() as session:
        for entity in (OAuthConnectionEntity, OAuthRegistrationEntity, OAuthCredentialEvidenceEntity, OAuthEventEntity):
            assert list(await session.scalars(select(entity))) == []
    assert credential not in vault.redaction_settings().values()


@pytest.mark.parametrize("read", ["directory", "summary", "effect"])
async def test_retained_oauth_public_packets_are_admitted_against_later_native_credentials(
    store, store_factory, tmp_path, read
):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    writer = service(store, key)
    replica = await store_factory(tmp_path / "replica" / "factory.db")
    reader = service(replica, key)
    user = actor()
    await reader.configure("design", configuration(public_client=True), user)
    identifier = await effect(store)
    async with store._unit() as session:
        retained = await session.get(OAuthEffectEntity, UUID(identifier))
        request_id = retained.request_id
    # Legacy encrypted publication is deliberate: public registration now refuses
    # credentials that collide with its own public status/action packet.
    await writer._configure("legacy", configuration(client_secret="false"), user)
    with pytest.raises(ValueError, match="credentials"):
        if read == "directory":
            await reader.directory(user)
        elif read == "summary":
            await reader.summary("design", user)
        else:
            await reader.effect_receipt("design", request_id, user)


async def test_supersession_response_admission_precedes_archival_revision_events_and_secret_cache(store):
    from firefly_forge.connection_oauth_models import OAuthSupersedeInput
    from firefly_forge.connection_oauth_persistence import OAuthCredentialEvidenceEntity, OAuthRegistrationEntity

    vault, user = service(store), actor()
    await vault.configure("design", configuration(public_client=True), user)
    unknown = await effect(store, state="executing", expired=True)
    request = OAuthSupersedeInput(
        expected_revision=1,
        unknown_effect_ids=[unknown],
        reason="Start an explicitly separate authorization.",
        acknowledge_unknown=True,
        registration=configuration(client_secret="false"),
    )
    with pytest.raises(ValueError, match="credentials"):
        await vault.supersede("design", request, user)
    async with store._unit() as session:
        connection = await session.scalar(select(OAuthConnectionEntity))
        assert connection.revision == 1 and connection.status == "authorization_required"
        retained = await session.get(OAuthEffectEntity, UUID(unknown))
        assert retained.state == "executing"
        assert len(list(await session.scalars(select(OAuthRegistrationEntity)))) == 1
        assert len(list(await session.scalars(select(OAuthCredentialEvidenceEntity)))) == 1
        events = list(await session.scalars(select(OAuthEventEntity)))
        assert [event.data["kind"] for event in events] == ["registration_configured"]
    assert "false" not in vault.redaction_settings().values()


async def test_recovery_reason_cannot_publish_the_new_write_only_secret(store):
    from firefly_forge.connection_oauth_models import OAuthSupersedeInput

    vault, user = service(store), actor()
    await vault.configure("design", configuration(public_client=True), user)
    unknown = await effect(store)
    secret = "owned-new-recovery-credential"
    request = OAuthSupersedeInput(
        expected_revision=1,
        unknown_effect_ids=[unknown],
        reason="Recover using " + secret,
        acknowledge_unknown=True,
        registration=configuration(client_secret=secret),
    )
    with pytest.raises(ValueError, match="credentials"):
        await vault.supersede("design", request, user)
    assert (await vault.summary("design", user))["revision"] == 1
    assert secret not in vault.redaction_settings().values()
