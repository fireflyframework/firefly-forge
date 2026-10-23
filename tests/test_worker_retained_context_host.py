# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Retained answers remain historical context without changing execution authority."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator
from test_bridge_inflight_cancel import RUN, MemoryStore, Rows

from firefly_forge.agent_roles import (
    NativeAllowanceWorkerFeedback,
    RoleRegistry,
    SourceBudgetWorkerFeedback,
    WorkerFeedback,
)
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.providers import AgentResult


def question(number: int) -> dict:
    return {
        "id": f"feedback-{number}",
        "role_id": "implementer",
        "task_id": "implement",
        "attempt": number,
        "disposition": "clarification",
        "summary": "A retained question",
        "request": {
            "id": f"question-{number}",
            "kind": "other",
            "question": "What diagnostic should be used?",
            "context": "Existing output only; no command replay",
        },
        "evidence_paths": [],
    }


def answer(number: int, text: str) -> dict:
    return {
        "id": f"response-{number}",
        "actor": "Operator",
        "answer": text,
        "request_id": f"question-{number}",
        "responding_actor": {"version": 1, "user_id": "1" * 32, "authentication_method": "local_bootstrap"},
    }


def snapshot(rows: list) -> dict:
    entries = [copy.deepcopy(row.data) for row in rows]
    selected = [e for e in entries if e["kind"] in {"worker_feedback", "worker_response"}]
    return {
        "journal_rows": len(entries),
        "message_rows": len(selected),
        "headers": [[e[k] for k in ("sequence", "previous_hash", "digest", "kind")] for e in entries],
        "entries": selected,
    }


class ContextRows(Rows):
    async def find_implementation_context(self, run_id):
        return snapshot(self.rows)


async def history(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.configuration = ConfigurationStore(tmp_path / "private")
    store.credential_registry = dict
    store.reject_credentials = lambda value: None
    store.run.data.update(
        id=RUN, role_snapshot=RoleRegistry(tmp_path / "roles").snapshot(), repair_budget=2, implementation_rounds=2
    )
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=ContextRows())
    await journal.ensure_plan(RUN)
    pairs = [
        (question(1), answer(1, "UndefinedColumnError: accounts.normalized_name is absent. Use the retained receipt.")),
        (question(2), answer(2, "The source hold is resolved; preserve all original limits.")),
    ]
    for feedback, response in pairs:
        await journal._append(RUN, "implement", feedback["id"], "worker_feedback", feedback)
        await journal._append(RUN, "implement", response["id"], "worker_response", response)
    store.run.data.update(
        worker_response=pairs[-1][1], worker_request={**pairs[-1][0]["request"], "status": "answered"}
    )
    return store, journal


@pytest.mark.parametrize("invalid_feedback", [False, True])
async def test_actual_engine_delivers_prior_diagnostic_and_latest_answer_in_one_provider_call(
    tmp_path, monkeypatch, invalid_feedback
):
    store, journal = await history(tmp_path)
    received = []

    class Worker:
        async def execute(self, workspace, brief, backend, frontend, emit):
            received.append(brief)
            feedback = question(3)
            if invalid_feedback:
                feedback.update(disposition="blocked", request=None)
            return AgentResult(json.dumps({"feedback": feedback}), "host", usage={"worker_feedback": feedback})

    async def inspect(*args):
        return {"base_revision": "b" * 40, "head": "b" * 40, "source_tree": "c" * 40}

    async def checked(argv, *args, **kwargs):
        assert argv == ["git", "ls-files", "-z"]
        return ""

    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal)
    (engine.workspaces / RUN).mkdir()
    monkeypatch.setattr(engine, "_inspect_workspace", inspect)
    monkeypatch.setattr("firefly_forge.engine.checked", checked)
    await engine.execute_run(RUN)
    assert len(received) == 1, (await store.get_run(RUN)).get("error")
    assert "UndefinedColumnError" in received[0]
    assert "source hold is resolved" in received[0]
    assert "historical" in received[0]
    current = await store.get_run(RUN)
    if invalid_feedback:
        assert current["status"] == "failed"
        assert (
            current["error"]
            == "The implementation report violates its typed feedback contract; inspect the retained provider report"
        )
        assert json.loads(current["summary"])["feedback"]["request"] is None
        assert current["worker_request"]["status"] == "answered"
        assert sum(row.kind == "worker_feedback" for row in journal.records.rows) == 2
    else:
        assert current["status"] == "paused"


