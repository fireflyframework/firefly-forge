# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED ONLY: actual PostgreSQL/API controls for the sole native operator."""

import asyncio
import json
import time

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient
from test_api import TOKEN
from test_collaboration import product
from test_identity import fixture as identity_fixture
from test_identity import request as identity_request

from firefly_forge.access_routes import AccessRoutes
from firefly_forge.api import create_application
from firefly_forge.collaboration import Collaboration, ConditionalProductSave
from firefly_forge.models import RepositoryInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects


async def test_native_product_conditional_concurrent_duplicate_can_insert_once(store, tmp_path):
    repo = await store.add_repository(RepositoryInput(name="Conditional product", url=str(tmp_path)))
    service = Collaboration(store)
    request = ConditionalProductSave(expected_revision=0, expected_digest=None, definition=product())
    results = await asyncio.gather(
        service.save_product(repo["id"], request.definition, condition=request),
        service.save_product(repo["id"], request.definition, condition=request),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    current = await service.product(repo["id"])
    assert current["revision"] == 1 and current["definition"] == product().model_dump()
    with pytest.raises(ValueError, match="changed"):
        await service.save_product(repo["id"], request.definition, condition=request)
    assert await service.product(repo["id"]) == current


async def test_native_product_conditional_insert_failure_rolls_back(store, tmp_path, monkeypatch):
    repo = await store.add_repository(RepositoryInput(name="Rollback product", url=str(tmp_path)))
    service = Collaboration(store)
    request = ConditionalProductSave(expected_revision=0, expected_digest=None, definition=product())
    save = service.products.save

    async def fail(row):
        await save(row)
        raise ValueError("Owned insert failure")

    monkeypatch.setattr(service.products, "save", fail)
    with pytest.raises(ValueError, match="Owned insert failure"):
        await service.save_product(repo["id"], request.definition, condition=request)
    assert await service.product(repo["id"]) is None
    monkeypatch.setattr(service.products, "save", save)
    assert (await service.save_product(repo["id"], request.definition, condition=request))["revision"] == 1


def test_native_product_conditional_registered_api_and_old_endpoint(tmp_path, postgres_schema):
    with TestClient(create_application(tmp_path, TOKEN, role="api"), client=("127.0.0.1", 50000)) as client:
        headers = {"Authorization": "Bearer " + TOKEN}
        response = client.post(
            "/api/v1/repositories", headers=headers, json={"name": "Conditional HTTP", "url": str(tmp_path)}
        )
        assert response.status_code == 201
        repo = response.json()["id"]
        url = f"/api/v1/repositories/{repo}/product"
        body = {"expected_revision": 0, "expected_digest": None, "definition": product().model_dump(mode="json")}
        assert client.put(url + "/conditional", json=body).status_code == 401
        assert client.get(url, headers=headers).json() is None
        first = client.put(url + "/conditional", headers=headers, json=body)
        assert first.status_code == 200, first.text
        assert first.json()["revision"] == 1
        assert client.put(url + "/conditional", headers=headers, json=body).status_code == 409
        assert client.get(url, headers=headers).json() == first.json()
        old = client.put(url, headers=headers, json=product().model_dump(mode="json"))
        assert old.status_code == 200 and old.json()["revision"] == 2
        assert old.json()["definition"] == first.json()["definition"]


async def test_native_product_conditional_nested_review_uses_current_native_permissions(store, tmp_path):
    identity, _, _, _, issue = await identity_fixture(store, initial_iat=int(time.time()) - 30)
    token = None
    try:
        administrator = await identity.authenticate(
            identity_request(bearer="Bearer " + issue("forge-api", iat=int(time.time()) - 25))
        )
        assert administrator is not None and await ProjectAccess(store).current_administrator(administrator)
        token = CURRENT_PRINCIPAL.set(administrator)
        repo = await store.add_repository(RepositoryInput(name="Scoped product", url=str(tmp_path)))
        project = await Projects(store).configure(
            ProjectDefinition(
                name="Scoped product",
                description="Fictional definition permission",
                topology="monorepo",
                owners=["Owner"],
                components=[dict(id="api", name="API", repository_id=repo["id"], responsibility="Serve stock")],
            )
        )
        access = ProjectAccess(store)
        await access.set_membership(project["id"], administrator, "developer")
        actor = await identity.authenticate(
            identity_request(
                bearer="Bearer "
                + issue(
                    "forge-api",
                    roles=["ordinary-user"],
                    realm_access={"roles": []},
                    iat=int(time.time()) - 10,
                )
            )
        )
        assert actor is not None and not await access.current_administrator(actor)
        CURRENT_PRINCIPAL.set(actor)
        body = json.dumps(
            {"expected_revision": 0, "expected_digest": None, "definition": {"functional_reviewed": True}}
        ).encode()

        async def receive():
            return {"type": "http.request", "body": body}

        request = Request(
            {
                "type": "http",
                "method": "PUT",
                "path": f"/api/v1/repositories/{repo['id']}/product/conditional",
                "headers": [],
            },
            receive,
        )
        await access.require_repository(actor, repo["id"], "edit")
        with pytest.raises(AccessDenied):
            await AccessRoutes(access, store).authorize(actor, request)
        assert await Collaboration(store).product(repo["id"]) is None
    finally:
        await identity.client.aclose()
        if token is not None:
            CURRENT_PRINCIPAL.reset(token)
