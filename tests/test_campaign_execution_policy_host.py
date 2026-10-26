# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project execution windows require current native authority and exact CAS receipts."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest


@pytest.fixture
def policy_case(monkeypatch):
    from firefly_forge.campaign_execution_policy import CampaignExecutionPolicies
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

    state = {"enabled": True, "administrator": True, "events": {}}
    project = SimpleNamespace(id=UUID("a" * 32), data={"definition": {"components": []}})
    principal = Principal(UUID("b" * 32), "public-fixture", "owner", authentication_method="local_bootstrap")

    @asynccontextmanager
    async def unit():
        async def execute(*args):
            pass

        yield SimpleNamespace(execute=execute)

    async def project_find(*args, **kwargs):
        return project

    async def save(row):
        return row

    async def event_find(id):
        return state["events"].get(id)

    async def event_save(row):
        state["events"][row.id] = row
        return row

    async def admit(value):
        pass

    async def admin(self, user):
        return state["enabled"] and state["administrator"]

    async def require(self, user, project_id, permission):
        if not state["enabled"] or permission == "members" and not state["administrator"]:
            raise AccessDenied("Current project authority required")

    async def identity(self, *args, **kwargs):
        return SimpleNamespace(enabled=state["enabled"])

    monkeypatch.setattr(ProjectAccess, "current_administrator", admin)
    monkeypatch.setattr(ProjectAccess, "require_project", require)
    monkeypatch.setattr("firefly_forge.campaign_execution_policy.ForgeIdentityUserRepository.find_by_id", identity)
    store = SimpleNamespace(_unit=unit, admit_credentials=admit, schema="public_fixture")
    service = CampaignExecutionPolicies(
        store,
        records=SimpleNamespace(find_by_id=project_find, save=save),
        events=SimpleNamespace(find_by_id=event_find, save=event_save),
    )
    token = CURRENT_PRINCIPAL.set(principal)
    yield service, project, state
    CURRENT_PRINCIPAL.reset(token)


async def test_native_policy_configuration_is_cas_and_request_idempotent(policy_case):
    from firefly_forge.campaign_execution_budget import CampaignExecutionPolicyInputV1

    service, project, state = policy_case
    body = CampaignExecutionPolicyInputV1(
        request_id="c" * 32, expected_previous_digest=None, policy={"dispatch_window": 5000, "max_parallel_runs": 128}
    )
    first = await service.configure(project.id.hex, body)
    replay = await service.configure(project.id.hex, body)
    assert first["digest"] == replay["digest"] and first["policy"]["dispatch_window"] == 5000
    assert len(state["events"]) == 1
    with pytest.raises(ValueError, match="changed"):
        await service.configure(project.id.hex, body.model_copy(update={"request_id": "d" * 32}))
    assert len(state["events"]) == 1


async def test_read_authority_does_not_grant_policy_configuration(policy_case):
    from firefly_forge.campaign_execution_budget import CampaignExecutionPolicyInputV1
    from firefly_forge.project_access import AccessDenied

    service, project, state = policy_case
    state["administrator"] = False
    assert (await service.view(project.id.hex))["permissions"]["configure"] is False
    with pytest.raises(AccessDenied):
        await service.configure(
            project.id.hex,
            CampaignExecutionPolicyInputV1(request_id="c" * 32, expected_previous_digest=None, policy={}),
        )
    assert not state["events"]


async def test_policy_authority_revoked_during_admission_prevents_any_write(policy_case):
    from firefly_forge.campaign_execution_budget import CampaignExecutionPolicyInputV1
    from firefly_forge.project_access import AccessDenied

    service, project, state = policy_case

    async def admit(value):
        state["enabled"] = False

    service.store.admit_credentials = admit
    with pytest.raises(AccessDenied):
        await service.configure(
            project.id.hex,
            CampaignExecutionPolicyInputV1(request_id="c" * 32, expected_previous_digest=None, policy={}),
        )
    assert not state["events"] and "campaign_execution_policy" not in project.data


async def test_replayed_policy_acknowledges_one_coherent_historical_cut_without_reverting_current_storage(policy_case):
    import copy

    from firefly_forge.campaign_execution_budget import CampaignExecutionPolicyInputV1

    service, project, state = policy_case
    first_body = CampaignExecutionPolicyInputV1(
        request_id="c" * 32,
        expected_previous_digest=None,
        policy={"dispatch_window": 1, "max_parallel_runs": 1},
    )
    first = await service.configure(project.id.hex, first_body)
    second = await service.configure(
        project.id.hex,
        CampaignExecutionPolicyInputV1(
            request_id="d" * 32,
            expected_previous_digest=first["digest"],
            policy={"dispatch_window": 20, "max_parallel_runs": 2},
        ),
    )
    saved = copy.deepcopy(project.data)
    events = {id: copy.deepcopy(event.data) for id, event in state["events"].items()}
    replay = await service.configure(project.id.hex, first_body)
    assert replay["policy"] == first["policy"] and replay["policy"]["dispatch_window"] == 1
    assert replay["digest"] == replay["effective_digest"] == first["digest"]
    assert replay["request_id"] == first_body.request_id
    assert project.data == saved and saved["campaign_execution_policy"]["policy"]["dispatch_window"] == 20
    current = await service.view(project.id.hex)
    assert current["digest"] == current["effective_digest"] == second["digest"]
    assert current["policy"]["dispatch_window"] == 20
    assert len(state["events"]) == 2
    assert {id: event.data for id, event in state["events"].items()} == events
