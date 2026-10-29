# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Returned provider data requires the same still-current grant and actor authority."""

import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx2
import pytest
from test_mcp_registry import KEY, approved, protocol
from test_mcp_registry_native import project_definition
from test_tool_broker import invoke as invoke_search
from test_tool_broker import seed as search_seed

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.mcp_registry import ExternalMCPRegistry, MCPConnectionInput, MCPGrantInput, MCPReviewInput
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.projects import Projects
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.tool_broker import ToolInvocationBroker, ToolReconciliationRequired
from firefly_forge.workspace_tools import tool_catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["grant", "connection", "project", "repository", "source", "role"])
async def test_scope_lost_during_provider_response_cannot_return_data(tmp_path, change):
    module, registry, _, grant, snapshot = await approved(tmp_path)

    def change_after_dispatch(method, value):
        if method == "tools/call":
            if change == "grant":
                registry.revoke("a" * 32, grant["id"])
            elif change == "connection":
                registry.configure("github", module.MCPConnectionInput(enabled=False))
            else:
                with registry.private.lock:
                    state = registry._state()
                    current = next(item for item in state["grants"] if item["id"] == grant["id"])
                    if change in {"project", "repository"}:
                        current[change + "_id"] = "c" * 32
                    elif change == "source":
                        current["scope"]["repo"] = "other-product"
                    else:
                        current["roles"] = ["qa"]
                    registry.private._write(registry.path, state)
        return value

    registry.transport, calls = protocol(changed=change_after_dispatch)
    with pytest.raises(ValueError):
        await registry.call(
            snapshot,
            "github",
            "get_file_contents",
            {"owner": "team", "repo": "product"},
            roles=["implementer"],
            stage="implement",
        )
    assert sum(item["method"] == "tools/call" for item in calls) == 1


@pytest.mark.asyncio
async def test_grant_lost_during_session_cleanup_cannot_return_data(tmp_path):
    _, registry, _, grant, snapshot = await approved(tmp_path)
    original_session = registry._session

    @asynccontextmanager
    async def close_then_revoke(*args, **kwargs):
        async with original_session(*args, **kwargs) as session:
            yield session
        registry.revoke("a" * 32, grant["id"])

    registry._session = close_then_revoke
    with pytest.raises(ValueError, match="revoked"):
        await registry.call(
            snapshot,
            "github",
            "get_file_contents",
            {"owner": "team", "repo": "product"},
            roles=["implementer"],
            stage="implement",
        )


@pytest.mark.asyncio
async def test_rotation_during_response_preserves_original_frozen_credential(tmp_path):
    module, registry, _, _, snapshot = await approved(tmp_path)

    def rotate(method, value):
        if method == "tools/call":
            registry.configure("github", module.MCPConnectionInput(enabled=True, api_key="fixture-new-key"))
        return value

    registry.transport, calls = protocol(changed=rotate)
    result = await registry.call(
        snapshot,
        "github",
        "get_file_contents",
        {"owner": "team", "repo": "product"},
        roles=["implementer"],
        stage="implement",
    )
    assert result["text"] == ["Owned source receipt"] and result["untrusted"]
    assert sum(item["method"] == "tools/call" for item in calls) == 1
    assert registry.snapshot("a" * 32, "b" * 32)["grants"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["grant", "connection", "project", "actor"])
