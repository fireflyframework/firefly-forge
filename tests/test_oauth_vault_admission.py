# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Two native API replicas must admit text against the current encrypted vault."""

from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from test_connection_oauth_ledger import actor, configuration
from test_connection_oauth_ledger import service as connection_service
from test_oauth_provider_policy_api import BASE, PROVIDER, registration

from firefly_forge.api import create_application
from firefly_forge.connection_oauth import ConnectionOAuth

TOKEN = "fixture-factory-vault-operator-32-characters"


def native_configuration(**overrides):
    return configuration(**overrides).model_copy(update={"provider": PROVIDER})


@asynccontextmanager
async def replicas(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_IDENTITY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "https://127.0.0.1:49200")
    monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "127.0.0.1")
    applications = [create_application(tmp_path / "shared", TOKEN, role="api") for _ in range(2)]
    async with (
        applications[0].router.lifespan_context(applications[0]),
        applications[1].router.lifespan_context(applications[1]),
    ):
        services = [app.state.runtime.context.get_bean(ConnectionOAuth) for app in applications]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=applications[1], client=("127.0.0.1", 50000)),
            base_url="https://127.0.0.1:49200",
            headers={"Authorization": "Bearer " + TOKEN},
        ) as client:
            policy = await client.put(BASE + "/" + PROVIDER, json=registration())
            assert policy.status_code == 200, policy.text
            yield services, client


@pytest.mark.parametrize("rotate", [False, True])
async def test_stale_replica_rejects_new_and_rotated_secrets_from_role_instructions(
    tmp_path, postgres_schema, monkeypatch, rotate
):
    async with replicas(tmp_path, monkeypatch) as (services, client):
        first = "owned-client-secret-" + uuid4().hex
        await services[0].configure("design", native_configuration(client_secret=first), actor())
        secrets = [first]
        if rotate:
            second = "owned-rotated-secret-" + uuid4().hex
            await services[0].configure("design", native_configuration(client_secret=second), actor())
            await services[0].disconnect("design", actor())
            secrets.append(second)
        document = (await client.get("/api/v1/agent-roles")).json()["document"]
        for value in secrets:
            document["roles"][0]["instructions"] = "Never publish " + value
            result = await client.put("/api/v1/agent-roles", json=document)
            assert result.status_code == 409, result.text
            assert value not in result.text
        retained = (await client.get("/api/v1/agent-roles")).json()["document"]
        assert all(value not in str(retained) for value in secrets)


async def test_current_vault_positive_role_edit_remains_usable(tmp_path, postgres_schema, monkeypatch):
    async with replicas(tmp_path, monkeypatch) as (services, client):
        await services[0].configure("design", native_configuration(client_secret="owned-only-secret"), actor())
        document = (await client.get("/api/v1/agent-roles")).json()["document"]
        document["roles"][0]["instructions"] = "Review the customer journey and report reproducible findings."
        result = await client.put("/api/v1/agent-roles", json=document)
        assert result.status_code == 200, result.text
        assert result.json()["document"]["roles"][0]["instructions"] == document["roles"][0]["instructions"]


async def test_shared_vault_encryption_key_cannot_become_public_role_instructions(
    tmp_path, postgres_schema, monkeypatch
):
    async with replicas(tmp_path, monkeypatch) as (services, client):
        key = services[0].store.credential_registry()["FORGE_IDENTITY_ENCRYPTION_KEY"]
        document = (await client.get("/api/v1/agent-roles")).json()["document"]
        document["roles"][0]["instructions"] = "Keep this internal: " + key
        result = await client.put("/api/v1/agent-roles", json=document)
        assert result.status_code == 409, result.text
        assert key not in result.text


async def test_stale_replica_rejects_current_secret_from_public_model_profile_fields(
    tmp_path, postgres_schema, monkeypatch
):
    async with replicas(tmp_path, monkeypatch) as (services, client):
        value = "owned-client-secret-" + uuid4().hex
        await services[0].configure("design", native_configuration(client_secret=value), actor())
        result = await client.put(
            "/api/v1/model-profiles/design",
            json={"name": "Design model", "description": value, "provider": "agentic", "values": {}},
        )
        assert result.status_code == 409, result.text
        assert value not in result.text


async def test_new_replica_rejects_rotated_and_erased_grant_tokens(store, store_factory, tmp_path):
    from test_connection_oauth_flows import complete, local_provider, setup

    async with local_provider() as (origin, _, _, _, tokens):
        writer, user, challenge, callback, key, policy = await setup(store, origin)
        replica_store = await store_factory(tmp_path / "replica" / "factory.db")
        reader = ConnectionOAuth(
            replica_store,
            encryption_key=key,
            browser_origin="https://forge.example",
            catalog={"fixture": policy},
            allow_loopback_http=True,
        )
        await reader.start()
        await complete(writer, user, challenge, callback)
        await writer.refresh("design", uuid4().hex, user)
        await writer.disconnect("design", user)
        for packet in tokens:
            for name in ("access_token", "refresh_token"):
                with pytest.raises(ValueError, match="credentials"):
                    await replica_store.admit_credentials("Retained model context: " + packet[name])


