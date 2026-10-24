# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Both actual writer aliases use structured Agentic responses without provider HTTP."""

from types import SimpleNamespace

import pytest
from communication_model_fixture import recorded_communication_models
from test_communication_writer import configured_store

from firefly_forge.communication import CommunicationWriter


@pytest.fixture
def native_communication_models():
    with recorded_communication_models() as install:
        yield install


@pytest.mark.parametrize("with_question", [False, True])
async def test_native_fixture_controls_both_distinct_writers_and_semantic_review(
    tmp_path, native_communication_models, with_question
):
    store = await configured_store(tmp_path)
    collaboration = SimpleNamespace(communication_writer=CommunicationWriter(store))
    engine = SimpleNamespace(communication_writer=CommunicationWriter(store))
    assert collaboration.communication_writer is not engine.communication_writer
    calls = []
    native_communication_models(collaboration, engine, calls=calls)
    run = {"status": "paused"}
    if with_question:
        run["worker_request"] = {
            "id": "current-question",
            "status": "pending",
            "kind": "other",
            "question": "Keep this source?",
            "context": "Verification is a separate decision.",
            "choices": ["Keep", "Stop"],
        }
    for writer in (collaboration.communication_writer, engine.communication_writer):
        result = await writer.write(run, "Native recorded update", lambda text: text)
        assert result["communication"]["mode"] == "ai", result["communication"]
        writer.assert_current(result["communication"])
        assert "Recorded project update" in result["body"]
        if with_question:
            assert all(value in result["body"] for value in ["Keep this source?", "Keep", "Stop"])
    assert [phase for phase, _, _ in calls] == ["draft", "review", "draft", "review"]
    assert all(not info.function_tools for _, _, info in calls)


async def test_uncontrolled_provider_alias_is_held_before_any_http(tmp_path, native_communication_models, monkeypatch):
    import httpx2
    from pydantic_ai import models

    assert models.ALLOW_MODEL_REQUESTS is False
    requests = []

    async def forbidden(*args, **kwargs):
        requests.append(True)
        raise AssertionError("A native fixture must never dispatch a provider request")

    monkeypatch.setattr(httpx2.AsyncClient, "send", forbidden)
    writer = CommunicationWriter(await configured_store(tmp_path))
    result = await writer.write({"status": "paused"}, "Native update", lambda text: text)
    assert result["communication"]["mode"] == "held" and result["body"] == ""
    assert requests == []
