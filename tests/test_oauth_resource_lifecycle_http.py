# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual owned HTTP resource dispatch through supported native policy and WAL."""

import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from oauth_repository_http_fixture import repository_provider
from starlette.requests import Request
from test_projects import definition

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.api import create_application
from firefly_forge.identity import IdentityService
from firefly_forge.journal import Journal
from firefly_forge.mcp_registry import ExternalMCPRegistry
from firefly_forge.models import RunInput
from firefly_forge.process import checked
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.tool_broker import ToolInvocationBroker, ToolPolicyInput
from firefly_forge.workspace_tools import tool_catalog

TOKEN = "owned-resource-lifecycle-operator-token-32-characters"
BASE = "/api/v1"
CONNECTOR = "local-repository-tools"


@asynccontextmanager
async def configured(tmp_path, monkeypatch, *, scheme="https"):
    from native_client_knowledge_fixtures import activate_native_client_target

    origin = scheme + "://127.0.0.1:49200"
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", origin)
    monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "127.0.0.1")
    async with repository_provider(origin) as owned:
        app = create_application(tmp_path, TOKEN, role="api")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app, client=("127.0.0.1", 50000)),
                base_url=origin,
                follow_redirects=False,
            ) as browser,
        ):
            browser.headers["Origin"] = origin
            assert (await browser.post(BASE + "/auth/login", json={"token": TOKEN})).status_code == 200
            await browser.get(BASE + "/auth/session")
            browser.headers["X-XSRF-TOKEN"] = browser.cookies["XSRF-TOKEN"]
            await activate_native_client_target(app.state.store, tmp_path / "framework-prerequisite")

            async def request(method, path, **kwargs):
                result = await browser.request(method, BASE + path, **kwargs)
                assert result.status_code in {200, 201}, result.text
                return result.json()

            repo = await request(
                "POST", "/repositories", json={"name": "Owned source", "url": "https://github.com/team/product.git"}
            )
            project = await request("POST", "/projects", json=definition(repo["id"]).model_dump(mode="json"))
            policy = await request(
                "PUT",
                "/oauth-provider-policies/" + CONNECTOR,
                json={
                    "enabled": True,
                    "local_origin": owned.origin,
                    "expected_revision": 0,
                    "request_id": uuid4().hex,
                },
            )
            await request(
                "PUT",
                "/connection-oauth/design",
                json={
                    "provider": CONNECTOR,
                    "client_id": "client",
                    "client_secret": "fixture-secret",
                    "authentication": "client_secret_basic",
                    "project_id": project["id"],
                    "expected_revision": 0,
                },
            )
            handoff = await request("POST", "/connection-oauth/design/prepare")
            consent = await browser.get(handoff["continuation_url"])
            assert consent.status_code == 303
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as provider_browser:
                approved = await provider_browser.get(consent.headers["location"])
            assert approved.status_code == 302, approved.text
            completed = await browser.get(approved.headers["location"])
            assert completed.status_code == 303, completed.text
            linked = await request(
                "PUT",
                "/tools/mcp/" + CONNECTOR + "/oauth",
                json={
                    "connection_name": "design",
                    "project_id": project["id"],
                    "enabled": True,
                },
            )
            assert linked["authentication"] == "oauth"
            discovery = await request("POST", "/tools/mcp/" + CONNECTOR + "/discover")
            names = sorted(discovery["tools"])
            assert names == ["get_commit", "get_file_contents", "list_branches", "list_commits"]
            await request(
                "POST", "/tools/mcp/" + CONNECTOR + "/review", json={"digest": discovery["digest"], "tools": names}
            )
            grant = await request(
                "POST",
                "/tools/mcp/projects/" + project["id"] + "/connectors/" + CONNECTOR + "/grants",
                json={
                    "repository_id": repo["id"],
                    "review_digest": discovery["digest"],
                    "tools": names,
                    "roles": ["implementer"],
                    "stages": ["implement"],
                },
            )
            context = app.state.runtime.context
            store = app.state.store
            broker = context.get_bean(ToolInvocationBroker)
            registry = context.get_bean(ExternalMCPRegistry)
            broker.configure(ToolPolicyInput(calls_limit=10, per_tool_limit=10, timeout_seconds=3))
            actor = await context.get_bean(IdentityService).authenticate(
                Request(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/",
                        "headers": [(b"authorization", ("Bearer " + TOKEN).encode())],
                        "client": ("127.0.0.1", 50000),
                        "scheme": "http",
                        "server": ("127.0.0.1", 49200),
                        "query_string": b"",
                    }
                )
            )
            assert actor is not None
            owner = CURRENT_PRINCIPAL.set(actor)
            try:
                run = await store.create_run(
                    RunInput(
                        repository_id=repo["id"],
                        project_id=project["id"],
                        component_id="stock",
                        agent="agentic",
                        brief="Read the owned repository",
                        skills=[],
                        roles=ProjectRoles(allowed_tools={"implementer": ["external_mcp"]}),
                    ),
                    prepared_runner={
                        "sandbox_snapshot": capture_sandbox(SandboxInput()),
                        "search_snapshot": None,
                        "tool_policy_snapshot": broker.snapshot(),
                        "runner_config_revision": None,
                    },
                    prepared_tools=tool_catalog({"external_mcp": True}),
                )
            finally:
                CURRENT_PRINCIPAL.reset(owner)
            await broker.journal.intent(run["id"], "implement")
            await store.update(run["id"], status="generating")
            git = tmp_path / "owned-product"
            git.mkdir()
            (git / "product.py").write_text("stock = 5\n")
            await checked(["git", "init", "-b", "main"], cwd=git)
            await checked(["git", "add", "product.py"], cwd=git)
            await checked(
                [
                    "git",
                    "-c",
                    "user.name=Owned fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-m",
                    "Capture owned source",
                ],
                cwd=git,
            )
            head = (await checked(["git", "rev-parse", "HEAD"], cwd=git)).strip()
            tree = (await checked(["git", "rev-parse", "HEAD^{tree}"], cwd=git)).strip()
            source = {"base_revision": head, "head": head, "source_tree": tree}
            yield owned, browser, request, policy, grant, store, broker, registry, run, source


