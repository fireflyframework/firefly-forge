# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Frozen opt-in and exact native ledger/projection authority boundaries."""

import pytest
from pydantic import ValidationError
from test_learning_ledger import completed

from firefly_forge.journal import Journal, digest
from firefly_forge.knowledge import KnowledgeManager
from firefly_forge.learning import LearningLedger
from firefly_forge.learning_configuration import configure_learning, frozen_learning
from firefly_forge.learning_models import LearningContext, LearningPolicy
from firefly_forge.learning_projection import LearningProjection
from firefly_forge.models import RunInput


@pytest.mark.asyncio
async def test_only_new_opted_in_runs_capture_projection_permission(store):
    legacy = await store.create_run(await _input(store))
    assert legacy["learning_snapshot"]["settings"].get("projected_retrieval", False) is False
    try:
        policy = LearningPolicy(projected_retrieval=True)
    except ValidationError:
        pytest.fail("A new run cannot capture its explicit projected-retrieval permission")
    configure_learning(store.configuration, policy)
    enabled = await store.create_run(await _input(store))
    configure_learning(store.configuration, LearningPolicy())
    assert enabled["learning_snapshot"]["settings"]["projected_retrieval"] is True
    assert (await store.get_run(legacy["id"]))["learning_snapshot"] == legacy["learning_snapshot"]
    old = {
        "version": 1,
        "revision": None,
        "settings": {
            "enabled": True,
            "max_age_hours": 72,
            "max_candidates": 8,
            "max_repeated_attempts": 2,
            "max_lookup_calls": 30,
        },
    }
    old["digest"] = digest(old)
    assert frozen_learning(old) == old
    assert not LearningPolicy.model_validate(frozen_learning(old)["settings"]).projected_retrieval


async def _input(store):
    from firefly_forge.models import RepositoryInput

    repository = await store.add_repository(
        RepositoryInput(name="Owned projected learning", url="/tmp/owned-projection-input")
    )
    return RunInput(repository_id=repository["id"], brief="Owned projection policy")


@pytest.mark.asyncio
async def test_pending_projection_cannot_be_retrieved_as_verified_evidence(store, tmp_path):
    engine, origin = await completed(store, tmp_path)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(origin["id"])
    projection = LearningProjection(store, engine.journal, ledger, KnowledgeManager(store))
    try:
        configure_learning(store.configuration, LearningPolicy(projected_retrieval=True))
    except ValidationError:
        pytest.fail("Projected retrieval cannot be configured")
    run = await store.create_run(RunInput(repository_id=origin["repository_id"], brief=origin["brief"]))
    result = await projection.retrieve(run["id"], origin["brief"], record["envelope"]["context"], record["id"])
    assert result == {"state": "unavailable", "reason": "projection_unverified"}
    assert (await projection.status(record["id"]))["state"] == "pending"
    assert await ledger.lookup(run["id"], origin["brief"], record["envelope"]["context"])


@pytest.mark.asyncio
async def test_projection_mapping_is_locked_and_rechecked_at_admission(store, tmp_path):
    from firefly_forge.store import identifier

    engine, origin = await completed(store, tmp_path)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(origin["id"])
    configure_learning(store.configuration, LearningPolicy(projected_retrieval=True))
    run = await store.create_run(RunInput(repository_id=origin["repository_id"], brief=origin["brief"]))
    projection = LearningProjection(store, engine.journal, ledger, KnowledgeManager(store))
    async with store._unit():
        row = await ledger.projections.for_record(identifier(record["id"]))
        row.state = "executing"
        row.data = {
            **row.data,
            "target": run["knowledge_snapshot"],
            "workspace_id": projection.workspace_id(LearningContext.model_validate(record["envelope"]["context"])),
            "source_id": "owned-source",
        }
        await ledger.projections.save(row)
        row.state = "verified"
        await ledger.projections.save(row)
        selected = {
            **projection._identity(row),
            "citations": [
                {"workspace_id": row.data["workspace_id"], "source_id": "owned-source", "chunk_id": "owned-chunk"}
            ],
        }
        selected["citations_digest"] = digest({"citations": selected["citations"]})
        _, local_guard = await ledger.retain(run["id"], origin["brief"], record["envelope"]["context"], record["id"])
        projection_guard = await projection.retain(run["id"], record["envelope"]["context"], selected)
        local_guard()
        projection_guard()
        retained_data = row.data
        row.data = {**row.data, "target": {"revision": "changed"}}
        with pytest.raises(ValueError, match="Retained projected evidence"):
            projection_guard()
        row.data = retained_data


