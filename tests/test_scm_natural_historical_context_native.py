# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real DI/PG legacy publication compatibility; AI, knowledge and SCM are controlled."""

import copy
from uuid import UUID

import pytest
from communication_model_fixture import reviewed_model
from scm_polling_native_fixtures import native_poll as native_poll
from test_scm_natural_native import assert_original, observe, prepare, product_child
from test_scm_poll_functional_native import observe as functional_observe

from firefly_forge.scm_natural_tasks import SCMNaturalTasks


@pytest.mark.parametrize("qualified", [False, True])
def test_native_legacy_publication_cannot_block_or_supply_current_product_questions(
    native_poll, monkeypatch, qualified
):
    case = native_poll
    prepare(case, monkeypatch)
    original = case.http.portal.call(case.store.get_run, case.run["id"])
    wal = case.http.portal.call(case.journal.entries, case.run["id"])
    first = case.process(observe(case, "@forge, design the warehouse CRM."))
    completed = product_child(case, first)
    published = None
    if qualified:
        writer_calls = []
        case.collaboration.communication_writer.model = reviewed_model(
            {
                "title": {"text": "Product conversation", "fact_ids": ["assistant"]},
                "paragraphs": [{"text": "The conversation has a retained reply.", "fact_ids": ["assistant"]}],
                "decision": None,
            },
            calls=writer_calls,
        )
        published = case.process(
            functional_observe(case, "status", conversation_id=completed["action_result"]["conversation_id"])
        )
        assert published["feedback_status"] == "sent"
        assert published["feedback_intent"]["communication"]["facts"].get("assistant")
        assert len(writer_calls) == 2

    async def historical_shape():
        async with case.store._unit():
            row = await case.collaboration.inbox.find_by_id(UUID(completed["id"]))
            intent = copy.deepcopy(row.data["feedback_intent"])
            intent["communication"]["facts"] = {"state": {"status": "paused"}}
            await case.collaboration._write_row(row, feedback_intent=intent)

    case.http.portal.call(historical_shape)
    case.state["natural_intent"] = "status"
    delivery = observe(case, "@forge, how is this going?")
    natural = SCMNaturalTasks(case.collaboration)
    result = case.http.portal.call(natural.process, delivery["id"])
    assert result["status"] == "processed", result
    products = [
        row for row in result["natural_turn"]["context"]["candidates"] if row.get("origin") == "product_assistant_turn"
    ]
    assert len(products) == int(qualified)
    if qualified:
        assert products[0]["publication"]["receipt_id"] == published["id"]
        assert products[0]["turn_id"] == completed["action_result"]["turn_id"]
    assert all(row["publication"]["receipt_id"] != completed["id"] for row in products)
    assert case.state["natural_calls"] == 2 and case.state["model_calls"] == 1
    assert case.http.portal.call(type(natural)(case.collaboration).process, delivery["id"]) == result
    assert case.state["natural_calls"] == 2 and case.state["model_calls"] == 1
    assert_original(case, original, wal)
