# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned PostgreSQL plus controlled provider transport; no live OAuth or board writes."""

import asyncio
import copy
import inspect
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import httpx2
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from pyfly.oauth2 import OAuth2Client, OAuth2Endpoints
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_bootstrap_native_identity import TOKEN, service
from test_github_projects_graphql import payload
from test_github_projects_models import connection
from test_identity import request as identity_request
from test_projects import definition

from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.connection_oauth_models import OAuthConnectionInput
from firefly_forge.github_projects import GitHubProjects
from firefly_forge.github_projects_graphql import GitHubProjectsGraphQL
from firefly_forge.github_projects_models import BoardConnectionInputV1, BoardDiscoveryInputV1
from firefly_forge.github_projects_oauth import github_policy
from firefly_forge.github_projects_persistence import (
    ForgeGitHubProjectsBindingRepository,
    ForgeGitHubProjectsConfigurationEntity,
)
from firefly_forge.identity_persistence import ForgeIdentityUserEntity
from firefly_forge.models import RepositoryInput
from firefly_forge.oauth_resources import OAuthResources
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.projects import Projects


async def native_run(board, store, tmp_path):
    from native_client_knowledge_fixtures import activate_native_client_target

    from firefly_forge.engine import Engine
    from firefly_forge.execution_requests import ExecutionRequests
    from firefly_forge.git_deliveries import GitDeliveries
    from firefly_forge.github_projects_requests import GitHubProjectsRequests
    from firefly_forge.github_projects_worker import GitHubProjectsWorker
    from firefly_forge.journal import Journal
    from firefly_forge.runtime_roles import RuntimeRoles

    if store.framework_knowledge is None:
        await activate_native_client_target(store, tmp_path / "board-framework-target")
    run = await board_run(board, store)
    runtime = RuntimeRoles(store)
    store.runtime_roles = runtime
    await runtime.start()
    await store.acquire_ownership()
    requests = ExecutionRequests(store, runtime)
    engine = Engine(
        store, tmp_path / "unused-workspaces", lambda _: (_ for _ in ()).throw(AssertionError("No model execution"))
    )
    tickets = GitHubProjectsRequests(board.manager, requests, GitDeliveries(store, engine, Journal(store)))
    worker = GitHubProjectsWorker(tickets)
    return run, requests, tickets, worker


async def board_run(board, store):
    from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity
    from firefly_forge.models import RunInput

    receipt_id = uuid4().hex
    run = await store.create_run(
        RunInput(
            repository_id=board.repository,
            project_id=board.project,
            component_id="stock",
            brief="Controlled board lifecycle",
            issue_context={"provider": "github", "receipt_id": receipt_id, "actor": "7", "kind": "issue", "number": 4},
        )
    )
    verification = {
        "version": 1,
        "provider": "github",
        "api_origin": "https://api.github.com",
        "installation_id": "34",
        "remote_repository_id": "77",
        "external_actor_id": "7",
        "web_origin": "https://github.com",
        "connection_revision": "c" * 32,
        "connection_scope_digest": "d" * 64,
        "webhook_key_revision": "e" * 32,
        "verification_method": "raw_body_hmac_sha256",
        "payload_digest": "f" * 64,
    }
    delivery = {
        "id": receipt_id,
        "provider": "github",
        "status": "processed",
        "run_id": run["id"],
        "digest": "f" * 64,
        "actor": "7",
        "remote_id": "77",
        "command": {"action": "run"},
        "issue": {"kind": "issue", "number": 4},
        "verification": verification,
    }
    async with store._unit() as session:
        session.add(
            ForgeWebhookInboxEntity(
                id=UUID(receipt_id),
                repository_id=UUID(board.repository),
                provider="github",
                delivery_id=receipt_id,
                source_key=receipt_id,
                status="processed",
                data=delivery,
            )
        )
    return run


