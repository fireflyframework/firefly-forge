# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline native-boundary and official SDK transport controls; no database claims."""

import copy
import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

PROJECT, WORKSPACE, DATASET, TABLE, OBJECT = [str(UUID(int=n)) for n in range(1, 6)]
ACTOR = Principal(UUID(int=6), "local:bootstrap", "operator", authentication_method="local_bootstrap")


def request(**changes):
    from firefly_forge.analytics_models import AnalyticsConfigurationInput

    return AnalyticsConfigurationInput.model_validate(
        dict(
            expected_revision=0,
            enabled=True,
            endpoint="https://analytics.example",
            tenant_id="fictional",
            workspace_id=WORKSPACE,
            dataset_id=DATASET,
            agent_token="scoped-test-token",
            **changes,
        )
    )


@pytest.fixture
def analytics(tmp_path):
    from firefly_forge.analytics import AnalyticsConnection, ProjectAnalytics

    state = {"head": None, "rows": [], "enabled": True, "admin": True, "allow": True, "depth": 0, "calls": []}

    async def scalar(statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity.__tablename__ == "forge_identity_users":
            if state.get("identity_missing"):
                return None
            return SimpleNamespace(
                id=ACTOR.id, issuer=ACTOR.issuer, subject=state.get("subject", ACTOR.subject), enabled=state["enabled"]
            )
        return SimpleNamespace(id=UUID(PROJECT), data={})

    async def execute(*args):
        pass

    def add(row):
        state["rows"].append(row)
        if row.__tablename__ == "forge_analytics_bindings":
            state["head"] = row

    @asynccontextmanager
    async def unit():
        state["depth"] += 1
        try:
            yield SimpleNamespace(scalar=scalar, execute=execute, add=add)
        finally:
            state["depth"] -= 1

    async def current_admin(actor):
        return state["admin"]

    async def allowed(*args):
        if not state["allow"]:
            raise AccessDenied("revoked")

    class Records:
        async def for_project(self, project):
            return state["head"]

    async def reply(req):
        assert not state["depth"]
        state["calls"].append(req)
        if state.get("transport_error"):
            raise httpx.ReadTimeout("owned transport failure", request=req)
        body = dict(
            id=TABLE,
            tenant_id="fictional",
            workspace_id=WORKSPACE,
            dataset_id=DATASET,
            business_owner=None,
            created_at="2026-10-17T00:00:00Z",
            updated_at="2026-10-17T00:00:00Z",
            current_snapshot_id=None,
            description=None,
            description_source=None,
            is_active=True,
            kind="table",
            name="Stock",
            qualified_name="Stock",
            sheet_or_json_path=None,
            source_file_id=None,
        )
        response = (
            {"items": [body], "total": 1, "offset": 0, "has_more": False} if req.url.path.endswith("/tables") else body
        )
        if state.get("response"):
            response = state["response"](response)
        if state.get("during"):
            state["during"]()
        return httpx.Response(state.get("status", 200), json=response)

    store = SimpleNamespace(schema="owned", _unit=unit, session_provider=SimpleNamespace(current=lambda: None))
    access = SimpleNamespace(current_administrator=current_admin, require_project=allowed)
    connection = AnalyticsConnection(ConfigurationStore(tmp_path), transport=httpx.MockTransport(reply))
    service = ProjectAnalytics(store, access, connection, Records())
    token = CURRENT_PRINCIPAL.set(ACTOR)
    yield service, state
    CURRENT_PRINCIPAL.reset(token)


async def test_disabled_optional_path_does_not_load_sdk_or_make_http(analytics, monkeypatch):
    service, state = analytics
    monkeypatch.setattr(importlib, "import_module", lambda *_: pytest.fail("optional SDK imported while disabled"))
    assert (await service.configuration(PROJECT, ACTOR))["status"] == "disabled"
    with pytest.raises(ValueError, match="not configured"):
        await service.catalog(PROJECT, ACTOR)
    assert state["calls"] == [] and state["rows"] == []


async def test_configuration_cas_receipt_and_exact_scoped_catalog(analytics):
    service, state = analytics
    result = await service.configure(PROJECT, request(), ACTOR)
    assert result["revision"] == 1 and result["status"] == "configured"
    assert "scoped-test-token" not in str(result)
    with pytest.raises(ValueError, match="revision"):
        await service.configure(PROJECT, request(), ACTOR)
    result = await service.catalog(PROJECT, ACTOR)
    assert result["data"]["items"][0]["id"] == TABLE
    assert result["configuration_revision"] == 1 and result["snapshot_assurance"] == "reported_identity_only"
    assert len(state["calls"]) == 1
    req = state["calls"][0]
    assert req.method == "GET" and req.url.path == f"/api/v1/agent/datasets/{DATASET}/tables"
    assert req.headers["x-tenant-id"] == "fictional" and req.headers["x-workspace-id"] == WORKSPACE
    assert req.headers["x-agent-token"] == "scoped-test-token"
    assert len([r for r in state["rows"] if r.__tablename__ == "forge_analytics_configuration_receipts"]) == 1


@pytest.mark.parametrize("denial", ["disabled", "permission", "context", "binding", "secret"])
async def test_late_denial_discards_remote_bytes(analytics, denial):
    service, state = analytics
    await service.configure(PROJECT, request(), ACTOR)

    def revoke():
        if denial == "disabled":
            state["enabled"] = False
        elif denial == "permission":
            state["allow"] = False
            state["admin"] = False
        elif denial == "context":
            CURRENT_PRINCIPAL.set(None)
        elif denial == "binding":
            state["head"].revision += 1
        else:
            record = copy.deepcopy(state["head"].data)
            service.connection.private._write(
                service.connection.private.revisions / (record["secret_revision"] + ".json"), {"agent_token": "changed"}
            )

    state["during"] = revoke
    with pytest.raises((AccessDenied, ValueError)):
        await service.catalog(PROJECT, ACTOR)
    assert len(state["calls"]) == 1


@pytest.mark.parametrize(
    "failure", ["scope", "partial", "missing_metadata", "404", "redirect", "transport", "oversized"]
)
async def test_remote_failure_has_no_retry_or_write_fallback(analytics, failure):
    service, state = analytics
    await service.configure(PROJECT, request(), ACTOR)
    if failure == "404":
        state["status"] = 404
    elif failure == "redirect":
        state["status"] = 302
    elif failure == "transport":
        state["transport_error"] = True
    elif failure == "partial":
        state["response"] = lambda r: {**r, "has_more": True}
    elif failure == "missing_metadata":
        state["response"] = lambda r: {"items": r["items"]}
    elif failure == "oversized":
        state["response"] = lambda r: {**r, "padding": "x" * 262145}
    else:
        state["response"] = lambda r: {**r, "items": [{**r["items"][0], "workspace_id": str(UUID(int=99))}]}
    with pytest.raises(ValueError, match="unavailable"):
        await service.catalog(PROJECT, ACTOR)
    assert len(state["calls"]) == 1 and state["calls"][0].method == "GET"


async def test_refusal_precedes_credential_sdk_and_network(analytics, monkeypatch):
    service, state = analytics
    await service.configure(PROJECT, request(), ACTOR)
    state["enabled"] = False
    monkeypatch.setattr(service.connection, "load", lambda *_: pytest.fail("credential read after refusal"))
    with pytest.raises(AccessDenied):
        await service.catalog(PROJECT, ACTOR)
    assert state["calls"] == []


async def test_prepared_queries_explicitly_unavailable_without_outbound_call(analytics):
    service, state = analytics
    await service.configure(PROJECT, request(), ACTOR)
    with pytest.raises(ValueError, match="prepared"):
        await service.prepared(PROJECT, str(UUID(int=77)), ACTOR)
    assert state["calls"] == []


async def test_retained_analytics_tokens_participate_in_existing_export_credential_rejection(analytics):
    from firefly_forge.secrets import contains_known_credential

    service, _ = analytics
    await service.configure(PROJECT, request(), ACTOR)
    assert contains_known_credential(
        "source mistakenly contains scoped-test-token", service.connection.redaction_settings()
    )
    assert not contains_known_credential("safe fictional stock source", service.connection.redaction_settings())


@pytest.mark.parametrize(
    "value",
    [
        "http://analytics.example",
        "https://analytics.example:8443",
        "https://user:secret@analytics.example",
        "https://analytics.example/path",
        "https://analytics.example?scope=other",
    ],
)
def test_configuration_refuses_unsupported_transport_authority(value):
    from firefly_forge.analytics_models import AnalyticsConfigurationInput

    body = request().model_dump()
    body["endpoint"] = value
    with pytest.raises(ValueError):
        AnalyticsConfigurationInput.model_validate(body)


async def test_nonadmin_configuration_is_refused_before_private_write(analytics, monkeypatch):
    service, state = analytics
    state["admin"] = False
    monkeypatch.setattr(service.connection, "prepare", lambda *_: pytest.fail("private write before authorization"))
    with pytest.raises(AccessDenied):
        await service.configure(PROJECT, request(), ACTOR)
    assert state["rows"] == [] and state["calls"] == []


@pytest.mark.parametrize("change", ["missing", "mismatched"])
async def test_unknown_or_mismatched_native_identity_never_reads_credentials(analytics, monkeypatch, change):
    service, state = analytics
    await service.configure(PROJECT, request(), ACTOR)
    state["identity_missing"] = change == "missing"
    state["subject"] = "foreign-subject"
    monkeypatch.setattr(service.connection, "load", lambda *_: pytest.fail("credential read after identity refusal"))
    with pytest.raises(AccessDenied):
        await service.catalog(PROJECT, ACTOR)
    assert state["calls"] == []


async def test_ambient_native_unit_is_refused_before_authorization_or_credentials(analytics, monkeypatch):
    import firefly_forge.analytics as module

    service, state = analytics
    monkeypatch.setattr(module, "current_unit_of_work", lambda: object())
    with pytest.raises(ValueError, match="ambient"):
        await service.catalog(PROJECT, ACTOR)
    assert state["rows"] == [] and state["calls"] == []


async def test_table_read_uses_exact_remote_uuid_and_reports_only_snapshot_identity(analytics):
    service, state = analytics
    await service.configure(PROJECT, request(), ACTOR)
    result = await service.table(PROJECT, TABLE, ACTOR)
    assert result["data"]["current_snapshot_id"] is None
    assert result["snapshot_assurance"] == "reported_identity_only"
    assert state["calls"][0].url.path == "/api/v1/agent/tables/00000000-0000-0000-0000-000000000004"


async def test_missing_parent_snapshot_refuses_schema_object_without_second_get(analytics):
    service, state = analytics
    await service.configure(PROJECT, request(), ACTOR)
    with pytest.raises(ValueError, match="unavailable"):
        await service.schema_object(PROJECT, TABLE, OBJECT, ACTOR)
    assert len(state["calls"]) == 1


async def test_scoped_schema_object_is_bound_to_observed_parent_snapshot_and_never_sends_cookies(analytics):
    service, state = analytics
    await service.configure(PROJECT, request(), ACTOR)
    snapshot = str(UUID(int=7))

    async def reply(req):
        state["calls"].append(req)
        assert "cookie" not in req.headers
        assert req.extensions["timeout"]["connect"] == 3.0
        if req.url.path.endswith(TABLE):
            return httpx.Response(
                200,
                json=dict(
                    id=TABLE,
                    tenant_id="fictional",
                    workspace_id=WORKSPACE,
                    dataset_id=DATASET,
                    business_owner=None,
                    created_at="2026-10-17T00:00:00Z",
                    updated_at="2026-10-17T00:00:00Z",
                    current_snapshot_id=snapshot,
                    description=None,
                    description_source=None,
                    is_active=True,
                    kind="table",
                    name="Stock",
                    qualified_name="Stock",
                    sheet_or_json_path=None,
                    source_file_id=None,
                ),
                headers={"Set-Cookie": "untrusted=never-forward; Path=/"},
            )
        return httpx.Response(
            200,
            json=dict(
                id=OBJECT,
                table_id=TABLE,
                snapshot_id=snapshot,
                tenant_id="fictional",
                workspace_id=WORKSPACE,
                business_owner=None,
                created_at="2026-10-17T00:00:00Z",
                data_type="integer",
                description=None,
                description_source=None,
                is_active=True,
                is_nullable=False,
                kind="column",
                last_changed_at="2026-10-17T00:00:00Z",
                pii_source=None,
                pii_tag=None,
                qualified_name="Stock.count",
            ),
        )

    service.connection.transport = httpx.MockTransport(reply)
    result = await service.schema_object(PROJECT, TABLE, OBJECT, ACTOR)
    assert result["data"]["snapshot_id"] == snapshot and result["data"]["data_type"] == "integer"
    assert [r.url.path for r in state["calls"]] == [
        "/api/v1/agent/tables/" + TABLE,
        "/api/v1/agent/schema-objects/" + OBJECT,
    ]
