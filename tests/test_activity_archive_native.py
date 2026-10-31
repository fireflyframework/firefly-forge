# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared for the sole native operator on dedicated disposable PostgreSQL schemas."""

import asyncio
import base64
import hashlib
import importlib.util
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from test_activity_archive import command_message
from test_campaign_reports import seeded

from firefly_forge.activity_models import ActivityContentQuery, ActivityUnavailable
from firefly_forge.activity_persistence import ForgeEventContentEntity
from firefly_forge.activity_service import ActivityArchive
from firefly_forge.migrations import SCHEMA_REVISION, migrate
from firefly_forge.persistence import ForgeEventEntity
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.run_evidence import EvidenceQuery, RunEvidence


async def observed(store, run_id):
    return [e for e in (await store.get_run(run_id))["events"] if "activity_content" in e]


async def test_native_atomic_large_message_chunks_and_descriptor_only_evidence(store, tmp_path):
    _, _, journal, campaign, token = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await journal.ensure_plan(run_id)
        before = await journal.entries(run_id)
        message = command_message("x" * 16370 + "🙂" * 17000)
        await store.event(run_id, "stdout", message)
        event = (await observed(store, run_id))[-1]
        descriptor = event["activity_content"]
        assert event["activity_summary"]["exit_code"] == 0 and len(event["message"].encode()) <= 1024
        service, chunks, chunk = ActivityArchive(store), [], 0
        while True:
            result = await service.content(
                run_id,
                event["id"],
                ActivityContentQuery(expected_descriptor=descriptor["descriptor_sha256"], chunk=chunk),
            )
            chunks.append(base64.b64decode(result["data_base64"], validate=True))
            if result["next_chunk"] is None:
                break
            chunk = result["next_chunk"]
        assert b"".join(chunks) == message.encode()
        assert hashlib.sha256(b"".join(chunks)).hexdigest() == descriptor["sanitized_message_sha256"]
        evidence = await RunEvidence(store).query(run_id, EvidenceQuery())
        assert evidence["manifest"]["activity_content_bytes"] == "not_included_with_bound_references"
        assert evidence["manifest"]["limits"]["json_bytes"] == 16777216
        assert await journal.entries(run_id) == before
        with pytest.raises(ActivityUnavailable, match="cut_changed"):
            await service.content(run_id, event["id"], ActivityContentQuery(expected_descriptor="0" * 64))
        with pytest.raises(KeyError):
            await service.content(
                run_id, "f" * 32, ActivityContentQuery(expected_descriptor=descriptor["descriptor_sha256"])
            )
        secret = "fictional-cross-chunk-credential"
        store.credential_registry = dict
        await store.event(run_id, "stdout", "x" * 16380 + secret + "tail")
        protected = (await observed(store, run_id))[-1]
        store.credential_registry = lambda: {"LATE_CREDENTIAL": secret}
        with pytest.raises(ValueError, match="configured credentials"):
            store.reject_credentials(secret)
        for ordinal in (0, 1):
            with pytest.raises(ActivityUnavailable, match="credential_admission"):
                await service.content(
                    run_id,
                    protected["id"],
                    ActivityContentQuery(
                        expected_descriptor=protected["activity_content"]["descriptor_sha256"], chunk=ordinal
                    ),
                )
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_native_writer_failure_is_atomic_and_concurrent_budget_is_serialized(store, tmp_path, monkeypatch):
    import firefly_forge.store as module

    _, _, _, campaign, token = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        before = (await store.get_run(run_id))["events"]
        save = store.event_content_records.save

        async def fail(row):
            await save(row)
            raise RuntimeError("Owned archive transaction failure")

        monkeypatch.setattr(store.event_content_records, "save", fail)
        with pytest.raises(RuntimeError, match="Owned archive"):
            await store.event(run_id, "stdout", "public rollback")
        assert (await store.get_run(run_id))["events"] == before
        async with store._unit() as session:
            assert await session.scalar(select(func.count()).select_from(ForgeEventContentEntity)) == 0
        monkeypatch.setattr(store.event_content_records, "save", save)
        monkeypatch.setattr(module, "MAX_EVENTS", 1)
        await asyncio.gather(store.event(run_id, "stdout", "one"), store.event(run_id, "stderr", "two"))
        events = await observed(store, run_id)
        assert sorted(e["activity_content"]["status"] for e in events) == ["available", "budget_exceeded"]
        async with store._unit() as session:
            assert await session.scalar(select(func.count()).select_from(ForgeEventContentEntity)) == 1
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_native_populated_schema29_upgrade_is_additive_and_does_not_backfill(
    store_factory, tmp_path, monkeypatch
):
    import firefly_forge.store as module

    source = Path(__file__).parent / "fixtures/activity_archive/schema29_migrations.py"
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == "732cde519d48066f4290c70b8b6ebb261690728bab5cf0f554ff91ffbefe6b35"
    )
    spec = importlib.util.spec_from_file_location("owned_activity_schema29", source)
    assert spec and spec.loader
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    assert legacy.SCHEMA_REVISION == 29 and SCHEMA_REVISION == 32
    monkeypatch.setattr(module, "migrate", legacy.migrate)
    store = await store_factory(tmp_path / "schema29/factory.db")
    from firefly_forge.models import RepositoryInput, RunInput

    repo = await store.add_repository(RepositoryInput(name="Legacy fictional activity", url=str(tmp_path)))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Preserve legacy activity"))
    async with store._unit() as session:
        event_id = UUID("e" * 32)
        legacy_data = {
            "id": event_id.hex,
            "stage": "stdout",
            "message": "historically cut JSON {",
            "created_at": "2026-10-20",
        }
        session.add(ForgeEventEntity(id=event_id, run_id=UUID(run["id"]), data=legacy_data))
    monkeypatch.setattr(module, "migrate", migrate)
    async with store._unit() as session:
        await migrate(session, store.schema)
        await migrate(session, store.schema)
        assert list(await session.scalars(text("SELECT revision FROM forge_schema_migrations ORDER BY revision"))) == [
            29,
            SCHEMA_REVISION,
        ]
        assert await session.scalar(select(func.count()).select_from(ForgeEventContentEntity)) == 0
        assert await session.scalar(select(ForgeEventEntity.data).where(ForgeEventEntity.id == event_id)) == legacy_data
    await store.event(run["id"], "stdout", command_message("new complete output"))
    assert (await observed(store, run["id"]))[-1]["activity_content"]["status"] == "available"


