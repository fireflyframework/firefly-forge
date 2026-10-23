# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Question authority never depends on unrelated command/tool output volume."""

import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from test_scm_natural_worker_context_host import fixture

from firefly_forge.persistence import ForgeJournalRepository
from firefly_forge.scm_natural_models import NaturalDecision, resolve_task


@pytest.mark.parametrize("bad", [(2001, 100), (1, 4 * 1024 * 1024 + 1)])
async def test_question_repository_refuses_relevant_budget_before_loading_payloads(monkeypatch, bad):
    calls = []

    async def execute(statement):
        calls.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(all=lambda: [(*bad, None)])

    async def scalars(statement):
        raise AssertionError("Oversized matching payloads must never be loaded")

    repository = ForgeJournalRepository(session=SimpleNamespace(execute=execute, scalars=scalars))
    with pytest.raises(ValueError, match="bounded"):
        await repository.find_question_context(UUID(int=1), "current-question")
    assert len(calls) == 1 and "run_id" in calls[0]


async def test_question_repository_filters_exact_request_before_ordered_bounded_fetch(monkeypatch):
    statements = []
    row = SimpleNamespace(data={"kind": "worker_feedback", "payload": {"request": {"id": "q"}}})

    async def execute(statement):
        statements.append(statement)
        return SimpleNamespace(all=lambda: [(1, len(json.dumps(row.data).encode()), row)])

    async def scalars(statement):
        statements.append(statement)
        return [row]

    repository = ForgeJournalRepository(session=SimpleNamespace(execute=execute, scalars=scalars))
    assert await repository.find_question_context(UUID(int=1), "q") == [row]
    assert len(statements) == 1
    for statement in statements:
        text = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        assert all(kind in text for kind in ("worker_feedback", "worker_response", "resume", "handoff"))
        assert "request_id" in text and "human_clarification" in text and "tool_receipt" not in text
    assert "LIMIT 2001" in str(statements[0].compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize("tamper", [False, True])
async def test_large_unrelated_history_preserves_only_exact_published_question_and_holds_tamper(tamper):
    service, delivery, scopes, _, runs = await fixture()
    repository = service.collaboration.deployments.engine.journal.records
    original = repository.find_question_context
    relevant = await original(UUID("1" * 32), "question-1")
    irrelevant = [{"kind": "tool_receipt", "payload": {"output": "public fixture output " * 70000}} for _ in range(4)]
    assert len(json.dumps(irrelevant).encode()) > 4 * 1024 * 1024
    queries = []

    async def forbidden(*args):
        raise AssertionError("The conversation cannot load every raw command/tool output")

    async def compact(run_id, request_id):
        queries.append((run_id.hex, request_id))
        rows = await original(run_id, request_id)
        assert request_id == runs[run_id.hex]["worker_request"]["id"]
        return rows

    repository.find_for_run = forbidden
    repository.find_question_context = compact
    if tamper:
        runs["1" * 32]["worker_request"]["question"] = "Changed outside the immutable producer"
    context = await service._context(delivery, scopes)
    current = next(row for row in context["candidates"] if row["run_id"] == "1" * 32)
    assert len(queries) == 3
    result = resolve_task(
        context["scope"],
        context["candidates"],
        NaturalDecision(intent="answer", message="Retained."),
        answer="Keep it.",
    )
    if tamper:
        assert result.state == "waiting" and current["question_id"] is None
    else:
        assert result.state == "answer" and current["question_digest"] == relevant[0].data["digest"]
        assert current["publication"]["receipt_id"] == "5" * 32
    assert await original(UUID("1" * 32), "question-1") == relevant


async def test_concurrent_matching_append_after_query_snapshot_cannot_trigger_a_second_payload_fetch():
    statements = []
    initial = SimpleNamespace(data={"kind": "worker_response", "payload": {"request_id": "q", "answer": "Keep it."}})
    snapshot = [(1, len(json.dumps(initial.data).encode()), initial)]
    database = [initial]

    async def execute(statement):
        statements.append(statement)
        # The statement has captured one server-side snapshot before this concurrent committed append.
        database.append(
            SimpleNamespace(
                data={"kind": "worker_response", "payload": {"request_id": "q", "answer": "x" * (4 * 1024 * 1024 + 1)}}
            )
        )
        return SimpleNamespace(all=lambda: snapshot, one=lambda: snapshot[0][:2])

    async def scalars(statement):
        statements.append(statement)
        return list(database)

    repository = ForgeJournalRepository(session=SimpleNamespace(execute=execute, scalars=scalars))
    assert await repository.find_question_context(UUID(int=1), "q") == [initial]
    assert len(database) == 2 and len(statements) == 1
    sql = str(statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "WITH question_budget AS" in sql and "LEFT OUTER JOIN" in sql
    assert "question_budget.bytes <= 4194304" in sql and "question_budget.rows <= 2000" in sql
