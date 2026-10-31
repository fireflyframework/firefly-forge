# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled projection transport: no PostgreSQL, models, processes or HTTP."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from firefly_forge.factory_dashboard import FactoryDashboard
from firefly_forge.factory_dashboard_models import DashboardPageInput, DashboardScope, cursor_decode
from firefly_forge.identity import Principal
from firefly_forge.project_access import AccessDenied


class Session:
    async def execute(self, value):
        assert str(value) == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"

    async def scalar(self, value):
        assert "clock_timestamp" in str(value)
        return datetime(2026, 10, 30, tzinfo=UTC)


class Store:
    schema = "tenant_a"
    units = 0
    inside = False

    @asynccontextmanager
    async def _unit(self):
        assert not self.inside
        self.units += 1
        self.inside = True
        try:
            yield Session()
        finally:
            self.inside = False


class Access:
    admin = False

    async def current_administrator(self, actor):
        return self.admin

    async def memberships(self, actor):
        return [{"project_id": "a" * 32, "role": "viewer", "revision": 1, "permissions": ["read"]}]

    async def require_project(self, actor, project_id, permission):
        assert project_id == "a" * 32 and permission == "read"


class Records:
    def __init__(self):
        self.calls = []
        self.witness = ("current-user", "current-session", "membership-revision", "current-admin")
        self.witness_reads = []

    async def authorization_witness(self, actor, session_hash=None):
        self.witness_reads.append(session_hash)
        return self.witness

    async def projects(self, *args, **kwargs):
        return [{"id": UUID(int=1), "name": "Customer product", "component_count": 2, "repository_count": 2}]

    async def project(self, actor, admin, project_id, **scope):
        return {"id": project_id, "name": "Customer product"} if project_id == "a" * 32 else None

    async def snapshot(self, actor, admin, scope, **page):
        self.calls.append((actor, admin, scope, page))
        return {
            "counts": {"active": 800, "expired": 2, "unknown": 3},
            "stages": [
                {"stage": "implement", "state": "unknown", "count": 500, "recorded_running": 500},
                {"stage": "future_stage", "state": "unknown", "count": 300, "recorded_running": 300},
            ],
            "associations": {"projects": 1200, "standalone_runs": 0, "repositories_with_runs": 4},
            "scope_project_id": "a" * 32,
            "rows": [
                {
                    "id": UUID(int=n),
                    "repository_id": UUID(int=9),
                    "sequence": 100 - n,
                    "status": "developing",
                    "brief": "Implement customer feature",
                    "brief_truncated": False,
                    "project_id": "a" * 32,
                    "project_name": "Customer product",
                    "repository_name": "Backend",
                    "component_id": "api",
                    "component_name": "API",
                    "created_at": "2026-10-30T00:00:00Z",
                    "branch": "forge/customer",
                    "pr_url": None,
                    "issue_url": None,
                    "issue_number": None,
                    "issue_title": None,
                    "private_settings": "must not escape",
                }
                for n in (1, 2)
            ],
        }


@pytest.fixture
def dashboard():
    records = Records()
    return SimpleNamespace(
        service=FactoryDashboard(Store(), Access(), records),
        records=records,
        actor=Principal(UUID(int=8), "https://id.example", "person"),
    )


@pytest.mark.parametrize("method", ["personal_token", "scm_mapping"])
async def test_new_surface_does_not_silently_expand_historical_credentials(dashboard, method):
    actor = Principal(UUID(int=8), "https://id.example", "person", authentication_method=method)
    with pytest.raises(AccessDenied):
        await dashboard.service.observe(actor, DashboardScope(), DashboardPageInput())
    assert dashboard.records.calls == []


async def test_cross_project_and_standalone_are_denied_before_loading_records(dashboard):
    for scope in (DashboardScope(kind="project", project_id="b" * 32), DashboardScope(kind="standalone")):
        with pytest.raises(AccessDenied):
            await dashboard.service.observe(dashboard.actor, scope, DashboardPageInput())
    assert dashboard.records.calls == []


