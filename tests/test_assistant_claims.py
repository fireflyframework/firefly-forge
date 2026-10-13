# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable dialogue ownership cannot be inferred from process-local task state."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update

from firefly_forge.assistant_persistence import ForgeAssistantTurnEntity
from firefly_forge.models import RepositoryInput
from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant
from firefly_forge.product_assistant_runtime import AssistantReply
from firefly_forge.store import identifier


async def fixture(store, tmp_path, lease_seconds=60):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def designer(prompt, settings):
        calls.append(prompt)
        entered.set()
        await release.wait()
        return AssistantReply(message="Actual completed proposal")

    owner = ProductAssistant(store, tmp_path / "assistant", designer=designer, lease_seconds=lease_seconds)
    reader = ProductAssistant(store, tmp_path / "assistant")
    repository = await store.add_repository(RepositoryInput(name="Scoped dialogue", url=str(tmp_path)))
    session = await owner.create(repository["id"], AssistantSessionInput())
    request = AssistantTurnInput(request_id="a" * 32, message="Define this customer journey")
    task = asyncio.create_task(owner.turn(repository["id"], session["id"], request))
    await entered.wait()
    return owner, reader, repository["id"], session["id"], task, release, calls, request


async def expire(store, turn_id):
    async with store._unit() as session:
        await session.execute(
            update(ForgeAssistantTurnEntity)
            .where(ForgeAssistantTurnEntity.id == identifier(turn_id))
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )


async def test_another_instance_reads_live_turn_without_interrupting_or_repeating_inference(store, tmp_path):
    owner, reader, repo, sid, task, release, calls, request = await fixture(store, tmp_path)
    try:
        observed = await reader.session(repo, sid)
        turn = observed["turns"][0]
        assert turn["status"] == "processing"
        assert turn["ownership"]["owner_id"] == owner.owner_id
        assert turn["ownership"]["claim_epoch"] == 1
        assert turn["recovery"]["eligible"] is False
        replayed = await reader.turn(repo, sid, request)
        assert replayed["id"] == turn["id"] and replayed["status"] == "processing"
        assert replayed["ownership"] == turn["ownership"]
        assert await reader.turn(repo, sid, request) == replayed
        assert len(calls) == 1
    finally:
        release.set()
        completed = await task
    assert completed["status"] == "completed" and completed["reply"]["message"] == "Actual completed proposal"
    assert (await reader.session(repo, sid))["journal"]["verified"]


async def test_expired_claim_requires_explicit_recovery_and_fences_late_completion(store, tmp_path):
    from firefly_forge.product_assistant import AssistantRecoveryInput

    owner, reader, repo, sid, task, release, calls, request = await fixture(store, tmp_path)
    try:
        turn = (await reader.session(repo, sid))["turns"][0]
        recovery = AssistantRecoveryInput(request_id="b" * 32, expected_epoch=1)
        with pytest.raises(ValueError, match="expired"):
            await reader.recover_turn(repo, sid, turn["id"], recovery)
        await expire(store, turn["id"])
        observed = await reader.session(repo, sid)
        assert observed["turns"][0]["status"] == "processing"
        assert observed["turns"][0]["recovery"]["eligible"] is True
        receipt = await reader.recover_turn(repo, sid, turn["id"], recovery)
        assert receipt["status"] == "needs_reconciliation" and receipt["ownership"]["claim_epoch"] == 2
        assert await reader.recover_turn(repo, sid, turn["id"], recovery) == receipt
        with pytest.raises(ValueError, match="different"):
            await reader.recover_turn(
                repo, sid, turn["id"], AssistantRecoveryInput(request_id="b" * 32, expected_epoch=2)
            )
        assert await reader.turn(repo, sid, request) == receipt
    finally:
        release.set()
        late = await task
    assert late["status"] == "needs_reconciliation" and late["reply"] is None
    assert len(calls) == 1


async def test_owner_heartbeat_is_fenced_and_cannot_reanimate_expired_claim(store, tmp_path):
    owner, reader, repo, sid, task, release, _, _ = await fixture(store, tmp_path)
    try:
        turn = (await reader.session(repo, sid))["turns"][0]
        assert await owner._renew_claim(turn["id"], 1)
        assert not await reader._renew_claim(turn["id"], 1)
        assert not await owner._renew_claim(turn["id"], 2)
        await expire(store, turn["id"])
        assert not await owner._renew_claim(turn["id"], 1)
    finally:
        release.set()
        await task


