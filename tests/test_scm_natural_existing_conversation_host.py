# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A current exact native conversation is reused; ambiguous candidates never dispatch."""

import copy

import pytest
from test_scm_natural_product_pipeline_host import prepared
from test_scm_poll_functional_service import command
from test_scm_polling import poll_case as poll_case


@pytest.mark.parametrize("count", [1, 2])
async def test_confirmed_issue_bound_conversations_are_reused_only_when_unique(poll_case, monkeypatch, count):
    async with poll_case() as case:
        _, _, receipt = await prepared(case, monkeypatch)
        original = copy.deepcopy(case.db["runs"][case.run_id].data)
        conversations = []
        for _ in range(count):
            started = await case.collaboration.process((await command(case, "start"))["id"])
            assert started["status"] == "processed", started
            conversations.append(started["action_result"]["conversation_id"])
        assert case.state.get("model_calls", 0) == 0
        result = await case.collaboration.process(receipt["id"])
        if count == 1:
            assert result["natural_turn"]["state"] == "product_completed", result
            children = [
                row.data for row in case.db["inbox"].values() if row.id.hex in result["natural_product_effects"]
            ]
            assert len(children) == 1 and children[0]["command"]["operation"] == "turn"
            assert children[0]["command"]["conversation_id"] == conversations[0]
            assert case.state["model_calls"] == 1
        else:
            assert result["natural_turn"]["state"] == "product_pending_permission", result
            assert not result.get("natural_product_effects")
            assert case.state.get("model_calls", 0) == 0
        assert case.state["coordinator_calls"] == 1
        assert await case.collaboration.process(receipt["id"]) == result
        assert case.state["coordinator_calls"] == 1
        assert case.db["runs"][case.run_id].data == original and not case.db["journal"]
