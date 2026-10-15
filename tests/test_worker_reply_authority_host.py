# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host behavioral controls model storage boundaries; native identity/locks are separate."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from firefly_forge.agent_roles import WorkerResponse
from firefly_forge.identity import Principal
from firefly_forge.journal import ZERO_HASH, Journal, digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


class Ledger:
    def __init__(self):
        self.schema = "unit_worker_reply"
        self.events = []
        self.depth = 0
        self.callback = None
        self.append_callback = None
        self.credentials = set()
        self.permissions = {"read", "execute", "release"}
        self.enabled = True
        self.admissions = 0
        self.run = SimpleNamespace(id=uuid4(), status="paused", data={})
        self.run.data = {
            "id": self.run.id.hex,
            "project_id": uuid4().hex,
            "repository_id": uuid4().hex,
            "status": "paused",
            "publish": False,
            "worker_request": {"id": "question-1", "status": "pending", "task_id": "implement"},
        }
        self.run.repository_id = UUID(self.run.data["repository_id"])
        self.run_records = self
        self.configuration = SimpleNamespace()

    @asynccontextmanager
    async def _unit(self, **kwargs):
        outer = self.depth == 0
        before = copy.deepcopy((self.events, self.run.data))
        self.depth += 1
        try:
            yield self
        except BaseException:
            if outer:
                self.events, self.run.data = before
            raise
        finally:
            self.depth -= 1

    async def execute(self, *args, **kwargs):
        return None

    async def get_run(self, run_id):
        assert run_id == self.run.id.hex
        return copy.deepcopy(self.run.data)

    async def save(self, row):
        self.run = row

    async def admit_credentials(self, value):
        assert self.depth == 0, "Remote credential admission cannot hold the writer unit"
        self.admissions += 1
        if self.callback:
            await self.callback(value)
        self.reject_credentials(value)

    def reject_credentials(self, value):
        if any(secret in str(value) for secret in self.credentials):
            raise ValueError("Configured credential refused")

    async def find_for_run(self, run_id):
        assert run_id == self.run.id
        return [
            SimpleNamespace(
                kind=e["kind"], idempotency_key=e["idempotency_key"], digest=e["digest"], data=copy.deepcopy(e)
            )
            for e in self.events
        ]


class HostJournal(Journal):
    def __init__(self, store):
        super().__init__(store)
        self.records = store

    async def _run(self, run_id):
        assert run_id == self.store.run.id.hex
        return self.store.run

    async def _append(self, run_id, task_id, key, kind, payload):
        event = {
            "run_id": run_id,
            "sequence": len(self.store.events) + 1,
            "task_id": task_id,
            "idempotency_key": key,
            "kind": kind,
            "payload": copy.deepcopy(payload),
            "created_at": "2026-10-15T00:00:00+00:00",
            "previous_hash": ZERO_HASH,
        }
        event["digest"] = digest(event)
        self.store.events.append(event)
        if self.store.append_callback:
            await self.store.append_callback()
        return copy.deepcopy(event)


@pytest.fixture
def setup(monkeypatch):
    store = Ledger()
    journal = HostJournal(store)
    # These unit contexts do not claim to authenticate a real user or prove PostgreSQL locks.
    actor = Principal(uuid4(), "https://fixture.invalid", "unit-context")

    async def require_project(self, user, project_id, permission):
        if not store.enabled or permission not in store.permissions:
            raise AccessDenied("Current unit permission refused")

    async def require_repository(self, user, repository_id, permission):
        if not store.enabled or permission not in store.permissions:
            raise AccessDenied("Current unit repository permission refused")

    async def administrator(self, user):
        return False

    async def identity(self, user_id, **kwargs):
        return SimpleNamespace(
            id=user_id, enabled=store.enabled, issuer="https://fixture.invalid", subject="unit-context"
        )

    from firefly_forge.identity_persistence import ForgeIdentityUserRepository

    monkeypatch.setattr(ProjectAccess, "require_project", require_project)
    monkeypatch.setattr(ProjectAccess, "require_repository", require_repository)
    monkeypatch.setattr(ProjectAccess, "current_administrator", administrator)
    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    return store, journal, actor


