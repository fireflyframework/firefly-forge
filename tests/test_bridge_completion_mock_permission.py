# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host-only characterization of the native SDK fixtures' scoped model permission."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
import test_bridge_completion_native as native
from pydantic_ai import models

from firefly_forge.bridge_completion_scope import BridgeCompletionScope


def host_preparation(monkeypatch):
    state = {"active": False, "record": None, "hold": None}

    @asynccontextmanager
    async def unit():
        previous = state["active"]
        state["active"] = True
        try:
            yield
        finally:
            state["active"] = previous

    async def get_run(run_id):
        return {"bridge_completion_hold": state["hold"]}

    async def entries(run_id):
        return []

    async def record(*args):
        return state["record"]

    async def prepare(body):
        if state["active"]:
            raise ValueError("Bridge completion refuses an active caller transaction")

    async def admit(descriptor, body):
        retained = {"digest": "d" * 64, "payload": {"descriptor": descriptor}}
        state["record"] = SimpleNamespace(data=retained)
        state["hold"] = {"request_id": descriptor["request_id"]}
        return retained

    async def guard(*args):
        pass

    async def response(*args):
        pass

    store = SimpleNamespace(
        _unit=unit,
        get_run=get_run,
        configuration=SimpleNamespace(
            load_snapshot=lambda revision: {
                "FORGE_BRIDGE_URL": "http://localhost:19191",
                "FORGE_BRIDGE_TOKEN": "fictional-host-token",
                "FORGE_BRIDGE_COMPLETION_RECOVERY": "required",
            }
        ),
    )
    journal = SimpleNamespace(entries=entries, records=SimpleNamespace(bridge_record=record))
    scope = BridgeCompletionScope(prepare, admit, guard, response)

    @asynccontextmanager
    async def prepared(*args, **kwargs):
        yield (None, None, {"id": UUID(int=1).hex, "config_revision": "fixture"}, journal, None, scope, {}, b"body")

    monkeypatch.setattr(native, "prepared", prepared)
    return store


@pytest.mark.parametrize(
    "case",
    [
        native.test_native_ambient_transaction_refuses_before_capability_or_post,
        native.test_native_sdk_transport_sees_committed_original_intent_before_one_post,
    ],
    ids=["ambient-before-http", "committed-before-post"],
)
async def test_native_mock_transport_reaches_intended_assertions_under_disabled_runner(case, tmp_path, monkeypatch):
    store = host_preparation(monkeypatch)
    with models.override_allow_model_requests(False):
        await case(store, tmp_path)
        assert models.ALLOW_MODEL_REQUESTS is False


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("outcome", ["success", "refusal", "cancellation"])
async def test_mock_permission_restores_original_guard_and_http_barriers(initial, outcome):
    import asyncio

    import httpx
    import httpx2
    from test_bridge_completion_callbacks import fixture_model

    model = fixture_model(lambda request: httpx2.Response(200))
    transports = [
        (module.AsyncHTTPTransport, module.AsyncHTTPTransport.handle_async_request) for module in (httpx, httpx2)
    ]
    try:
        with models.override_allow_model_requests(initial):

            def body():
                with native.mock_model_requests(model):
                    assert models.ALLOW_MODEL_REQUESTS is True
                    if outcome == "refusal":
                        raise ValueError("Owned mock refusal")
                    if outcome == "cancellation":
                        raise asyncio.CancelledError()

            if outcome == "success":
                body()
            else:
                with pytest.raises(ValueError if outcome == "refusal" else asyncio.CancelledError):
                    body()
            assert models.ALLOW_MODEL_REQUESTS is initial
            assert all(cls.handle_async_request is original for cls, original in transports)
    finally:
        await model.client.close()


@pytest.mark.parametrize("unbound", ["default-transport", "mounted-transport"])
async def test_permission_refuses_unbound_transport_without_enabling_model_requests(unbound):
    import httpx2
    from test_bridge_completion_callbacks import fixture_model

    model = fixture_model(lambda request: httpx2.Response(200))
    default = httpx2.AsyncHTTPTransport()
    try:
        if unbound == "default-transport":
            model.client._client._transport = default
        else:
            model.client._client._mounts = {"unowned": default}
        with models.override_allow_model_requests(False):
            with pytest.raises(AssertionError, match="installed mock"), native.mock_model_requests(model):
                pytest.fail("An unbound transport cannot enable model requests")
            assert models.ALLOW_MODEL_REQUESTS is False
    finally:
        model.client._client._mounts = {}
        await model.client.close()
        await default.aclose()


@pytest.mark.parametrize("library", ["httpx", "httpx2"])
@pytest.mark.parametrize("synchronous", [False, True])
async def test_scoped_permission_refuses_real_http_before_dns(monkeypatch, library, synchronous):
    import importlib
    import socket

    import httpx2
    from test_bridge_completion_callbacks import fixture_model

    module = importlib.import_module(library)
    model = fixture_model(lambda request: httpx2.Response(200))
    dns = []

    def refuse_dns(*args, **kwargs):
        dns.append(args)
        raise AssertionError("DNS must not be reached")

    monkeypatch.setattr(socket, "getaddrinfo", refuse_dns)
    try:
        with models.override_allow_model_requests(False):
            with native.mock_model_requests(model), pytest.raises(AssertionError, match="forbids real HTTP transport"):
                if synchronous:
                    with module.Client(trust_env=False) as client:
                        client.get("https://fixture.invalid/no-dispatch")
                else:
                    async with module.AsyncClient(trust_env=False) as client:
                        await client.get("https://fixture.invalid/no-dispatch")
            assert models.ALLOW_MODEL_REQUESTS is False
        assert dns == []
    finally:
        await model.client.close()
