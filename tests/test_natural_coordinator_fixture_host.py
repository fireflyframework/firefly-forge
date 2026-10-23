# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The controlled native fixture exercises real routing and structured classification."""

from types import SimpleNamespace

import pytest
from anyio.from_thread import start_blocking_portal
from natural_coordinator_model_fixture import native_coordinator
from test_communication_writer import configured_store

from firefly_forge.task_routing import TaskRoutingInput


@pytest.mark.parametrize("intent,message,language", [("status", "¿cómo va?", "es"), ("product", "Build a CRM", "en")])
def test_native_coordinator_fixture_keeps_product_route_and_uses_real_typed_runtime(
    tmp_path, monkeypatch, intent, message, language
):
    with start_blocking_portal() as portal:
        store = portal.call(configured_store, tmp_path)
        router = store.task_router
        portal.call(router.save, TaskRoutingInput(bindings={"product": "updates", "communication": "updates"}))
        case = SimpleNamespace(store=store, http=SimpleNamespace(portal=portal), state={})
        calls = native_coordinator(case, monkeypatch, intent=intent)
        assert router.current()["bindings"]["product"] == "updates"
        selection = portal.call(router.resolve, "communication")
        settings = {**router.profiles.validate_snapshot(selection), "ASSISTANT_PROVIDER": selection["provider"]}
        from firefly_forge.scm_natural_tasks import classify

        reply = portal.call(classify, message, {"status_context": {"selection": "missing"}}, settings)
        assert reply.intent == intent and reply.language == language
        assert len(calls) == 1 and reply.message
