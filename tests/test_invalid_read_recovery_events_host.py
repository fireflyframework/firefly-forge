# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native request-event custody with explicit memory reads, never PostgreSQL authority."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from firefly_forge.execution_request_persistence import ForgeExecutionRequestEventEntity
from firefly_forge.execution_requests import digest

REQUEST = "a" * 32


def events(kinds=("request_admitted", "worker_failed")):
    rows, previous = [], "0" * 64
    for sequence, kind in enumerate(kinds, 1):
        value = dict(
            request_id=REQUEST,
            sequence=sequence,
            previous_hash=previous,
            kind=kind,
            evidence={"fictional": True},
            created_at="2026-10-27T00:00:00+00:00",
        )
        value["digest"] = previous = digest(value)
        rows.append(
            ForgeExecutionRequestEventEntity(id=uuid4(), request_id=UUID(REQUEST), sequence=sequence, data=value)
        )
    return rows


def context(rows):
    state = SimpleNamespace(
        rows=rows,
        fetches=0,
        budget_reads=0,
        in_unit=False,
        budget_override=None,
        fetched_override=None,
        later_budget=None,
    )

    @asynccontextmanager
    async def unit():
        assert not state.in_unit
        state.in_unit = True
        try:
            yield
        finally:
            state.in_unit = False

    async def budget(key):
        assert state.in_unit and key == UUID(REQUEST)
        state.budget_reads += 1
        if state.budget_reads > 1 and state.later_budget is not None:
            return state.later_budget
        if state.budget_override is not None:
            return state.budget_override
        selected = [row for row in state.rows if row.request_id == key]
        return len(selected), sum(len(json.dumps(row.data, ensure_ascii=False).encode()) for row in selected)

    async def fetch(key):
        assert state.in_unit and key == UUID(REQUEST)
        state.fetches += 1
        return state.fetched_override if state.fetched_override is not None else state.rows[:100]

    return SimpleNamespace(
        store=SimpleNamespace(_unit=unit), journal=SimpleNamespace(custody_budget=budget, events=fetch)
    ), state


async def read(requests, request_id=REQUEST):
    from firefly_forge.invalid_read_recovery_events import read_recovery_events

    return await read_recovery_events(requests, request_id)


async def test_exact_native_event_history_is_detached_and_complete():
    rows = events()
    requests, state = context(rows)
    result = await read(requests)
    assert result == [row.data for row in rows]
    assert state.fetches == 1 and state.budget_reads == 2 and not state.in_unit
    result[0]["evidence"]["fictional"] = False
    assert rows[0].data["evidence"]["fictional"] is True


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
async def test_v1_bound_reads_all_admissible_history_lengths(count):
    rows = events(
        ("request_admitted", "worker_failed", "request_recovered_before_claim", "worker_claim", "worker_completed")[
            :count
        ]
    )
    requests, _ = context(rows)
    assert len(await read(requests)) == count


@pytest.mark.parametrize(
    "budget",
    [
        (0, 0),
        (6, 800),
        (100, 800),
        (101, 800),
        (2, 16 * 1024 * 1024 + 1),
        (True, 800),
        (2, True),
        (-1, 800),
        (2, -1),
        ("2", 800),
        (2, 1.5),
    ],
)
async def test_unbounded_or_inexact_budget_holds_before_fetch(budget):
    requests, state = context(events())
    state.budget_override = budget
    with pytest.raises(ValueError):
        await read(requests)
    assert state.fetches == 0


async def test_hidden_claim_after_first100_is_refused_without_paginated_fetch():
    rows = events(tuple(["request_admitted"] + ["worker_failed"] * 99 + ["worker_claim"]))
    requests, state = context(rows)
    with pytest.raises(ValueError):
        await read(requests)
    assert state.fetches == 0


@pytest.mark.parametrize(
    "change",
    [
        "native_request",
        "native_sequence",
        "data_request",
        "data_sequence",
        "bool_native_sequence",
        "bool_data_sequence",
        "digest",
        "previous",
        "missing_data",
        "non_dict",
        "missing_row",
        "extra_row",
        "reorder",
    ],
)
async def test_native_columns_full_count_and_chain_must_all_match(change):
    rows = events()
    requests, state = context(rows)
    state.budget_override = (2, 800)
    if change == "native_request":
        rows[0].request_id = uuid4()
    elif change == "native_sequence":
        rows[0].sequence = 2
    elif change == "data_request":
        rows[0].data["request_id"] = "b" * 32
    elif change == "data_sequence":
        rows[0].data["sequence"] = 2
    elif change == "bool_native_sequence":
        rows[0].sequence = True
    elif change == "bool_data_sequence":
        rows[0].data["sequence"] = True
    elif change == "digest":
        rows[0].data["digest"] = "f" * 64
    elif change == "previous":
        rows[1].data["previous_hash"] = "f" * 64
    elif change == "missing_data":
        del rows[0].data["digest"]
    elif change == "non_dict":
        rows[0].data = None
    elif change == "missing_row":
        state.fetched_override = rows[:1]
    elif change == "extra_row":
        state.fetched_override = rows + events()[:1]
    else:
        state.fetched_override = list(reversed(rows))
    if change in {"data_request", "data_sequence", "bool_data_sequence"}:
        previous = "0" * 64
        for row in rows:
            row.data["previous_hash"] = previous
            row.data["digest"] = previous = digest({key: item for key, item in row.data.items() if key != "digest"})
    with pytest.raises(ValueError):
        await read(requests)


@pytest.mark.parametrize("later_budget", [(3, 800), (2, 801), (2, 16 * 1024 * 1024 + 1)])
async def test_budget_race_after_fetch_never_returns_a_stale_cut(later_budget):
    requests, state = context(events())
    state.budget_override = (2, 800)
    state.later_budget = later_budget
    with pytest.raises(ValueError):
        await read(requests)
    assert state.fetches == 1 and not state.in_unit


@pytest.mark.parametrize("request_id", ["../not-an-id", "A" * 32, "a" * 8 + "-" + "a" * 24])
async def test_request_identity_is_exact_lowercase_native_scope(request_id):
    requests, state = context(events())
    with pytest.raises(ValueError):
        await read(requests, request_id)
    assert state.fetches == state.budget_reads == 0


async def test_repository_budget_is_scoped_logical_json_count_not_compressed_storage():
    from sqlalchemy.dialects import postgresql

    from firefly_forge.execution_request_persistence import ForgeExecutionRequestEventRepository

    statements = []

    class Session:
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(one=lambda: (2, 200))

    repository = ForgeExecutionRequestEventRepository(session=Session())
    assert await repository.custody_budget(UUID(REQUEST)) == (2, 200)
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "count(" in sql and "sum(octet_length(CAST(" in sql and " AS TEXT)" in sql
    assert "pg_column_size" not in sql
    assert "forge_execution_request_events.request_id =" in sql
    assert UUID(REQUEST) in compiled.params.values()


async def test_native_unicode_hash_contract_and_existing_exact_byte_envelope():
    rows = events(("request_admitted",))
    value = rows[0].data
    value["evidence"] = {"observation": "Fictional café Ω"}
    value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
    requests, state = context(rows)
    state.budget_override = (1, 16 * 1024 * 1024)
    assert (await read(requests))[0] == value
