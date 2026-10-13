# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Upgrading a populated legacy schema preserves response receipts and adds ownership fields."""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import text

from firefly_forge.assistant_persistence import (
    ForgeAssistantSessionEntity,
    ForgeAssistantSessionRepository,
    ForgeAssistantTurnEntity,
    ForgeAssistantTurnRepository,
)
from firefly_forge.migrations import SCHEMA_REVISION, migrate
from firefly_forge.models import RepositoryInput
from firefly_forge.store import identifier


async def test_assistant_claim_upgrade_preserves_legacy_receipt_and_is_idempotent(store, tmp_path):
    assert store.schema.startswith("forge_test_")
    repository = await store.add_repository(RepositoryInput(name="Legacy", url=str(tmp_path)))
    session_id, turn_id = uuid4(), uuid4()
    receipt = {"status": "completed", "reply": {"message": "Retained customer response"}, "context": {"history": []}}
    expected_digest = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    async with store._unit() as session:
        await ForgeAssistantSessionRepository().save(
            ForgeAssistantSessionEntity(id=session_id, repository_id=identifier(repository["id"]), data={})
        )
        await ForgeAssistantTurnRepository().save(
            ForgeAssistantTurnEntity(
                id=turn_id, session_id=session_id, request_id="a" * 32, status="completed", data=receipt
            )
        )
        # Reproduce revision 15 only in this fixture's disposable schema.
        for column in ("owner_id", "claim_epoch", "lease_expires_at", "last_heartbeat_at"):
            await session.execute(text(f"ALTER TABLE forge_assistant_turns DROP COLUMN IF EXISTS {column}"))
        await session.execute(
            text("UPDATE forge_schema_migrations SET revision=15 WHERE revision=:revision"),
            {"revision": SCHEMA_REVISION},
        )
    async with store._unit() as session:
        await migrate(session, store.schema)
        await migrate(session, store.schema)
        row = (
            (
                await session.execute(
                    text(
                        "SELECT data, status, owner_id, claim_epoch, lease_expires_at, last_heartbeat_at "
                        "FROM forge_assistant_turns WHERE id=:id"
                    ),
                    {"id": turn_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "completed" and row["data"] == receipt
        assert hashlib.sha256(json.dumps(row["data"], sort_keys=True).encode()).hexdigest() == expected_digest
        assert row["owner_id"] is None and row["claim_epoch"] == 0
        assert row["lease_expires_at"] is None and row["last_heartbeat_at"] is None
        assert (
            await session.scalar(
                text("SELECT count(*) FROM forge_schema_migrations WHERE revision=:revision"),
                {"revision": SCHEMA_REVISION},
            )
            == 1
        )