@pytest_asyncio.fixture
async def board(store, monkeypatch, request):
    identity = service(store)
    actor = await identity.authenticate(identity_request(bearer="Bearer " + TOKEN))
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        repository = await store.add_repository(
            RepositoryInput(
                name="Board fixture",
                url="https://github.com/team/product",
                provider="github",
                remote_repository_id="77",
            )
        )
        project = await Projects(store).configure(definition(repository["id"]))
        oauth = ConnectionOAuth(
            store,
            encryption_key=Fernet.generate_key().decode(),
            browser_origin="https://forge.example",
            catalog={name: github_policy(name).provider for name in ("github-projects", "github-projects-private")},
        )
        await oauth.configure(
            "github-board",
            OAuthConnectionInput(
                provider="github-projects-private",
                project_id=project["id"],
                client_id="fixture-client",
                client_secret="fixture-github-client-secret",
            ),
            actor,
        )
        wire = []

        def token_reply(req):
            body = parse_qs(req.content.decode())
            assert "resource" not in body
            assert req.headers["accept"] == "application/json"
            wire.append(body)
            tokens = {
                "access_token": "fixture-github-access-token",
                "token_type": "bearer",
                "expires_in": 28800,
                "refresh_token": "fixture-github-refresh-token",
                "scope": "project,repo",
            }
            if getattr(request, "param", None) == "nonexpiring":
                tokens.pop("expires_in")
                tokens.pop("refresh_token")
            return httpx.Response(200, json=tokens)

        monkeypatch.setattr(
            oauth,
            "_client",
            lambda frozen, secret: OAuth2Client(
                frozen["client_id"],
                OAuth2Endpoints(frozen["policy"]["token_endpoint"]),
                client_secret=secret,
                transport=httpx.MockTransport(token_reply),
            ),
        )
        handoff = await oauth.prepare("github-board", actor)
        challenge = await oauth.begin("github-board", handoff["ticket"], actor, browser_authenticated=True)
        parameters = parse_qs(urlsplit(challenge.authorization_url).query)
        assert "resource" not in parameters and parameters["code_challenge_method"] == ["S256"]
        await oauth.complete(
            "github-board", parameters["state"][0], "controlled-code", challenge.browser_nonce, actor, issuer=""
        )
        resources = OAuthResources(oauth, ProjectAccess(store))
        manager = GitHubProjects(store, ProjectAccess(store), resources, ForgeGitHubProjectsBindingRepository())
        state = {"response": payload(), "before_reply": None, "handle": None}

        async def reply(req):
            from pyfly.data.transaction import current_unit_of_work

            assert current_unit_of_work() is None
            assert req.headers["authorization"] == "Bearer fixture-github-access-token"
            if state["before_reply"]:
                await state["before_reply"]()
            if state["handle"]:
                value = state["handle"](req)
                return await value if inspect.isawaitable(value) else value
            return httpx2.Response(200, json=copy.deepcopy(state["response"]))

        manager.remote = GitHubProjectsGraphQL(resources, transport=httpx2.MockTransport(reply))
        yield SimpleNamespace(
            manager=manager,
            oauth=oauth,
            actor=actor,
            project=project["id"],
            repository=repository["id"],
            state=state,
            wire=wire,
        )
    finally:
        CURRENT_PRINCIPAL.reset(context)
        await identity.client.aclose()


async def save(board, **changes):
    body = connection() | changes
    return await board.manager.configure(
        board.project, board.repository, BoardConnectionInputV1.model_validate(body), board.actor
    )


async def test_native_configuration_replay_returns_original_receipt_and_history_is_immutable(board, store):
    first = await save(board)
    second = await save(board, request_id="2" * 32, expected_digest=first.binding.digest, enabled=False)
    assert second.binding.revision == 2
    replay = await save(board)
    assert replay == first
    assert (await board.manager.view(board.project, board.repository, board.actor)).binding == second.binding
    async with store._unit() as session:
        assert await session.scalar(select(func.count()).select_from(ForgeGitHubProjectsConfigurationEntity)) == 2
    with pytest.raises(DBAPIError, match="immutable"):
        async with store._unit() as session:
            await session.execute(text("UPDATE forge_github_projects_configuration_receipts SET data='{}'::jsonb"))


async def test_native_identical_concurrent_admission_returns_one_original_receipt(board, store):
    results = await asyncio.gather(save(board), save(board))
    assert results[0] == results[1]
    async with store._unit() as session:
        assert await session.scalar(select(func.count()).select_from(ForgeGitHubProjectsConfigurationEntity)) == 1


async def test_native_disconnect_changes_current_readiness_without_erasing_configuration(board):
    first = await save(board)
    await board.oauth.disconnect("github-board", board.actor)
    view = await board.manager.view(board.project, board.repository, board.actor)
    assert view.binding == first.binding and view.status == "authorization_required"


