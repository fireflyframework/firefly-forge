# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A controlled configured classifier retains an exact multilingual answer through native custody."""

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from scm_polling_native_fixtures import native_poll as native_poll
from test_scm_natural_native import observe, prepare_worker

from firefly_forge.scm_natural_runtime import classify


@pytest.mark.parametrize("misbound", [False, True])
def test_native_cross_language_answer_requires_exact_offered_evidence(native_poll, monkeypatch, misbound):
    case = native_poll
    prepare_worker(case, monkeypatch)
    original = case.http.portal.call(case.store.get_run, case.run["id"])
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    human = "Conserva el resultado existente."
    calls = []

    async def configured(message, context, settings):
        def model(messages, info):
            calls.append(message)
            assert not info.function_tools
            assert "different languages" in info.instructions
            rows = [row for row in context["candidates"] if row.get("question_id")]
            assert len(rows) == 1 and rows[0]["question_id"] == original["worker_request"]["id"]
            value = {
                "intent": "answer",
                "message": "Conservo tu respuesta; el trabajo sigue en pausa.",
                "choice": 1,
                "selection_evidence": {
                    "human_span": human,
                    "question_span": "Foreign pending question" if misbound else rows[0]["question"],
                },
            }
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, value)])

        return await classify(message, context, settings, model=FunctionModel(model))

    monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", configured)
    delivery = observe(case, human)
    assert delivery["command"]["source_kind"] == "retained_question_reply"
    result = case.process(delivery)
    after = case.http.portal.call(case.store.get_run, case.run["id"])
    entries = case.http.portal.call(case.journal.entries, case.run["id"])
    if misbound:
        assert result["natural_turn"]["resolution"]["state"] == "waiting", result
        assert after == original and entries == before
    else:
        assert result["natural_turn"]["state"] == "answer_saved_pending_resume", result
        assert after["status"] == "paused" and after["worker_request"]["status"] == "answered"
        assert entries[: len(before)] == before
        assert [row["kind"] for row in entries[len(before) :]] == ["worker_response"]
        assert entries[-1]["payload"]["answer"] == human
        for key in ("repair_budget", "implementation_rounds", "model_selection", "execution_config", "base_revision"):
            assert after.get(key) == original.get(key)
    assert case.process(delivery) == result and calls == [human]