async def test_native_broker_cannot_admit_or_replay_result_after_authority_loss(store, tmp_path, loss):
    registry = ExternalMCPRegistry(store.configuration)
    registry.transport, _ = protocol()
    registry.configure("github", MCPConnectionInput(enabled=True, api_key=KEY))
    discovery = await registry.discover("github")
    registry.review("github", MCPReviewInput(digest=discovery["digest"], tools=["get_file_contents"]))
    repo = await store.add_repository(RepositoryInput(name="Owned source", url="https://github.com/team/product.git"))
    project = await Projects(store).configure(project_definition(repo["id"]))
    grant = registry.grant(
        project["id"],
        "github",
        MCPGrantInput(repository_id=repo["id"], review_digest=discovery["digest"], tools=["get_file_contents"]),
        repository=repo,
    )
    actor = Principal(uuid4(), "https://fixture.invalid", "owned-developer")
    access = ProjectAccess(store)
    await access.set_membership(project["id"], actor, "developer")
    sandbox = SandboxInput(mode="docker", image="owned@sha256:" + "a" * 64, context="owned-fixture")
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        run = await store.create_run(
            RunInput(
                repository_id=repo["id"],
                project_id=project["id"],
                component_id="stock",
                brief="Inspect owned stock source",
                agent="agentic",
                assurance={"review_agent": "agentic"},
                roles=ProjectRoles(allowed_tools={"implementer": ["external_mcp"]}),
                sandbox=sandbox,
            ),
            prepared_tools=tool_catalog({"external_mcp": True}),
            prepared_runner={
                "sandbox_snapshot": capture_sandbox(sandbox),
                "runner_config_revision": None,
                "search_snapshot": None,
            },
        )
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert "work_duration" not in run
    assert "work_duration" not in run["sandbox_snapshot"]["configuration"]
    assert run["sandbox_snapshot"] == capture_sandbox(sandbox)
    broker = ToolInvocationBroker(store, Journal(store), store.path.parent)
    broker.external_mcp = registry
    broker.credential_registry = registry.credentials
    await broker.journal.intent(run["id"], "implement")
    await store.update(run["id"], status="generating")
    original_transport, calls = protocol()

    async def revoke_after_call(request):
        response = await original_transport.handle_async_request(request)
        if request.method == "POST" and json.loads(request.content).get("method") == "tools/call":
            if loss == "grant":
                registry.revoke(project["id"], grant["id"])
            elif loss == "connection":
                registry.configure("github", MCPConnectionInput(enabled=False))
            elif loss == "project":
                await access.set_membership(project["id"], actor, None)
            else:
                async with store._unit():
                    users = ForgeIdentityUserRepository()
                    user = await users.find_by_id(actor.id)
                    user.enabled = False
                    await users.save(user)
        return response

    registry.transport = httpx2.MockTransport(revoke_after_call)
    args = {"connector": "github", "tool": "get_file_contents", "arguments": {"owner": "team", "repo": "product"}}

    async def callback():
        return await registry.call(
            run["mcp_snapshot"],
            "github",
            "get_file_contents",
            args["arguments"],
            roles=["implementer"],
            stage="implement",
        )

    async def invoke():
        return await broker.invoke(
            run["id"],
            "implement",
            "external_mcp:github:get_file_contents",
            "1" * 32,
            args,
            callback,
            source={"base_revision": "a" * 40, "head": "a" * 40, "source_tree": "b" * 40},
            configuration=run["mcp_snapshot"],
        )

    with pytest.raises(ToolReconciliationRequired):
        await invoke()
    records = await broker.invocations(run["id"])
    assert len(records) == 1 and records[0]["status"] in {"pending", "unknown"}
    assert "result" not in records[0]
    with pytest.raises((ValueError, AccessDenied, ToolReconciliationRequired)):
        await invoke()
    if loss == "project":
        await access.set_membership(project["id"], actor, "developer")
    elif loss == "actor":
        async with store._unit():
            users = ForgeIdentityUserRepository()
            user = await users.find_by_id(actor.id)
            user.enabled = True
            await users.save(user)
    elif loss == "connection":
        registry.configure("github", MCPConnectionInput(enabled=True))
    if loss != "grant":
        with pytest.raises(ToolReconciliationRequired):
            await invoke()
    assert sum(item["method"] == "tools/call" for item in calls) == 1
    events = await broker.journal.entries(run["id"])
    assert Journal.verify_chain(events) and not any(item["kind"] == "tool_receipt" for item in events)


@pytest.mark.asyncio
async def test_search_completion_with_suspended_actor_remains_unresolved(store, tmp_path):
    _, broker, run, source = await search_seed(store, tmp_path)
    actor = Principal(uuid4(), "local:bootstrap", "owned-operator", authentication_method="local_bootstrap")
    async with store._unit():
        from firefly_forge.identity_persistence import ForgeIdentityUserEntity

        await ForgeIdentityUserRepository().save(
            ForgeIdentityUserEntity(id=actor.id, issuer=actor.issuer, subject=actor.subject)
        )
    await store.update(run["id"], execution_actor=ProjectAccess.actor_snapshot(actor))
    calls = []

    async def callback():
        calls.append(1)
        async with store._unit():
            users = ForgeIdentityUserRepository()
            user = await users.find_by_id(actor.id)
            user.enabled = False
            await users.save(user)
        return {"results": []}

    with pytest.raises(ToolReconciliationRequired):
        await invoke_search(broker, run, source, "1" * 32, callback)
    record = (await broker.invocations(run["id"]))[0]
    assert record["status"] == "pending" and "result" not in record
    with pytest.raises(AccessDenied):
        await invoke_search(broker, run, source, "1" * 32, callback)
    assert calls == [1]
