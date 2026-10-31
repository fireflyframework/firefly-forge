# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Factory observations describe scoped records, never execution authority or liveness."""

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from firefly_forge.factory_dashboard_models import DashboardScope, run_bucket
from firefly_forge.factory_dashboard_persistence import visible_runs_statement


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("queued", "queued"),
        ("developing", "active"),
        ("verifying", "active"),
        ("needs_reconciliation", "needs_action"),
        ("failed", "failed"),
        ("succeeded", "completed"),
        ("cancelled", "cancelled"),
        ("expired", "expired"),
        ("quarantined", "quarantined"),
        ("future_worker_state", "unknown"),
        ("", "unknown"),
    ],
)
def test_recorded_statuses_keep_terminal_and_unknown_states_distinct(status, expected):
    assert run_bucket(status) == expected


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "project"},
        {"kind": "run"},
        {"kind": "factory", "project_id": "a" * 32},
        {"kind": "project", "project_id": "a" * 32, "run_id": "b" * 32},
        {"kind": "standalone", "project_id": "a" * 32},
        {"kind": "factory", "tenant": "other"},
    ],
)
def test_scope_never_infers_a_project_or_accepts_an_external_tenant(value):
    with pytest.raises(ValidationError):
        DashboardScope.model_validate(value)


def test_project_visibility_uses_recorded_product_identity_not_shared_repository():
    statement = visible_runs_statement(DashboardScope(kind="project", project_id="a" * 32), administrator=False)
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "project_id" in sql
    assert "forge_project_memberships" in sql
    assert "forge_identity_users" in sql
    assert "FOR UPDATE" not in sql and "FOR SHARE" not in sql
    assert "LIMIT" not in sql


def test_standalone_scope_cannot_relabel_an_orphaned_product_reference():
    statement = visible_runs_statement(DashboardScope(kind="standalone"), administrator=True)
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "project_id" in sql and "IS NULL" in sql
    assert "forge_runs.repository_id" not in sql.split("WHERE", 1)[1]


def test_authority_witness_is_constant_space_and_binds_current_identity_session_and_all_memberships():
    from uuid import UUID

    from firefly_forge.factory_dashboard_persistence import authorization_witness_statement
    from firefly_forge.identity import Principal

    actor = Principal(
        UUID(int=9), "https://id.example", "person", authentication_method="oidc_session", api_audience="forge-api"
    )
    sql = str(authorization_witness_statement(actor, "d" * 64).compile(dialect=postgresql.dialect()))
    assert "sha256" in sql and sql.count("sum(") == 5
    assert "forge_project_memberships.role" in sql and "forge_project_memberships.revision" in sql
    assert "forge_project_memberships.active" in sql and "forge_project_memberships.project_id" in sql
    assert "forge_identity_sessions.cookie_hash" in sql and "forge_identity_sessions.user_id" in sql
    assert "forge_identity_sessions.expires_at > clock_timestamp()" in sql
    assert (
        "forge_identity_users.api_roles_audience" in sql
        and "forge_identity_users.api_roles_expires_at > clock_timestamp()" in sql
    )
    for forbidden in ("string_agg", "array_agg", "jsonb_agg", "FOR UPDATE", "FOR SHARE", "LIMIT"):
        assert forbidden not in sql