@pytest.mark.asyncio
async def test_frozen_target_equivalence_checks_credentials_without_changing_revisions(store):
    from firefly_forge.knowledge import KnowledgeConfiguration, snapshot_knowledge

    knowledge = KnowledgeManager(store)
    values = {
        "enabled": True,
        "endpoint": "http://127.0.0.1:1",
        "tenant_id": "owned",
        "api_key": "fixture-private-target-first",
        "auto_memory": False,
    }
    knowledge.configure(KnowledgeConfiguration(values=values))
    first = snapshot_knowledge(store.configuration)
    second = snapshot_knowledge(store.configuration)
    projection = LearningProjection(store, Journal(store), LearningLedger(store, Journal(store)), knowledge)
    assert first["revision"] != second["revision"]
    assert projection._same_target({"knowledge_snapshot": second}, first)
    knowledge.configure(KnowledgeConfiguration(values={"api_key": "fixture-private-target-second"}))
    third = snapshot_knowledge(store.configuration)
    assert not projection._same_target({"knowledge_snapshot": third}, first)
    assert not projection._same_target({"knowledge_snapshot": second}, {"revision": "missing"})


@pytest.mark.asyncio
async def test_fresh_result_guard_refusal_preserves_unknown_effect_without_success(store, tmp_path):
    from test_tool_broker import invoke, seed

    module, broker, run, source = await seed(store, tmp_path)

    async def result():
        return {"results": [{"citation_id": "owned-remote-citation"}]}

    async def validate(data):
        assert data["results"][0]["citation_id"] == "owned-remote-citation"

        def guard():
            raise ValueError("Fresh authority changed")

        return guard

    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, source, "9" * 32, result, validate_result=validate)
    rows = await broker.invocations(run["id"])
    assert len(rows) == 1 and rows[0]["status"] == "unknown" and "result" not in rows[0]
    events = await broker.journal.entries(run["id"])
    assert not any(e["kind"] == "tool_receipt" and e["payload"]["status"] == "succeeded" for e in events)


