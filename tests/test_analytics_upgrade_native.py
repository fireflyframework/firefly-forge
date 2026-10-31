# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared only: actual populated schema28 to optional analytics schema29."""

import re
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_campaign_delivery_plans import prepared
from test_run_creation_cancellation_native import cancellation
from test_run_creation_cancellation_upgrade_native import previous_module
from test_run_creation_requests_native import registered

from firefly_forge.analytics import AnalyticsConnection, ProjectAnalytics
from firefly_forge.analytics_models import AnalyticsConfigurationInput
from firefly_forge.analytics_persistence import ForgeAnalyticsBindingRepository
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.migrations import SCHEMA_REVISION, migrate
from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
from firefly_forge.run_creation_requests import RunCreationRequests


async def retained_rows(store, tables=None):
    async with store._unit() as session:
        if tables is None:
            tables = list(
                await session.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema=:schema AND table_type='BASE TABLE' "
                        "AND starts_with(table_name, 'forge_') AND table_name<>'forge_schema_migrations' "
                        "ORDER BY table_name"
                    ),
                    {"schema": store.schema},
                )
            )
        assert tables and all(re.fullmatch(r"forge_[a-z0-9_]+", table) for table in tables)
        return {
            table: list(
                await session.scalars(
                    text(f'SELECT to_jsonb(row)::text FROM "{table}" AS row ORDER BY to_jsonb(row)::text')
                )
            )
            for table in tables
        }


async def test_populated_actual_schema28_to29_preserves_authority_and_receipts_then_admits_catalog_state(
    store_factory, tmp_path, monkeypatch
):
    import firefly_forge.store as store_module

    legacy = previous_module("schema28_migrations", "05afcf7c9d6f792206fccd8505e1d24f4d828b9f0d171062ac2b6bb98274b021")
    assert legacy.SCHEMA_REVISION == 28
    monkeypatch.setattr(store_module, "migrate", legacy.migrate)
    store = await store_factory(tmp_path / "schema28" / "factory.db")
    async with store._unit() as session:
        assert await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations")) == 28
        for table in ("forge_analytics_bindings", "forge_analytics_configuration_receipts"):
            assert await session.scalar(text("SELECT to_regclass(:table)"), {"table": table}) is None
    c = await prepared(store, tmp_path)
    try:
        delivery = await c.plans.save(c.project["id"], c.campaign["id"], c.body)
        run_id = c.campaign["children"][0]["run_id"]
        await c.journal.ensure_plan(run_id)
        await c.journal.intent(run_id, "verify")
        async with registered(store, tmp_path) as (actor, _, body):
            requests = RunCreationRequests(store)
            admitted_id, cancelled_id = uuid4().hex, uuid4().hex
            admission, created = await requests.admit(admitted_id, body)
            assert created
            cancel_input = cancellation(body.model_dump(exclude_unset=True))
            cancelled, created = await requests.cancel(cancelled_id, cancel_input)
            assert created and cancelled["outcome"] == "cancelled"
            before = await retained_rows(store)
            for table in (
                "forge_identity_users",
                "forge_projects",
                "forge_project_revisions",
                "forge_repositories",
                "forge_runs",
                "forge_events",
                "forge_plans",
                "forge_journal",
                "forge_campaigns",
                "forge_campaign_events",
                "forge_campaign_report_archives",
                "forge_campaign_delivery_plan_revisions",
                "forge_run_creation_requests",
                "forge_run_creation_reservations",
                "forge_run_creation_cancellations",
            ):
                assert before[table], table
            monkeypatch.setattr(store_module, "migrate", migrate)
            async with store._unit() as session:
                await migrate(session, store.schema)
                await migrate(session, store.schema)
                assert list(
                    await session.scalars(text("SELECT revision FROM forge_schema_migrations ORDER BY revision"))
                ) == [28, SCHEMA_REVISION]
                for table in ("forge_analytics_bindings", "forge_analytics_configuration_receipts"):
                    assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            assert await retained_rows(store, before) == before
            assert await requests.get(admitted_id) == admission
            assert await requests.admit(admitted_id, body) == (admission, False)
            assert await requests.decision(cancelled_id) == cancelled
            assert await requests.cancel(cancelled_id, cancel_input) == (cancelled, False)

            calls = []

            async def reply(request):
                calls.append(request)
                return httpx.Response(200, json={"items": [], "total": 0, "offset": 0, "has_more": False})

            connection = AnalyticsConnection(
                ConfigurationStore(store.path.parent), transport=httpx.MockTransport(reply)
            )
            analytics = ProjectAnalytics(store, ProjectAccess(store), connection, ForgeAnalyticsBindingRepository())
            initial = await analytics.configuration(c.project["id"], actor)
            assert initial["revision"] == 0 and initial["status"] == "disabled"
            configuration = AnalyticsConfigurationInput(
                expected_revision=0,
                enabled=True,
                endpoint="https://analytics.example",
                tenant_id="fictional",
                workspace_id="00000000-0000-0000-0000-000000000002",
                dataset_id="00000000-0000-0000-0000-000000000003",
                agent_token="owned-upgrade-analytics-token",
            )
            saved = await analytics.configure(c.project["id"], configuration, actor)
            assert saved["revision"] == 1 and saved["status"] == "configured"
            observation = await analytics.catalog(c.project["id"], actor)
            assert observation["configuration_revision"] == 1 and observation["data"]["items"] == []
            assert (observation["tenant_id"], observation["workspace_id"], observation["dataset_id"]) == (
                configuration.tenant_id,
                configuration.workspace_id,
                configuration.dataset_id,
            )
            assert len(calls) == 1 and calls[0].method == "GET"
            async with store._unit() as session:
                assert await session.scalar(text("SELECT count(*) FROM forge_analytics_bindings")) == 1
                assert await session.scalar(text("SELECT count(*) FROM forge_analytics_configuration_receipts")) == 1
                receipt = await session.scalar(text("SELECT data::text FROM forge_analytics_configuration_receipts"))
                assert "owned-upgrade-analytics-token" not in receipt
            for statement in (
                "UPDATE forge_analytics_configuration_receipts SET data='{}'::jsonb",
                "DELETE FROM forge_analytics_configuration_receipts",
            ):
                with pytest.raises(DBAPIError, match="immutable"):
                    async with store._unit() as session:
                        await session.execute(text(statement))
            async with store._unit() as session:
                assert (
                    await session.scalar(text("SELECT data::text FROM forge_analytics_configuration_receipts"))
                    == receipt
                )
            assert await retained_rows(store, before) == before
        assert await c.history.get(c.project["id"], c.campaign["id"], c.archive["request_id"]) == c.archive
        assert await c.plans.get(c.project["id"], c.campaign["id"], c.body.request_id) == delivery
        assert c.journal.verify_chain(await c.journal.entries(run_id))
    finally:
        CURRENT_PRINCIPAL.reset(c.token)