@pytest.mark.parametrize("change", ["identity_revoked", "credential_display"])
async def test_native_discovery_rechecks_identity_and_credential_egress(board, store, change):
    if change == "identity_revoked":

        async def revoke():
            async with store._unit() as session:
                row = await session.get(ForgeIdentityUserEntity, board.actor.id)
                row.enabled = False

        board.state["before_reply"] = revoke
    else:
        board.state["response"]["data"]["owner"]["projectV2"]["title"] = "fixture-github-access-token"
    with pytest.raises((ValueError, AccessDenied)):
        await board.manager.discover(
            board.project,
            board.repository,
            BoardDiscoveryInputV1(
                version=1, connection_name="github-board", owner_kind="user", owner="octocat", number=2
            ),
            board.actor,
        )


def board_server(board, *, fail_add=False, initial_value=None):
    import json

    state = {"items": [], "mutations": []}
    if initial_value:
        state["items"] = [
            {
                "id": "PVTI_1",
                "content": {"id": "I_4", "repository": {"id": "R_123"}},
                "fieldValues": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [{"field": {"id": "PVTSSF_123"}, "optionId": initial_value}],
                },
            }
        ]

    def handle(req):
        body = json.loads(req.content)
        query, variables = body["query"], body["variables"]
        if "query ForgeBoard(" in query:
            return httpx2.Response(200, json=copy.deepcopy(board.state["response"]))
        if "query ForgeBoardContent" in query:
            remote = copy.deepcopy(payload()["data"]["repository"])
            remote["issue"] = {"id": "I_4", "number": 4, "url": "https://github.com/team/product/issues/4"}
            return httpx2.Response(200, json={"data": {"repository": remote}})
        if "query ForgeBoardItems" in query:
            return httpx2.Response(
                200,
                json={
                    "data": {
                        "node": {"items": {"pageInfo": {"hasNextPage": False}, "nodes": copy.deepcopy(state["items"])}}
                    }
                },
            )
        state["mutations"].append(query)
        if "mutation ForgeBoardAdd" in query:
            state["items"].append(
                {
                    "id": "PVTI_1",
                    "content": {"id": "I_4", "repository": {"id": "R_123"}},
                    "fieldValues": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                }
            )
            if fail_add:
                raise httpx2.ReadError("Controlled lost add response")
            return httpx2.Response(
                200,
                json={
                    "data": {
                        "addProjectV2ItemById": {
                            "item": {"id": "PVTI_1"},
                            "clientMutationId": variables["input"]["clientMutationId"],
                        }
                    }
                },
            )
        assert "mutation ForgeBoardState" in query
        state["items"][0]["fieldValues"]["nodes"] = [
            {"field": {"id": "PVTSSF_123"}, "optionId": variables["input"]["value"]["singleSelectOptionId"]}
        ]
        return httpx2.Response(
            200,
            json={
                "data": {
                    "updateProjectV2ItemFieldValue": {
                        "projectV2Item": {"id": "PVTI_1"},
                        "clientMutationId": variables["input"]["clientMutationId"],
                    }
                }
            },
        )

    board.state["handle"] = handle
    return state


async def test_native_owner_dispatch_retains_separate_intents_and_exact_readback(board, store, tmp_path):
    from firefly_forge.github_projects_models import BoardSyncInputV1

    configured = await save(board)
    run, requests, tickets, worker = await native_run(board, store, tmp_path)
    state = board_server(board)
    request = BoardSyncInputV1(version=1, request_id=uuid4().hex, expected_binding_digest=configured.binding.digest)
    await tickets.admit(board.project, board.repository, run["id"], request, board.actor)
    context = CURRENT_PRINCIPAL.set(None)
    try:
        await worker.execute(request.request_id)
    finally:
        CURRENT_PRINCIPAL.reset(context)
    receipt = await tickets.get(board.project, board.repository, request.request_id, board.actor)
    assert receipt["status"] == "completed", receipt
    assert len(state["mutations"]) == 2
    kinds = [entry["kind"] for entry in receipt["events"]]
    assert (
        kinds.index("board_add_intent")
        < kinds.index("board_add_result")
        < kinds.index("board_field_intent")
        < kinds.index("board_field_result")
        < kinds.index("board_field_confirmed")
    )
    assert receipt["projection"]["product_completion_verified"] is False