async def invoke(broker, registry, run, source, correlation):
    arguments = {"owner": "team", "repo": "product", "path": "product.py"}

    async def dispatch():
        assert (await broker.journal.entries(run["id"]))[-1]["kind"] == "tool_intent"
        return await registry.call(
            run["mcp_snapshot"],
            CONNECTOR,
            "get_file_contents",
            arguments,
            roles=["implementer"],
            stage="implement",
            run_id=run["id"],
        )

    return await broker.invoke(
        run["id"],
        "implement",
        "external_mcp:" + CONNECTOR + ":get_file_contents",
        correlation,
        {"connector": CONNECTOR, "tool": "get_file_contents", "arguments": arguments},
        dispatch,
        source=source,
        configuration=run["mcp_snapshot"],
    )


async def test_supported_policy_stock_as_actual_mcp_wal_refresh_and_withdrawal(tmp_path, postgres_schema, monkeypatch):
    import pytest

    async with configured(tmp_path, monkeypatch) as (
        owned,
        browser,
        request,
        policy,
        _,
        store,
        broker,
        registry,
        run,
        source,
    ):
        correlation = uuid4().hex
        first = await invoke(broker, registry, run, source, correlation)
        assert first["oauth_source"][-1]["grant_revision"] == 1
        assert "stock = 5" in json.dumps(first)
        assert await invoke(broker, registry, run, source, correlation) == first
        assert len(owned.tool_calls) == 1
        replica = create_application(tmp_path, TOKEN, role="api")
        async with (
            replica.router.lifespan_context(replica),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(replica, client=("127.0.0.1", 50000)),
                base_url="https://127.0.0.1:49200",
                headers={"Authorization": "Bearer " + TOKEN},
            ) as observer,
        ):
            response = await observer.post(BASE + "/connection-oauth/design/refresh", json={"request_id": uuid4().hex})
            assert response.status_code == 200, response.text
            refreshed = response.json()
        assert refreshed["grant_revision"] == 2
        second = await invoke(broker, registry, run, source, uuid4().hex)
        assert second["oauth_source"][-1]["grant_revision"] == 2
        assert len(owned.tool_calls) == 2
        assert [packet["grant_type"] for packet in owned.token_requests] == ["authorization_code", "refresh_token"]
        assert not any(owned.browser_cookie_seen)
        events = await broker.journal.entries(run["id"])
        assert Journal.verify_chain(events)
        assert [item["kind"] for item in events].count("tool_intent") == 2
        evidence = json.dumps(events) + json.dumps(await broker.invocations(run["id"]))
        for path in registry.revisions.glob("*.json"):
            evidence += path.read_text()
        for secret in [
            "fixture-secret",
            *(token[key] for token in owned.tokens for key in ("access_token", "refresh_token")),
        ]:
            assert secret not in evidence
        await request(
            "PUT",
            "/oauth-provider-policies/" + CONNECTOR,
            json={
                "enabled": False,
                "local_origin": owned.origin,
                "expected_revision": policy["revision"],
                "request_id": uuid4().hex,
            },
        )
        with pytest.raises((ValueError, RuntimeError)):
            await invoke(broker, registry, run, source, uuid4().hex)
        assert len(owned.tool_calls) == 2
        diagnostic = await request("GET", "/connection-oauth/design")
        assert not diagnostic["policy_current"] and diagnostic["allowed_actions"]["disconnect"]
        assert (await request("POST", "/connection-oauth/design/disconnect"))["status"] == "disconnected"