def response():
    return WorkerResponse(id="answer-1", actor="caller supplied label", answer="Preserve the source")


async def test_new_reply_without_authenticated_principal_has_no_write(setup):
    store, journal, _ = setup
    token = CURRENT_PRINCIPAL.set(None)
    try:
        with pytest.raises(AccessDenied):
            await journal.worker_response(store.run.id.hex, "question-1", response())
        assert store.events == [] and store.run.data["worker_request"]["status"] == "pending"
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_new_reply_records_server_actor_independently_of_caller_label(setup):
    store, journal, actor = setup
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        event = await journal.worker_response(store.run.id.hex, "question-1", response())
        assert event["payload"]["actor"] == "caller supplied label"
        assert event["payload"]["responding_actor"] == {
            "version": 1,
            "user_id": actor.id.hex,
            "authentication_method": actor.authentication_method,
        }
        assert store.run.data["status"] == "paused" and store.run.data["worker_request"]["status"] == "answered"
        assert len(store.events) == 1
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("lost", ["execute", "release", "disabled", "context", "cancel"])
async def test_late_admission_loss_never_creates_a_response(setup, lost):
    store, journal, actor = setup
    store.run.data["publish"] = True
    token = CURRENT_PRINCIPAL.set(actor)

    async def revoke(value):
        if lost in {"execute", "release"}:
            store.permissions.remove(lost)
        elif lost == "disabled":
            store.enabled = False
        elif lost == "context":
            CURRENT_PRINCIPAL.set(None)
        else:
            store.run.data["cancel_request"] = {"status": "requested"}

    store.callback = revoke
    try:
        with pytest.raises((AccessDenied, ValueError)):
            await journal.worker_response(store.run.id.hex, "question-1", response())
        assert store.events == [] and store.run.data["worker_request"]["status"] == "pending"
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_late_registered_credential_after_append_rolls_back(setup):
    store, journal, actor = setup
    token = CURRENT_PRINCIPAL.set(actor)

    async def register():
        store.credentials.add("Preserve the source")

    store.append_callback = register
    try:
        with pytest.raises(ValueError):
            await journal.worker_response(store.run.id.hex, "question-1", response())
        assert store.events == [] and store.run.data["worker_request"]["status"] == "pending"
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_new_reply_other_actor_cannot_reuse_original_response_identity(setup):
    store, journal, actor = setup
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        await journal.worker_response(store.run.id.hex, "question-1", response())
        CURRENT_PRINCIPAL.set(Principal(uuid4(), actor.issuer, "other-unit-context"))
        with pytest.raises(AccessDenied):
            await journal.worker_response(store.run.id.hex, "question-1", response())
        assert len(store.events) == 1
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_permission_to_return_loss_preserves_committed_response(setup):
    store, journal, actor = setup
    token = CURRENT_PRINCIPAL.set(actor)

    async def late(value):
        if store.events:
            store.permissions.remove("execute")

    store.callback = late
    try:
        with pytest.raises(AccessDenied):
            await journal.worker_response(store.run.id.hex, "question-1", response())
        assert len(store.events) == 1 and store.run.data["worker_request"]["status"] == "answered"
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_authorized_legacy_replay_returns_original_bytes_without_actor_backfill(setup):
    store, journal, actor = setup
    original = await journal._append(
        store.run.id.hex,
        "implement",
        "worker-response:answer-1",
        "worker_response",
        {**response().model_dump(), "request_id": "question-1"},
    )
    store.run.data["worker_request"]["status"] = "answered"
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        assert await journal.worker_response(store.run.id.hex, "question-1", response()) == original
        assert store.events == [original] and "responding_actor" not in original["payload"]
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_message_reader_context_loss_during_credential_admission_refuses(setup):
    store, journal, actor = setup
    await journal._append(
        store.run.id.hex,
        "implement",
        "worker-response:answer-1",
        "worker_response",
        {**response().model_dump(), "request_id": "question-1"},
    )
    token = CURRENT_PRINCIPAL.set(actor)

    async def lose(value):
        CURRENT_PRINCIPAL.set(None)

    store.callback = lose
    try:
        with pytest.raises(AccessDenied):
            await journal.worker_messages(store.run.id.hex)
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("identity_state", ["missing", "issuer", "subject"])
async def test_new_provenance_needs_matching_persisted_identity(setup, monkeypatch, identity_state):
    from dataclasses import replace

    from firefly_forge.identity_persistence import ForgeIdentityUserRepository

    store, journal, actor = setup

    async def identity(self, user_id, **kwargs):
        if identity_state == "missing":
            return None
        return SimpleNamespace(
            id=user_id,
            enabled=True,
            issuer="other" if identity_state == "issuer" else actor.issuer,
            subject="other" if identity_state == "subject" else actor.subject,
        )

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    context = CURRENT_PRINCIPAL.set(replace(actor, authentication_method="local_bootstrap"))
    try:
        with pytest.raises(AccessDenied):
            await journal.worker_response(store.run.id.hex, "question-1", response())
        assert store.events == []
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_reader_context_changed_inside_acl_await_cannot_return_snapshot(setup, monkeypatch):
    store, journal, actor = setup
    await journal._append(
        store.run.id.hex,
        "implement",
        "worker-response:answer-1",
        "worker_response",
        {**response().model_dump(), "request_id": "question-1"},
    )
    context = CURRENT_PRINCIPAL.set(actor)

    async def switch(self, user, project_id, permission):
        CURRENT_PRINCIPAL.set(None)

    monkeypatch.setattr(ProjectAccess, "require_project", switch)
    try:
        with pytest.raises(AccessDenied):
            await journal.entries(store.run.id.hex)
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_scope_drift_during_admission_refuses_before_write(setup):
    store, journal, actor = setup
    context = CURRENT_PRINCIPAL.set(actor)

    async def drift(value):
        store.run.data["repository_id"] = uuid4().hex

    store.callback = drift
    try:
        with pytest.raises(AccessDenied, match="scope changed"):
            await journal.worker_response(store.run.id.hex, "question-1", response())
        assert store.events == []
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_authentication_method_change_cannot_reuse_response_identity(setup):
    from dataclasses import replace

    store, journal, actor = setup
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        original = await journal.worker_response(store.run.id.hex, "question-1", response())
        CURRENT_PRINCIPAL.set(replace(actor, authentication_method="oidc_session"))
        with pytest.raises(AccessDenied, match="another authenticated actor"):
            await journal.worker_response(store.run.id.hex, "question-1", response())
        assert store.events == [original]
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_cancellation_during_admission_has_no_write(setup):
    import asyncio

    store, journal, actor = setup
    context = CURRENT_PRINCIPAL.set(actor)

    async def cancel(value):
        raise asyncio.CancelledError

    store.callback = cancel
    try:
        with pytest.raises(asyncio.CancelledError):
            await journal.worker_response(store.run.id.hex, "question-1", response())
        assert store.events == []
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_ambient_caller_transaction_refuses_before_credential_io(setup, monkeypatch):
    import pyfly.data.transaction

    store, journal, actor = setup
    context = CURRENT_PRINCIPAL.set(actor)
    monkeypatch.setattr(pyfly.data.transaction, "current_unit_of_work", lambda: object())
    try:
        with pytest.raises(ValueError, match="caller transaction"):
            await journal.worker_response(store.run.id.hex, "question-1", response())
        assert store.events == [] and store.admissions == 0
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_exact_replay_preserves_event_and_changed_body_refuses(setup):
    store, journal, actor = setup
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        first = await journal.worker_response(store.run.id.hex, "question-1", response())
        assert await journal.worker_response(store.run.id.hex, "question-1", response()) == first
        with pytest.raises(ValueError, match="different content"):
            await journal.worker_response(
                store.run.id.hex, "question-1", response().model_copy(update={"actor": "different label"})
            )
        assert store.events == [first]
    finally:
        CURRENT_PRINCIPAL.reset(context)