def test_native_api_di_sdk_uses_authenticated_exact_content_route(tmp_path, postgres_schema, monkeypatch):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.client import ForgeClient
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.store import Store

    monkeypatch.setenv("FORGE_AUTH_MODE", "local_bootstrap")
    token = "fictional-native-activity-operator-token"
    app = create_application(tmp_path / "api", token, role="api")

    class Client(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with Client(
        app,
        base_url="http://127.0.0.1/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        client = ForgeClient("http://127.0.0.1", token)
        client.http.close()
        client.http = http
        repo = client.add_repository(RepositoryInput(name="Native activity", url=str(tmp_path)))
        run = client.create_run(RunInput(repository_id=repo["id"], brief="Read observed activity", operation="test"))
        store = app.state.runtime.context.get_bean(Store)
        evidence = app.state.runtime.context.get_bean(RunEvidence)
        authorize = evidence._authorize
        authorizations = []

        async def observed_authorize(run, repositories, reader):
            authorizations.append(run["id"] if "id" in run else run["repository_id"])
            await authorize(run, repositories, reader)

        monkeypatch.setattr(evidence, "_authorize", observed_authorize)
        http.portal.call(store.event, run["id"], "stdout", command_message("x" * 70000))
        event = next(e for e in client.get_run(run["id"])["events"] if "activity_content" in e)
        query = {"expected_descriptor": event["activity_content"]["descriptor_sha256"], "chunk": 0}
        result = client.run_activity_content(run["id"], event["id"], query)
        assert result["total_bytes"] > 70000 and result["next_chunk"] == 1
        assert authorizations
        path = f"runs/{run['id']}/activity/{event['id']}/content"
        assert http.get(path, params=query, headers={"Authorization": "Bearer incorrect"}).status_code == 401
        assert http.get(path, params={**query, "chunk": 256}).status_code == 422


async def test_native_reader_revocation_during_complete_payload_admission_returns_no_chunk(store, tmp_path):
    from uuid import uuid4

    from pyfly.data.transaction import is_transaction_active

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    project, _, journal, campaign, token = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await journal.ensure_plan(run_id)
        await store.event(run_id, "stdout", command_message("public retained output"))
        event = (await observed(store, run_id))[-1]
        viewer = Principal(uuid4(), "https://identity.invalid", "activity-viewer")
        access = ProjectAccess(store)
        await access.set_membership(project["id"], viewer, "viewer")
        reader = CURRENT_PRINCIPAL.set(viewer)
        try:
            admissions = []

            async def revoke(value):
                assert not is_transaction_active()
                assert "public retained output" in value
                admissions.append(value)
                await access.set_membership(project["id"], viewer, None)

            store.vault_credential_admission = revoke
            with pytest.raises(AccessDenied):
                await ActivityArchive(store).content(
                    run_id,
                    event["id"],
                    ActivityContentQuery(expected_descriptor=event["activity_content"]["descriptor_sha256"]),
                )
            assert len(admissions) == 1
        finally:
            CURRENT_PRINCIPAL.reset(reader)
    finally:
        CURRENT_PRINCIPAL.reset(token)
