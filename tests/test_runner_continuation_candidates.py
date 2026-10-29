# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Local candidate reads never dispatch or promise that the leader can continue a command."""

import asyncio
from uuid import uuid4

import pytest
from test_runner_continuations import waiting

from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


async def stop_fixture(fixture):
    fixture[5].set()
    fixture[4].cancel()
    await asyncio.gather(fixture[4], return_exceptions=True)
    CURRENT_PRINCIPAL.reset(fixture[6])


async def test_candidate_read_uses_persistent_proof_without_rpc_or_worker_memory(store):
    fixture = await waiting(store, store.path.parent)
    manager, controller, run, journal, *_ = fixture
    try:

        async def forbidden(*args, **kwargs):
            pytest.fail("Candidate GET must not call a controller or use the active worker")

        controller.profile = forbidden
        manager.worker = None
        before = await journal.entries(run["id"])
        rows = await journal.sandbox_executions(run["id"])
        candidate = await manager.candidates(run["id"])
        assert candidate["state"] == "requires_leader_confirmation"
        assert candidate["actor_id"] == CURRENT_PRINCIPAL.get().id.hex
        assert len(candidate["commands"]) == 1
        command = candidate["commands"][0]
        assert command["execution_id"] == rows[0]["execution_id"]
        assert command["command_label"] == "Git command"
        assert command["argument_count"] == 1 and "argv" not in command
        assert command["task_id"] == "verify" and command["attempt"] == 1
        assert "workspace" not in command and "fence" not in command
        assert str(store.path.parent) not in str(candidate)
        assert await journal.entries(run["id"]) == before
        assert await journal.sandbox_executions(run["id"]) == rows
        assert await manager.requests.for_run(run["id"]) == []
        assert controller.submissions == 1 and controller.continuations == 0
    finally:
        await stop_fixture(fixture)


@pytest.mark.parametrize("change", ["source", "stage", "cancel", "owner", "previous"])
async def test_known_ineligible_original_command_is_not_offered(store, change):
    fixture = await waiting(store, store.path.parent)
    manager, controller, run, journal, *_ = fixture
    try:
        row = (await journal.sandbox_executions(run["id"]))[0]
        if change == "source":
            (manager.worker.workspaces / run["id"] / "main.txt").write_text("concurrent source\n")
        elif change == "stage":
            intent = next(entry for entry in await journal.entries(run["id"]) if entry["kind"] == "intent")
            await journal.receipt(run["id"], intent["idempotency_key"], {"fixture": "finished"})
        elif change == "cancel":
            await store.update(run["id"], cancel_request={"status": "requested"})
        elif change == "owner":
            await store.release_ownership()
        else:
            from firefly_forge.runner_continuations import RunnerContinuationInput

            await manager.request(
                run["id"], RunnerContinuationInput(request_id=uuid4().hex, execution_id=row["execution_id"])
            )
        before = await journal.entries(run["id"])
        result = await manager.candidates(run["id"])
        assert result["commands"] == []
        assert result["reason"]
        assert await journal.entries(run["id"]) == before
        assert controller.continuations == 0
    finally:
        await stop_fixture(fixture)


@pytest.mark.parametrize("who", ["operator", "execution_actor"])
async def test_candidate_read_requires_current_requester_and_original_execution_authority(store, who):
    identities = {}
    fixture = await waiting(store, store.path.parent, scoped=True, identities=identities)
    try:
        await ProjectAccess(store).set_membership(identities["project"]["id"], identities[who], None)
        with pytest.raises(AccessDenied):
            await fixture[0].candidates(fixture[2]["id"])
        assert fixture[1].continuations == 0
    finally:
        await stop_fixture(fixture)


