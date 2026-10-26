# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The native bilingual fixture exercises the complete current-facts card contract."""

from types import SimpleNamespace

import pytest
from communication_model_fixture import reviewed_model
from test_addressed_status_ai_native import card
from test_communication_writer import configured_store

from firefly_forge.communication import CommunicationConversationV1, CommunicationWriter


@pytest.mark.parametrize("language", ["en", "es"])
@pytest.mark.parametrize("repair", [False, True])
async def test_native_fixture_card_cites_current_scope_checks_and_exact_pending_decision(tmp_path, language, repair):
    run = {
        "id": "1" * 32,
        "status": "paused",
        "attempt": 1,
        "operation": "test",
        "brief": "Check the explicitly selected existing result",
        "component_snapshot": {"name": "API", "responsibility": "Serve requests"},
        "worker_request": {
            "id": "poll-native-question",
            "kind": "product_definition",
            "status": "pending",
            "question": "Keep the existing result?",
            "context": "A native operator must answer before any explicit continuation.",
            "choices": [],
        },
    }
    case = SimpleNamespace(
        run=run, store=SimpleNamespace(get_run=None), http=SimpleNamespace(portal=SimpleNamespace(call=lambda *_: run))
    )
    corrected = card(case, language)
    candidate = {
        **corrected,
        "paragraphs": [
            {
                "text": "El trabajo ha terminado." if language == "es" else "All work has finished.",
                "fact_ids": ["conversation", "state", "decision"],
            },
            *corrected["paragraphs"][1:],
        ],
    }
    calls = []
    store = await configured_store(tmp_path)
    model = reviewed_model(
        candidate if repair else corrected,
        verdict="unsupported" if repair else "supported",
        correction=corrected,
        calls=calls,
    )
    result = await CommunicationWriter(store, model=model).write(
        run,
        "Historical classifier wording",
        lambda value: value,
        conversation=CommunicationConversationV1(selection="run", language=language, linked_runs=1),
    )
    evidence = result["communication"]
    assert evidence["mode"] == "ai"
    assert evidence["repair_attempted"] is repair
    assert {"work", "requested_work", "execution_scope", "check_status", "decision"} <= evidence["facts"].keys()
    assert [phase for phase, *_ in calls] == ["draft", "review"] + (["draft"] if repair else [])
    assert run["worker_request"]["question"] in result["body"]
    assert run["worker_request"]["context"].replace(".", r"\.") in result["body"]
    assert "Historical classifier wording" not in result["body"]
    assert candidate["paragraphs"][0]["text"] not in result["body"]
