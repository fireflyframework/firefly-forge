# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled SQL calls retain native WAL identity without changing execution tasks."""

import copy
import json

import httpx
import pytest
from test_bridge_completion_journal_host import fixture
from test_koinon_knowledge_host import ACTOR, REPOSITORY, Access, context, entry, settings

from firefly_forge.knowledge import KnowledgeManager, snapshot_knowledge
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


@pytest.fixture
async def sql_manager(tmp_path, monkeypatch):
    store, journal = await fixture(tmp_path, monkeypatch)
    store.run.data.update(repository_id=REPOSITORY, source_tree="b" * 40, active_task="implement")
    access = Access()
    state = {"posts": 0, "timeout": False, "revoke": False}

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer private-machine-token"
        if request.url.path.endswith("/context"):
            if state.get("change_binding"):
                store.run.data["source_tree"] = "d" * 40
            value = context()
        elif request.url.path.endswith("/catalog/dataset"):
            value = entry("dataset", "dataset", parent="project")
        elif request.url.path.endswith("/catalog/project"):
            value = entry("project", "project", parent="workspace")
        elif request.url.path.endswith("/query"):
            state["posts"] += 1
            assert json.loads(request.content) == {"dataset_id": "dataset", "question": "", "sql": "SELECT 1"}
            assert any(row.kind == "koinon_sql_intent" for row in journal.records.rows)
            assert store.run.data["active_task"] == "implement"
            if state["timeout"]:
                raise httpx.ReadTimeout("Controlled lost reply", request=request)
            if state["revoke"]:
                access.allowed = False
            value = {
                "query_id": "query",
                "execution_status": "OK",
                "preview": [{"value": 1}],
                "row_count": 1,
                "snapshot_pins": state.get("snapshot_pins", {"table": "snapshot"}),
            }
        else:
            raise AssertionError("Unadmitted request " + request.url.path)
        return httpx.Response(200, json=value)

    knowledge = KnowledgeManager(store, transport=httpx.MockTransport(handler), access=access)
    knowledge.journal = journal
    scopes = settings().values["project_scopes"]
    scopes[REPOSITORY]["dataset_ids"] = ["dataset"]
    knowledge.configure(settings(project_scopes=scopes))
    store.run.data["knowledge_snapshot"] = snapshot_knowledge(store.configuration)
    token = CURRENT_PRINCIPAL.set(ACTOR)
    try:
        yield knowledge, store, journal, state, access
    finally:
        CURRENT_PRINCIPAL.reset(token)


def body(**changes):
    from firefly_forge.knowledge_platform import KoinonSqlInput

    return KoinonSqlInput.model_validate(
        {"request_id": "c" * 32, "dataset_id": "dataset", "sql": "SELECT 1", **changes}
    )


async def test_sql_query_commits_intent_before_one_native_post_and_preserves_active_task(sql_manager):
    knowledge, store, journal, state, _ = sql_manager
    original_task = store.run.data["active_task"]
    response = await knowledge.query_sql(store.run.id.hex, body())
    assert response["status"] == "completed" and response["result"]["preview"] == [{"value": 1}]
    assert response["receipt"]["snapshot_pins"] == {"table": "snapshot"}
    assert response["receipt"]["generation_requested"] is False
    second = await knowledge.query_sql(store.run.id.hex, body())
    assert second["receipt"] == response["receipt"] and second["result"] is None
    assert second["result_state"] == "not_retained"
    assert state["posts"] == 1 and store.run.data["active_task"] == original_task
    assert len([row for row in journal.records.rows if row.kind == "koinon_sql_receipt"]) == 1
    with pytest.raises(ValueError, match="identity"):
        await knowledge.query_sql(store.run.id.hex, body(sql="SELECT 2"))
    assert state["posts"] == 1


async def test_uncertain_sql_request_has_read_only_status_and_never_reposts(sql_manager):
    knowledge, store, journal, state, _ = sql_manager
    state["timeout"] = True
    with pytest.raises(ValueError, match="uncertain"):
        await knowledge.query_sql(store.run.id.hex, body())
    before = copy.deepcopy(store.run.data)
    status = await knowledge.query_status(store.run.id.hex, "c" * 32)
    assert status["status"] == "outcome_unknown" and status["query_id"] is None
    with pytest.raises(ValueError, match="uncertain"):
        await knowledge.query_sql(store.run.id.hex, body())
    with pytest.raises(ValueError, match="uncertain"):
        await knowledge.query_sql(store.run.id.hex, body(request_id="d" * 32))
    assert state["posts"] == 1 and store.run.data == before
    assert not any(row.kind == "koinon_sql_receipt" for row in journal.records.rows)


async def test_late_revocation_keeps_actual_receipt_but_withholds_query_data(sql_manager):
    knowledge, store, journal, state, access = sql_manager
    state["revoke"] = True
    with pytest.raises(AccessDenied):
        await knowledge.query_sql(store.run.id.hex, body())
    assert state["posts"] == 1
    assert any(row.kind == "koinon_sql_receipt" for row in journal.records.rows)
    access.allowed = True
    response = await knowledge.query_sql(store.run.id.hex, body())
    assert response["receipt"]["query_id"] == "query" and response["result"] is None
    assert state["posts"] == 1


def test_natural_language_generation_is_not_implicitly_enabled():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        body(sql=None, question="Generate a query using an unspecified model")


async def test_receipt_never_persists_registered_credentials_as_snapshot_identity(sql_manager):
    knowledge, store, journal, state, _ = sql_manager
    state["snapshot_pins"] = {"table": "private-machine-token"}
    with pytest.raises(ValueError, match="credential"):
        await knowledge.query_sql(store.run.id.hex, body())
    assert state["posts"] == 1
    assert not any(row.kind == "koinon_sql_receipt" for row in journal.records.rows)
    assert (await knowledge.query_status(store.run.id.hex, "c" * 32))["status"] == "outcome_unknown"


async def test_changed_current_run_binding_refuses_before_the_remote_sql_post(sql_manager):
    knowledge, store, _, state, _ = sql_manager
    state["change_binding"] = True
    with pytest.raises(AccessDenied, match="binding"):
        await knowledge.query_sql(store.run.id.hex, body())
    assert state["posts"] == 0
