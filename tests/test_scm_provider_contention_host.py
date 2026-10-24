# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real private file-lock contention with controlled HTTP only; no database or provider."""

import asyncio
import json
from time import monotonic

import httpx
import pytest
from test_scm_provider_backoff_host import connection

from firefly_forge.scm_provider_backoff import ProviderRequestDeferred

BASE = "https://api.github.com"


async def still_waiting(task):
    done, _ = await asyncio.wait({task}, timeout=0.08)
    assert not done, "A healthy occupied scope must wait locally before dispatch"


async def test_healthy_poll_and_native_source_read_serialize_across_real_lock_instances(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append((request.method, request.url.path))
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200, json={"ok": True})

    first, second = connection(tmp_path, handler), connection(tmp_path, handler)
    poll = asyncio.create_task(first._request("GET", BASE + "/installation/repositories"))
    await entered.wait()
    native = asyncio.create_task(second._request("GET", BASE + "/repos/team/product"))
    try:
        await still_waiting(native)
        assert calls == [("GET", "/installation/repositories")]
    finally:
        release.set()
    responses = await asyncio.gather(poll, native)
    assert [r.status_code for r in responses] == [200, 200]
    assert calls == [("GET", "/installation/repositories"), ("GET", "/repos/team/product")]
    await first._client.aclose()
    await second._client.aclose()


@pytest.mark.parametrize("status", [403, 429])
async def test_waiter_rechecks_persisted_cooldown_without_dispatch_or_replay(tmp_path, status):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(status, headers={"retry-after": "120"})

    first, second = connection(tmp_path, handler), connection(tmp_path, handler)
    owner = asyncio.create_task(first._request("GET", BASE + "/installation/repositories"))
    await entered.wait()
    waiting = asyncio.create_task(second._request("POST", BASE + "/repos/team/product/issues/4/comments"))
    try:
        await still_waiting(waiting)
    finally:
        release.set()
    results = await asyncio.gather(owner, waiting, return_exceptions=True)
    assert isinstance(results[0], ValueError) and f"HTTP {status}" in str(results[0])
    assert isinstance(results[1], ProviderRequestDeferred) and "cooldown" in str(results[1])
    state = next((tmp_path / "scm-runtime/configuration").glob("*.json"))
    retained = state.read_bytes()
    assert json.loads(retained)["evidence"]["status"] == status
    with pytest.raises(ProviderRequestDeferred, match="cooldown"):
        await second._request("POST", BASE + "/repos/team/product/issues/4/comments")
    assert state.read_bytes() == retained and len(calls) == 1
    await first._client.aclose()
    await second._client.aclose()


async def test_wait_deadline_is_bounded_and_definite_nondispatch(tmp_path, monkeypatch):
    monkeypatch.setattr("firefly_forge.scm_provider_backoff.LOCK_WAIT_SECONDS", 0.12, raising=False)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200)

    scm = connection(tmp_path, handler)
    owner = asyncio.create_task(scm._request("GET", BASE + "/installation/repositories"))
    await entered.wait()
    start = monotonic()
    try:
        with pytest.raises(ProviderRequestDeferred, match="in progress") as error:
            await asyncio.wait_for(scm._request("POST", BASE + "/repos/team/product/issues/4/comments"), 2)
        assert 0.08 <= monotonic() - start < 1
        assert error.value.evidence({"request": "public"})["outcome"] == "not_dispatched"
        assert len(calls) == 1
    finally:
        release.set()
        await owner
    assert (await scm._request("GET", BASE + "/repos/team/product")).status_code == 200
    assert [r.method for r in calls] == ["GET", "GET"]
    await scm._client.aclose()


async def test_cancelled_waiter_never_dispatches_or_releases_another_owners_lock(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200)

    first, second = connection(tmp_path, handler), connection(tmp_path, handler)
    owner = asyncio.create_task(first._request("GET", BASE + "/installation/repositories"))
    await entered.wait()
    waiting = asyncio.create_task(second._request("POST", BASE + "/repos/team/product/issues/4/comments"))
    try:
        await still_waiting(waiting)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        third = asyncio.create_task(second._request("GET", BASE + "/repos/team/product"))
        await still_waiting(third)
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(owner, waiting, return_exceptions=True)
    assert (await third).status_code == 200
    assert [r.method for r in calls] == ["GET", "GET"]
    await first._client.aclose()
    await second._client.aclose()


async def test_connection_change_while_waiting_refuses_before_any_stale_dispatch(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200)

    first, second = connection(tmp_path, handler), connection(tmp_path, handler)
    owner = asyncio.create_task(first._request("GET", BASE + "/installation/repositories"))
    await entered.wait()
    waiting = asyncio.create_task(second._request("POST", BASE + "/repos/team/product/issues/4/comments"))
    try:
        await still_waiting(waiting)
        data = second.private._read(second.path)
        data["github"]["revision"] = "changed-while-waiting"
        second.private._write(second.path, data)
    finally:
        release.set()
    assert (await owner).status_code == 200
    with pytest.raises(ValueError, match="settings changed"):
        await waiting
    assert len(calls) == 1
    await first._client.aclose()
    await second._client.aclose()


async def test_unknown_post_is_not_replayed_when_an_independent_read_waits(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        if request.method == "POST":
            entered.set()
            await release.wait()
            raise httpx.ReadError("Controlled unknown write outcome")
        return httpx.Response(200)

    scm = connection(tmp_path, handler)
    owner = asyncio.create_task(scm._request("POST", BASE + "/repos/team/product/issues/4/comments"))
    await entered.wait()
    waiting = asyncio.create_task(scm._request("GET", BASE + "/repos/team/product"))
    try:
        await still_waiting(waiting)
    finally:
        release.set()
    with pytest.raises(ValueError, match="could not be completed"):
        await owner
    assert (await waiting).status_code == 200
    assert [r.method for r in calls] == ["POST", "GET"]
    await scm._client.aclose()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_unknown_or_cancelled_refresh_keeps_original_uncertain_grant_without_retry(tmp_path, cancelled):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        raise httpx.ReadError("Controlled unknown refresh")

    scm = connection(tmp_path, handler)
    data = scm.private._read(scm.path)
    data["gitlab"] = {
        "revision": "g1",
        "values": {"client_id": "public", "redirect_uri": "https://forge.example/callback"},
        "grant": {"access_token": "expired", "refresh_token": "fictional-refresh", "expires_at": 0},
    }
    scm.private._write(scm.path, data)
    first = asyncio.create_task(scm._oauth_token("gitlab"))
    await entered.wait()
    if cancelled:
        first.cancel()
    else:
        release.set()
    with pytest.raises(asyncio.CancelledError if cancelled else ValueError):
        await first
    retained = scm.path.read_bytes()
    assert json.loads(retained)["gitlab"]["grant"]["refresh_state"] == "dispatching"
    with pytest.raises(ValueError, match="uncertain"):
        await scm._oauth_token("gitlab")
    assert scm.path.read_bytes() == retained and len(calls) == 1
    await scm._client.aclose()
