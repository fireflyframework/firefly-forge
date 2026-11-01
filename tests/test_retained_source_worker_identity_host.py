# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Background authority uses canonical ACLs; native custody/source scanning are separate controls."""

import asyncio
import copy
from contextlib import asynccontextmanager
from contextvars import Context
from types import SimpleNamespace
from uuid import UUID

import pytest

from firefly_forge import retained_verified_source as retained
from firefly_forge.access_persistence import ForgeMembershipRepository
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


@pytest.fixture
def worker_cut(tmp_path, monkeypatch):
    principal = Principal(UUID("a" * 32), "https://identity.invalid", "canonical-worker")
    user = SimpleNamespace(id=principal.id, issuer=principal.issuer, subject=principal.subject, enabled=True)
    state = SimpleNamespace(role="developer", ownership=object(), after_admit=None, source_preparations=0, reads=[])
    run = dict(
        id="b" * 32,
        project_id="c" * 32,
        repository_id="d" * 32,
        execution_actor=ProjectAccess.actor_snapshot(principal),
        agent="agentic",
        publish=False,
        status="developing",
        attempt=3,
        active_task="implement",
        invalid_read_continuity={"request_id": "e" * 32},
        verification_handoff={"status": "completed", "failed_commands": 0},
    )
    ticket = {"id": "e" * 32, "status": "completed"}
    intent = dict(
        parent_attempt=2,
        parent_round=2,
        workspace=str(tmp_path),
        **{key: "1" * 64 for key in ("source_digest", "commands_digest", "sandbox_digest", "scope_digest")},
    )
    entries = [
        dict(
            kind="terminal_completion_receipt",
            sequence=2,
            digest="2" * 64,
            payload={"proof": {"terminal_report_sha256": "4" * 64}},
        ),
        dict(kind="verification_handoff_intent", sequence=3, digest="3" * 64, payload=intent),
    ]

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(scalars=projects)

    async def projects(_query):
        return [UUID(run["project_id"])]

    async def identity(_self, user_id, **_kwargs):
        state.reads.append(user_id)
        return user if user_id == principal.id else None

    async def memberships(_self, user_id):
        assert user_id == principal.id
        return [SimpleNamespace(project_id=UUID(run["project_id"]), role=state.role, revision=1)]

    async def owned():
        if state.ownership is None:
            raise RuntimeError("Controlled worker ownership revoked")

    async def get_run(run_id):
        assert run_id == run["id"]
        return copy.deepcopy(run)

    async def get_ticket(request_id):
        assert request_id == ticket["id"]
        return copy.deepcopy(ticket)

    async def native(_run_id):
        return copy.deepcopy(run), None, copy.deepcopy(entries)

    async def rows(_run_id):
        return []

    async def task(_run_id, task_id):
        assert task_id == "implement"
        return SimpleNamespace(attempt=3, status="running", data={"idempotency_key": "f" * 32})

    async def journal_entries(_run_id):
        return copy.deepcopy(entries)

    async def admitted(_value):
        if state.after_admit:
            state.after_admit()

    store = SimpleNamespace(
        path=tmp_path / "data",
        execution_ownership_token=state.ownership,
        assert_ownership=owned,
        _unit=unit,
        get_run=get_run,
        admit_credentials=admitted,
    )
    store.execution_requests = SimpleNamespace(
        store=store,
        get=get_ticket,
        invalid_read_continuities=SimpleNamespace(recoveries=SimpleNamespace(_native=native)),
    )
    journal = SimpleNamespace(
        sandboxes=SimpleNamespace(for_run=rows), _task=task, entries=journal_entries, source_catalog_budget=None
    )

    async def allowed(*_args, **_kwargs):
        return None

    async def events(*_args, **_kwargs):
        return []

    async def prepare(_workspace, _catalog, *, budget, assert_current):
        state.source_preparations += 1
        await assert_current()

        async def revalidate(*, assert_current):
            await assert_current()

        return SimpleNamespace(source_digest="1" * 64, revalidate=revalidate, fence=lambda: None)

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    monkeypatch.setattr(ForgeMembershipRepository, "for_user", memberships)
    monkeypatch.setattr(retained, "assert_generic_dispatch_allowed", allowed)
    monkeypatch.setattr(retained, "read_recovery_events", events)
    monkeypatch.setattr(
        retained,
        "final_record",
        lambda *_: dict(
            sequence=1,
            digest="5" * 64,
            payload={
                "candidate": dict(outcome="blocked", followup_kind="verification", attempt=2, report_digest="4" * 64)
            },
        ),
    )
    monkeypatch.setattr(
        retained,
        "validate_completed_handoff",
        lambda *_: dict(
            digest="6" * 64,
            handoff_intent_digest="3" * 64,
            outcome_digest="7" * 64,
            continuation_intent_digest="8" * 64,
        ),
    )
    monkeypatch.setattr(retained, "validate_native_receipt", lambda *_: None)
    monkeypatch.setattr(retained, "check_verification_authority", lambda *_: None)
    monkeypatch.setattr(retained, "prepare_verification_source", prepare)
    return SimpleNamespace(store=store, journal=journal, run=run, user=user, state=state, workspace=tmp_path)