async def test_totals_cover_all_visible_records_not_the_current_page(dashboard):
    value = await dashboard.service.observe(dashboard.actor, DashboardScope(), DashboardPageInput(limit=1))
    assert value["counts"]["total"] == 805
    assert value["counts"]["active"] == 800
    assert len(value["runs"]["items"]) == 1 and value["runs"]["next_cursor"]
    assert value["associations"]["projects"] == 1200
    assert value["stages"][1]["active"] == 0
    assert value["stages"][1]["unknown"] == 500
    assert value["stages"][1]["recorded_running"] == 500
    assert value["unmapped_active_tasks"] == 0
    assert "private_settings" not in str(value)
    assert value["unavailable_metrics"] == [
        "worker_liveness",
        "queue_wait_duration",
        "deployment_health",
        "runtime_metrics",
    ]


async def test_paging_is_bound_to_scope_and_identity_and_does_not_repeat_the_first_page(dashboard):
    first = await dashboard.service.observe(dashboard.actor, DashboardScope(), DashboardPageInput(limit=1))
    cursor = first["runs"]["next_cursor"]
    await dashboard.service.observe(dashboard.actor, DashboardScope(), DashboardPageInput(limit=1, cursor=cursor))
    assert dashboard.records.calls[-1][3]["after"] == 99
    with pytest.raises(ValueError, match="scope or identity"):
        await dashboard.service.observe(
            dashboard.actor, DashboardScope(kind="project", project_id="a" * 32), DashboardPageInput(cursor=cursor)
        )
    other = Principal(UUID(int=77), dashboard.actor.issuer, "other")
    with pytest.raises(ValueError, match="scope or identity"):
        await dashboard.service.observe(other, DashboardScope(), DashboardPageInput(cursor=cursor))
    with pytest.raises(ValueError):
        cursor_decode("not a valid cursor", "a" * 64)


async def test_personal_project_observation_uses_only_its_current_native_grant(dashboard):
    actor = Principal(
        dashboard.actor.id,
        dashboard.actor.issuer,
        dashboard.actor.subject,
        authentication_method="personal_token",
        kind="service",
        personal_token_id=UUID(int=90),
    )
    value = await dashboard.service.observe(
        actor, DashboardScope(kind="project", project_id="a" * 32), DashboardPageInput(limit=1)
    )
    assert value["standalone_available"] is False
    assert dashboard.records.calls[0][3]["allowed_projects"] == ("a" * 32,)
    with pytest.raises(AccessDenied):
        await dashboard.service.observe(
            actor, DashboardScope(kind="project", project_id="c" * 32), DashboardPageInput()
        )
    assert len(dashboard.records.calls) == 1


async def test_personal_credential_revoked_during_the_read_discards_the_projection(dashboard):
    actor = Principal(
        dashboard.actor.id,
        dashboard.actor.issuer,
        dashboard.actor.subject,
        authentication_method="personal_token",
        kind="service",
        personal_token_id=UUID(int=90),
    )
    checks = 0

    async def memberships(_):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise AccessDenied("This personal credential is unavailable")
        return [{"project_id": "a" * 32}]

    dashboard.service.access.memberships = memberships
    with pytest.raises(AccessDenied, match="unavailable"):
        await dashboard.service.observe(actor, DashboardScope(kind="run", run_id=UUID(int=1).hex), DashboardPageInput())
    assert checks == 2 and len(dashboard.records.calls) == 1


async def test_personal_credentials_cannot_enumerate_factory_or_standalone_scope(dashboard):
    actor = Principal(
        dashboard.actor.id,
        dashboard.actor.issuer,
        dashboard.actor.subject,
        authentication_method="personal_token",
        kind="service",
        personal_token_id=UUID(int=90),
    )
    for scope in (DashboardScope(), DashboardScope(kind="standalone")):
        with pytest.raises(AccessDenied):
            await dashboard.service.observe(actor, scope, DashboardPageInput())
    with pytest.raises(AccessDenied):
        await dashboard.service.projects(actor, DashboardPageInput())
    assert dashboard.records.calls == []


async def test_empty_filtered_run_page_does_not_relabel_an_authorized_run_as_missing(dashboard):
    async def snapshot(*args, **kwargs):
        return {
            "counts": {"active": 1},
            "rows": [],
            "stages": [],
            "associations": {"projects": 1},
            "scope_project_id": "a" * 32,
        }

    dashboard.records.snapshot = snapshot
    value = await dashboard.service.observe(
        dashboard.actor, DashboardScope(kind="run", run_id=UUID(int=1).hex), DashboardPageInput(bucket="queued")
    )
    assert value["counts"]["total"] == 1 and value["runs"]["items"] == []


