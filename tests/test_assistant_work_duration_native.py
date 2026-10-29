# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Frozen conversation deadlines and current authority survive durable ownership."""

import asyncio
import copy
import uuid

import pytest

from firefly_forge.assistant_persistence import ForgeAssistantSessionEntity
from firefly_forge.models import RepositoryInput
from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant, digest
from firefly_forge.product_assistant_runtime import WORK_DURATION_SETTING, AssistantReply
from firefly_forge.store import identifier, now
from firefly_forge.work_duration_models import WorkDurationPolicyV2


@pytest.mark.parametrize("timeout", [None, 864000])
async def test_new_conversation_binds_policy_and_reuses_completed_turn_without_model_replay(store, tmp_path, timeout):
    repository = await store.add_repository(RepositoryInput(name="Scoped design", url=str(tmp_path)))
    calls = []

    async def designer(prompt, settings):
        calls.append(copy.deepcopy(settings))
        return AssistantReply(message="Actual controlled design response")

    manager = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    policy = WorkDurationPolicyV2(timeout_seconds=timeout)
    session = await manager.create(repository["id"], AssistantSessionInput(work_duration=policy))
    assert session["work_duration"] == policy.model_dump()
    async with store._unit():
        events = await manager.events.events(identifier(session["id"]))
        assert events[0].data["payload"]["work_duration_digest"] == digest(policy.model_dump())
    request = AssistantTurnInput(request_id="a" * 32, message="Define the customer journey")
    result = await manager.turn(repository["id"], session["id"], request)
    assert result["status"] == "completed"
    assert result["context"]["work_duration"] == policy.model_dump()
    assert calls[0][WORK_DURATION_SETTING] == policy.model_dump_json()
    assert await ProductAssistant(store, tmp_path / "reader").turn(repository["id"], session["id"], request) == result
    assert len(calls) == 1 and (await manager.session(repository["id"], session["id"]))["journal"]["verified"]


async def legacy_conversation(manager, repository):
    """Build an original unmarked creation receipt in a fresh test database."""
    data = dict(
        id=uuid.uuid4().hex,
        repository_id=repository,
        agent="bridge",
        task="product",
        model_selection=None,
        project_id=None,
        project_snapshot=None,
        analysis_snapshot=None,
        created_at=now(),
        **manager.store.configuration.snapshot("bridge"),
    )
    async with manager.store._unit():
        row = ForgeAssistantSessionEntity(id=identifier(data["id"]), repository_id=identifier(repository), data=data)
        await manager.records.save(row)
        await manager._event(
            row,
            "conversation_created",
            dict(
                repository_id=repository,
                agent="bridge",
                task="product",
                model_selection_digest=None,
                project_digest=None,
                analysis_digest=None,
                analysis_context_digest=None,
            ),
        )
        return copy.deepcopy(row.data)


async def test_retained_unmarked_session_stays_unmarked_and_ignores_policy_setting_injection(
    store, tmp_path, monkeypatch
):
    repository = await store.add_repository(RepositoryInput(name="Retained design", url=str(tmp_path)))
    calls = []

    async def designer(prompt, settings):
        calls.append(copy.deepcopy(settings))
        return AssistantReply(message="Original finite dialogue contract")

    manager = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    session = await legacy_conversation(manager, repository["id"])
    before = copy.deepcopy(session)
    original = store.configuration.load_snapshot

    def load(revision):
        return {**original(revision), WORK_DURATION_SETTING: WorkDurationPolicyV2().model_dump_json()}

    monkeypatch.setattr(store.configuration, "load_snapshot", load)
    observed = await manager.session(repository["id"], session["id"])
    assert "work_duration" not in observed and session == before
    request = AssistantTurnInput(request_id="b" * 32, message="Read the original scope")
    result = await manager.turn(repository["id"], session["id"], request)
    assert result["status"] == "completed" and "work_duration" not in result["context"]
    assert len(calls) == 1 and WORK_DURATION_SETTING not in calls[0]
    async with store._unit():
        creation = (await manager.events.events(identifier(session["id"])))[0].data
        assert "work_duration_digest" not in creation["payload"]
        assert "work_duration" not in (await manager.records.find_by_id(identifier(session["id"]))).data


