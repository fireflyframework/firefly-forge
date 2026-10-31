# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Store ownership lifecycle with host-only SQL and provider transports."""

from types import SimpleNamespace

import httpx
import pytest

from firefly_forge.collaboration import Collaboration
from firefly_forge.store import Store


class LeaseConnection:
    def __init__(self):
        self.held = False
        self.invalidated = False
        self.closed = False
        self.backend_start = "2026-10-31 00:00:00+00"

    async def scalar(self, statement, parameters=None):
        if self.invalidated:
            raise RuntimeError("The ownership connection was lost")
        query = str(statement)
        if "pg_try_advisory_lock" in query:
            self.held = True
            return True
        if "EXISTS" in query:
            return self.held and parameters["backend_start"] == self.backend_start
        if "backend_start::text" in query:
            return self.backend_start
        assert query == "SELECT pg_backend_pid()"
        return 741

    async def execute(self, statement, parameters=None):
        assert "pg_advisory_unlock" in str(statement)
        self.held = False

    async def commit(self):
        pass

    async def invalidate(self):
        self.invalidated = True

    async def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "change",
    [
        "release",
        "lost_connection",
        "backend_start",
        "stopping",
        "poller_stops",
        "acquired_during_token",
        "poller_starts",
        "callback_release",
        "acquired_then_callback_release",
    ],
)
async def test_owned_write_refuses_lost_ownership_during_token_preparation(tmp_path, change):
    posts, errors = [], []
    connection = LeaseConnection()

    async def connect():
        return connection

    store = Store(tmp_path / "factory.db", engine=SimpleNamespace(connect=connect))
    store._initialized = True
    engine = SimpleNamespace(poller=object() if change == "poller_stops" else None)
    if change not in {"poller_stops", "acquired_during_token", "poller_starts", "acquired_then_callback_release"}:
        await store.acquire_ownership()
        await store.assert_ownership()

    async def authentication(repository, **kwargs):
        if change == "release":
            await store.release_ownership()
        elif change == "lost_connection":
            await connection.invalidate()
        elif change == "backend_start":
            connection.backend_start = "1900-01-01 00:00:00+00"
        elif change == "stopping":
            store.execution_stopping = True
        elif change == "poller_stops":
            engine.poller = None
        elif change == "acquired_during_token":
            await store.acquire_ownership()
            await connection.invalidate()
        elif change == "poller_starts":
            engine.poller = object()
        elif change == "acquired_then_callback_release":
            await store.acquire_ownership()
        return "host-only-token", "https://api.github.com"

    async def before_write():
        if change in {"callback_release", "acquired_then_callback_release"}:
            await store.release_ownership()

    def transport(request):
        posts.append(request)
        return httpx.Response(201, json={"id": 81})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        scm = SimpleNamespace(issue_authentication=authentication, _request=client.request)
        service = Collaboration(store, scm, deployments=SimpleNamespace(engine=engine))
        try:
            await service._remote_write(
                {"name": "team/product", "connection_provider": "github"},
                {"body": "Owned status"},
                {"kind": "issue", "number": 3},
                before_write=before_write,
            )
        except RuntimeError as error:
            errors.append(str(error))
        finally:
            await store.release_ownership()
    assert len(posts) == 0
    assert len(errors) == 1 and ("ownership" in errors[0] or "drains" in errors[0])


@pytest.mark.parametrize("owned", [False, True])
async def test_engine_off_write_preserves_held_and_unowned_manual_semantics(tmp_path, owned):
    posts = []
    connection = LeaseConnection()

    async def connect():
        return connection

    store = Store(tmp_path / "factory.db", engine=SimpleNamespace(connect=connect))
    store._initialized = True
    if owned:
        await store.acquire_ownership()

    async def authentication(repository, **kwargs):
        return "host-only-token", "https://api.github.com"

    def transport(request):
        posts.append(request)
        return httpx.Response(201, json={"id": 81})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        scm = SimpleNamespace(issue_authentication=authentication, _request=client.request)
        service = Collaboration(store, scm, deployments=SimpleNamespace(engine=SimpleNamespace(poller=None)))
        try:
            result = await service._remote_write(
                {"name": "team/product", "connection_provider": "github"},
                {"body": "Manual status"},
                {"kind": "issue", "number": 3},
            )
        finally:
            await store.release_ownership()
    assert len(posts) == 1 and result["id"] == 81
