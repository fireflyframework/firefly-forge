# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual native API/worker ledger with controlled runner, persistence and ticket-read seams."""

import copy
import importlib
import importlib.util
from types import SimpleNamespace
from uuid import UUID

import pytest

from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

pytest_plugins = ["test_linked_verification_consumer_host"]


@pytest.fixture(autouse=True)
def component_before_historical_snapshot(monkeypatch):
    import test_original_selection_readmission_host as fixture

    original = fixture.historical_case

    def component_case():
        value = original()
        value["run"]["component_id"] = "inventory-api"
        return value

    monkeypatch.setattr(fixture, "historical_case", component_case)


def implementation():
    name = "firefly_forge.scm_native_verification_status"
    assert importlib.util.find_spec(name), "Authenticated native verification read service is missing"
    return importlib.import_module(name)


@pytest.fixture
async def reader_case(linked_case, monkeypatch):
    from test_linked_verification_consumer_host import admit_and_execute

    from firefly_forge import engine as engine_module
    from firefly_forge.models import Command

    c = linked_case
    monkeypatch.setattr(
        engine_module,
        "policy_commands",
        lambda *args: [Command(argv=["controlled-policy", str(n)], timeout=10) for n in range(6)],
    )
    backend = c.env.engine.sandbox.backends.select(None)

    class Backend:
        async def run(self, *args):
            result = await backend.run(*args)
            if len(c.calls) == 4:
                c.inputs_state["value"] = "changed-after-four-known-results"
            return result

    monkeypatch.setattr(c.env.engine.sandbox.backends, "select", lambda config: Backend())
    native = await admit_and_execute(c)
    assert native["status"] == "needs_reconciliation" and native["candidate"]["effect_budget"] == 7
    assert native["progress"]["completed"] == 4 and native["progress"]["unknown"] is False
    assert len(c.calls) == 4
    run = await c.env.store.get_run(c.env.run_id)
    delivery = dict(
        id="1" * 32,
        run_id=run["id"],
        repository_id=run["repository_id"],
        provider="github",
        actor="7",
        issue={"kind": "issue", "number": 7},
    )
    scope = dict(
        version=1,
        action="status",
        transport="github_rest_poll_v1",
        project_id=run["project_id"],
        repository_id=run["repository_id"],
        native_user_id=c.env.user.id.hex,
        component_ids=[run["component_id"]],
        binding_key="2" * 64,
        binding_digest="3" * 64,
        binding_request_id="4" * 32,
        verified_receipt_id=delivery["id"],
        verified_reference_digest="5" * 64,
        project_binding_digest="6" * 64,
        observation_digest="7" * 64,
    )
    reference = dict(
        receipt_id="8" * 32,
        sequence=1,
        run_id=run["id"],
        project_id=run["project_id"],
        component_id=run["component_id"],
        context_digest="9" * 64,
    )
    state = {"read": True, "pages": []}

    async def scopes(current, *, remote):
        if not state["read"] or current != delivery:
            raise AccessDenied("Controlled current ticket read permission changed")
        return [copy.deepcopy(scope)]

    async def resolve(current, source, current_scopes):
        if current != reference or source != delivery or current_scopes != [scope]:
            raise AccessDenied("Controlled authenticated ticket/run association changed")
        return {"run_id": run["id"]}

    async def references(source, current_scopes, *, durable):
        assert source == delivery and current_scopes == [scope] and durable
        return [reference], {run["id"]: run}

    async def page(run_id, kind, *, after=None, limit=64):
        assert run_id == UUID(run["id"]) and kind == "linked_verify"
        state["pages"].append(after)
        return sorted(
            [
                row
                for row in c.env.requests.records.rows
                if row.run_id == run_id and row.kind == kind and (after is None or row.id > after)
            ],
            key=lambda row: row.id,
        )[:limit]

    async def count(run_id, kind):
        return sum(row.run_id == run_id and row.kind == kind for row in c.env.requests.records.rows)

    c.env.requests.records.for_run_page = page
    c.env.requests.records.count_for_run = count
    collaboration = SimpleNamespace(store=c.env.store)
    service = implementation().SCMNativeVerificationStatus(collaboration, c.api)
    service.conversations = SimpleNamespace(scopes=scopes, resolve_reference=resolve, references=references)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Status cannot borrow native execution, administrator, proposal or runner authority")

    monkeypatch.setattr(c.api, "get", forbidden)
    monkeypatch.setattr(c.api, "_actor", forbidden)
    monkeypatch.setattr(c.api, "_authority", forbidden)
    monkeypatch.setattr(c.env.engine.sandbox.backends, "select", forbidden)
    return SimpleNamespace(
        c=c, service=service, delivery=delivery, scope=scope, reference=reference, state=state, native=native
    )


async def test_api_origin_native_operation_reports_four_known_fresh_results_under_ticket_read_scope(reader_case):
    r = reader_case
    original = copy.deepcopy(await r.c.env.store.get_run(r.c.env.run_id))
    posts = len(r.c.env.posts)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        view = await r.service.read(r.delivery, r.scope, r.reference)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert view.current_request_id == r.native["request_id"]
    current = next(op for op in view.operations if op.receipt.request_id == view.current_request_id)
    assert current.known_passed == 4 and current.known_failed == 0
    assert current.receipt.candidate.effect_budget == 7 and current.receipt.progress.unknown is False
    assert view.held_predecessor.request_id == r.native["request_id"]
    assert view.execution_grant is False
    assert await r.c.env.store.get_run(r.c.env.run_id) == original
    assert len(r.c.env.posts) == posts and len(r.c.calls) == 4


async def test_current_read_revocation_and_foreign_ticket_refs_refuse_before_disclosure(reader_case):
    r = reader_case
    r.state["read"] = False
    with pytest.raises(AccessDenied):
        await r.service.read(r.delivery, r.scope, r.reference)
    assert r.state["pages"] == []
    r.state["read"] = True
    wrong = {**r.reference, "run_id": "a" * 32}
    with pytest.raises(AccessDenied):
        await r.service.read(r.delivery, r.scope, wrong)
    assert r.state["pages"] == []


async def test_native_receipt_read_and_publication_freshness_refuse_revoked_or_mutated_views(reader_case, monkeypatch):
    r = reader_case
    view = await r.service.for_delivery(r.delivery)
    original_receipt = r.c.api._receipt
    calls = 0

    async def revoked_after_receipt(*args):
        nonlocal calls
        value = await original_receipt(*args)
        calls += 1
        if calls == 2:
            r.state["read"] = False
        return value

    monkeypatch.setattr(r.c.api, "_receipt", revoked_after_receipt)
    with pytest.raises(AccessDenied):
        await r.service.for_delivery(r.delivery)
    assert calls == 2
    r.state["read"] = True
    monkeypatch.setattr(r.c.api, "_receipt", original_receipt)
    await r.service.assert_current(view, r.delivery, r.scope, r.reference)
    view.operations[0].receipt.candidate.__dict__["source_tree"] = "f" * 40
    with pytest.raises(ValueError):
        await r.service.assert_current(view, r.delivery, r.scope, r.reference)
    assert len(r.c.calls) == 4
