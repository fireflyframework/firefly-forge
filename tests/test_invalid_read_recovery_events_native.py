# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared only: actual JSONB custody in the root-owned fresh PostgreSQL cohort."""

import copy
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from firefly_forge.execution_request_persistence import (
    ForgeExecutionRequestEntity,
    ForgeExecutionRequestEventEntity,
    ForgeExecutionRequestEventRepository,
    ForgeExecutionRequestRepository,
)
from firefly_forge.execution_requests import digest
from firefly_forge.models import RepositoryInput, RunInput


async def test_native_jsonb_event_custody_is_logical_scoped_complete_and_bounded(store, monkeypatch):
    from firefly_forge.invalid_read_recovery_events import read_recovery_events

    repository = await store.add_repository(
        RepositoryInput(name="Fictional native event custody", url="https://example.invalid/event-custody.git")
    )
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"], brief="Read fictional native request receipts", agent="bridge", skills=[]
        )
    )
    request_rows = ForgeExecutionRequestRepository()
    event_rows = ForgeExecutionRequestEventRepository()

    async def seed(kinds, *, content="Fictional café Ω 😀", corrupt=None):
        request_id = uuid4().hex
        values, previous = [], "0" * 64
        for sequence, kind in enumerate(kinds, 1):
            value = dict(
                request_id=request_id,
                sequence=sequence,
                previous_hash=previous,
                kind=kind,
                evidence={"observation": content},
                created_at="2026-10-27T00:00:00+00:00",
            )
            if corrupt == "request":
                value["request_id"] = uuid4().hex
            elif corrupt == "sequence":
                value["sequence"] = True
            value["digest"] = previous = digest(value)
            values.append(value)
        async with store._unit() as session:
            await request_rows.save(
                ForgeExecutionRequestEntity(
                    id=UUID(request_id),
                    run_id=UUID(run["id"]),
                    kind="invalid_read",
                    request_key=request_id,
                    status="failed",
                    data={"id": request_id, "run_id": run["id"], "kind": "invalid_read", "status": "failed"},
                )
            )
            session.add_all(
                [
                    ForgeExecutionRequestEventEntity(
                        id=uuid4(), request_id=UUID(request_id), sequence=index, data=copy.deepcopy(value)
                    )
                    for index, value in enumerate(values, 1)
                ]
            )
            await session.flush()
        return request_id, values

    # Repetition makes physical JSONB storage measurably smaller than logical UTF-8 JSON.
    target, values = await seed(("request_admitted", "worker_failed"), content="Fictional café Ω 😀 " * 4096)
    other, _ = await seed(("request_admitted",))
    expected_bytes = sum(len(json.dumps(value, ensure_ascii=False).encode("utf-8")) for value in values)
    assert expected_bytes != sum(len(json.dumps(value).encode("utf-8")) for value in values)
    async with store._unit() as session:
        budget = await event_rows.custody_budget(UUID(target))
        physical = await session.scalar(
            select(func.sum(func.pg_column_size(ForgeExecutionRequestEventEntity.data))).where(
                ForgeExecutionRequestEventEntity.request_id == UUID(target)
            )
        )
        assert budget == (2, expected_bytes)
        assert isinstance(physical, int) and physical < expected_bytes
        assert (await event_rows.custody_budget(UUID(other)))[0] == 1

    requests = SimpleNamespace(store=store, journal=event_rows)
    fetches = []
    original_events = event_rows.events

    async def fetch(request_id):
        fetches.append(request_id.hex)
        return await original_events(request_id)

    monkeypatch.setattr(event_rows, "events", fetch)
    assert await read_recovery_events(requests, target) == values
    assert fetches == [target]
    for corrupt in ("request", "sequence"):
        invalid, _ = await seed(("request_admitted",), corrupt=corrupt)
        before = len(fetches)
        with pytest.raises(ValueError, match="native columns"):
            await read_recovery_events(requests, invalid)
        assert len(fetches) == before + 1
    for count in (6, 101):
        invalid, _ = await seed(tuple(["request_admitted"] + ["worker_failed"] * (count - 2) + ["worker_claim"]))
        before = len(fetches)
        with pytest.raises(ValueError, match="complete bounded"):
            await read_recovery_events(requests, invalid)
        assert len(fetches) == before
