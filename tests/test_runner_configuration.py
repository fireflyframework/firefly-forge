# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import importlib
import json

import pytest


async def service(store, tmp_path):
    from firefly_forge.browser_runner import BrowserRunner
    from firefly_forge.journal import Journal
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.search_tools import SearchTools

    executor = SandboxExecutor(store, Journal(store))
    try:
        cls = importlib.import_module("firefly_forge.runner_configuration").RunnerConfiguration
    except ImportError as exc:
        raise AssertionError("Durable repository runner defaults are missing") from exc
    return cls(store, executor, SearchTools(tmp_path / "private"), BrowserRunner(executor, tmp_path / "private"))


@pytest.mark.asyncio
async def test_repository_runner_defaults_freeze_and_explicit_override_does_not_mutate_defaults(store, tmp_path):
    from firefly_forge.models import RepositoryInput
    from firefly_forge.sandbox_models import SandboxInput

    settings = await service(store, tmp_path)
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    assert (await settings.capture_run(repo["id"]))["sandbox_snapshot"]["mode"] == "host"
    settings_module = importlib.import_module("firefly_forge.runner_configuration")
    await settings.configure_repository(
        repo["id"], settings_module.RunnerDefaults(sandbox=SandboxInput(command_limit=3))
    )
    frozen = await settings.capture_run(repo["id"])
    await settings.configure_repository(
        repo["id"], settings_module.RunnerDefaults(sandbox=SandboxInput(command_limit=7))
    )
    assert frozen["sandbox_snapshot"]["configuration"]["command_limit"] == 3
    explicit = await settings.capture_run(repo["id"], SandboxInput(command_limit=2))
    assert explicit["sandbox_snapshot"]["configuration"]["command_limit"] == 2
    assert (await settings.capture_run(repo["id"]))["sandbox_snapshot"]["configuration"]["command_limit"] == 7
    assert settings.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_repository_runner_only_freezes_nonsecret_search_revision_and_enforces_configuration(store, tmp_path):
    from firefly_forge.models import RepositoryInput
    from firefly_forge.sandbox_models import SandboxInput

    settings = await service(store, tmp_path)
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    with pytest.raises(ValueError, match="search"):
        await settings.capture_run(repo["id"], SandboxInput(internet_search=True))
    settings.search.configure({"api_key": "selected-private-search-key"})
    snapshot = await settings.capture_run(repo["id"], SandboxInput(internet_search=True))
    assert snapshot["search_snapshot"]["configured"]
    assert "selected-private-search-key" not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_search_rejects_shared_factory_secrets_before_provider_request(tmp_path):
    import httpx

    from firefly_forge.search_tools import SearchTools

    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"web": {"results": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        search = SearchTools(tmp_path, client=client)
        search.configure({"api_key": "search-selected-key"})
        search.credential_registry = lambda: {"FORGE_SCM_TOKEN": "private-scm-token"}
        with pytest.raises(ValueError, match="credential"):
            await search.search("Find private-scm-token")
        assert calls == []


def test_native_runner_configuration_routes_are_authenticated_and_write_only(tmp_path, postgres_schema):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application

    token = "test-only-runner-owner-token-32chars"
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_application(tmp_path, token), client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/v1/search/configuration").status_code == 401
        repo = client.post(
            "/api/v1/repositories",
            headers=headers,
            json={"name": "product", "url": "https://github.com/team/product.git"},
        ).json()
        route = "/api/v1/repositories/" + repo["id"] + "/runner"
        assert (
            client.put(route, headers=headers, json={"sandbox": {"mode": "host", "command_limit": 3}}).status_code
            == 200
        )
        assert client.get(route, headers=headers).json()["sandbox"]["command_limit"] == 3
        response = client.put(
            "/api/v1/search/configuration", headers=headers, json={"api_key": "private-http-search-key"}
        )
        assert response.status_code == 200 and "private-http-search-key" not in response.text
        assert client.get("/api/v1/search/configuration", headers=headers).json()["configured"]
        schema = client.get("/api/v1/openapi.json", headers=headers).json()
        assert "/api/v1/repositories/{id}/runner" in schema["paths"]
        assert "/api/v1/runner-evidence/{id}/screenshot" in schema["paths"]
        assert client.get("/api/v1/runner-evidence/../../private/screenshot", headers=headers).status_code in {400, 404}
