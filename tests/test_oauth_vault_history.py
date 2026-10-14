# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native history cannot silently lose authority, source binding or retired evidence."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_connection_oauth_ledger import actor, configuration, service

from firefly_forge.connection_oauth_persistence import OAuthCredentialEvidenceEntity, OAuthGrantEntity


@pytest.mark.parametrize(
    "operation",
    ["UPDATE forge_oauth_credential_evidence SET data='{}'::jsonb", "DELETE FROM forge_oauth_credential_evidence"],
)
async def test_native_evidence_history_is_immutable(store, operation):
    vault = service(store)
    await vault.configure("design", configuration(client_secret="owned-long-credential"), actor())
    with pytest.raises(DBAPIError, match="immutable"):
        async with store._unit() as session:
            await session.execute(text(operation))


async def test_store_without_current_vault_authority_denies_populated_history(store, store_factory, tmp_path):
    vault = service(store)
    await vault.configure("design", configuration(client_secret="owned-long-credential"), actor())
    replica = await store_factory(tmp_path / "no-vault" / "factory.db")
    with pytest.raises(ValueError, match="service is unavailable"):
        await replica.admit_credentials("A public project instruction.")


async def test_relocated_row_context_cannot_authorize_admission(store):
    vault = service(store)
    await vault.configure("design", configuration(client_secret="owned-long-credential"), actor())
    async with store._unit() as session:
        original = await session.scalar(select(OAuthCredentialEvidenceEntity))
        assert original is not None
        session.add(
            OAuthCredentialEvidenceEntity(
                id=uuid4(),
                connection_id=original.connection_id,
                kind="grant",
                source_id=uuid4(),
                revision=1,
                data=original.data,
            )
        )
    with pytest.raises(ValueError, match="source context"):
        await store.admit_credentials("A public project instruction.")


async def test_changed_shared_key_cannot_silently_read_current_evidence(store):
    vault = service(store)
    await vault.configure("design", configuration(client_secret="owned-long-credential"), actor())
    service(store)
    with pytest.raises(ValueError, match="cannot be authenticated"):
        await store.admit_credentials("A public project instruction.")


async def test_legacy_erased_grant_history_is_not_fabricated_by_startup(store):
    vault, user = service(store), actor()
    await vault.configure("design", configuration(client_secret="owned-long-credential"), user)
    async with vault._locked("design") as session:
        row = await vault._required("design")
        grant = OAuthGrantEntity(
            id=uuid4(),
            connection_id=row.id,
            registration_revision=row.revision,
            family=uuid4().hex,
            revision=2,
            actor_id=user.id,
            status="revoked",
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
            data={"scopes": ["read"]},
        )
        grant.encrypted_data = vault._seal(vault._grant_context(row, grant), {})
        session.add(grant)
    await vault.start()
    async with store._unit() as session:
        entries = list(await session.scalars(select(OAuthCredentialEvidenceEntity)))
        assert all(entry.kind != "grant" for entry in entries)
    with pytest.raises(ValueError, match="history is incomplete"):
        await store.admit_credentials("A public project instruction.")