@pytest.mark.parametrize("change", ["missing", "null", "changed", "bool", "float"])
async def test_changed_policy_cannot_admit_a_turn_or_reinterpret_new_receipt_as_legacy(store, tmp_path, change):
    repository = await store.add_repository(RepositoryInput(name="Immutable design", url=str(tmp_path)))
    calls = []

    async def designer(prompt, settings):
        calls.append(prompt)
        return AssistantReply(message="Must not execute")

    manager = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    session = await manager.create(repository["id"], AssistantSessionInput())
    async with store._unit():
        row = await manager.records.find_by_id(identifier(session["id"]))
        data = copy.deepcopy(row.data)
        if change == "missing":
            data.pop("work_duration")
        elif change == "null":
            data["work_duration"] = None
        else:
            data["work_duration"]["timeout_seconds"] = {"changed": 1, "bool": False, "float": 1.0}[change]
        row.data = data
        await manager.records.save(row)
    with pytest.raises(ValueError, match="duration"):
        await manager.turn(repository["id"], session["id"], AssistantTurnInput(request_id="c" * 32, message="Refuse"))
    assert calls == []
    async with store._unit():
        assert await manager.turns.latest(identifier(session["id"])) == []


async def test_policy_change_during_admission_is_refused_before_turn_intent(store, tmp_path, monkeypatch):
    repository = await store.add_repository(RepositoryInput(name="Admission race", url=str(tmp_path)))
    calls = []

    async def designer(prompt, settings):
        calls.append(prompt)
        return AssistantReply(message="Must not run")

    manager = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    conversation = await manager.create(repository["id"], AssistantSessionInput())
    admit = store.admit_credentials

    async def changed(value):
        await admit(value)
        if value == "Change before intent":
            async with store._unit():
                row = await manager.records.find_by_id(identifier(conversation["id"]))
                row.data = {**row.data, "work_duration": WorkDurationPolicyV2(timeout_seconds=1).model_dump()}
                await manager.records.save(row)

    monkeypatch.setattr(store, "admit_credentials", changed)
    with pytest.raises(ValueError, match="duration"):
        await manager.turn(
            repository["id"],
            conversation["id"],
            AssistantTurnInput(
                request_id="e" * 32,
                message="Change before intent",
            ),
        )
    assert not calls
    async with store._unit():
        assert await manager.turns.latest(identifier(conversation["id"])) == []


async def test_new_uncapped_turn_stops_on_current_actor_revocation_without_retry(store, tmp_path, monkeypatch):
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    repository = await store.add_repository(RepositoryInput(name="Owned dialogue", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Owned assistant",
            description="Durable current authority",
            topology="monorepo",
            owners=["Platform"],
            components=[dict(id="api", name="API", repository_id=repository["id"], responsibility="API")],
        )
    )
    actor = Principal(uuid.uuid4(), "https://id.example/realms/forge", uuid.uuid4().hex)
    access = ProjectAccess(store)
    await access.set_membership(project["id"], actor, "developer")
    entered, stopped = asyncio.Event(), asyncio.Event()
    calls = []

    async def designer(prompt, settings):
        calls.append(settings[WORK_DURATION_SETTING])
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    manager = ProductAssistant(store, tmp_path / "assistant", designer=designer, lease_seconds=15)
    real_sleep = asyncio.sleep

    async def short_heartbeat(delay):
        await real_sleep(0.05 if delay == 15 / 4 else delay)

    monkeypatch.setattr(asyncio, "sleep", short_heartbeat)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        session = await manager.create(repository["id"], AssistantSessionInput(project_id=project["id"]))
        task = asyncio.create_task(
            manager.turn(
                repository["id"],
                session["id"],
                AssistantTurnInput(
                    request_id="d" * 32,
                    message="Continue while current ownership is valid",
                ),
            )
        )
    finally:
        CURRENT_PRINCIPAL.reset(token)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await access.set_membership(project["id"], actor, None)
        with pytest.raises((asyncio.CancelledError, AccessDenied)):
            await asyncio.wait_for(task, 3)
        assert stopped.is_set() and len(calls) == 1
        result = (await manager.session(repository["id"], session["id"]))["turns"][0]
        assert result["status"] == "needs_reconciliation" and result["reply"] is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
