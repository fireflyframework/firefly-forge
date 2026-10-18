# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared native analytics controls; run only against an operator-owned test database."""

import asyncio

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_bootstrap_native_identity import TOKEN, service
from test_identity import request as identity_request
from test_projects import definition

from firefly_forge.analytics import AnalyticsConnection, ProjectAnalytics
from firefly_forge.analytics_models import AnalyticsConfigurationInput
from firefly_forge.analytics_persistence import (
    ForgeAnalyticsBindingRepository,
    ForgeAnalyticsConfigurationReceiptEntity,
)
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.identity_persistence import ForgeIdentityUserEntity
from firefly_forge.models import RepositoryInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.projects import Projects


@pytest_asyncio.fixture
async def native_analytics(store, tmp_path):
    identity = service(store)
    actor = await identity.authenticate(identity_request(bearer="Bearer " + TOKEN))
    assert actor is not None
    repository = await store.add_repository(RepositoryInput(name="Fictional analytics", url=str(tmp_path)))
    project = await Projects(store).configure(definition(repository["id"]))
    connection = AnalyticsConnection(ConfigurationStore(store.path.parent))
    analytics = ProjectAnalytics(store, ProjectAccess(store), connection, ForgeAnalyticsBindingRepository())
    request = AnalyticsConfigurationInput(
        expected_revision=0,
        enabled=True,
        endpoint="https://analytics.example",
        tenant_id="fictional",
        workspace_id="00000000-0000-0000-0000-000000000002",
        dataset_id="00000000-0000-0000-0000-000000000003",
        agent_token="owned-native-analytics-scope",
    )
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        yield analytics, actor, project["id"], request
    finally:
        CURRENT_PRINCIPAL.reset(context)
        await identity.client.aclose()


async def test_native_two_replica_configuration_cas_commits_one_immutable_receipt(
    native_analytics, store_factory, tmp_path
):
    first, actor, project, request = native_analytics
    replica = await store_factory(tmp_path / "replica" / "factory.db")
    second = ProjectAnalytics(replica, ProjectAccess(replica), first.connection, ForgeAnalyticsBindingRepository())
    barrier = asyncio.Barrier(2)
    for instance in (first, second):
        original = instance._binding

        async def read(*args, _original=original, **kwargs):
            value = await _original(*args, **kwargs)
            await barrier.wait()
            return value

        instance._binding = read
    outcomes = await asyncio.gather(
        first.configure(project, request, actor), second.configure(project, request, actor), return_exceptions=True
    )
    assert len([r for r in outcomes if isinstance(r, dict) and r["revision"] == 1]) == 1
    assert len([r for r in outcomes if isinstance(r, ValueError)]) == 1
    async with first.store._unit() as session:
        assert await session.scalar(select(func.count()).select_from(ForgeAnalyticsConfigurationReceiptEntity)) == 1


async def test_native_configuration_receipt_cannot_be_rewritten(native_analytics):
    analytics, actor, project, request = native_analytics
    await analytics.configure(project, request, actor)
    with pytest.raises(DBAPIError, match="immutable"):
        async with analytics.store._unit() as session:
            await session.execute(text("UPDATE forge_analytics_configuration_receipts SET data='{}'::jsonb"))
    async with analytics.store._unit() as session:
        receipt = await session.scalar(select(ForgeAnalyticsConfigurationReceiptEntity))
        assert receipt.data["actor_id"] == actor.id.hex and receipt.revision == 1
        assert "owned-native-analytics-scope" not in str(receipt.data)


async def test_native_identity_revocation_after_get_discards_catalog(native_analytics):
    analytics, actor, project, request = native_analytics
    await analytics.configure(project, request, actor)
    calls = []

    async def reply(req):
        calls.append(req)
        async with analytics.store._unit() as session:
            row = await session.get(ForgeIdentityUserEntity, actor.id, with_for_update=True)
            row.enabled = False
        return httpx.Response(200, json={"items": [], "total": 0, "offset": 0, "has_more": False})

    analytics.connection.transport = httpx.MockTransport(reply)
    with pytest.raises(AccessDenied):
        await analytics.catalog(project, actor)
    assert len(calls) == 1 and calls[0].method == "GET"
