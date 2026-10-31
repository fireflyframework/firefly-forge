# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Retained history traversal controls; native SQL qualification is separate."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from firefly_forge.github_projects_worker import BoardConflict, GitHubProjectsWorker

PROJECT = "1" * 32
REPOSITORY = "2" * 32


class History:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    @asynccontextmanager
    async def _unit(self):
        yield self

    async def scalars(self, query):
        self.queries.append(query)
        limit = query._limit_clause.value
        start = (len(self.queries) - 1) * limit
        return self.rows[start : start + limit]

    async def get(self, entity, key):
        return SimpleNamespace(repository_id=UUID(REPOSITORY), sequence=1)

    async def get_run(self, key):
        return {"id": key}


def history_fixture(*, last_unknown=False):
    rows = [
        SimpleNamespace(id=UUID(int=i + 1), created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=i))
        for i in range(101)
    ]
    store = History(rows)
    worker = object.__new__(GitHubProjectsWorker)
    worker.store = store

    async def retained(key):
        index = int(UUID(hex=key)) - 1
        return (
            {
                "status": "unknown" if last_unknown and index == 100 else "completed",
                "request": {
                    "project_id": PROJECT,
                    "repository_id": REPOSITORY,
                    "field_id": "other-field" if last_unknown else "field",
                    "contents": [{"kind": "issue", "number": 4}],
                },
            },
            [{"kind": "board_field_confirmed", "evidence": {"content_id": "ISSUE", "option_id": f"option{index}"}}],
        )

    worker.tickets = SimpleNamespace(retained=retained)
    data = {
        "id": "f" * 32,
        "run_id": "3" * 32,
        "request": {
            "board_id": "BOARD",
            "remote_repository_id": "77",
            "project_id": PROJECT,
            "repository_id": REPOSITORY,
            "field_id": "field",
        },
    }
    return worker, store, data


async def test_history_preserves_latest_confirmation_after100_prior_requests():
    worker, store, data = history_fixture()
    assert await worker.history(data, {"kind": "issue", "number": 4, "id": "ISSUE"}) == "option100"
    assert len(store.queries) == 2
    sql = str(store.queries[0].compile(dialect=postgresql.dialect()))
    assert "@>" in sql and "created_at" in sql
    assert any(
        value == [{"kind": "issue", "number": 4}]
        for value in store.queries[0].compile(dialect=postgresql.dialect()).params.values()
    )


async def test_unknown_after100_rows_still_quarantines_board_across_field_aliases():
    worker, _, data = history_fixture(last_unknown=True)
    with pytest.raises(BoardConflict, match="prior item outcome is unknown"):
        await worker.history(data, {"kind": "issue", "number": 4, "id": "ISSUE"})


async def test_newer_issue_projection_is_checked_after100_unrelated_units():
    worker, store, data = history_fixture()
    for index, row in enumerate(store.rows):
        row.sequence = index + 2

    async def references(run):
        return [{"kind": "issue", "number": 4}] if run["id"] == store.rows[-1].id.hex else []

    worker.tickets = SimpleNamespace(issue_references=references)
    with pytest.raises(BoardConflict, match="newer native unit owns this issue projection"):
        await worker.current_issue_projection(data, {"kind": "issue", "number": 4})
    assert len(store.queries) == 2
