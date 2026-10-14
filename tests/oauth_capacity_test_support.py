# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated native fixture seeding; these rows are not remote publications."""

from uuid import uuid4

from sqlalchemy import func, select

from firefly_forge.connection_oauth_persistence import (
    OAuthConnectionEntity,
    OAuthCredentialEvidenceEntity,
    OAuthRegistrationEntity,
)


async def fill_history(vault, store, target, *, representations=1, last_representations=None):
    async with store._unit() as session:
        existing = await session.scalar(select(func.count()).select_from(OAuthCredentialEvidenceEntity))
        template = await session.scalar(select(OAuthRegistrationEntity))
        count = target - existing
        assert count > 0
        auxiliary = OAuthConnectionEntity(
            id=uuid4(), name="capacity-seed", revision=count, status="authorization_required"
        )
        session.add(auxiliary)
        await session.flush()
        data = {**template.data, "callback": vault.browser_origin + "/api/v1/connection-oauth/capacity-seed/callback"}
        for revision in range(1, count + 1):
            source_id, evidence_id = uuid4(), uuid4()
            size = last_representations if revision == count and last_representations is not None else representations
            values = [f"owned-capacity-{revision}-{index}" for index in range(size)]
            session.add(
                OAuthRegistrationEntity(
                    id=source_id,
                    connection_id=auxiliary.id,
                    revision=revision,
                    data=data,
                    encrypted_secret=vault._seal(
                        vault._client_context(auxiliary.id, revision, data), {"secret": values[0]}
                    ),
                )
            )
            session.add(
                OAuthCredentialEvidenceEntity(
                    id=evidence_id,
                    connection_id=auxiliary.id,
                    kind="registration",
                    source_id=source_id,
                    revision=revision,
                    data=vault.matcher.packet(
                        evidence_id.hex, "registration", source_id.hex, revision, values, auxiliary.id.hex
                    ),
                )
            )


async def history_size(store):
    async with store._unit() as session:
        return await session.scalar(select(func.count()).select_from(OAuthCredentialEvidenceEntity))
