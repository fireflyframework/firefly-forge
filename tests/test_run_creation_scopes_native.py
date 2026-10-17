# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared only: signed OIDC, current native ACLs and registered creation routes."""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_identity import fixture as identity_fixture
from test_identity import request as identity_request

from firefly_forge.api import create_application
from firefly_forge.identity import IdentityService
from firefly_forge.models import RepositoryInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.run_creation_models import RunCreationInput
from firefly_forge.run_creation_requests import RunCreationRequests


@asynccontextmanager
async def nonadministrator(store, tmp_path):
    identity, _, _, _, issue = await identity_fixture(store)
    try:
        credential = issue("forge-api", roles=["forge-worker"])
        actor = await identity.authenticate(identity_request(bearer="Bearer " + credential))
        assert actor is not None and actor.api_roles == frozenset({"forge-worker"})
        access = ProjectAccess(store)
        assert not await access.current_administrator(actor)
        repositories, projects = [], []
        for index in range(2):
            repository = await store.add_repository(
                RepositoryInput(name=f"Fictional stock {index}", url=str(tmp_path / f"source-{index}"))
            )
            project = await Projects(store).configure(
                ProjectDefinition(
                    name=f"Fictional product {index}",
                    description="Native admission only",
                    topology="monorepo",
                    owners=["Fictional team"],
                    components=[
                        {
                            "id": "stock-api",
                            "name": "Stock API",
                            "repository_id": repository["id"],
                            "responsibility": "Count fictional stock",
                        }
                    ],
                )
            )
            repositories.append(repository)
            projects.append(project)
        await access.set_membership(projects[0]["id"], actor, "developer")
        body = {
            "version": 1,
            "input": {
                "repository_id": repositories[0]["id"],
                "project_id": projects[0]["id"],
                "component_id": "stock-api",
                "brief": "Verify fictional stock without executing a worker",
                "operation": "test",
                "sandbox": {
                    "mode": "docker",
                    "context": "owned-native-admission-only",
                    "image": "owned-admission-fixture@sha256:" + "a" * 64,
                },
            },
        }
        yield identity, issue, credential, actor, access, repositories, projects, body
    finally:
        await identity.client.aclose()


async def counts(store):
    async with store._unit() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in ("forge_runs", "forge_events", "forge_run_creation_requests")
            ]
        )


async def test_registered_nonadmin_creation_routes_use_current_project_component_and_original_owner(
    store, tmp_path, monkeypatch
):
    async with nonadministrator(store, tmp_path) as c:
        identity, issue, credential, actor, access, repositories, projects, body = c
        for key, value in {
            "FORGE_AUTH_MODE": "oidc",
            "FORGE_API_TOKEN": "",
            "FORGE_OIDC_ISSUER": identity.config.issuer,
            "FORGE_OIDC_CLIENT_ID": identity.config.client_id,
            "FORGE_OIDC_CLIENT_SECRET": identity.config.client_secret.get_secret_value(),
            "FORGE_OIDC_API_AUDIENCE": identity.config.api_audience,
            "FORGE_OIDC_REDIRECT_URI": identity.config.redirect_uri,
            "FORGE_IDENTITY_ENCRYPTION_KEY": identity.config.encryption_key.get_secret_value(),
        }.items():
            monkeypatch.setenv(key, value)
        app = create_application(tmp_path / "api", role="api")
        async with app.router.lifespan_context(app):
            native_identity = app.state.runtime.context.get_bean(IdentityService)
            await native_identity.client.aclose()
            # Only the remote JWKS transport is replaced; the registered native verifier and ACLs run normally.
            native_identity.client = identity.client
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="https://testserver",
                headers={"Authorization": "Bearer " + credential},
            ) as client:
                assert (await client.get("/api/v1/auth/session")).json()["global_admin"] is False
                assert await counts(store) == (0, 0, 0)
                for changed, status in (
                    ({"project_id": projects[1]["id"]}, 403),
                    ({"repository_id": repositories[1]["id"]}, 403),
                    ({"component_id": "foreign-component"}, 409),
                    ({"project_id": None, "component_id": None}, 403),
                    ({"publish": True}, 403),
                    ({"sandbox": {"mode": "host"}}, 409),
                ):
                    denied = await client.post(
                        "/api/v1/run-creation-requests/" + uuid4().hex,
                        json={"version": 1, "input": {**body["input"], **changed}},
                    )
                    assert denied.status_code == status, denied.text
                    assert await counts(store) == (0, 0, 0)
                request_id = uuid4().hex
                url = "/api/v1/run-creation-requests/" + request_id
                admitted = await client.post(url, json=body)
                assert admitted.status_code == 201, admitted.text
                receipt = admitted.json()
                assert receipt["original_actor"]["user_id"] == actor.id.hex
                assert receipt["project_id"] == projects[0]["id"] and receipt["component_id"] == "stock-api"
                assert receipt["repository_id"] == repositories[0]["id"]
                repeated = await client.post(url, json=body)
                assert repeated.status_code == 200 and repeated.content == admitted.content
                assert (await client.get(url)).content == admitted.content
                assert await counts(store) == (1, 1, 1)
                other_credential = issue("forge-api", roles=["forge-worker"], sub="other-native-owner")
                other = await identity.authenticate(identity_request(bearer="Bearer " + other_credential))
                assert other is not None and other.id != actor.id
                await access.set_membership(projects[0]["id"], other, "developer")
                headers = {"Authorization": "Bearer " + other_credential}
                assert (await client.get(url, headers=headers)).status_code == 403
                assert (await client.post(url, json=body, headers=headers)).status_code == 403
                await access.set_membership(projects[0]["id"], actor, "viewer")
                read = await client.get(url)
                assert read.status_code == 200 and read.content == admitted.content
                assert (await client.post(url, json=body)).status_code == 403
                await access.set_membership(projects[0]["id"], actor, None)
                assert (await client.get(url)).status_code == 403
                await access.set_membership(projects[0]["id"], actor, "developer")
                assert (await client.get(url)).content == admitted.content
                assert await counts(store) == (1, 1, 1)


async def test_native_membership_revoked_after_real_preparation_refuses_all_admission_rows(
    store, tmp_path, monkeypatch
):
    async with nonadministrator(store, tmp_path) as c:
        _, _, _, actor, access, _, projects, body = c
        prepare = store.prepare_run

        async def revoke_after_preparation(request):
            prepared = await prepare(request)
            await access.set_membership(projects[0]["id"], actor, "viewer")
            return prepared

        monkeypatch.setattr(store, "prepare_run", revoke_after_preparation)
        context = CURRENT_PRINCIPAL.set(actor)
        try:
            manager = RunCreationRequests(store)
            request_id = uuid4().hex
            with pytest.raises(AccessDenied):
                await manager.admit(request_id, RunCreationInput.model_validate(body))
            assert await counts(store) == (0, 0, 0)
            with pytest.raises(KeyError):
                await manager.get(request_id)
        finally:
            CURRENT_PRINCIPAL.reset(context)