def test_native_factory_exposes_opt_in_policy_and_links_actual_projection(tmp_path, postgres_schema):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.knowledge_first import KnowledgeFirst

    token = "owned-projection-policy-admin-token-32-characters"
    app = create_application(tmp_path, token)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        headers = {"Authorization": "Bearer " + token}
        policy = client.get("/api/v1/learning/policy", headers=headers).json()
        assert policy["settings"]["projected_retrieval"] is False
        changed = client.put(
            "/api/v1/learning/policy", headers=headers, json={**policy["settings"], "projected_retrieval": True}
        )
        assert changed.status_code == 200 and changed.json()["settings"]["projected_retrieval"] is True
        assert (
            client.put(
                "/api/v1/learning/policy", headers=headers, json={**policy["settings"], "projected_retrieval": "true"}
            ).status_code
            == 422
        )
        context = app.state.runtime.context
        first = context.get_bean(KnowledgeFirst)
        assert first.projection is context.get_bean(LearningProjection)
        assert first.projection.knowledge is first.knowledge


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["invalidated", "expired"])
async def test_local_only_fresh_outcome_changed_after_lookup_cannot_be_admitted(store, tmp_path, monkeypatch, change):
    import httpx

    from firefly_forge.agent_roles import ProjectRoles
    from firefly_forge.knowledge import KnowledgeConfiguration
    from firefly_forge.knowledge_first import KnowledgeFirst
    from firefly_forge.search_tools import SearchTools
    from firefly_forge.tool_broker import ToolInvocationBroker, ToolReconciliationRequired
    from firefly_forge.workspace_tools import tool_catalog

    engine, origin = await completed(store, tmp_path)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(origin["id"])
    knowledge = KnowledgeManager(
        store, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"elapsed_ms": 1, "hits": []}))
    )
    knowledge.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "owned",
                "api_key": "fixture-private-local-evidence",
                "auto_memory": False,
            }
        )
    )
    run = await store.create_run(
        RunInput(
            repository_id=origin["repository_id"],
            brief=origin["brief"],
            agent="agentic",
            backend=origin["backend"],
            frontend=origin["frontend"],
            skills=[],
            roles=ProjectRoles(allowed_tools={"implementer": ["knowledge_lookup"]}),
        ),
        prepared_tools=tool_catalog({"knowledge_lookup": True}),
    )
    assert run["learning_snapshot"]["settings"]["projected_retrieval"] is False
    if change == "expired":
        import copy
        from datetime import timedelta
        from uuid import uuid4

        from firefly_forge.learning_persistence import ForgeLearningRecordEntity
        from firefly_forge.store import identifier

        async with store._unit():
            at = await ledger.records_repository.database_time()
            data = {key: copy.deepcopy(value) for key, value in record.items() if key not in {"id", "state", "kind"}}
            correlation = uuid4().hex
            data["envelope"]["correlation_id"] = correlation
            data["verified_at"] = (at - timedelta(hours=72) + timedelta(seconds=5)).isoformat()
            data["expires_at"] = (at + timedelta(seconds=5)).isoformat()
            row = ForgeLearningRecordEntity(
                id=uuid4(),
                repository_id=identifier(origin["repository_id"]),
                run_id=identifier(origin["id"]),
                correlation_id=correlation,
                state="verified",
                kind="local_outcome",
                data=data,
            )
            await ledger.records_repository.save(row)
            record = {"id": row.id.hex, "state": "verified", "kind": "local_outcome", **data}
    broker = ToolInvocationBroker(store, engine.journal, store.path.parent)
    first = KnowledgeFirst(store, engine.journal, ledger, broker, SearchTools(tmp_path / "no-web-key"))
    first.knowledge = knowledge
    await store.acquire_ownership()
    await engine.journal.intent(run["id"], "implement")
    await store.update(run["id"], status="developing", attempt=1)
    workspace = engine.workspaces / origin["id"]
    source = await engine._inspect_workspace(origin["id"], workspace, {})
    native_lookup = ledger.lookup
    invalidated = False

    async def lookup_then_invalidate(*args, **kwargs):
        nonlocal invalidated
        result = await native_lookup(*args, **kwargs)
        if not invalidated:
            assert result["eligible"], result["reason"]
            invalidated = True
            from uuid import uuid4

            if change == "invalidated":
                await ledger.invalidate(record["id"], reason="Owned fresh admission race", request_id=uuid4().hex)
            else:
                import asyncio
                from datetime import datetime

                until = datetime.fromisoformat(record["expires_at"])
                while True:
                    async with store._unit():
                        now = await ledger.records_repository.database_time()
                    if now >= until:
                        break
                    await asyncio.sleep(min((until - now).total_seconds(), 0.1))
        return result

    monkeypatch.setattr(ledger, "lookup", lookup_then_invalidate)
    with pytest.raises(ToolReconciliationRequired):
        await first.search(
            run["id"], "implement", origin["brief"], 5, source=source, workspace=workspace, allow_external=False
        )
    assert invalidated
    rows = await broker.invocations(run["id"])
    assert len(rows) == 1 and rows[0]["status"] == "unknown" and "result" not in rows[0]