async def test_https_factory_origin_keeps_local_resource_policy_admissible(tmp_path, postgres_schema, monkeypatch):
    async with configured(tmp_path, monkeypatch, scheme="https") as (_, _, _, _, _, _, broker, registry, run, source):
        assert "stock = 5" in json.dumps(await invoke(broker, registry, run, source, uuid4().hex))


async def test_discovery_locks_current_owner_before_native_policy_lookup(tmp_path, postgres_schema, monkeypatch):
    import asyncio
    from contextvars import Context

    import pytest
    from sqlalchemy import select
    from sqlalchemy.exc import DBAPIError

    from firefly_forge.identity_persistence import ForgeIdentityUserEntity

    async with configured(tmp_path, monkeypatch) as (_, _, _, _, _, store, _, registry, _, _):
        resources = registry.resources
        # The signed binding carries the actual authenticated owner captured by the native grant.
        revision = next(registry.revisions.glob("*.json"))
        binding = json.loads(revision.read_text())["oauth_binding"]
        from uuid import UUID

        from firefly_forge.identity import Principal
        from firefly_forge.project_access import ProjectAccess

        snapshot = binding["owner_actor"]
        principal = Principal(
            UUID(snapshot["user_id"]),
            "local:bootstrap",
            "operator",
            authentication_method="local_bootstrap",
            kind="local_bootstrap",
        )
        looked_up, release = asyncio.Event(), asyncio.Event()
        original = resources.connections._registration

        async def observed(session, row):
            result = await original(session, row)
            looked_up.set()
            await release.wait()
            return result

        monkeypatch.setattr(resources.connections, "_registration", observed)
        pending = asyncio.create_task(resources._admission(binding, actor=principal), context=Context())
        try:
            await asyncio.wait_for(looked_up.wait(), 5)

            async def independent_policy_writer():
                async with store._unit() as session:
                    await session.scalar(
                        select(ForgeIdentityUserEntity)
                        .where(ForgeIdentityUserEntity.id == principal.id)
                        .with_for_update(nowait=True)
                    )

            with pytest.raises(DBAPIError, match="lock"):
                await asyncio.create_task(independent_policy_writer(), context=Context())
        finally:
            release.set()
            await pending
        assert ProjectAccess.is_admin(principal)


async def test_actual_mcp_timeout_retains_unknown_without_duplicate_wire(tmp_path, postgres_schema, monkeypatch):
    import pytest

    from firefly_forge.tool_broker import ToolReconciliationRequired

    async with configured(tmp_path, monkeypatch) as (owned, _, _, _, _, _, broker, registry, run, source):
        owned.hold_tool = True
        correlation = uuid4().hex
        with pytest.raises(ToolReconciliationRequired):
            await invoke(broker, registry, run, source, correlation)
        assert len(owned.tool_calls) == 1
        assert (await broker.invocations(run["id"]))[0]["status"] == "unknown"
        for repeated in (correlation, uuid4().hex):
            with pytest.raises(ToolReconciliationRequired):
                await invoke(broker, registry, run, source, repeated)
        assert len(owned.tool_calls) == 1
        owned.release.set()
        assert Journal.verify_chain(await broker.journal.entries(run["id"]))


