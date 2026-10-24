# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual HTTP mock transport and private on-disk cooldown; no provider or database."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from test_scm_delegated_feedback import RECEIPT, received
from test_scm_delegated_feedback import delegated_feedback as delegated_feedback
from test_scm_polling import poll_case as poll_case

from firefly_forge.scm_connections import SCMConnections


def connection(tmp_path, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scm = SCMConnections(SimpleNamespace(path=tmp_path / "store.db"), client=client)
    scm.private._write(scm.path, {"github": {"revision": "r1", "values": {"app_id": "12", "installation_id": "34"}}})
    return scm


@pytest.mark.parametrize("status", [403, 429])
async def test_provider_failure_holds_all_endpoints_and_survives_restart(tmp_path, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"retry-after": "120", "x-github-request-id": "PUBLIC:123", "x-ratelimit-remaining": "4990"},
            json={"message": "PRIVATE BODY MUST NOT BE RETAINED"},
        )

    scm = connection(tmp_path, handler)
    original = scm.path.read_bytes()
    with pytest.raises(ValueError, match=f"HTTP {status}"):
        await scm._request(
            "GET", "https://api.github.com/installation/repositories", headers={"Authorization": "Bearer private-token"}
        )
    other = connection(tmp_path, handler)
    for instance in (scm, other, scm, other):
        with pytest.raises(ValueError, match="cooldown"):
            await instance._request("POST", "https://api.github.com/repos/team/product/issues/4/comments")
    assert len(calls) == 1
    assert scm.path.read_bytes() == original
    # Creating the second connection intentionally rewrites the fixture configuration.
    generation = scm.private.settings_generation
    with pytest.raises(ValueError, match="cooldown"):
        await scm._request("GET", "https://api.github.com/installation/repositories")
    assert scm.private.settings_generation is generation
    states = list((tmp_path / "scm-runtime" / "configuration").glob("*.json"))
    assert len(states) == 1
    text = states[0].read_text()
    assert "private-token" not in text and "PRIVATE BODY" not in text
    evidence = json.loads(text)["evidence"]
    assert evidence["status"] == status and evidence["remaining"] == 4990
    assert evidence["request_id"] == "PUBLIC:123"
    await scm._client.aclose()
    await other._client.aclose()


async def test_locally_deferred_oauth_refresh_is_not_recorded_as_unknown(tmp_path):
    calls = []
    scm = connection(tmp_path, lambda request: calls.append(request) or httpx.Response(403))
    data = scm.private._read(scm.path)
    data["gitlab"] = {
        "revision": "g1",
        "values": {
            "client_id": "public",
            "redirect_uri": "https://forge.example/api/v1/scm/connections/gitlab/callback",
        },
        "grant": {"access_token": "expired", "refresh_token": "private-refresh", "expires_at": 0},
    }
    scm.private._write(scm.path, data)
    with pytest.raises(ValueError, match="HTTP 403"):
        await scm._request("GET", "https://gitlab.com/api/v4/projects")
    before = scm.path.read_bytes()
    with pytest.raises(ValueError, match="cooldown"):
        await scm._oauth_token("gitlab")
    assert scm.path.read_bytes() == before
    assert len(calls) == 1
    await scm._client.aclose()


async def test_unchanged_sent_feedback_does_not_request_remote_authority(delegated_feedback, monkeypatch):
    service, row, run, sent, state = delegated_feedback
    received(row, "status")
    await service.process(RECEIPT)
    calls = []
    original = service._feedback_authority

    async def authority(*args, **kwargs):
        if not kwargs.get("local"):
            calls.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_feedback_authority", authority)
    for _ in range(8):
        await service.sync_feedback()
    assert calls == [] and len(sent) == 1
    state["deny"] = True
    run.update(status="running", active_task="implement", worker_request={})
    await service.sync_feedback()
    assert len(sent) == 1


async def test_polling_due_gate_precedes_scope_hydration(poll_case, monkeypatch):
    async with poll_case() as c:
        await c.ready()
        calls = []
        original = c.service._scope

        async def scope(*args):
            calls.append(1)
            return await original(*args)

        monkeypatch.setattr(c.service, "_scope", scope)
        await c.service.step(c.repository)
        calls.clear()
        await c.service.step(c.repository)
        assert calls == []