@pytest.mark.parametrize("changed", [0, 1, 2, 3])
async def test_current_reader_authority_change_during_snapshot_discards_all_data(dashboard, changed):
    original = dashboard.records.snapshot

    async def revoke(*args, **kwargs):
        value = await original(*args, **kwargs)
        witness = list(dashboard.records.witness)
        witness[changed] = "revoked"
        dashboard.records.witness = tuple(witness)
        return value

    dashboard.records.snapshot = revoke
    with pytest.raises(AccessDenied, match="changed"):
        await dashboard.service.observe(dashboard.actor, DashboardScope(), DashboardPageInput())
    assert dashboard.service.store.units == 2


async def test_after_read_uses_new_transaction_before_canonical_project_recheck(dashboard):
    observations = []

    async def require_project(actor, project_id, permission):
        observations.append((dashboard.service.store.inside, dashboard.service.store.units, project_id, permission))
        raise AccessDenied("Membership was revoked while reading")

    dashboard.service.access.require_project = require_project
    with pytest.raises(AccessDenied, match="revoked"):
        await dashboard.service.observe(
            dashboard.actor, DashboardScope(kind="run", run_id=UUID(int=1).hex), DashboardPageInput(bucket="queued")
        )
    assert observations == [(False, 2, "a" * 32, "read")]


async def test_cookie_reader_requires_exact_current_native_session_binding(dashboard):
    actor = Principal(
        dashboard.actor.id, dashboard.actor.issuer, dashboard.actor.subject, authentication_method="oidc_session"
    )
    with pytest.raises(AccessDenied, match="session"):
        await dashboard.service.observe(actor, DashboardScope(), DashboardPageInput())
    assert dashboard.records.calls == []
    await dashboard.service.observe(actor, DashboardScope(), DashboardPageInput(), session_hash="d" * 64)
    assert dashboard.records.witness_reads == ["d" * 64] * 3


async def test_missing_fresh_witness_does_not_return_old_snapshot(dashboard):
    original = dashboard.records.snapshot

    async def disappear(*args, **kwargs):
        value = await original(*args, **kwargs)
        dashboard.records.witness = None
        return value

    dashboard.records.snapshot = disappear
    with pytest.raises(AccessDenied):
        await dashboard.service.observe(dashboard.actor, DashboardScope(), DashboardPageInput())


async def test_project_choices_are_discarded_after_current_membership_changes(dashboard):
    original = dashboard.records.projects

    async def revoke(*args, **kwargs):
        value = await original(*args, **kwargs)
        dashboard.records.witness = ("revoked",)
        return value

    dashboard.records.projects = revoke
    with pytest.raises(AccessDenied, match="changed"):
        await dashboard.service.projects(dashboard.actor, DashboardPageInput())
    assert dashboard.service.store.units == 2


def test_controller_binds_cookie_digest_only_from_authenticated_transport():
    from starlette.requests import Request

    from firefly_forge.factory_dashboard_controllers import FactoryDashboardController
    from firefly_forge.identity import digest

    def request(headers):
        return Request({"type": "http", "headers": headers, "query_string": b"session_hash=arbitrary-client-value"})

    assert FactoryDashboardController._session_hash(
        request([(b"cookie", b"forge_session=fictional-session")])
    ) == digest("fictional-session")
    assert FactoryDashboardController._session_hash(request([])) is None
    assert (
        FactoryDashboardController._session_hash(
            request([(b"cookie", b"forge_session=ignored"), (b"authorization", b"Bearer fictional")])
        )
        is None
    )


@pytest.mark.parametrize("changed", [None, ("changed-current-identity",), ("changed-current-membership",)])
async def test_final_scope_permission_await_cannot_return_a_revoked_reader_snapshot(dashboard, changed):
    original = dashboard.service.access.require_project

    async def revoke_after_permission(*args):
        await original(*args)
        dashboard.records.witness = changed

    dashboard.service.access.require_project = revoke_after_permission
    with pytest.raises(AccessDenied, match="changed"):
        await dashboard.service.observe(
            dashboard.actor, DashboardScope(kind="project", project_id="a" * 32), DashboardPageInput()
        )
