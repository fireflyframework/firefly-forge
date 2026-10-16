# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact-read transport and current-context fences; ACL/database substitutes are explicit."""

import copy
import hashlib
from uuid import UUID

import httpx
import pytest
from test_bridge_completion_journal_host import fixture
from test_bridge_completion_records import seal, snapshot
from test_bridge_inflight_cancel import RUN

from firefly_forge import bridge_completions as module
from firefly_forge.bridge_completion_models import ObservationInput
from firefly_forge.bridge_completions import OBSERVATION, BridgeCompletions
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


async def prepared(tmp_path, monkeypatch):
    store, journal = await fixture(tmp_path, monkeypatch)
    actor = Principal(UUID(int=30), "https://fixture.invalid", "host-control")
    other = Principal(UUID(int=31), "https://fixture.invalid", "another-host-control")

    async def permitted(self, *args):
        return None

    async def identity(self, user_id):
        return None  # This host fixture does not assert native identity admission.

    monkeypatch.setattr(BridgeCompletions, "_identity", identity)
    monkeypatch.setattr(ProjectAccess, "require_project", permitted)
    monkeypatch.setattr(ProjectAccess, "require_repository", permitted)
    effect = await journal.intent(RUN, "implement")
    service = BridgeCompletions(store, journal)
    body = b'{"model":"bridge/default","messages":[]}'
    descriptor, raw = snapshot()
    descriptor.update(
        endpoint="http://localhost:19191/v1", wire_bytes=len(body), wire_sha256=hashlib.sha256(body).hexdigest()
    )
    raw["admission"] = seal({**raw["admission"], "wire_bytes": len(body), "wire_sha256": descriptor["wire_sha256"]})
    raw["terminal"] = seal({**raw["terminal"], "admission_digest": raw["admission"]["digest"]})
    retained = await service.scope(await store.get_run(RUN), effect).admit(descriptor, body)
    return store, journal, service, retained, actor, other, raw


async def test_one_authenticated_get_retains_original_result_and_exact_replay_never_queries_again(
    tmp_path, monkeypatch
):
    store, journal, service, intent, actor, other, raw = await prepared(tmp_path, monkeypatch)
    calls = []
    client_type = httpx.AsyncClient

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path.endswith(intent["idempotency_key"])
        return httpx.Response(200, json=raw)

    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs)
    )
    input = ObservationInput(observation_id="c" * 32, expected_intent_digest=intent["digest"])
    context = CURRENT_PRINCIPAL.set(actor)
    before_run = copy.deepcopy(store.run.data)
    try:
        first = await service.observe(RUN, intent["idempotency_key"], input)
        assert first["payload"]["snapshot"] == raw
        assert len(calls) == 1
        assert await service.observation(RUN, intent["idempotency_key"], input.observation_id) == first
        assert await service.observe(RUN, intent["idempotency_key"], input) == first
        assert len(calls) == 1
        CURRENT_PRINCIPAL.set(other)
        assert await service.observation(RUN, intent["idempotency_key"], input.observation_id) == first
        with pytest.raises(ValueError, match="already bound"):
            await service.observe(RUN, intent["idempotency_key"], input)
        assert len(calls) == 1
        assert store.run.data == before_run
        assert len([row for row in journal.records.rows if row.kind == OBSERVATION]) == 1
    finally:
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("loss", ["context", "scope", "permission", "credential"])
async def test_exact_get_rechecks_current_reader_after_credential_await_without_rewriting_history(
    tmp_path, monkeypatch, loss
):
    store, journal, service, intent, actor, other, raw = await prepared(tmp_path, monkeypatch)
    before = copy.deepcopy([row.data for row in journal.records.rows])
    denied = False

    async def require(self, *args):
        if denied:
            raise AccessDenied("Current permission revoked")

    monkeypatch.setattr(ProjectAccess, "require_project", require)

    async def admission(value):
        nonlocal denied
        if loss == "context":
            CURRENT_PRINCIPAL.set(other)
        elif loss == "scope":
            store.run.data["project_id"] = "e" * 32
        elif loss == "permission":
            denied = True
        else:

            def reject(value):
                raise ValueError("Fresh configured credential would be reflected")

            store.reject_credentials = reject

    store.admit_credentials = admission
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises((AccessDenied, ValueError)):
            await service.exact(RUN, intent["idempotency_key"])
        assert [row.data for row in journal.records.rows] == before
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_404_cannot_create_a_local_observation_or_dispatch_inference(tmp_path, monkeypatch):
    store, journal, service, intent, actor, _, raw = await prepared(tmp_path, monkeypatch)
    before = copy.deepcopy([row.data for row in journal.records.rows])
    calls = []
    client_type = httpx.AsyncClient

    def respond(request):
        calls.append(request.method)
        return httpx.Response(404)

    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs)
    )
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises(ValueError, match="do not resubmit"):
            await service.observe(
                RUN,
                intent["idempotency_key"],
                ObservationInput(observation_id="c" * 32, expected_intent_digest=intent["digest"]),
            )
        assert calls == ["GET"]
        assert [row.data for row in journal.records.rows] == before
        assert store.run.data["bridge_completion_hold"]["request_id"] == intent["idempotency_key"]
    finally:
        CURRENT_PRINCIPAL.reset(context)
