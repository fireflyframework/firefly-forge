# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Legacy publication facts cannot become questions or block a current conversation."""

import copy
from types import SimpleNamespace

import pytest
from test_scm_natural_intake_host import prepared, product_context
from test_scm_polling import poll_case as poll_case


@pytest.mark.parametrize("qualified", [False, True])
async def test_legacy_turn_publication_does_not_block_current_question_context(poll_case, monkeypatch, qualified):
    async with poll_case() as case:
        natural, receipt = await prepared(case, monkeypatch)
        publication, session, turn, _ = await product_context(case, natural, receipt)
        legacy = copy.deepcopy(publication)
        legacy["id"] = "1" * 32
        legacy["command"]["operation"] = "turn"
        legacy["action_result"] = {
            "conversation_id": session["id"],
            "turn_id": turn["id"],
            "status": "completed",
            "reply": turn["reply"]["message"],
            "questions": turn["reply"]["questions"],
            "proposal_reviewed": False,
        }
        # The actual historical33fb publication retained execution-state facts.
        legacy["feedback_intent"]["communication"]["facts"] = {"state": {"status": "paused"}}
        previous_context = case.collaboration.inbox.conversation_context

        async def context(delivery):
            rows = await previous_context(delivery)
            if not qualified:
                rows = [row for row in rows if row.data["id"] != publication["id"]]
            return [SimpleNamespace(data=copy.deepcopy(legacy), sequence=1000), *rows]

        case.collaboration.inbox.conversation_context = context
        result = await natural.process(receipt)
        assert result["status"] == "processed", result.get("reason")
        candidates = result["natural_turn"]["context"]["candidates"]
        products = [row for row in candidates if row.get("origin") == "product_assistant_turn"]
        assert len(products) == int(qualified)
        if qualified:
            assert products[0]["publication"]["receipt_id"] == publication["id"]
            assert result["natural_turn"]["resolution"]["state"] == "product_answer"
        else:
            assert result["natural_turn"]["resolution"]["state"] == "waiting"
        assert case.state["natural_calls"] == 1
        assert not case.db["journal"]
        assert await type(natural)(case.collaboration).process(receipt) == result
        assert case.state["natural_calls"] == 1
