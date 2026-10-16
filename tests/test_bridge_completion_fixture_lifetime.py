# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native fixture lifecycle only, using explicit host substitutes and the real scope guard."""

import asyncio
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
import test_bridge_completion_native as native

from firefly_forge.bridge_completions import BridgeCompletions
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess


@asynccontextmanager
async def use_prepared(store, path):
    # Exercise the original helper as well as its context-managed successor for causal RED/GREEN.
    resource = native.prepared(store, path)
    if inspect.isawaitable(resource):
        yield await resource
    else:
        async with resource as value:
            yield value


def host_dependencies(tmp_path, monkeypatch, *, setup_failure=False):
    actor = Principal(UUID(int=80), "https://host-fixture.invalid", "returned-not-installed")
    events = []

    async def close():
        events.append(("close", CURRENT_PRINCIPAL.get()))

    class Identity:
        client = SimpleNamespace(aclose=close)

        async def authenticate(self, request):
            events.append(("authenticate", CURRENT_PRINCIPAL.get()))
            return actor  # Actual IdentityService.authenticate also returns without installing a context.

    async def oidc(store):
        return Identity(), None, None, None, lambda *args, **kwargs: "fictional-host-token"

    class Projects:
        def __init__(self, store):
            pass

        async def configure(self, definition):
            return {"id": UUID(int=81).hex}

    class Journal:
        def __init__(self, store):
            pass

        async def ensure_plan(self, run_id):
            pass

        async def intent(self, run_id, task_id):
            return {"idempotency_key": UUID(int=82).hex, "disposition": "execute"}

    class Store:
        configuration = ConfigurationStore(tmp_path / "private")

        async def add_repository(self, request):
            return {"id": UUID(int=83).hex}

        async def create_run(self, request):
            if setup_failure:
                raise RuntimeError("Owned fixture setup refusal")
            self.run = {
                **request.model_dump(mode="json"),
                "id": UUID(int=84).hex,
                "execution_actor": ProjectAccess.actor_snapshot(actor),
                "status": "developing",
                **self.configuration.snapshot("bridge"),
            }
            return dict(self.run)

        async def get_run(self, run_id):
            return dict(self.run)

        async def acquire_ownership(self):
            events.append(("ownership", CURRENT_PRINCIPAL.get()))

        async def assert_ownership(self):
            pass

        async def admit_credentials(self, value):
            await asyncio.sleep(0)

        def reject_credentials(self, value):
            pass

    async def administrator(self, current):
        return current == actor

    async def execution(self, run, permission="execute"):
        assert run["execution_actor"]["user_id"] == actor.id.hex

    async def authorize(self, *args, **kwargs):
        return True

    async def identity(self, user_id):
        assert user_id == actor.id.hex
        return SimpleNamespace(id=actor.id, issuer=actor.issuer, subject=actor.subject, enabled=True)

    monkeypatch.setattr(native, "oidc_fixture", oidc)
    monkeypatch.setattr(native, "Projects", Projects)
    monkeypatch.setattr(native, "Journal", Journal)
    monkeypatch.setattr(ProjectAccess, "current_administrator", administrator)
    monkeypatch.setattr(ProjectAccess, "authorize_execution", execution)
    monkeypatch.setattr(ProjectAccess, "authorize_actor", authorize)
    monkeypatch.setattr(BridgeCompletions, "_identity", identity)
    return Store(), actor, events


@pytest.mark.parametrize("outcome", ["success", "refusal", "cancellation"])
async def test_prepared_fixture_keeps_actor_for_scope_and_restores_previous_reader(tmp_path, monkeypatch, outcome):
    store, actor, events = host_dependencies(tmp_path, monkeypatch)
    previous = Principal(UUID(int=90), "https://host-fixture.invalid", "previous-reader")
    token = CURRENT_PRINCIPAL.set(previous)
    try:

        async def body():
            async with use_prepared(store, tmp_path) as (_, current, _, _, _, scope, _, wire):
                await scope.prepare(wire)  # Real BridgeCompletions.current/_execution identity-context check.
                assert current == actor and CURRENT_PRINCIPAL.get() == actor
                assert events[0] == ("authenticate", previous)
                assert not any(event[0] == "close" for event in events)
                if outcome == "refusal":
                    raise RuntimeError("Owned body refusal")
                if outcome == "cancellation":
                    raise asyncio.CancelledError()

        if outcome == "success":
            await body()
        else:
            with pytest.raises(RuntimeError if outcome == "refusal" else asyncio.CancelledError):
                await body()
        assert CURRENT_PRINCIPAL.get() == previous
        assert events.count(("close", actor)) == 1
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_fixture_setup_refusal_also_closes_client_and_restores_reader(tmp_path, monkeypatch):
    store, actor, events = host_dependencies(tmp_path, monkeypatch, setup_failure=True)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        with pytest.raises(RuntimeError, match="setup refusal"):
            async with use_prepared(store, tmp_path):
                pytest.fail("A failed preparation cannot yield a native scope")
        assert CURRENT_PRINCIPAL.get() is None
        assert events.count(("close", actor)) == 1
    finally:
        CURRENT_PRINCIPAL.reset(token)