async def test_automatic_heartbeat_renews_database_claim_before_expiration(store, tmp_path):
    owner, reader, repo, sid, task, release, _, _ = await fixture(store, tmp_path, lease_seconds=15)
    try:
        original = (await reader.session(repo, sid))["turns"][0]["ownership"]
        async with asyncio.timeout(6):
            while True:
                await asyncio.sleep(0.1)
                renewed = (await reader.session(repo, sid))["turns"][0]["ownership"]
                if renewed["last_heartbeat_at"] != original["last_heartbeat_at"]:
                    break
        assert renewed["lease_expires_at"] > original["lease_expires_at"]
        assert renewed["owner_id"] == owner.owner_id and renewed["claim_epoch"] == 1
    finally:
        release.set()
        completed = await task
    assert completed["status"] == "completed"


async def test_automatic_lost_claim_cancels_local_call_without_fabricating_recovery(store, tmp_path):
    _, reader, repo, sid, task, release, calls, _ = await fixture(store, tmp_path, lease_seconds=15)
    turn = (await reader.session(repo, sid))["turns"][0]
    await expire(store, turn["id"])
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 6)
    finally:
        release.set()
    observed = (await reader.session(repo, sid))["turns"][0]
    assert observed["status"] == "processing" and observed["recovery"]["eligible"]
    assert observed["reply"] is None and len(calls) == 1


async def test_foreign_owner_cannot_write_terminal_receipt_for_live_claim(store, tmp_path):
    _, reader, repo, sid, task, release, _, _ = await fixture(store, tmp_path)
    try:
        turn = (await reader.session(repo, sid))["turns"][0]
        denied = await reader._finish(
            repo, sid, turn["id"], status="completed", reply={"message": "Forged stale-owner completion"}
        )
        assert denied["status"] == "processing" and denied["reply"] is None
    finally:
        release.set()
        assert (await task)["reply"]["message"] == "Actual completed proposal"


async def test_legacy_processing_never_recovers_on_read_and_requires_explicit_owner_confirmation(store, tmp_path):
    from firefly_forge.assistant_persistence import ForgeAssistantTurnRepository
    from firefly_forge.product_assistant import AssistantRecoveryInput

    manager = ProductAssistant(store, tmp_path / "assistant")
    repository = await store.add_repository(RepositoryInput(name="Legacy dialogue", url=str(tmp_path)))
    session = await manager.create(repository["id"], AssistantSessionInput())
    async with store._unit():
        row = await manager._record(repository["id"], session["id"], lock=True)
        tid = "c" * 32
        data = {
            "id": tid,
            "session_id": session["id"],
            "request_id": "d" * 32,
            "message": "Historical model request",
            "status": "processing",
            "reply": None,
            "context": {},
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
        }
        await ForgeAssistantTurnRepository().save(
            ForgeAssistantTurnEntity(
                id=identifier(tid),
                session_id=row.id,
                request_id="d" * 32,
                status="processing",
                data=data,
            )
        )
        await manager._event(row, "turn_intent", {"turn_id": tid})
    read = await manager.session(repository["id"], session["id"])
    assert read["turns"][0]["status"] == "processing"
    assert read["turns"][0]["recovery"]["legacy_confirmation_required"]
    with pytest.raises(ValueError, match="confirm"):
        await manager.recover_turn(
            repository["id"], session["id"], tid, AssistantRecoveryInput(request_id="e" * 32, expected_epoch=0)
        )
    receipt = await manager.recover_turn(
        repository["id"],
        session["id"],
        tid,
        AssistantRecoveryInput(request_id="e" * 32, expected_epoch=0, legacy_confirmed_stopped=True),
    )
    assert receipt["status"] == "needs_reconciliation"
    assert receipt["reply"] is None


async def test_recovery_is_scoped_and_graceful_shutdown_records_unknown_without_retry(store, tmp_path):
    from firefly_forge.product_assistant import AssistantRecoveryInput

    owner, reader, repo, sid, task, _, calls, _ = await fixture(store, tmp_path)
    turn = (await reader.session(repo, sid))["turns"][0]
    other = await store.add_repository(RepositoryInput(name="Other", url=str(tmp_path / "other")))
    with pytest.raises(KeyError):
        await reader.recover_turn(
            other["id"], sid, turn["id"], AssistantRecoveryInput(request_id="f" * 32, expected_epoch=1)
        )
    await owner.stop()
    assert task.cancelled()
    observed = await reader.session(repo, sid)
    assert observed["turns"][0]["status"] == "needs_reconciliation"
    assert len(calls) == 1 and observed["journal"]["verified"]
    with pytest.raises(ValueError, match="stopping"):
        await owner.turn(repo, sid, AssistantTurnInput(request_id="1" * 32, message="Another turn"))


