# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project authorization must leave the JSON body available to native controllers."""

import asyncio

from httpx import ASGITransport, AsyncClient
from test_project_access import principal, seed

from firefly_forge.api import create_application
from firefly_forge.identity import IdentityService
from firefly_forge.project_access import ProjectAccess


async def test_project_owner_can_update_product_after_body_authorization(tmp_path, postgres_schema, monkeypatch):
    owner = principal()

    async def authenticate(self, request):
        return owner

    monkeypatch.setattr(IdentityService, "authenticate", authenticate)
    app = create_application(tmp_path / "server", "local-test-operator-token-32chars", role="api")
    async with app.router.lifespan_context(app):
        _, projects = await seed(app.state.store, tmp_path)
        project = projects[0]
        await app.state.runtime.context.get_bean(ProjectAccess).set_membership(project["id"], owner, "owner")
        definition = {**project["definition"], "description": "Reserve inventory for the customer's café"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            async with asyncio.timeout(3):
                response = await client.put("/api/v1/projects/" + project["id"], json=definition)
            assert response.status_code == 200, response.text
            assert response.json()["definition"] == definition
            current = await client.get("/api/v1/projects/" + project["id"])
            assert current.json()["definition"] == definition
