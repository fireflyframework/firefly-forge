# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Persisted MCP success requires current frozen authority across native receipt awaits."""

from uuid import uuid4

import pytest
from test_mcp_registry import KEY, protocol
from test_mcp_registry_native import project_definition

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.mcp_registry import ExternalMCPRegistry, MCPConnectionInput, MCPGrantInput, MCPReviewInput
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.projects import Projects
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.tool_broker import ToolInvocationBroker, ToolReconciliationRequired
from firefly_forge.workspace_tools import tool_catalog


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "loss,boundary",
    [
        (loss, boundary)
        for loss in ["grant", "connection", "source", "role", "rotation"]
        for boundary in ["run_lock", "row_read", "row_save", "journal_append", "cached_read"]
    ]
    + [("project", "cached_read"), ("actor", "cached_read")],
)
async def test_native_mcp_receipt_rechecks_frozen_grant_at_each_await(store, loss, boundary):
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
                sandbox=SandboxInput(mode="docker", image="owned@sha256:" + "a" * 64, context="owned-fixture"),
            ),
            prepared_tools=tool_catalog({"external_mcp": True}),
        )
    finally:
        CURRENT_PRINCIPAL.reset(token)
    broker = ToolInvocationBroker(store, Journal(store), store.path.parent)
    broker.external_mcp = registry
    broker.credential_registry = registry.credentials
    await broker.journal.intent(run["id"], "implement")
    await store.update(run["id"], status="generating")
    registry.transport, calls = protocol()
    armed, fired = False, False
    target, method = {
        "run_lock": (broker.journal, "_run"),
        "row_read": (broker.records, "for_run"),
        "row_save": (broker.records, "save"),
        "journal_append": (broker.journal, "_append"),
        "cached_read": (broker.records, "for_run"),
    }[boundary]
    original = getattr(target, method)

    async def finish_await_then_change(*args, **kwargs):
        nonlocal fired
        result = await original(*args, **kwargs)
        if armed and not fired:
            fired = True
            if loss == "grant":
                registry.revoke(project["id"], grant["id"])
            elif loss == "connection":
                registry.configure("github", MCPConnectionInput(enabled=False))
            elif loss == "project":
                await access.set_membership(project["id"], actor, None)
            elif loss == "actor":
                async with store._unit():
                    users = ForgeIdentityUserRepository()
                    user = await users.find_by_id(actor.id)
                    user.enabled = False
                    await users.save(user)
            elif loss == "rotation":
                registry.configure("github", MCPConnectionInput(enabled=True, api_key="fixture-next-key"))
            else:
                with registry.private.lock:
                    state = registry._state()
                    current = next(item for item in state["grants"] if item["id"] == grant["id"])
                    if loss == "source":
                        current["scope"]["repo"] = "other-product"
                    else:
                        current["roles"] = ["qa"]
                    registry.private._write(registry.path, state)
        return result

    setattr(target, method, finish_await_then_change)
    args = {"connector": "github", "tool": "get_file_contents", "arguments": {"owner": "team", "repo": "product"}}

    async def callback():
        nonlocal armed
        result = await registry.call(
            run["mcp_snapshot"],
            "github",
            "get_file_contents",
            args["arguments"],
            roles=["implementer"],
            stage="implement",
        )
        armed = boundary != "cached_read"
        return result

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

    if boundary == "cached_read":
        original_result = await invoke()
        armed = True
        if loss == "rotation":
            assert await invoke() == original_result
        else:
            with pytest.raises((ValueError, AccessDenied)):
                await invoke()
        records = await broker.invocations(run["id"])
        assert records[0]["status"] == "succeeded" and records[0]["result"] == original_result
        assert fired
        assert sum(item["method"] == "tools/call" for item in calls) == 1
        events = await broker.journal.entries(run["id"])
        assert Journal.verify_chain(events)
        assert sum(item["kind"] == "tool_receipt" for item in events) == 1
        return
    if loss == "rotation":
        result = await invoke()
        assert result["text"] == ["Owned source receipt"] and result["untrusted"]
        records = await broker.invocations(run["id"])
        assert records[0]["status"] == "succeeded" and records[0]["result"] == result
        assert await invoke() == result
    else:
        with pytest.raises(ToolReconciliationRequired):
            await invoke()
        records = await broker.invocations(run["id"])
        assert len(records) == 1 and records[0]["status"] == "unknown"
        assert "result" not in records[0]
        with pytest.raises(ValueError):
            await invoke()
    assert fired
    assert sum(item["method"] == "tools/call" for item in calls) == 1
    events = await broker.journal.entries(run["id"])
    assert Journal.verify_chain(events)
    assert sum(item["kind"] == "tool_receipt" for item in events) == (1 if loss == "rotation" else 0)


@pytest.mark.asyncio
async def test_cached_search_result_requires_current_actor_authority(store, tmp_path):
    from test_tool_broker import invoke as invoke_search
    from test_tool_broker import seed as search_seed

    from firefly_forge.identity_persistence import ForgeIdentityUserEntity

    _, broker, run, source = await search_seed(store, tmp_path)
    actor = Principal(uuid4(), "local:bootstrap", "owned-operator", authentication_method="local_bootstrap")
    async with store._unit():
        await ForgeIdentityUserRepository().save(
            ForgeIdentityUserEntity(id=actor.id, issuer=actor.issuer, subject=actor.subject)
        )
    await store.update(run["id"], execution_actor=ProjectAccess.actor_snapshot(actor))
    calls = []

    async def callback():
        calls.append(1)
        return {"results": []}

    original_result = await invoke_search(broker, run, source, "1" * 32, callback)
    original_read = broker.records.for_run

    async def read_then_suspend(*args, **kwargs):
        records = await original_read(*args, **kwargs)
        users = ForgeIdentityUserRepository()
        user = await users.find_by_id(actor.id)
        user.enabled = False
        await users.save(user)
        return records

    broker.records.for_run = read_then_suspend
    with pytest.raises(AccessDenied):
        await invoke_search(broker, run, source, "1" * 32, callback)
    records = await broker.invocations(run["id"])
    assert records[0]["status"] == "succeeded" and records[0]["result"] == original_result
    assert calls == [1]