async def test_shared_repository_never_leaks_other_project_conversations_or_capabilities(store, tmp_path):
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    repository = await store.add_repository(RepositoryInput(name="Shared source", url=str(tmp_path)))
    projects = []
    manager = ProductAssistant(store, tmp_path / "assistant")
    sessions = []
    for name in ("Owned product", "Other product"):
        project = await Projects(store).configure(
            ProjectDefinition(
                name=name,
                description="Project-specific customer context",
                topology="monorepo",
                owners=["Platform"],
                components=[dict(id="api", name="API", repository_id=repository["id"], responsibility="API")],
            )
        )
        projects.append(project)
        sessions.append(await manager.create(repository["id"], AssistantSessionInput(project_id=project["id"])))
    user = Principal(uuid4(), "https://id.example/realms/forge", uuid4().hex)
    await ProjectAccess(store).set_membership(projects[0]["id"], user, "viewer")
    context = CURRENT_PRINCIPAL.set(user)
    try:
        assert [row["id"] for row in await manager.sessions(repository["id"])] == [sessions[0]["id"]]
        assert (await manager.session(repository["id"], sessions[0]["id"]))["id"] == sessions[0]["id"]
        with pytest.raises(AccessDenied):
            await manager.session(repository["id"], sessions[1]["id"])
        with pytest.raises(AccessDenied):
            await manager.capabilities(repository["id"], sessions[1]["id"])
        with pytest.raises(AccessDenied):
            await manager.turn(
                repository["id"],
                sessions[0]["id"],
                AssistantTurnInput(request_id="2" * 32, message="Viewer cannot submit"),
            )
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_capability_query_uses_exact_frozen_settings_without_inference(store, tmp_path, monkeypatch):
    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.providers import ConfiguredProvider

    repository = await store.add_repository(RepositoryInput(name="Model scope", url=str(tmp_path)))
    store.configuration.update("agentic", ProviderConfiguration(values={"model": "openai:original-model"}))
    manager = ProductAssistant(store, tmp_path / "assistant")
    session = await manager.create(repository["id"], AssistantSessionInput(agent="agentic"))
    store.configuration.update("agentic", ProviderConfiguration(values={"model": "openai:new-model"}))
    seen = []

    class Provider(ConfiguredProvider):
        async def image_input_capability(self, **kwargs):
            seen.append(self.settings)
            return {"status": "verified", "config_revision": "3" * 64}

    monkeypatch.setattr("firefly_forge.providers.get_provider", lambda agent, settings: Provider(settings))
    assert (await manager.capabilities(repository["id"], session["id"]))["image_input"]["status"] == "verified"
    assert seen[0]["FORGE_AGENTIC_MODEL"] == "openai:original-model"
    assert (await manager.session(repository["id"], session["id"]))["turns"] == []

    async def unavailable(*args, **kwargs):
        raise RuntimeError("Private transport detail")

    monkeypatch.setattr(Provider, "image_input_capability", unavailable)
    assert (await manager.capabilities(repository["id"], session["id"]))["image_input"]["status"] == "unverified"


async def test_native_http_shared_repository_reader_cannot_access_other_project_dialogue(
    tmp_path, postgres_schema, monkeypatch
):
    from httpx import ASGITransport, AsyncClient

    from firefly_forge.api import create_application
    from firefly_forge.engine import Engine
    from firefly_forge.identity import IdentityService, Principal
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    user = Principal(uuid4(), "https://id.example/realms/forge", uuid4().hex)

    async def authenticate(self, request):
        return user

    async def idle(_):
        await asyncio.Event().wait()

    monkeypatch.setattr(IdentityService, "authenticate", authenticate)
    monkeypatch.setattr(Engine, "_poll", idle)
    app = create_application(tmp_path / "native-scope", "native-dialogue-scope-token-32chars")
    async with app.router.lifespan_context(app):
        store = app.state.store
        manager = app.state.runtime.context.get_bean(ProductAssistant)
        repository = await store.add_repository(RepositoryInput(name="Shared", url=str(tmp_path)))
        sessions = []
        for name in ("Owned HTTP project", "Other HTTP project"):
            project = await Projects(store).configure(
                ProjectDefinition(
                    name=name,
                    description="A bounded project-specific dialogue",
                    topology="monorepo",
                    owners=["Platform"],
                    components=[dict(id="api", name="API", repository_id=repository["id"], responsibility="API")],
                )
            )
            sessions.append(await manager.create(repository["id"], AssistantSessionInput(project_id=project["id"])))
        await ProjectAccess(store).set_membership(sessions[0]["project_id"], user, "viewer")
        base = f"/api/v1/repositories/{repository['id']}/assistant/conversations"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            assert [row["id"] for row in (await client.get(base)).json()] == [sessions[0]["id"]]
            assert (await client.get(base + "/" + sessions[0]["id"])).status_code == 200
            assert (await client.get(base + "/" + sessions[1]["id"])).status_code == 403
            assert (await client.get(base + "/" + sessions[1]["id"] + "/capabilities")).status_code == 403