async def test_candidate_output_rechecks_dynamic_credentials_and_authority_after_await(store):
    identities = {}
    fixture = await waiting(store, store.path.parent, scoped=True, identities=identities)
    try:
        manager, controller, run, *_ = fixture
        first = await manager.candidates(run["id"])
        registered = first["commands"][0]["source_digest"]
        store.credential_registry = lambda: {"FORGE_TEST_API_TOKEN": registered}
        try:
            value = await manager.candidates(run["id"])
        except ValueError as error:
            assert registered not in str(error)
        else:
            assert registered not in str(value)
        store.credential_registry = dict

        async def revoke(value):
            if isinstance(value, dict) and value.get("version") == 1:
                await ProjectAccess(store).set_membership(identities["project"]["id"], identities["operator"], None)

        store.vault_credential_admission = revoke
        with pytest.raises(AccessDenied):
            await manager.candidates(run["id"])
        assert controller.continuations == 0
    finally:
        await stop_fixture(fixture)


async def command_fixture(store, argv):
    from test_runner_pool_worker import fixture

    from firefly_forge.identity import Principal
    from firefly_forge.runner_continuations import RunnerContinuations
    from firefly_forge.runtime_roles import RuntimeRoles

    worker, controller, run, journal, workspace = await fixture(store, store.path.parent)
    controller.continuations = 0
    principal = Principal(uuid4(), "local", "owned-original", authentication_method="local_bootstrap")
    await store.update(run["id"], execution_actor=ProjectAccess.actor_snapshot(principal))
    ready, release = asyncio.Event(), asyncio.Event()
    original_observe = controller.observe

    async def observe(invocation):
        ready.set()
        if release.is_set():
            return await original_observe(invocation)
        return {"status": "queued", "request": invocation.model_dump(mode="json"), "receipt": None}

    controller.observe = observe
    context = CURRENT_PRINCIPAL.set(principal)
    task = asyncio.create_task(worker.execute(run["id"], workspace, workspace, argv, 5, "verify", ordinal=1))
    await asyncio.wait_for(ready.wait(), 5)
    runtime = store.runtime_roles or RuntimeRoles(store)
    if not runtime.started:
        await runtime.start()
    return RunnerContinuations(store, runtime, None), controller, run, journal, task, release, context


@pytest.mark.parametrize("program", ["/owned/private-canary/python", "unregistered-executable-canary"])
async def test_candidate_preview_never_reflects_arguments_or_arbitrary_executable_paths(store, program):
    argv = [program, "/owned/source-sensitive-canary/script.py", "--token=unregistered-argument-canary"]
    fixture = await command_fixture(store, argv)
    try:
        result = await fixture[0].candidates(fixture[2]["id"])
        assert len(result["commands"]) == 1
        candidate = result["commands"][0]
        assert candidate["command_label"] == ("Python command" if program.endswith("/python") else "Project command")
        assert candidate["argument_count"] == 2
        assert "argv" not in candidate
        assert not any(value in str(result) for value in argv)
    finally:
        await stop_fixture(fixture)


async def test_native_api_candidates_route_precedes_exact_request_and_is_split_role_read_only(
    postgres_schema, tmp_path
):
    import httpx
    from native_client_knowledge_fixtures import activate_native_client_target

    from firefly_forge.api import create_application
    from firefly_forge.runner_continuations import RunnerContinuations
    from firefly_forge.store import Store

    token = "owned-continuation-candidate-native-bootstrap"
    app = create_application(tmp_path / "api", token, role="api")
    async with app.router.lifespan_context(app):
        store = app.state.runtime.context.get_bean(Store)
        (store.path.parent / "workspaces").rmdir()  # Empty directory created by this owned API fixture.
        await activate_native_client_target(store, tmp_path / "framework-prerequisite")
        fixture = await command_fixture(store, ["git", "diff"])
        try:
            service = app.state.runtime.context.get_bean(RunnerContinuations)
            assert service.worker is None and service.runtime.role == "api"
            route = "/api/v1/runs/" + fixture[2]["id"] + "/runner-continuations"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                assert (await client.get(route + "/candidates")).status_code == 401
                headers = {"Authorization": "Bearer " + token}
                before = await fixture[3].entries(fixture[2]["id"])
                response = await client.get(route + "/candidates", headers=headers)
                assert response.status_code == 200
                assert response.json()["state"] == "requires_leader_confirmation"
                command = response.json()["commands"][0]
                assert command["command_label"] == "Git command"
                assert await fixture[3].entries(fixture[2]["id"]) == before
                body = {"request_id": uuid4().hex, "execution_id": command["execution_id"]}
                admitted = await client.post(route, headers=headers, json=body)
                assert admitted.status_code == 200
                assert admitted.json()["request_digest"] == command["binding_digest"]
                exact = await client.get(route + "/" + body["request_id"], headers=headers)
                assert exact.status_code == 200 and exact.json()["id"] == body["request_id"]
                assert "commands" not in exact.json()
                assert (await client.get(route + "/candidates", headers=headers)).json()["commands"] == []
                assert fixture[1].submissions == 1 and fixture[1].continuations == 0
        finally:
            await stop_fixture(fixture)