async def test_native_unknown_add_is_not_replayed_after_matching_readonly_observation(board, store, tmp_path):
    from firefly_forge.github_projects_models import BoardObserveInputV1, BoardSyncInputV1

    configured = await save(board)
    run, requests, tickets, worker = await native_run(board, store, tmp_path)
    state = board_server(board, fail_add=True)
    first = BoardSyncInputV1(version=1, request_id=uuid4().hex, expected_binding_digest=configured.binding.digest)
    await tickets.admit(board.project, board.repository, run["id"], first, board.actor)
    await worker.execute(first.request_id)
    receipt = await tickets.get(board.project, board.repository, first.request_id, board.actor)
    assert receipt["status"] == "unknown" and len(state["mutations"]) == 1
    await worker.observe(
        board.project,
        board.repository,
        first.request_id,
        BoardObserveInputV1(version=1, expected_request_digest=receipt["request_digest"]),
        board.actor,
    )
    after = await tickets.get(board.project, board.repository, first.request_id, board.actor)
    assert after["status"] == "unknown" and after["events"][-1]["evidence"]["replay_authorized"] is False
    second = first.model_copy(update={"request_id": uuid4().hex})
    await tickets.admit(board.project, board.repository, run["id"], second, board.actor)
    await worker.execute(second.request_id)
    assert (await tickets.get(board.project, board.repository, second.request_id, board.actor))["status"] == "conflict"
    assert len(state["mutations"]) == 1


async def test_native_existing_human_field_conflict_produces_no_write(board, store, tmp_path):
    from firefly_forge.github_projects_models import BoardSyncInputV1

    configured = await save(board)
    run, requests, tickets, worker = await native_run(board, store, tmp_path)
    state = board_server(board, initial_value="human-owned")
    request = BoardSyncInputV1(version=1, request_id=uuid4().hex, expected_binding_digest=configured.binding.digest)
    await tickets.admit(board.project, board.repository, run["id"], request, board.actor)
    await worker.execute(request.request_id)
    assert (await tickets.get(board.project, board.repository, request.request_id, board.actor))["status"] == "conflict"
    assert state["mutations"] == [] and state["items"][0]["fieldValues"]["nodes"][0]["optionId"] == "human-owned"