@pytest.mark.parametrize("change", ["revoke", "suspend"])
async def test_owner_can_record_failed_outcome_but_revoked_actor_cannot_receive_model_reply(store, tmp_path, change):
    from firefly_forge.identity import Principal
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    repository = await store.add_repository(RepositoryInput(name="In-flight authority", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Bounded authority",
            description="Retain real outcomes without leaking revoked context",
            topology="monorepo",
            owners=["Platform"],
            components=[dict(id="api", name="API", repository_id=repository["id"], responsibility="API")],
        )
    )
    user = Principal(uuid4(), "https://id.example/realms/forge", uuid4().hex)
    access = ProjectAccess(store)
    await access.set_membership(project["id"], user, "developer")
    entered, release = asyncio.Event(), asyncio.Event()

    async def designer(prompt, settings):
        entered.set()
        await release.wait()
        return AssistantReply(message="Private proposal must not reach revoked actor")

    manager = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    context = CURRENT_PRINCIPAL.set(user)
    try:
        session = await manager.create(repository["id"], AssistantSessionInput(project_id=project["id"]))
        task = asyncio.create_task(
            manager.turn(
                repository["id"],
                session["id"],
                AssistantTurnInput(request_id="5" * 32, message="Discuss customer requirements"),
            )
        )
    finally:
        CURRENT_PRINCIPAL.reset(context)
    await entered.wait()
    if change == "revoke":
        await access.set_membership(project["id"], user, None)
    else:
        async with store._unit():
            users = ForgeIdentityUserRepository()
            row = await users.find_by_id(user.id)
            row.enabled = False
            await users.save(row)
    release.set()
    with pytest.raises(AccessDenied):
        await task
    observed = await manager.session(repository["id"], session["id"])
    assert observed["turns"][0]["status"] == "failed"
    assert observed["turns"][0]["reply"] is None
    assert observed["journal"]["verified"]


async def test_create_rechecks_edit_authority_after_awaited_profile_resolution(store, tmp_path):
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    repository = await store.add_repository(RepositoryInput(name="Admission", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Admission scope",
            description="Current grant at conversation write",
            topology="monorepo",
            owners=["Team"],
            components=[dict(id="api", name="API", repository_id=repository["id"], responsibility="API")],
        )
    )
    user = Principal(uuid4(), "https://id.example/realms/forge", uuid4().hex)
    access = ProjectAccess(store)
    await access.set_membership(project["id"], user, "developer")

    class Resolver:
        async def resolve(self, *args):
            await access.set_membership(project["id"], user, "viewer")
            return None

    store.task_router = Resolver()
    manager = ProductAssistant(store, tmp_path / "assistant")
    context = CURRENT_PRINCIPAL.set(user)
    try:
        with pytest.raises(AccessDenied):
            await manager.create(repository["id"], AssistantSessionInput(project_id=project["id"]))
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert await manager.sessions(repository["id"]) == []


async def test_native_pyfly_lifecycle_drains_owned_model_before_store_shutdown(tmp_path, postgres_schema, monkeypatch):
    from firefly_forge.api import create_application
    from firefly_forge.engine import Engine
    from firefly_forge.store import Store

    async def idle(_):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle)
    app = create_application(tmp_path / "native", "native-claim-lifecycle-token-32chars")
    entered = asyncio.Event()
    checked = []
    original_stop = Store.stop
    manager = task = repository = session = None

    async def check_store_stop(self):
        assert task is not None and task.cancelled()
        observed = await manager.session(repository["id"], session["id"])
        checked.append(observed["turns"][0]["status"])
        await original_stop(self)

    monkeypatch.setattr(Store, "stop", check_store_stop)
    async with app.router.lifespan_context(app):
        manager = app.state.runtime.context.get_bean(ProductAssistant)
        from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager

        app.state.runtime.context.get_bean(KnowledgeManager).configure(
            KnowledgeConfiguration(values={"enabled": False})
        )

        async def designer(prompt, settings):
            entered.set()
            await asyncio.Event().wait()

        manager.designer = designer
        repository = await app.state.store.add_repository(RepositoryInput(name="Lifecycle source", url=str(tmp_path)))
        session = await manager.create(repository["id"], AssistantSessionInput())
        task = asyncio.create_task(
            manager.turn(
                repository["id"],
                session["id"],
                AssistantTurnInput(request_id="4" * 32, message="Own this bounded request"),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
    assert checked == ["needs_reconciliation"]
