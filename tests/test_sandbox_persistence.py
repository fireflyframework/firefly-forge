# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import uuid

import pytest
from pyfly.kernel.exceptions import DuplicateKeyException

from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.sandbox_persistence import ForgeSandboxEntity, ForgeSandboxRepository
from firefly_forge.store import identifier


async def test_runner_intent_slot_is_unique_and_scoped_to_a_real_run(store, tmp_path):
    repository = await store.add_repository(RepositoryInput(name="Sandbox fixture", url=str(tmp_path)))
    run = await store.create_run(RunInput(repository_id=repository["id"], brief="Run isolated checks"))
    records = ForgeSandboxRepository()
    execution_id = uuid.uuid4()
    data = {"id": execution_id.hex, "status": "intent", "snapshot_digest": "a" * 64}
    async with store._unit():
        await records.save(
            ForgeSandboxEntity(
                id=execution_id,
                run_id=identifier(run["id"]),
                task_id="verify",
                parent_attempt=1,
                ordinal=1,
                container_name="forge-sandbox-" + execution_id.hex,
                status="intent",
                snapshot_digest="a" * 64,
                data=data,
            )
        )
        row = await records.slot(identifier(run["id"]), "verify", 1, 1)
        assert row is not None and row.data == data
        assert await records.slot(identifier(run["id"]), "verify", 1, 2) is None
    with pytest.raises(DuplicateKeyException):
        async with store._unit():
            await records.save(
                ForgeSandboxEntity(
                    run_id=identifier(run["id"]),
                    task_id="verify",
                    parent_attempt=1,
                    ordinal=1,
                    container_name="different-name",
                    status="intent",
                    snapshot_digest="b" * 64,
                    data={},
                )
            )
    async with store._unit():
        assert len(await records.for_run(identifier(run["id"]))) == 1
