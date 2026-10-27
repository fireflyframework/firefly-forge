# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED ONLY: native admission tests for the sole PostgreSQL/API operator."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from starlette.testclient import TestClient
from test_identity import fixture as identity_fixture
from test_identity import request as identity_request

from firefly_forge.api import create_application
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.models import RepositoryInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.run_creation_models import RunCreationInput
from firefly_forge.run_creation_requests import RunCreationRequests

REQUEST = "e" * 32


@asynccontextmanager
async def registered(store, tmp_path):
    identity, _, _, _, issue = await identity_fixture(store)
    token = None
    try:
        actor = await identity.authenticate(identity_request(bearer="Bearer " + issue("forge-api")))
        assert actor is not None
        token = CURRENT_PRINCIPAL.set(actor)
        repo = await store.add_repository(RepositoryInput(name="Fictional stock", url=str(tmp_path)))
        body = RunCreationInput.model_validate(
            {"version": 1, "input": {"repository_id": repo["id"], "brief": "Fictional admission only"}}
        )
        yield actor, repo, body
    finally:
        if token is not None:
            CURRENT_PRINCIPAL.reset(token)
        await identity.client.aclose()


async def test_native_original_creation_concurrent_identity_and_immutable_readback(store, store_factory, tmp_path):
    async with registered(store, tmp_path) as (_, _, body):
        second = await store_factory(tmp_path / "second" / "factory.db")
        first, other = RunCreationRequests(store), RunCreationRequests(second)
        results = await asyncio.gather(first.admit(REQUEST, body), other.admit(REQUEST, body))
        assert sorted(created for _, created in results) == [False, True]
        assert results[0][0] == results[1][0]
        receipt = results[0][0]
        runs = await store.runs()
        assert len(runs) == 1 and runs[0]["id"] == receipt["run_id"]
        assert len(runs[0]["events"]) == 1 and runs[0]["events"][0]["stage"] == "queued"
        await store.finish(receipt["run_id"], "failed", error="Fictional terminal state")
        assert await first.get(REQUEST) == receipt
        assert (await first.admit(REQUEST, body)) == (receipt, False)
        for statement in (
            "UPDATE forge_run_creation_requests SET request_digest=:digest WHERE id=:request",
            "DELETE FROM forge_run_creation_requests WHERE id=:request",
        ):
            with pytest.raises(DBAPIError):
                async with store._unit() as session:
                    await session.execute(text(statement), {"digest": "0" * 64, "request": REQUEST})
        assert await first.get(REQUEST) == receipt


async def test_native_receipt_append_failure_rolls_back_entire_creation(store, tmp_path, monkeypatch):
    async with registered(store, tmp_path) as (_, _, body):
        manager = RunCreationRequests(store)
        save = manager.records.save

        async def fail(row):
            await save(row)
            raise ValueError("Owned receipt insertion failure")

        monkeypatch.setattr(manager.records, "save", fail)
        with pytest.raises(ValueError, match="insertion failure"):
            await manager.admit(REQUEST, body)
        assert await store.runs() == []
        async with store._unit() as session:
            assert await session.scalar(text("SELECT count(*) FROM forge_events")) == 0
            assert await session.scalar(text("SELECT count(*) FROM forge_run_creation_requests")) == 0
        with pytest.raises(KeyError):
            await manager.get(REQUEST)
        monkeypatch.setattr(manager.records, "save", save)
        receipt, created = await manager.admit(REQUEST, body)
        assert created and len(await store.runs()) == 1
        assert await manager.get(REQUEST) == receipt


async def test_native_disabled_original_actor_cannot_read_or_replay_creation(store, tmp_path):
    async with registered(store, tmp_path) as (actor, _, body):
        manager = RunCreationRequests(store)
        receipt, _ = await manager.admit(REQUEST, body)
        async with store._unit():
            users = ForgeIdentityUserRepository()
            user = await users.find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
            assert user is not None and user.enabled
            user.enabled = False
            await users.save(user)
        with pytest.raises(AccessDenied):
            await manager.get(REQUEST)
        with pytest.raises(AccessDenied):
            await manager.admit(REQUEST, body)
        assert (await store.runs())[0]["id"] == receipt["run_id"]


def test_native_registered_creation_api_exact_replay_and_legacy_contract(tmp_path, postgres_schema):
    from native_client_knowledge_fixtures import activate_native_client_target

    token = "owned-original-run-creation-bootstrap-fixture"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52561),
    ) as client:
        client.portal.call(activate_native_client_target, client.app.state.store, tmp_path / "framework-prerequisite")
        assert client.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        prerequisite_runs = client.get("/api/v1/runs").json()
        repo = client.post("/api/v1/repositories", json={"name": "Fictional admission", "url": str(tmp_path)})
        assert repo.status_code == 201
        body = {"version": 1, "input": {"repository_id": repo.json()["id"], "brief": "Fictional admission only"}}
        url = "/api/v1/run-creation-requests/" + REQUEST
        assert client.get(url).status_code == 404
        original = client.post(url, json=body)
        assert original.status_code == 201, original.text
        assert client.get(url).content == original.content
        repeated = client.post(url, json=body)
        assert repeated.status_code == 200 and repeated.content == original.content
        conflicting = {"version": 1, "input": {**body["input"], "commands": []}}
        assert client.post(url, json=conflicting).status_code == 409
        runs = client.get("/api/v1/runs").json()
        runs = [run for run in runs if run["id"] not in {item["id"] for item in prerequisite_runs}]
        assert len(runs) == 1 and runs[0]["id"] == original.json()["run_id"]
        assert runs[0]["status"] == "queued"
        legacy = client.post("/api/v1/runs", json=body["input"])
        assert legacy.status_code == 201 and "request_id" not in legacy.json()
        assert legacy.json()["id"] != original.json()["run_id"]
