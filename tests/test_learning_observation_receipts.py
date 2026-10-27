# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact observation readback is local, immutable and currently authorized."""

import asyncio
from uuid import uuid4

import pytest
from starlette.testclient import TestClient
from test_learning_ledger import completed

from firefly_forge.api import create_application
from firefly_forge.engine import Engine
from firefly_forge.knowledge import KnowledgeManager
from firefly_forge.learning import LearningLedger
from firefly_forge.store import identifier


def test_native_exact_historical_observation_receipt_is_readable_without_remote_io(
    tmp_path, postgres_schema, monkeypatch
):
    from native_client_knowledge_fixtures import activate_native_client_target

    async def idle(_self):
        await asyncio.Event().wait()

    def forbidden_remote(*args, **kwargs):
        raise AssertionError("Receipt read must never contact knowledge service")

    monkeypatch.setattr(Engine, "_poll", idle)
    token = "owned-observation-bootstrap-token-32chars"
    with TestClient(
        create_application(tmp_path / "api", token),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 52000),
    ) as http:

        async def seed():
            store = http.app.state.store
            remote = await activate_native_client_target(store, tmp_path / "framework-prerequisite")
            engine, run = await completed(store, tmp_path, scoped=True, framework_remote=remote)
            ledger = LearningLedger(store, engine.journal)
            ledger.engine = engine
            record = await ledger.capture_outcome(run["id"])
            request_id = uuid4().hex
            async with store._unit() as session:
                row = await ledger.records_repository.find_by_id(identifier(record["id"]))
                projection = await ledger.projections.for_record(row.id)
                projection.state = "executing"
                await ledger.projections.save(projection)
                await session.flush()
                projection.state = "verified"
                await ledger.projections.save(projection)
                from firefly_forge.journal import digest
                from firefly_forge.learning_persistence import ForgeLearningEventEntity

                history = await ledger.events.for_record(row.id)
                event = {
                    "record_id": row.id.hex,
                    "run_id": row.run_id.hex,
                    "sequence": len(history) + 1,
                    "request_id": request_id,
                    "kind": "projection_observation",
                    "payload": {
                        "source_id": "owned-source",
                        "content_sha256": "f" * 64,
                        "citations": [{"source_id": "owned-source", "chunk_id": "owned-chunk"}],
                        "state": "verified",
                    },
                    "created_at": (await ledger.records_repository.database_time()).isoformat(),
                    "previous_hash": history[-1].digest,
                }
                event["digest"] = digest(event)
                await ledger.events.save(
                    ForgeLearningEventEntity(
                        id=uuid4(),
                        record_id=row.id,
                        sequence=event["sequence"],
                        request_id=request_id,
                        digest=event["digest"],
                        data=event,
                    )
                )
            return record, request_id, event

        record, request_id, event = http.portal.call(seed)
        monkeypatch.setattr(KnowledgeManager, "client", forbidden_remote)
        response = http.get(f"/api/v1/learning/records/{record['id']}/projection/observations/{request_id}")
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["request_id"] == request_id and receipt["record_id"] == record["id"]
        assert receipt["kind"] == "projection_observation" and receipt["event_state"] == "verified"
        assert receipt["digest"] == event["digest"]
        assert receipt["source_id"] == "owned-source"
        assert receipt["actor_id"] is None and receipt["target_revision"] is None
        assert receipt["projection_id"] is None and receipt["submission_request_id"] is None


async def admitted_projection(
    store, tmp_path, *, source_id="owned-source", before_search=None, remote_service=None, framework_remote=None
):
    import hashlib
    import json

    import httpx

    from firefly_forge.knowledge import KnowledgeConfiguration, snapshot_knowledge
    from firefly_forge.learning_models import LearningContext
    from firefly_forge.learning_projection import LearningProjection

    engine, run = await completed(store, tmp_path, scoped=True, framework_remote=framework_remote)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(run["id"])
    calls = []
    source = {} if remote_service is None else remote_service["source"]

    async def remote(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/v1/sources/owned-source":
            return httpx.Response(200, json={**source, "id": source_id})
        assert request.method == "POST" and request.url.path == "/api/v1/search"
        assert json.loads(request.content)["source_ids"] == ["owned-source"]
        if before_search is not None:
            await before_search()
        return httpx.Response(
            200,
            json={
                "elapsed_ms": 1,
                "hits": [
                    {"source_id": "owned-source", "chunk_id": "owned-chunk", "content": "Owned evidence", "score": 1}
                ],
            },
        )

    manager = KnowledgeManager(store, transport=httpx.MockTransport(remote) if remote_service is None else None)
    manager.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500" if remote_service is None else remote_service["endpoint"],
                "tenant_id": "owned",
                "api_key": "owned-projection-key-no-provider",
                "auto_memory": False,
            }
        )
    )
    service = LearningProjection(store, engine.journal, ledger, manager)
    async with store._unit() as session:
        row = await ledger.records_repository.find_by_id(identifier(record["id"]))
        projection = await ledger.projections.for_record(row.id)
        body = service._body(projection.data["payload"])
        target = snapshot_knowledge(store.configuration)
        projection.data = {
            **projection.data,
            "target": target,
            "workspace_id": service.workspace_id(LearningContext.model_validate(record["envelope"]["context"])),
            "request_id": uuid4().hex,
            "content_sha256": hashlib.sha256(body).hexdigest(),
            "source_id": "owned-source",
            "submission_started": True,
        }
        projection.state = "executing"
        await ledger.projections.save(projection)
        await session.flush()
        projection.state = "source_admitted"
        await ledger.projections.save(projection)
        source.update(
            {
                "id": "owned-source",
                "kind": "text",
                "status": "ingested",
                "filename": "forge-learning-" + record["id"] + ".json",
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "content_bytes": len(body),
                "n_chunks": 1,
                "metadata": {
                    "extra": {"learning_record_id": record["id"], "payload_digest": projection.payload_digest}
                },
                "created_at": "2026-10-13T00:00:00Z",
                "updated_at": "2026-10-13T00:00:00Z",
            }
        )
    return service, record, calls