async def test_api_host_without_shared_source_has_structured_unavailable_state(store, monkeypatch):
    fixture = await waiting(store, store.path.parent)
    try:
        original = store.path
        monkeypatch.setattr(store, "path", original.parent / "separate-api-host" / "factory.db")
        result = await fixture[0].candidates(fixture[2]["id"])
        assert result["commands"] == []
        assert result["state"] == "source_unavailable"
        assert "shared workspace" in result["reason"]
        assert str(original.parent) not in str(result)
        assert fixture[1].continuations == 0
    finally:
        await stop_fixture(fixture)


async def test_candidate_rechecks_source_after_async_credential_admission_without_subprocess(store, monkeypatch):
    fixture = await waiting(store, store.path.parent)
    try:
        manager, _, run, _, *_ = fixture
        import firefly_forge.process as process

        async def forbidden(*args, **kwargs):
            pytest.fail("Read-only local candidate source verification must not run a subprocess")

        monkeypatch.setattr(process, "execute", forbidden)
        monkeypatch.setattr(process, "checked", forbidden)

        async def change_source(value):
            if isinstance(value, dict) and value.get("version") == 1:
                (manager.worker.workspaces / run["id"] / "main.txt").write_text("changed after await\n")

        store.vault_credential_admission = change_source
        result = await manager.candidates(run["id"])
        assert result["commands"] == [] and result["state"] == "evidence_changed"
        assert fixture[1].continuations == 0
    finally:
        await stop_fixture(fixture)


@pytest.mark.parametrize("limit", ["CANDIDATE_READ_SECONDS", "CANDIDATE_READ_COMMANDS"])
async def test_exhausted_local_read_budget_has_structured_no_effect_result(store, monkeypatch, limit):
    import firefly_forge.runner_continuations as module

    fixture = await waiting(store, store.path.parent)
    try:
        manager, controller, run, journal, *_ = fixture
        before = await journal.entries(run["id"])
        monkeypatch.setattr(module, limit, 0)
        result = await manager.candidates(run["id"])
        assert result["state"] == "read_budget_exceeded" and result["commands"] == []
        assert await journal.entries(run["id"]) == before
        assert await manager.requests.for_run(run["id"]) == [] and controller.continuations == 0
    finally:
        await stop_fixture(fixture)


async def test_candidate_source_is_read_once_per_pass_with_original_bytes_preserved(store, monkeypatch):
    import firefly_forge.runner_continuations as module

    fixture = await waiting(store, store.path.parent)
    try:
        manager, _, run, _, *_ = fixture
        original = module.pack_workspace
        calls = []

        def capture(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, "pack_workspace", capture)
        workspace = manager.worker.workspaces / run["id"]
        source = (workspace / "main.txt").read_bytes()
        result = await manager.candidates(run["id"])
        assert result["commands"] and len(calls) == 2
        assert (workspace / "main.txt").read_bytes() == source
        assert (workspace / ".git" / "retained").read_text() == "original host metadata"
    finally:
        await stop_fixture(fixture)
