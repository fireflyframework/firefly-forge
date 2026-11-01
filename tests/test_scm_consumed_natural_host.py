# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Verified REST intake and reviewed AI reply; the native adapter is controlled separately."""

import copy
import json
from types import SimpleNamespace

import pytest
from communication_model_fixture import reviewed_model
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from test_scm_natural_intake_host import prepared
from test_scm_polling import poll_case as poll_case


@pytest.mark.parametrize("supported", [True, False])
async def test_natural_dispatch_and_reviewed_reply_use_actual_settlement_facts(poll_case, monkeypatch, supported):
    async with poll_case() as c:
        _, receipt = await prepared(c, monkeypatch, intent="resume")
        run = c.db["runs"][c.run_id].data
        run.update(
            status="paused",
            worker_request=None,
            consumed_terminal_recovery=dict(
                version=2, state="claimed", request_id="a" * 32, record_digest="b" * 64, claim_digest="c" * 64
            ),
        )
        calls, packets, phases = [], [], []

        async def settle(receipt_id):
            calls.append(receipt_id)
            row = c.db["inbox"][receipt_id]
            assert row.data["natural_turn"]["resolution"]["state"] == "consumed_continue"
            assert row.data["verification"]["transport"] == "github_rest_poll_v1"
            run["consumed_terminal_recovery"]["state"] = "accepted"
            run["pause"] = {"kind": "consumed_terminal_result", "automatic": False}
            row.data["natural_turn"]["state"] = "consumed_accepted"
            return copy.deepcopy(row.data)

        c.collaboration.consumed_continuation_authority = SimpleNamespace(apply=settle)

        def draft(messages, info):
            if not supported:
                raise ValueError("Controlled unavailable AI authoring")
            prompt = next(p.content for m in reversed(messages) for p in m.parts if isinstance(p, UserPromptPart))
            prefix = "Draft the project update using only this recorded fact packet:\n"
            facts, _ = json.JSONDecoder().raw_decode(prompt.removeprefix(prefix))
            packets.append(facts)
            assert facts["state"]["status"] == "paused" and "consumed_terminal_recovery" not in facts
            keys = sorted(facts)
            card = dict(
                title={"text": "Recorded local result accepted", "fact_ids": keys[:4]},
                paragraphs=[
                    {"text": "The run remains paused for its separate remaining stages.", "fact_ids": keys[n : n + 4]}
                    for n in range(4, len(keys), 4)
                ]
                or [{"text": "Remaining stages need their own authority.", "fact_ids": keys[:4]}],
                decision=None,
            )
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, card)])

        c.collaboration.communication_writer.model = reviewed_model(draft, calls=phases)
        posts = len(c.state["posts"])
        result = await c.collaboration.process(receipt)
        assert result["natural_turn"]["state"] == "consumed_accepted"
        assert calls == [receipt]
        assert result["feedback_status"] == ("sent" if supported else "held"), result.get("reason")
        assert [phase for phase, _, _ in phases] == (["draft", "review"] if supported else ["draft"])
        assert len(packets) == int(supported) and c.state["natural_calls"] == 1
        assert len(c.state["posts"]) == posts + int(supported)
        if supported:
            assert result["feedback_intent"]["communication"]["mode"] == "ai"
            assert "Recorded local result accepted" in result["feedback_intent"]["body"]
        else:
            assert result["reason"] == "ai_communication_consumed_continuation_requires_configured_authoring"
            assert not result.get("feedback_remote")


async def test_polling_catch_up_only_reads_a_retained_consumed_intent():
    from firefly_forge.collaboration import Collaboration

    calls = []

    async def apply(receipt, **kwargs):
        calls.append((receipt, kwargs))

    owner = SimpleNamespace(consumed_continuation_authority=SimpleNamespace(apply=apply))
    await Collaboration._catch_up_natural_answer(
        owner,
        dict(
            id="a" * 32,
            status="processed",
            natural_effects={},
            natural_turn={"state": "consumed_held", "resolution": {"state": "consumed_continue"}},
        ),
    )
    assert calls == [("a" * 32, {"retained_only": True})]
