# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Configuration advertises current authority without denying readable project state."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

from firefly_forge.git_delivery_models import GitDeliveryPolicyInputV1
from firefly_forge.git_delivery_policy import GitDeliveryPolicies
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


@pytest.fixture
def policy_case(monkeypatch):
    state = {"role": "viewer", "enabled": True, "events": {}}
    project = SimpleNamespace(id=UUID("3" * 32), data={"definition": {"components": [{"repository_id": "2" * 32}]}})
    user = Principal(UUID("4" * 32), "public-fixture", "owner", authentication_method="local_bootstrap")

    @asynccontextmanager
    async def unit():
        async def execute(*args):
            pass

        yield SimpleNamespace(execute=execute)

    async def find(id, **kwargs):
        return project

    async def save(row):
        return row

    async def repo(id):
        return {"id": id, "provider": "github", "connection_provider": "github"}

    async def reject(value):
        pass

    async def event(id):
        return state["events"].get(id)

    async def event_save(row):
        state["events"][row.id] = row
        return row

    async def authority(self, project_id, *, configure=False, remote=False):
        if not state["enabled"] or (configure and state["role"] != "owner"):
            raise AccessDenied("Current owner required")
        return {"user_id": user.id.hex, "authentication_method": "local_bootstrap"}

    async def authorize(self, actor, project_id, permission):
        if not state["enabled"] or (permission in {"members", "release"} and state["role"] != "owner"):
            raise AccessDenied("Current owner required")
        return state["role"] == "owner"

    async def identity(self, id, **kwargs):
        return SimpleNamespace(enabled=state["enabled"])

    monkeypatch.setattr(GitDeliveryPolicies, "_authority", authority)
    monkeypatch.setattr("firefly_forge.project_access.ProjectAccess.authorize_actor", authorize)
    monkeypatch.setattr("firefly_forge.git_delivery_policy.ForgeIdentityUserRepository.find_by_id", identity)
    store = SimpleNamespace(_unit=unit, repository=repo, admit_credentials=reject, schema="public_fixture")
    service = GitDeliveryPolicies(
        store, SimpleNamespace(find_by_id=find, save=save), SimpleNamespace(find_by_id=event, save=event_save)
    )
    token = CURRENT_PRINCIPAL.set(user)
    yield service, state, project
    CURRENT_PRINCIPAL.reset(token)


async def test_viewer_can_read_disabled_defaults_without_configuration_permission(policy_case):
    service, _, _ = policy_case
    result = await service.view("3" * 32)
    assert result["policy"]["enabled"] is False and result["digest"] is None
    assert result["permissions"] == {"configure": False, "publish": False}
    assert result["remote_capabilities"][0]["draft_updates"] is True


async def test_policy_cas_receipt_and_disabled_remote_revocation_remain_explicit(policy_case):
    service, state, project = policy_case
    state["role"] = "owner"
    body = GitDeliveryPolicyInputV1(
        version=1, request_id="a" * 32, expected_previous_digest=None, policy={"enabled": True}
    )
    first = await service.configure("3" * 32, body)
    assert first["policy"]["enabled"] and first["request_id"] == body.request_id
    assert await service.configure("3" * 32, body) == first
    assert len(state["events"]) == 1
    with pytest.raises(ValueError, match="changed"):
        await service.configure("3" * 32, body.model_copy(update={"request_id": "b" * 32}))
    state["enabled"] = False
    with pytest.raises(AccessDenied):
        await service.configure("3" * 32, body)
    assert project.data["git_delivery_policy"]["digest"] == first["digest"]