async def test_concurrent_requests_do_not_queue_after_provider_failure(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(403)

    scm = connection(tmp_path, handler)
    first = asyncio.create_task(scm._request("GET", "https://api.github.com/installation/repositories"))
    await entered.wait()
    waiting = asyncio.create_task(scm._request("GET", "https://api.github.com/repos/team/product"))
    try:
        done, _ = await asyncio.wait({waiting}, timeout=0.08)
        assert not done and len(calls) == 1
    finally:
        release.set()
    with pytest.raises(ValueError, match="HTTP 403"):
        await first
    with pytest.raises(ValueError, match="cooldown"):
        await waiting
    assert len(calls) == 1
    await scm._client.aclose()


@pytest.mark.parametrize(
    "headers,seconds",
    [
        ({}, 60),
        ({"retry-after": "nonsense"}, 60),
        ({"retry-after": "120"}, 120),
        ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1180"}, 180),
    ],
)
async def test_backoff_deadline_and_allowed_response_are_preserved(tmp_path, monkeypatch, headers, seconds):
    import firefly_forge.scm_provider_backoff as module

    now, calls = [1000.0], []
    monkeypatch.setattr(module, "time", lambda: now[0])

    def handler(request):
        calls.append(request)
        return httpx.Response(429 if len(calls) == 1 else 200, headers=headers, json={})

    scm = connection(tmp_path, handler)
    response = await scm._request(
        "GET", "https://api.github.com/installation/repositories", allowed_statuses=(200, 429)
    )
    assert response.status_code == 429
    now[0] += seconds - 1
    with pytest.raises(ValueError, match="cooldown"):
        await scm._request("GET", "https://api.github.com/installation/repositories")
    assert len(calls) == 1
    now[0] += 2
    assert (await scm._request("GET", "https://api.github.com/installation/repositories")).status_code == 200
    assert len(calls) == 2
    await scm._client.aclose()


async def test_scope_is_shared_across_revisions_but_not_installations(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403 if len(calls) == 1 else 200)

    scm = connection(tmp_path, handler)
    with pytest.raises(ValueError, match="HTTP 403"):
        await scm._request("GET", "https://api.github.com/installation/repositories")
    data = scm.private._read(scm.path)
    data["github"]["revision"] = "new-revision"
    scm.private._write(scm.path, data)
    with pytest.raises(ValueError, match="cooldown"):
        await scm._request("GET", "https://api.github.com/installation/repositories")
    data["github"]["values"]["installation_id"] = "35"
    scm.private._write(scm.path, data)
    assert (await scm._request("GET", "https://api.github.com/installation/repositories")).status_code == 200
    assert len(calls) == 2
    await scm._client.aclose()


async def test_invalid_or_nonprivate_state_fails_closed_without_http(tmp_path):
    calls = []
    scm = connection(tmp_path, lambda request: calls.append(request) or httpx.Response(403))
    with pytest.raises(ValueError, match="HTTP 403"):
        await scm._request("GET", "https://api.github.com/installation/repositories")
    path = next((tmp_path / "scm-runtime" / "configuration").glob("*.json"))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-private"):
        await scm._request("GET", "https://api.github.com/installation/repositories")
    path.chmod(0o600)
    path.write_bytes(b"x" * 16385)
    with pytest.raises(ValueError, match="size limit"):
        await scm._request("GET", "https://api.github.com/installation/repositories")
    assert len(calls) == 1
    await scm._client.aclose()


async def test_denial_headers_start_cooldown_before_bounded_body_read(tmp_path):
    calls = []
    scm = connection(tmp_path, lambda request: calls.append(request) or httpx.Response(403, content=b"x" * 100))
    with pytest.raises(ValueError, match="HTTP 403"):
        await scm._request("GET", "https://api.github.com/installation/repositories", response_limit=8)
    with pytest.raises(ValueError, match="cooldown"):
        await scm._request("GET", "https://api.github.com/installation/repositories", response_limit=8)
    assert len(calls) == 1
    await scm._client.aclose()