async def test_native_cancelled_field_dispatch_retains_partial_add_and_unknown(board, store, tmp_path):
    import json

    from firefly_forge.github_projects_models import BoardSyncInputV1

    configured = await save(board)
    run, requests, tickets, worker = await native_run(board, store, tmp_path)
    state = board_server(board)
    original = board.state["handle"]
    dispatched = asyncio.Event()

    async def pause(req):
        if "mutation ForgeBoardState" in json.loads(req.content)["query"]:
            dispatched.set()
            await asyncio.Event().wait()
        return original(req)

    board.state["handle"] = pause
    request = BoardSyncInputV1(version=1, request_id=uuid4().hex, expected_binding_digest=configured.binding.digest)
    await tickets.admit(board.project, board.repository, run["id"], request, board.actor)
    task = asyncio.create_task(worker.execute(request.request_id))
    await asyncio.wait_for(dispatched.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    receipt = await tickets.get(board.project, board.repository, request.request_id, board.actor)
    assert receipt["status"] == "unknown" and len(state["mutations"]) == 1
    assert any(event["kind"] == "board_add_result" for event in receipt["events"])
    assert any(event["kind"] == "board_field_intent" for event in receipt["events"])


async def test_native_identity_revocation_blocks_before_provider_write(board, store, tmp_path):
    from firefly_forge.github_projects_models import BoardSyncInputV1

    configured = await save(board)
    run, requests, tickets, worker = await native_run(board, store, tmp_path)
    state = board_server(board)
    request = BoardSyncInputV1(version=1, request_id=uuid4().hex, expected_binding_digest=configured.binding.digest)
    await tickets.admit(board.project, board.repository, run["id"], request, board.actor)
    async with store._unit() as session:
        row = await session.get(ForgeIdentityUserEntity, board.actor.id)
        row.enabled = False
    context = CURRENT_PRINCIPAL.set(None)
    try:
        await worker.execute(request.request_id)
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert (await requests.get(request.request_id))["status"] == "failed" and state["mutations"] == []
    with pytest.raises(AccessDenied):
        await tickets.get(board.project, board.repository, request.request_id, board.actor)


async def test_native_newer_progress_suppresses_old_issue_and_preserves_parent(board, store, tmp_path):
    from firefly_forge.github_projects_models import BoardSyncInputV1

    configured = await save(board)
    run, requests, tickets, worker = await native_run(board, store, tmp_path)
    state = board_server(board)
    request = BoardSyncInputV1(version=1, request_id=uuid4().hex, expected_binding_digest=configured.binding.digest)
    await tickets.admit(board.project, board.repository, run["id"], request, board.actor)
    before = await store.get_run(run["id"])
    await native_run(board, store, tmp_path)
    await worker.execute(request.request_id)
    assert (await tickets.get(board.project, board.repository, request.request_id, board.actor))["status"] == "conflict"
    assert state["mutations"] == [] and await store.get_run(run["id"]) == before


async def test_native_later_credential_history_blocks_old_view_and_config_replay(board):
    await save(board)
    await board.oauth.configure(
        "later-account",
        OAuthConnectionInput(
            provider="github-projects-private",
            project_id=board.project,
            client_id="later-client",
            client_secret="octocat",
        ),
        board.actor,
    )
    with pytest.raises(ValueError, match="credentials"):
        await board.manager.view(board.project, board.repository, board.actor)
    with pytest.raises(ValueError, match="credentials"):
        await save(board)


async def test_native_unknown_row_tamper_cannot_release_replay_hold(board, store, tmp_path):
    from firefly_forge.github_projects_models import BoardSyncInputV1
    from firefly_forge.store import identifier

    configured = await save(board)
    run, requests, tickets, worker = await native_run(board, store, tmp_path)
    state = board_server(board, fail_add=True)
    first = BoardSyncInputV1(version=1, request_id=uuid4().hex, expected_binding_digest=configured.binding.digest)
    await tickets.admit(board.project, board.repository, run["id"], first, board.actor)
    await worker.execute(first.request_id)
    async with store._unit():
        row = await requests.records.find_by_id(identifier(first.request_id))
        row.status = "completed"
        row.data = {**row.data, "status": "completed", "result": {"reason": "forged-completion"}}
        await requests.records.save(row)
    with pytest.raises(ValueError, match="immutable outcome"):
        await tickets.get(board.project, board.repository, first.request_id, board.actor)
    second = first.model_copy(update={"request_id": uuid4().hex})
    await tickets.admit(board.project, board.repository, run["id"], second, board.actor)
    await worker.execute(second.request_id)
    assert (await tickets.get(board.project, board.repository, second.request_id, board.actor))["status"] == "failed"
    assert len(state["mutations"]) == 1


async def test_native_automatic_requests_keep_original_cut_across_mapping_revision_without_api_principal(
    board, store, tmp_path
):
    configured = await save(board)
    original_cut = (await board.manager.binding(board.project, board.repository))["cut_sequence"]
    run, requests, tickets, worker = await native_run(board, store, tmp_path)
    await save(board, request_id=uuid4().hex, expected_digest=configured.binding.digest)
    assert (await board.manager.binding(board.project, board.repository))["cut_sequence"] == original_cut
    state = board_server(board)
    context = CURRENT_PRINCIPAL.set(None)
    try:
        await worker.queue_automatic()
        await worker.queue_automatic()
        async with store._unit():
            rows = await requests.records.for_run(UUID(run["id"]), "github_board")
        assert len(rows) == 1 and rows[0].status == "queued"
        await worker.execute(rows[0].id.hex)
    finally:
        CURRENT_PRINCIPAL.reset(context)
    receipt = await tickets.get(board.project, board.repository, rows[0].id.hex, board.actor)
    assert receipt["status"] == "completed" and len(state["mutations"]) == 2


async def test_native_headless_worker_wires_board_dispatch_before_consumers_start(tmp_path, postgres_schema):
    from firefly_forge.api import create_runtime
    from firefly_forge.execution_requests import ExecutionRequests
    from firefly_forge.github_projects_worker import GitHubProjectsWorker

    runtime = create_runtime(tmp_path / "headless", TOKEN, "worker")
    try:
        await runtime.startup()
        requests = runtime.context.get_bean(ExecutionRequests)
        assert isinstance(requests.github_projects, GitHubProjectsWorker)
        assert requests.poller is not None and not requests.poller.done()
    finally:
        await runtime.shutdown()


async def test_native_field_change_cannot_hide_an_unknown_item_add(board, store, tmp_path):
    from firefly_forge.github_projects_models import BoardSyncInputV1

    configured = await save(board)
    run, requests, tickets, worker = await native_run(board, store, tmp_path)
    state = board_server(board, fail_add=True)
    first = BoardSyncInputV1(version=1, request_id=uuid4().hex, expected_binding_digest=configured.binding.digest)
    await tickets.admit(board.project, board.repository, run["id"], first, board.actor)
    await worker.execute(first.request_id)
    board.state["response"]["data"]["owner"]["projectV2"]["fields"]["nodes"].append(
        {
            "__typename": "ProjectV2SingleSelectField",
            "id": "PVTSSF_456",
            "name": "Other field",
            "options": [{"id": "todo", "name": "Todo"}],
        }
    )
    changed = await save(
        board, request_id=uuid4().hex, expected_digest=configured.binding.digest, field_id="PVTSSF_456"
    )
    second = BoardSyncInputV1(version=1, request_id=uuid4().hex, expected_binding_digest=changed.binding.digest)
    await tickets.admit(board.project, board.repository, run["id"], second, board.actor)
    await worker.execute(second.request_id)
    assert (await tickets.get(board.project, board.repository, second.request_id, board.actor))["status"] == "conflict"
    assert len(state["mutations"]) == 1


async def test_native_unknown_after100_retained_admissions_never_replays(board, store, tmp_path):
    from firefly_forge.github_projects_models import BoardSyncInputV1

    configured = await save(board)
    first, requests, tickets, worker = await native_run(board, store, tmp_path)
    runs = [first, await board_run(board, store), await board_run(board, store)]
    for index in range(100):
        await tickets.admit(
            board.project,
            board.repository,
            runs[index // 50]["id"],
            BoardSyncInputV1(version=1, request_id=uuid4().hex, expected_binding_digest=configured.binding.digest),
            board.actor,
        )
    state = board_server(board, fail_add=True)
    unknown = uuid4().hex
    await tickets.admit(
        board.project,
        board.repository,
        runs[-1]["id"],
        BoardSyncInputV1(version=1, request_id=unknown, expected_binding_digest=configured.binding.digest),
        board.actor,
    )
    await worker.execute(unknown)
    assert (await tickets.get(board.project, board.repository, unknown, board.actor))["status"] == "unknown"
    attempts = len(state["mutations"])
    held = uuid4().hex
    await tickets.admit(
        board.project,
        board.repository,
        runs[-1]["id"],
        BoardSyncInputV1(version=1, request_id=held, expected_binding_digest=configured.binding.digest),
        board.actor,
    )
    await worker.execute(held)
    assert (await tickets.get(board.project, board.repository, held, board.actor))["status"] == "conflict"
    assert len(state["mutations"]) == attempts == 1


async def test_native_newer_projection_after100_unrelated_runs_still_fences(board, store, tmp_path):
    from firefly_forge.github_projects_worker import BoardConflict
    from firefly_forge.models import RunInput

    await save(board)
    original, _, _, worker = await native_run(board, store, tmp_path)
    for _ in range(100):
        await store.create_run(
            RunInput(
                repository_id=board.repository,
                project_id=board.project,
                component_id="stock",
                brief="Unrelated controlled native unit",
            )
        )
    await board_run(board, store)
    with pytest.raises(BoardConflict, match="newer native unit owns this issue projection"):
        await worker.current_issue_projection(
            {"run_id": original["id"], "request": {"project_id": board.project}}, {"kind": "issue", "number": 4}
        )


async def test_native_schema31_upgrade_preserves_retained_unknown_framework_part(postgres_schema):
    import importlib.util
    import os
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from firefly_forge.framework_knowledge_persistence import FrameworkKnowledgeRecord
    from firefly_forge.migrations import migrate

    path = Path(__file__).parent / "fixtures/github_projects/schema31_migrations.py"
    spec = importlib.util.spec_from_file_location("retained_schema31", path)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    engine = create_async_engine(os.environ["FORGE_TEST_DATABASE_URL"])
    key = uuid4().hex
    payload = {"state": "unknown", "intent": "retained-original-part", "spent": 1}
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            async with session.begin():
                await old.migrate(session, postgres_schema)
                session.add(
                    FrameworkKnowledgeRecord(id=UUID(key), kind="part", key=key, framework="pyfly", data=payload)
                )
            async with session.begin():
                await migrate(session, postgres_schema)
                assert await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations")) == 34
                retained = (
                    await session.execute(
                        select(FrameworkKnowledgeRecord.data, FrameworkKnowledgeRecord.revision).where(
                            FrameworkKnowledgeRecord.id == UUID(key)
                        )
                    )
                ).one()
                assert retained.data == payload and retained.revision == 1
                indexes = set(
                    (
                        await session.execute(
                            text("SELECT indexname FROM pg_indexes WHERE schemaname=:schema"),
                            {"schema": postgres_schema},
                        )
                    ).scalars()
                )
                assert {"forge_github_board_history_scope", "forge_github_board_history_contents"} <= indexes
    finally:
        await engine.dispose()


@pytest.mark.parametrize("board", ["nonexpiring"], indirect=True)
async def test_native_nonexpiring_oauth_remains_null_in_encrypted_custody_and_resource_admission(board, store):
    from firefly_forge.connection_oauth_persistence import OAuthGrantEntity

    summary = await board.oauth.summary("github-board", board.actor)
    assert summary["grant_expires_at"] is None and summary["refresh_available"] is False
    async with store._unit() as session:
        grant = await session.scalar(select(OAuthGrantEntity))
        assert grant.expires_at is None and grant.actor_id == board.actor.id
    await save(board)
    assert (await board.manager.view(board.project, board.repository, board.actor)).status == "ready"
    await board.oauth.disconnect("github-board", board.actor)
    assert (await board.manager.view(board.project, board.repository, board.actor)).status == "authorization_required"


def creation_server(board, *, lost=False):
    import json

    from firefly_forge import github_projects_graphql as graphql

    state = {"creates": 0}
    created = {
        "id": "PVT_CREATED",
        "title": "Native delivery",
        "number": 9,
        "url": "https://github.com/users/octocat/projects/9",
        "public": False,
        "closed": False,
        "viewerCanUpdate": True,
        "owner": {"__typename": "User", "id": "U_123", "login": "octocat"},
    }

    def handle(request):
        body = json.loads(request.content)
        base = copy.deepcopy(payload()["data"])
        if body["query"] == graphql.CREATION_OWNER:
            return httpx2.Response(200, json={"data": {"viewer": base["viewer"], "repository": base["repository"]}})
        if body["query"] == graphql.CREATE:
            state["creates"] += 1
            if lost:
                raise httpx2.ReadError("Synthetic lost creation response")
            return httpx2.Response(
                200,
                json={
                    "data": {
                        "createProjectV2": {
                            "clientMutationId": body["variables"]["input"]["clientMutationId"],
                            "projectV2": created,
                        }
                    }
                },
            )
        assert body["query"] == graphql.CREATION_READBACK
        return httpx2.Response(
            200,
            json={
                "data": {
                    "viewer": base["viewer"],
                    "repository": base["repository"],
                    "node": created,
                }
            },
        )

    board.state["handle"] = handle
    return state


def create_body():
    from firefly_forge.github_projects_models import BoardCreateInputV1

    return BoardCreateInputV1(
        version=1,
        request_id=uuid4().hex,
        connection_name="github-board",
        owner="octocat",
        title="Native delivery",
        acknowledge_private_creation=True,
    )


async def test_native_creation_receipt_is_immutable_and_same_id_never_replays(board, store):
    state = creation_server(board)
    body = create_body()
    first = await board.manager.create(board.project, board.repository, body, board.actor)
    assert first.status == "completed" and first.board.private
    assert await board.manager.create(board.project, board.repository, body, board.actor) == first
    assert state["creates"] == 1
    for table in ("forge_github_projects_creations", "forge_github_projects_creation_receipts"):
        with pytest.raises(DBAPIError, match="immutable"):
            async with store._unit() as session:
                await session.execute(text(f"UPDATE {table} SET data='{{}}'::jsonb"))


async def test_native_lost_creation_is_unknown_and_replacement_id_is_held(board):
    state = creation_server(board, lost=True)
    body = create_body()
    with pytest.raises(httpx2.ReadError):
        await board.manager.create(board.project, board.repository, body, board.actor)
    result = await board.manager.create(board.project, board.repository, body, board.actor)
    assert result.status == "unknown" and result.replay_authorized is False
    replacement = body.model_copy(update={"request_id": uuid4().hex})
    with pytest.raises(ValueError, match="unknown outcome"):
        await board.manager.create(board.project, board.repository, replacement, board.actor)
    assert state["creates"] == 1


async def test_native_rollback_does_not_publish_a_creation_intent_or_dispatch(board, store, monkeypatch):
    state = creation_server(board)
    original = board.manager.locked
    entered = 0

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def interrupted(*args):
        nonlocal entered
        entered += 1
        async with original(*args) as session:
            yield session
            if entered == 2:
                raise RuntimeError("Synthetic admission rollback")

    monkeypatch.setattr(board.manager, "locked", interrupted)
    body = create_body()
    with pytest.raises(RuntimeError, match="admission rollback"):
        await board.manager.create(board.project, board.repository, body, board.actor)
    from firefly_forge.github_projects_persistence import ForgeGitHubProjectsCreationEntity

    async with store._unit() as session:
        assert await session.get(ForgeGitHubProjectsCreationEntity, UUID(body.request_id)) is None
    assert state["creates"] == 0


async def test_native_migration_rollback_and_upgrade_preserve_expiring_grant_ciphertext(postgres_schema, monkeypatch):
    import importlib.util
    import os
    from datetime import UTC, datetime
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from firefly_forge.connection_oauth_persistence import OAuthConnectionEntity, OAuthGrantEntity
    from firefly_forge.migrations import migrate

    path = Path(__file__).parent / "fixtures/github_projects/schema31_migrations.py"
    spec = importlib.util.spec_from_file_location("retained_oauth_schema31", path)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    engine = create_async_engine(os.environ["FORGE_TEST_DATABASE_URL"])
    oauth = ConnectionOAuth.__new__(ConnectionOAuth)
    oauth.store = SimpleNamespace(schema=postgres_schema)
    oauth.cipher = Fernet(Fernet.generate_key())
    connection = OAuthConnectionEntity(id=uuid4(), name="retained-grant", revision=1, status="authorized_unverified")
    grant = OAuthGrantEntity(
        id=uuid4(),
        connection_id=connection.id,
        registration_revision=1,
        family=uuid4().hex,
        revision=1,
        actor_id=uuid4(),
        status="authorized_unverified",
        expires_at=datetime(2026, 12, 1, tzinfo=UTC),
        data={"issuer": "https://github.com", "resource": "https://api.github.com/graphql", "scopes": ["project"]},
    )
    payload = {"access_token": "synthetic-retained-token", "refresh_token": None}
    context = oauth._grant_context(connection, grant)
    grant.encrypted_data = ciphertext = oauth._seal(context, payload)
    connection_id, grant_id = connection.id, grant.id
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            async with session.begin():
                await old.migrate(session, postgres_schema)
                await session.execute(text("ALTER TABLE forge_oauth_grants ALTER COLUMN expires_at SET NOT NULL"))
                session.add(connection)
                await session.flush()
                session.add(grant)
            execute = session.execute

            async def fail_before_revision(statement, *args, **kwargs):
                if "INSERT INTO forge_schema_migrations" in str(statement):
                    raise RuntimeError("Synthetic migration rollback")
                return await execute(statement, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(session, "execute", fail_before_revision)
                with pytest.raises(RuntimeError, match="migration rollback"):
                    async with session.begin():
                        await migrate(session, postgres_schema)
            async with session.begin():
                await session.execute(text(f'SET LOCAL search_path TO "{postgres_schema}"'))
                assert await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations")) == 31
                assert await session.scalar(text("SELECT to_regclass('forge_github_projects_creations')")) is None
                nullable = await session.scalar(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_schema=:schema AND table_name='forge_oauth_grants' AND column_name='expires_at'"
                    ),
                    {"schema": postgres_schema},
                )
                assert nullable == "NO"
            async with session.begin():
                await migrate(session, postgres_schema)
                restored = await session.get(OAuthGrantEntity, grant_id, populate_existing=True)
                restored_connection = await session.get(OAuthConnectionEntity, connection_id, populate_existing=True)
                assert restored.encrypted_data == ciphertext
                assert oauth._grant_context(restored_connection, restored) == context
                assert oauth._open(restored.encrypted_data, context) == payload
                assert await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations")) == 34
    finally:
        await engine.dispose()
