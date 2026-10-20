# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real PostgreSQL ownership transitions; only provider HTTP is recorded locally."""

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from firefly_forge.collaboration import Collaboration
from firefly_forge.engine import Engine
from firefly_forge.scm_connections import SCMConnections


@pytest.mark.parametrize(
    ("change", "expected_posts"),
    [
        ("release", 0),
        ("lost_lock", 0),
        ("lost_connection", 0),
        ("stopping", 0),
        ("acquired_during_token", 0),
        ("callback_release", 0),
        ("acquired_then_callback_release", 0),
        ("held", 1),
        ("manual", 1),
    ],
)
async def test_native_engine_off_token_wait_revalidates_acquired_ownership(
    store, tmp_path, monkeypatch, change, expected_posts
):
    posts, errors = [], []
    engine = Engine(store, tmp_path / "workspaces")
    assert engine.poller is None and not engine.active
    if change not in {"manual", "acquired_during_token", "acquired_then_callback_release"}:
        await store.acquire_ownership()
        await store.assert_ownership()

    async def authentication(repository, **kwargs):
        assert engine.poller is None
        if change == "release":
            await store.release_ownership()
        elif change == "lost_lock":
            await store._ownership_connection.execute(text("SELECT pg_advisory_unlock_all()"))
        elif change == "lost_connection":
            await store._ownership_connection.invalidate()
        elif change == "stopping":
            store.execution_stopping = True
        elif change == "acquired_during_token":
            await store.acquire_ownership()
            await store.assert_ownership()
            await store._ownership_connection.execute(text("SELECT pg_advisory_unlock_all()"))
        elif change == "acquired_then_callback_release":
            await store.acquire_ownership()
            await store.assert_ownership()
        return "owned-native-fixture-token", "https://api.github.com"

    async def before_write():
        if change in {"callback_release", "acquired_then_callback_release"}:
            await store.release_ownership()

    def transport(request):
        assert request.method == "POST"
        assert str(request.url) == "https://api.github.com/repos/team/product/issues/3/comments"
        posts.append(request)
        return httpx.Response(201, json={"id": 81})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        scm = SCMConnections(store, client=client)
        monkeypatch.setattr(scm, "issue_authentication", authentication)
        service = Collaboration(store, scm, deployments=SimpleNamespace(engine=engine))
        try:
            await service._remote_write(
                {"name": "team/product", "connection_provider": "github"},
                {"body": "Native ownership status"},
                {"kind": "issue", "number": 3},
                before_write=before_write,
            )
        except RuntimeError as error:
            errors.append(str(error))
        finally:
            await store.release_ownership()
    assert len(posts) == expected_posts
    assert len(errors) == (0 if expected_posts else 1)
    assert not errors or "ownership" in errors[0] or "drains" in errors[0]
    assert engine.poller is None and not engine.active