async def test_actual_http_postdispatch_owner_revocation_denies_result_without_locks(
    tmp_path, postgres_schema, monkeypatch
):
    import asyncio
    from contextvars import Context
    from uuid import UUID

    import pytest
    from sqlalchemy import select, text

    from firefly_forge.identity_persistence import ForgeIdentityUserEntity
    from firefly_forge.tool_broker import ToolReconciliationRequired

    async with configured(tmp_path, monkeypatch) as (owned, _, _, _, _, store, broker, registry, run, source):
        owned.hold_tool = True
        binding = json.loads(next(registry.revisions.glob("*.json")).read_text())["oauth_binding"]
        correlation = uuid4().hex
        pending = asyncio.create_task(invoke(broker, registry, run, source, correlation))
        try:
            await asyncio.wait_for(owned.entered.wait(), 5)

            async def revoke():
                async with store._unit() as session:
                    acquired = await session.scalar(
                        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"),
                        {"key": store.schema + ":connection-oauth:design"},
                    )
                    assert acquired is True
                    user = await session.scalar(
                        select(ForgeIdentityUserEntity)
                        .where(ForgeIdentityUserEntity.id == UUID(binding["owner_actor"]["user_id"]))
                        .with_for_update(nowait=True)
                    )
                    assert user is not None
                    user.enabled = False

            await asyncio.wait_for(asyncio.create_task(revoke(), context=Context()), 5)
            owned.release.set()
            with pytest.raises(ToolReconciliationRequired):
                await pending
        finally:
            owned.release.set()
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
        assert len(owned.tool_calls) == 1
        # Revoked execution authority cannot publish a completion; its durable intent stays unresolved.
        assert (await broker.invocations(run["id"]))[0]["status"] == "pending"
        assert await broker.unresolved(run["id"])
        assert Journal.verify_chain(await broker.journal.entries(run["id"]))


@pytest.mark.parametrize("change", ["policy_withdrawal", "disconnect", "owner_revocation"])
async def test_cached_receipt_rechecks_native_oauth_without_replay_or_new_observation(
    tmp_path, postgres_schema, monkeypatch, change
):
    import asyncio
    from contextvars import Context
    from uuid import UUID

    from sqlalchemy import select

    from firefly_forge.identity_persistence import ForgeIdentityUserEntity
    from firefly_forge.project_access import AccessDenied

    async with configured(tmp_path, monkeypatch) as (
        owned,
        _,
        request,
        policy,
        _,
        store,
        broker,
        registry,
        run,
        source,
    ):
        correlation = uuid4().hex
        first = await invoke(broker, registry, run, source, correlation)
        receipt = await broker.invocations(run["id"])
        events = await broker.journal.entries(run["id"])
        if change == "policy_withdrawal":
            await request(
                "PUT",
                "/oauth-provider-policies/" + CONNECTOR,
                json={
                    "enabled": False,
                    "local_origin": owned.origin,
                    "expected_revision": policy["revision"],
                    "request_id": uuid4().hex,
                },
            )
        elif change == "disconnect":
            await request("POST", "/connection-oauth/design/disconnect")
        else:
            binding = json.loads(next(registry.revisions.glob("*.json")).read_text())["oauth_binding"]

            async def revoke():
                async with store._unit() as session:
                    user = await session.scalar(
                        select(ForgeIdentityUserEntity).where(
                            ForgeIdentityUserEntity.id == UUID(binding["owner_actor"]["user_id"])
                        )
                    )
                    user.enabled = False

            await asyncio.create_task(revoke(), context=Context())
        with pytest.raises((ValueError, AccessDenied)):
            await invoke(broker, registry, run, source, correlation)
        assert len(owned.tool_calls) == 1 and len(owned.token_requests) == 1
        assert await broker.invocations(run["id"]) == receipt
        assert await broker.journal.entries(run["id"]) == events
        assert first["oauth_source"][-1]["grant_revision"] == 1