async def test_wrong_source_identity_does_not_create_an_observation_receipt(store, tmp_path):
    import pytest

    service, record, calls = await admitted_projection(store, tmp_path, source_id="another-source")
    request = uuid4().hex
    with pytest.raises(ValueError, match="match|identity"):
        await service.observe(record["id"], request_id=request)
    async with store._unit():
        assert await service.ledger.events.for_request(identifier(record["id"]), request) is None
    assert (await service.status(record["id"]))["state"] == "source_admitted"
    assert calls == [("GET", "/api/v1/sources/owned-source")]


async def test_projection_identity_drift_during_observation_never_commits_a_receipt(store, tmp_path):
    import pytest
    from sqlalchemy import update

    from firefly_forge.learning_persistence import ForgeLearningProjectionEntity

    async def drift():
        async with store._unit() as session:
            await session.execute(
                update(ForgeLearningProjectionEntity)
                .where(ForgeLearningProjectionEntity.record_id == identifier(record["id"]))
                .values(id=uuid4())
            )

    service, record, _ = await admitted_projection(store, tmp_path, before_search=drift)
    request = uuid4().hex
    with pytest.raises(ValueError, match="identity|changed"):
        await service.observe(record["id"], request_id=request)
    async with store._unit():
        assert await service.ledger.events.for_request(identifier(record["id"]), request) is None
    assert (await service.status(record["id"]))["state"] == "source_admitted"


async def test_exact_receipt_retains_original_actor_anchors_and_citations_after_invalidation(
    store, tmp_path, monkeypatch
):
    from test_learning_invalidation_receipts import acting, retained

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import ProjectAccess

    service, record, calls = await admitted_projection(store, tmp_path)
    first = Principal(uuid4(), "https://issuer.invalid", "original-reviewer")
    second = Principal(uuid4(), "https://issuer.invalid", "later-reviewer")
    project_id = record["envelope"]["context"]["project_id"]
    access = ProjectAccess(store)
    await access.set_membership(project_id, first, "reviewer")
    await access.set_membership(project_id, second, "reviewer")
    before = await service.status(record["id"])
    request = uuid4().hex
    with acting(first):
        await service.observe(record["id"], request_id=request)
    history = await retained(service.ledger, record)
    with acting(second):
        await service.ledger.invalidate(record["id"], reason="Retained evidence changed", request_id=uuid4().hex)
    before_read = await retained(service.ledger, record)
    remote_count = len(calls)
    with acting(second):
        receipt = await service.observation_receipt(record["id"], request)
    assert receipt["actor_id"] == first.id.hex
    assert receipt["event_state"] == "verified" and receipt["current_record_state"] == "invalidated"
    assert receipt["current_projection_state"] == "verified"
    assert receipt["projection_id"] == before["id"]
    assert receipt["submission_request_id"] == before["request_id"]
    assert receipt["workspace_id"] == before["workspace_id"]
    assert receipt["target_revision"] == before["target"]["revision"]
    assert receipt["payload_digest"] == before["payload_digest"]
    assert receipt["content_sha256"] == before["content_sha256"]
    assert receipt["citations"] == [{"source_id": "owned-source", "chunk_id": "owned-chunk"}]
    assert receipt["digest"] == history[-1][2]
    assert len(calls) == remote_count and await retained(service.ledger, record) == before_read