@pytest.mark.parametrize("target", ["roles", "models"])
async def test_retained_public_text_is_withheld_after_later_vault_publication(
    tmp_path, postgres_schema, monkeypatch, target
):
    async with replicas(tmp_path, monkeypatch) as (services, client):
        value = "future-owned-credential-" + uuid4().hex
        if target == "roles":
            document = (await client.get("/api/v1/agent-roles")).json()["document"]
            document["roles"][0]["instructions"] = value
            saved = await client.put("/api/v1/agent-roles", json=document)
            endpoint = "/api/v1/agent-roles"
        else:
            saved = await client.put(
                "/api/v1/model-profiles/design",
                json={"name": "Design model", "description": value, "provider": "agentic", "values": {}},
            )
            endpoint = "/api/v1/model-profiles"
        assert saved.status_code == 200, saved.text
        await services[0].configure("design", native_configuration(client_secret=value), actor())
        retained = await client.get(endpoint)
        assert retained.status_code == 409, retained.text
        assert value not in retained.text


async def paired_services(store, store_factory, tmp_path):
    replica = await store_factory(tmp_path / "replica" / "factory.db")
    key = Fernet.generate_key().decode()
    writer, reader = connection_service(store, key), connection_service(replica, key)
    await writer.start()
    await reader.start()
    return writer, replica


async def test_run_brief_checks_current_native_vault_before_queue_admission(store, store_factory, tmp_path):
    from firefly_forge.models import RepositoryInput, RunInput

    writer, replica = await paired_services(store, store_factory, tmp_path)
    repo = await store.add_repository(RepositoryInput(name="Product", url="https://github.com/team/product.git"))
    value = "owned-client-secret-" + uuid4().hex
    await writer.configure("design", configuration(client_secret=value), actor())
    with pytest.raises(ValueError, match="credentials"):
        await replica.create_run(RunInput(repository_id=repo["id"], brief="Implement " + value))
    assert await replica.runs() == []


async def test_dialogue_checks_current_native_vault_before_wal_or_designer(store, store_factory, tmp_path):
    from firefly_forge.models import RepositoryInput
    from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant
    from firefly_forge.product_assistant_runtime import AssistantReply

    writer, replica = await paired_services(store, store_factory, tmp_path)
    repo = await store.add_repository(RepositoryInput(name="Product", url="https://github.com/team/product.git"))
    calls = []

    async def designer(prompt, settings):
        calls.append(prompt)
        return AssistantReply(message="This fixture should not dispatch.")

    assistant = ProductAssistant(replica, tmp_path / "assistant", designer=designer)
    session = await assistant.create(repo["id"], AssistantSessionInput())
    value = "owned-client-secret-" + uuid4().hex
    await writer.configure("design", configuration(client_secret=value), actor())
    with pytest.raises(ValueError, match="credentials"):
        await assistant.turn(repo["id"], session["id"], AssistantTurnInput(request_id=uuid4().hex, message=value))
    assert calls == []
    assert (await assistant.session(repo["id"], session["id"]))["turns"] == []


async def test_broker_checks_current_native_vault_before_tool_intent_or_dispatch(store, store_factory, tmp_path):
    from test_tool_broker import seed

    writer, replica = await paired_services(store, store_factory, tmp_path)
    _, broker, run, source = await seed(replica, tmp_path)
    value = "owned-client-secret-" + uuid4().hex
    await writer.configure("design", configuration(client_secret=value), actor())
    calls = []

    async def external():
        calls.append(1)
        return {"results": []}

    with pytest.raises(ValueError, match="credentials"):
        await broker.invoke(
            run["id"],
            "implement",
            "internet_search",
            uuid4().hex,
            {"query": "Find " + value, "max_results": 1},
            external,
            source=source,
            configuration=run["search_snapshot"],
        )
    assert calls == []
    assert await broker.invocations(run["id"]) == []


async def test_direct_search_api_checks_current_vault_before_provider_dispatch(tmp_path, postgres_schema, monkeypatch):
    async with replicas(tmp_path, monkeypatch) as (services, client):
        calls = []

        async def provider(request):
            calls.append(request)
            return httpx.Response(200, json={"results": []})

        search = services[1].store.runners.search
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as remote:
            search.client = remote
            search.configure({"provider": "tavily", "api_key": "owned-fixture-search-key"})
            value = "owned-client-secret-" + uuid4().hex
            await services[0].configure("design", native_configuration(client_secret=value), actor())
            result = await client.post("/api/v1/search/query", json={"query": "Find " + value, "max_results": 1})
            assert result.status_code == 409, result.text
            assert calls == []


async def test_structured_numeric_tool_result_cannot_reflect_a_current_credential(store, store_factory, tmp_path):
    from test_tool_broker import invoke, seed

    writer, replica = await paired_services(store, store_factory, tmp_path)
    module, broker, run, source = await seed(replica, tmp_path)
    secret = "12345678901234567890"
    await writer.configure("design", configuration(client_secret=secret), actor())
    calls = []

    async def external():
        calls.append(1)
        return {"results": [], "observed_limit": int(secret)}

    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, source, "c" * 32, external)
    assert calls == [1]
    assert (await broker.invocations(run["id"]))[0]["status"] == "unknown"
