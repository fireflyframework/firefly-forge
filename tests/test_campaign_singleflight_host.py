# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real snapshot-generation writes around a controlled, outside-UOW preparation barrier."""

import asyncio
from types import SimpleNamespace

import pytest

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns
from firefly_forge.store import current_unit_of_work


@pytest.fixture
def preparing_campaign(tmp_path):
    configuration = ConfigurationStore(tmp_path)
    service = ProjectCampaigns(SimpleNamespace(configuration=configuration), SimpleNamespace(), SimpleNamespace())
    state = SimpleNamespace(
        prepared=[], first=asyncio.Event(), second=asyncio.Event(), release=asyncio.Event(), rows={}
    )

    async def prepare(project_id, request):
        assert current_unit_of_work() is None
        key = project_id, request.request_id
        fingerprint = digest(request.model_dump())
        if key in state.rows:
            if state.rows[key]["input"] != fingerprint:
                raise ValueError("Campaign request identity already has another launch definition")
            return state.rows[key]["result"]
        configuration.snapshot(request.agent)
        generation = configuration.write_generation
        state.prepared.append(key)
        (state.first if len(state.prepared) == 1 else state.second).set()
        await state.release.wait()
        if generation is not configuration.write_generation:
            raise ValueError("Campaign settings changed during source and runner preflight; launch again")
        result = {"id": request.request_id}
        state.rows[key] = {"input": fingerprint, "result": result}
        return result

    service._launch = prepare
    token = CURRENT_PRINCIPAL.set(None)
    yield service, state
    CURRENT_PRINCIPAL.reset(token)


async def test_identical_requests_prepare_one_snapshot_and_both_observe_the_same_admission(preparing_campaign):
    service, state = preparing_campaign
    request = CampaignLaunch(request_id="a" * 32, brief="One owned delivery")
    settings = service.store.configuration.settings_generation
    first = asyncio.create_task(service.launch("b" * 32, request))
    await state.first.wait()
    second = asyncio.create_task(service.launch("b" * 32, request))
    await asyncio.sleep(0)
    state.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert len(state.prepared) == 1
    assert results == [{"id": request.request_id}] * 2
    assert service.store.configuration.settings_generation is settings
    assert len(state.rows) == 1
    assert not service._launch_locks


@pytest.mark.parametrize("distinct", ["project", "request", "input"])
async def test_distinct_scope_preparation_is_not_globally_serialized_and_generation_checks_remain(
    preparing_campaign, distinct
):
    service, state = preparing_campaign
    request = CampaignLaunch(request_id="a" * 32, brief="One owned delivery")
    first = asyncio.create_task(service.launch("b" * 32, request))
    await state.first.wait()
    second = asyncio.create_task(
        service.launch(
            "c" * 32 if distinct == "project" else "b" * 32,
            request.model_copy(update={"request_id": "d" * 32})
            if distinct == "request"
            else request.model_copy(update={"brief": "Different owned delivery"})
            if distinct == "input"
            else request,
        )
    )
    try:
        await asyncio.wait_for(state.second.wait(), 2)
    finally:
        state.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert len(state.prepared) == 2
    assert any(isinstance(result, ValueError) and "settings changed" in str(result) for result in results)


async def test_cancelled_waiter_never_releases_another_callers_reservation(preparing_campaign):
    service, state = preparing_campaign
    request = CampaignLaunch(request_id="a" * 32, brief="One owned delivery")
    first = asyncio.create_task(service.launch("b" * 32, request))
    await state.first.wait()
    second = asyncio.create_task(service.launch("b" * 32, request))
    await asyncio.sleep(0)
    second.cancel()
    cancelled = await asyncio.gather(second, return_exceptions=True)
    state.release.set()
    completed = await asyncio.gather(first, return_exceptions=True)
    assert isinstance(cancelled[0], asyncio.CancelledError)
    assert completed == [{"id": request.request_id}]
    assert len(state.prepared) == 1
    assert await service.launch("b" * 32, request) == completed[0]


async def test_cancelled_owner_releases_inflight_scope_without_sharing_its_exception(preparing_campaign):
    service, state = preparing_campaign
    request = CampaignLaunch(request_id="a" * 32, brief="One owned delivery")
    first = asyncio.create_task(service.launch("b" * 32, request))
    await state.first.wait()
    second = asyncio.create_task(service.launch("b" * 32, request))
    await asyncio.sleep(0)
    first.cancel()
    cancelled = await asyncio.gather(first, return_exceptions=True)
    try:
        await asyncio.wait_for(state.second.wait(), 2)
    finally:
        state.release.set()
    assert isinstance(cancelled[0], asyncio.CancelledError)
    assert await second == {"id": request.request_id}
    assert len(state.prepared) == 2 and len(state.rows) == 1


async def test_waiting_and_returning_callers_recheck_current_authority(preparing_campaign, monkeypatch):
    from uuid import uuid4

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    service, state = preparing_campaign
    permission = {"enabled": True}

    async def require(access, actor, project_id, action):
        if not permission["enabled"]:
            raise AccessDenied("Current campaign authority revoked")

    monkeypatch.setattr(ProjectAccess, "require_project", require)
    actor = Principal(uuid4(), "controlled", "owner", authentication_method="local_bootstrap")
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        request = CampaignLaunch(request_id="a" * 32, brief="One owned delivery")
        first = asyncio.create_task(service.launch("b" * 32, request))
        await state.first.wait()
        second = asyncio.create_task(service.launch("b" * 32, request))
        await asyncio.sleep(0)
        permission["enabled"] = False
        state.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, AccessDenied) for result in results)
        assert len(state.prepared) == 1
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_caller_writer_unit_is_refused_before_any_mutex_wait_or_snapshot(preparing_campaign, monkeypatch):
    service, state = preparing_campaign
    state.release.set()
    monkeypatch.setattr("pyfly.data.transaction.current_unit_of_work", lambda: object())
    request = CampaignLaunch(request_id="a" * 32, brief="One owned delivery")
    with pytest.raises(ValueError, match="outside.*transaction"):
        await service.launch("b" * 32, request)
    assert not state.prepared and not state.rows
    assert not service._launch_locks