@pytest.mark.parametrize("model", [WorkerFeedback, NativeAllowanceWorkerFeedback, SourceBudgetWorkerFeedback])
def test_published_schema_explains_and_enforces_request_disposition_invariant(model):
    schema = model.model_json_schema()
    payload = question(1 if model is WorkerFeedback else 5)
    if model is not WorkerFeedback:
        payload.update(
            protocol="source-budget-continuation-v1"
            if model is SourceBudgetWorkerFeedback
            else "native-provider-recovery-allowance-v1",
            allowance_id="1" * 32,
            allowance_digest="a" * 64,
        )
        for key in ("attempt", "protocol", "allowance_id", "allowance_digest"):
            schema["properties"][key]["const"] = payload[key]
    validator = Draft202012Validator(schema)
    for disposition in ["blocked", "clarification", "completed", "failed"]:
        body = {**payload, "disposition": disposition}
        for request in [body["request"], None]:
            value = {**body, "request": request}
            expected = (disposition in {"blocked", "clarification"}) == (request is not None)
            assert validator.is_valid(value) is expected, (disposition, request)
    assert "request" in schema["description"] and "do not invent" in schema["description"].lower()
    if model is not WorkerFeedback:
        for key in ("attempt", "protocol", "allowance_id", "allowance_digest"):
            invalid = {**payload, key: "foreign"}
            assert not validator.is_valid(invalid)


@pytest.mark.parametrize(
    "tamper", ["payload", "omitted", "scope", "orphan", "actor", "order", "latest", "count", "bytes"]
)
async def test_retained_projection_refuses_tampered_or_oversized_messages(tmp_path, tamper):
    from firefly_forge.worker_context import MAX_BYTES, implementation_context

    store, journal = await history(tmp_path)
    run = await store.get_run(RUN)
    data = snapshot(journal.records.rows)
    if tamper == "payload":
        data["entries"][1]["payload"]["answer"] = "Forged answer"
    elif tamper == "omitted":
        data["entries"].pop()
        data["message_rows"] -= 1
    elif tamper == "scope":
        run["id"] = "2" * 32
    elif tamper == "orphan":
        data["entries"] = [e for e in data["entries"] if e["kind"] == "worker_response"]
        data["message_rows"] = len(data["entries"])
    elif tamper == "actor":
        data["entries"][1]["payload"]["responding_actor"]["version"] = True
    elif tamper == "order":
        data["entries"].reverse()
    elif tamper == "latest":
        run["worker_response"]["answer"] = "Unrecorded latest answer"
    elif tamper == "count":
        data["journal_rows"] = True
    else:
        data["entries"][1]["payload"]["answer"] = "x" * MAX_BYTES
    with pytest.raises(ValueError):
        implementation_context(run, data)


@pytest.mark.parametrize("drift", ["response", "scope", "actor", "cancel", "history", "secret"])
async def test_context_credential_wait_rechecks_scope_actor_history_and_credentials(tmp_path, monkeypatch, drift):
    from firefly_forge.project_access import AccessDenied

    store, journal = await history(tmp_path)
    calls = []
    original_reader = journal._diagnostic_reader

    async def reader(run, actor):
        calls.append(1)
        if drift == "actor" and len(calls) > 1:
            raise AccessDenied("The current reader was revoked")
        await original_reader(run, actor)

    async def admit(value):
        assert "UndefinedColumnError" in json.dumps(value)
        if drift == "secret":
            raise ValueError("Registered credential refused")
        if drift == "response":
            store.run.data["worker_response"]["answer"] = "Changed while awaiting credentials"
        elif drift == "scope":
            store.run.data["component_id"] = "foreign"
        elif drift == "cancel":
            store.run.data["cancel_request"] = {"status": "requested"}
        elif drift == "history":
            await journal._append(RUN, "implement", "late", "tool_receipt", {"output": "Late unrelated evidence"})

    monkeypatch.setattr(journal, "_diagnostic_reader", reader)
    monkeypatch.setattr(store, "admit_credentials", admit)
    with pytest.raises((AccessDenied, ValueError)):
        await journal.implementation_context(RUN)


@pytest.mark.parametrize("budget", [(10001, 1, 1), (1, 65, 1), (1, 1, 262145)])
async def test_repository_refuses_oversized_projection_in_one_statement(budget):
    from types import SimpleNamespace
    from uuid import UUID

    from sqlalchemy.dialects import postgresql

    from firefly_forge.persistence import ForgeJournalRepository

    statements = []

    async def execute(statement):
        statements.append(str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})))
        return SimpleNamespace(one=lambda: (*budget, [], []))

    repository = ForgeJournalRepository(session=SimpleNamespace(execute=execute))
    with pytest.raises(ValueError, match="bounded"):
        await repository.find_implementation_context(UUID(RUN))
    assert len(statements) == 1
    assert "worker_feedback" in statements[0] and "worker_response" in statements[0]
    assert "worker_message_budget.bytes <= 262144" in statements[0]
    assert "jsonb_agg" in statements[0] and "tool_receipt" not in statements[0]
