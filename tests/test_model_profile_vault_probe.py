# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Retained public profile probes obey current native credential custody."""

from uuid import uuid4

import pytest
from test_connection_oauth_ledger import actor, configuration
from test_oauth_vault_admission import replicas


@pytest.mark.parametrize("credential", ["agentic", "unverified", "false"])
async def test_model_connection_probe_checks_current_public_metadata(
    tmp_path, postgres_schema, monkeypatch, credential
):
    async with replicas(tmp_path, monkeypatch) as (services, reader):
        created = await reader.put(
            "/api/v1/model-profiles/design", json={"name": "Owned model", "provider": "agentic", "values": {}}
        )
        assert created.status_code == 200
        # Reconstruct actual encrypted legacy history; prospective connection
        # admission now refuses a secret colliding with its own JSON packet.
        await services[0]._configure("design", configuration(client_secret=credential), actor())
        calls = []
        profiles = services[1].store.task_router.profiles
        original = profiles.test

        async def observed(name):
            calls.append(name)
            return await original(name)

        monkeypatch.setattr(profiles, "test", observed)
        response = await reader.post("/api/v1/model-profiles/design/test")
        assert response.status_code == 409, response.text
        assert credential not in response.text
        assert calls == []


async def test_generated_probe_result_is_admitted_before_public_return(tmp_path, postgres_schema, monkeypatch):
    async with replicas(tmp_path, monkeypatch) as (services, reader):
        created = await reader.put(
            "/api/v1/model-profiles/design", json={"name": "Owned model", "provider": "agentic", "values": {}}
        )
        assert created.status_code == 200
        credential = "owned-probe-credential-" + uuid4().hex
        await services[0].configure("design", configuration(client_secret=credential), actor())
        calls = []

        async def observed(name):
            calls.append(name)
            return {"profile_id": name, "diagnostic": credential}

        monkeypatch.setattr(services[1].store.task_router.profiles, "test", observed)
        response = await reader.post("/api/v1/model-profiles/design/test")
        assert response.status_code == 409, response.text
        assert credential not in response.text
        assert calls == ["design"]
