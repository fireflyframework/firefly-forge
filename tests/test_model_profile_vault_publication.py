# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Generated public metadata must be admitted before local profile publication."""

import pytest
from test_connection_oauth_ledger import actor, configuration
from test_oauth_vault_admission import replicas


@pytest.mark.parametrize("credential", ["agentic", "unverified", "false"])
async def test_generated_profile_metadata_rejects_credentials_without_mutating_configuration(
    tmp_path, postgres_schema, monkeypatch, credential
):
    async with replicas(tmp_path, monkeypatch) as (services, client):
        profiles = services[1].store.task_router.profiles
        before = profiles.path.read_bytes() if profiles.path.exists() else None
        await services[0].configure("design", configuration(client_secret=credential), actor())
        response = await client.put(
            "/api/v1/model-profiles/design",
            json={"name": "Design model", "description": "Design guidance", "provider": "agentic", "values": {}},
        )
        assert response.status_code == 409, response.text
        assert credential not in response.text
        assert (profiles.path.read_bytes() if profiles.path.exists() else None) == before


async def test_profile_change_during_native_admission_cannot_publish_a_different_unchecked_response(
    tmp_path, postgres_schema, monkeypatch
):
    from firefly_forge.model_profiles import ModelProfileInput

    async with replicas(tmp_path, monkeypatch) as (services, client):
        store = services[1].store
        profiles = store.task_router.profiles
        original = store.admit_credentials
        calls = []

        async def concurrent_change(value):
            await original(value)
            calls.append(value)
            if len(calls) == 2:
                profiles.save("other", ModelProfileInput(name="Other model", provider="agentic"))

        monkeypatch.setattr(store, "admit_credentials", concurrent_change)
        response = await client.put(
            "/api/v1/model-profiles/design", json={"name": "Design model", "provider": "agentic", "values": {}}
        )
        assert response.status_code == 409, response.text
        assert "changed" in response.text
        assert [profile["id"] for profile in profiles.catalog()] == ["other"]