async def test_already_verified_shortcut_never_fabricates_another_request_receipt(store, tmp_path):
    import pytest
    from test_learning_invalidation_receipts import retained

    service, record, calls = await admitted_projection(store, tmp_path)
    original, another = uuid4().hex, uuid4().hex
    await service.observe(record["id"], request_id=original)
    before = await retained(service.ledger, record)
    count = len(calls)
    assert (await service.observe(record["id"], request_id=another))["state"] == "verified"
    for request in [another, before[0][1]["request_id"]]:
        with pytest.raises(KeyError, match="not observed"):
            await service.observation_receipt(record["id"], request)
    assert len(calls) == count and await retained(service.ledger, record) == before


async def test_receipt_rechecks_new_credentials_without_mutating_events(store, tmp_path, monkeypatch):
    import pytest
    from test_learning_invalidation_receipts import retained

    service, record, _ = await admitted_projection(store, tmp_path)
    request = uuid4().hex
    await service.observe(record["id"], request_id=request)
    before = await retained(service.ledger, record)
    # An originally harmless retained workspace can become a configured credential later.
    value = (await service.status(record["id"]))["workspace_id"]
    monkeypatch.setenv("FORGE_TEST_API_TOKEN", value)
    with pytest.raises(ValueError, match="credentials") as failure:
        await service.observation_receipt(record["id"], request)
    assert value not in str(failure.value)
    assert await retained(service.ledger, record) == before


@pytest.mark.parametrize("authority", ["viewer", "revoked", "suspended", "foreign"])
async def test_observation_receipt_requires_current_review_authority(store, tmp_path, authority):
    from test_learning_invalidation_receipts import acting, retained

    from firefly_forge.identity import Principal
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    service, record, _ = await admitted_projection(store, tmp_path)
    reviewer = Principal(uuid4(), "https://issuer.invalid", "owned-reviewer")
    project_id = record["envelope"]["context"]["project_id"]
    access = ProjectAccess(store)
    await access.set_membership(project_id, reviewer, "reviewer")
    request = uuid4().hex
    with acting(reviewer):
        await service.observe(record["id"], request_id=request)
    if authority in {"viewer", "revoked"}:
        await access.set_membership(project_id, reviewer, "viewer" if authority == "viewer" else None)
    elif authority == "suspended":
        async with store._unit():
            repository = ForgeIdentityUserRepository()
            user = await repository.find_by_id(reviewer.id)
            user.enabled = False
            await repository.save(user)
    else:
        reviewer = Principal(uuid4(), "https://issuer.invalid", "foreign-reviewer")
    before = await retained(service.ledger, record)
    with acting(reviewer), pytest.raises(AccessDenied):
        await service.observation_receipt(record["id"], request)
    assert await retained(service.ledger, record) == before


@pytest.mark.parametrize("field", ["payload", "record_id", "run_id", "request_id", "sequence", "digest"])
async def test_changed_repository_result_never_becomes_an_observation_receipt(store, tmp_path, monkeypatch, field):
    import copy
    from types import SimpleNamespace

    from test_learning_invalidation_receipts import retained

    service, record, _ = await admitted_projection(store, tmp_path)
    request = uuid4().hex
    await service.observe(record["id"], request_id=request)
    before = await retained(service.ledger, record)
    original = service.ledger.events.for_request

    async def changed(record_id, request_id):
        actual = await original(record_id, request_id)
        data = copy.deepcopy(actual.data)
        data[field] = {"state": "verified"} if field == "payload" else (99 if field == "sequence" else "0" * 32)
        return SimpleNamespace(id=actual.id, sequence=actual.sequence, digest=actual.digest, data=data)

    monkeypatch.setattr(service.ledger.events, "for_request", changed)
    with pytest.raises(ValueError, match="integrity"):
        await service.observation_receipt(record["id"], request)
    assert await retained(service.ledger, record) == before


@pytest.mark.parametrize("change", ["review_revoked", "record_invalidated"])
async def test_observation_rechecks_authority_and_record_after_remote_read(store, tmp_path, change):
    from test_learning_invalidation_receipts import acting

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    async def change_admission():
        with acting(None):
            if change == "review_revoked":
                await ProjectAccess(store).set_membership(project_id, reviewer, "viewer")
            else:
                await service.ledger.invalidate(
                    record["id"], reason="Evidence changed during retrieval", request_id=uuid4().hex
                )

    service, record, _ = await admitted_projection(store, tmp_path, before_search=change_admission)
    reviewer = Principal(uuid4(), "https://issuer.invalid", "owned-reviewer")
    project_id = record["envelope"]["context"]["project_id"]
    await ProjectAccess(store).set_membership(project_id, reviewer, "reviewer")
    request = uuid4().hex
    error = AccessDenied if change == "review_revoked" else ValueError
    with acting(reviewer), pytest.raises(error):
        await service.observe(record["id"], request_id=request)
    async with store._unit():
        assert await service.ledger.events.for_request(identifier(record["id"]), request) is None
    assert (await service.status(record["id"]))["state"] == "source_admitted"
