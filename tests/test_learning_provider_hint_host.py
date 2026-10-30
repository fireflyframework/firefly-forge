# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Provider hints use the already authorized origin snapshot without current fallback."""

from uuid import UUID

import pytest
from test_koinon_learning_host import learning_case as learning_case
from test_koinon_sql_host import sql_manager as sql_manager

from firefly_forge.project_access import AccessDenied, ProjectAccess


@pytest.fixture
async def provider_case(learning_case, monkeypatch):
    c = learning_case

    async def repository(_self, actor, identifier, permission):
        await c.access.require_repository(actor, identifier, permission)

    async def visible(_self, rows):
        if c.state.get("revoke_after_visibility"):
            c.access.allowed = False
        return rows

    monkeypatch.setattr(ProjectAccess, "require_repository", repository)
    monkeypatch.setattr(ProjectAccess, "filter_runs", visible)
    return c


async def test_pending_native_provider_hint_comes_from_its_exact_recorded_origin(provider_case):
    c = provider_case
    rows = await c.ledger.records(c.run["repository_id"])
    assert rows[0]["knowledge_provider"] == "koinon"
    assert c.state["projection"].state == "pending" and c.state["source_posts"] == 0
    assert "access_token" not in str(rows) and "private-machine-token" not in str(rows)


@pytest.mark.parametrize("snapshot", [None, {}, {"revision": "a" * 32}, {"provider": "flycanon"}])
async def test_missing_or_retained_origin_never_borrows_current_native_configuration(provider_case, snapshot):
    c = provider_case
    c.run["knowledge_snapshot"] = snapshot
    assert c.knowledge.settings()["provider"] == "koinon"
    assert (await c.ledger.records(c.run["repository_id"]))[0]["knowledge_provider"] is None


async def test_foreign_origin_hint_is_refused_even_when_the_origin_would_be_visible(provider_case):
    c = provider_case
    c.record.run_id = UUID("9" * 32)
    with pytest.raises(ValueError, match="origin|scope"):
        await c.ledger.records(c.run["repository_id"])


async def test_current_grant_loss_after_visibility_withholds_provider_and_evidence(provider_case):
    c = provider_case
    c.state["revoke_after_visibility"] = True
    with pytest.raises(AccessDenied):
        await c.ledger.records(c.run["repository_id"])


async def test_missing_origin_cannot_be_replaced_by_current_configuration(provider_case):
    c = provider_case

    async def missing(_identifier):
        raise KeyError("Origin unavailable")

    c.store.get_run = missing
    with pytest.raises(KeyError, match="Origin"):
        await c.ledger.records(c.run["repository_id"])
    assert c.state.get("http_requests", []) == []


async def test_negative_native_hint_preserves_candidate_state_and_missing_projection(provider_case):
    c = provider_case
    c.record.kind, c.record.state = "observed_failure", "candidate"
    c.state["projection"] = None
    rows = await c.ledger.records(c.run["repository_id"])
    assert rows[0]["knowledge_provider"] == "koinon" and rows[0]["state"] == "candidate"
    assert c.state["projection"] is None and c.state["source_posts"] == 0