async def background(c):
    async def execute():
        assert CURRENT_PRINCIPAL.get() is None
        return await retained.prepare_retained_source(c.store, c.journal, copy.deepcopy(c.run), c.workspace)

    return await asyncio.create_task(execute(), context=Context())


async def test_background_worker_without_http_context_uses_current_canonical_actor(worker_cut):
    c = worker_cut
    prepared = await background(c)
    assert prepared.proof.run_id == c.run["id"] and prepared.proof.attempt == 3
    assert c.state.reads and set(c.state.reads) == {c.user.id}
    assert c.state.source_preparations == 1
    assert CURRENT_PRINCIPAL.get() is None


@pytest.mark.parametrize("change", ["disabled", "missing", "permission"])
async def test_background_actor_revoked_before_source_preparation_is_refused(worker_cut, change):
    c = worker_cut
    if change == "disabled":
        c.user.enabled = False
    elif change == "missing":
        c.run["execution_actor"]["user_id"] = "0" * 32
    else:
        c.state.role = "viewer"
    with pytest.raises(AccessDenied, match="identity|project role"):
        await background(c)
    assert c.state.source_preparations == 0


@pytest.mark.parametrize("change", ["disabled", "permission", "actor", "reader", "owner", "token"])
async def test_background_actor_and_owned_scope_rechecked_after_credential_admission(worker_cut, change):
    c = worker_cut

    def alter():
        if change == "disabled":
            c.user.enabled = False
        elif change == "permission":
            c.state.role = "viewer"
        elif change == "actor":
            c.run["execution_actor"]["user_id"] = "0" * 32
        elif change == "reader":
            CURRENT_PRINCIPAL.set(Principal(UUID("0" * 32), "https://identity.invalid", "foreign-reader"))
        elif change == "owner":
            c.state.ownership = None
        else:
            c.store.execution_ownership_token = object()

    c.state.after_admit = alter
    with pytest.raises((AccessDenied, ValueError, RuntimeError), match="identity|project role|context|ownership|scope"):
        await background(c)
    assert c.state.source_preparations == 1


async def test_an_explicit_unavailable_reader_cannot_borrow_the_worker_execution_grant(worker_cut):
    c = worker_cut
    reader = CURRENT_PRINCIPAL.set(Principal(UUID("0" * 32), "https://identity.invalid", "unavailable-reader"))
    try:
        with pytest.raises(AccessDenied, match="identity.*unavailable"):
            await retained.prepare_retained_source(c.store, c.journal, copy.deepcopy(c.run), c.workspace)
    finally:
        CURRENT_PRINCIPAL.reset(reader)
