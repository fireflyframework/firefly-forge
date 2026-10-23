# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Natural comments reach one configured whole-card writer under native read authority."""

import copy
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from communication_model_fixture import reviewed_model
from pydantic_ai.messages import ModelResponse, ToolCallPart
from test_addressed_status_polling import addressed
from test_addressed_status_signed import grant, signed_address
from test_communication_writer import configured_store
from test_conversation_communication_host import TEXT, output
from test_scm_polling import poll_case as poll_case


async def writer_address(c, body="@forge how is this going?", number=80):
    delivery, comment = await signed_address(c, body=body, number=number)
    c.state["comments"].append(comment)
    await grant(c, delivery)
    return delivery


@pytest.mark.parametrize("language", ["en", "es"])
@pytest.mark.parametrize("selection", ["run", "missing", "ambiguous", "clarify"])
async def test_signed_status_pipeline_uses_configured_whole_ai_card_for_every_language_and_context(
    poll_case, tmp_path, language, selection
):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        c.db["runs"][c.run_id].data.update(status="developing", worker_request={})
        if selection in {"run", "ambiguous"}:
            prior = await c.new_command("status")
            await c.collaboration.process(prior["id"])
        if selection == "ambiguous":
            other = uuid4().hex
            run = copy.deepcopy(c.db["runs"][c.run_id].data)
            run["id"] = other
            c.db["runs"][other] = SimpleNamespace(
                id=UUID(other), repository_id=UUID(c.repository), status="developing", data=run
            )
            second = await addressed(c, "/forge status " + json.dumps({"run_id": other}), 81)
            await c.collaboration.process(second["id"])
        baseline = len(c.state["posts"])
        c.store.task_router = (await configured_store(tmp_path)).task_router
        calls = []

        c.collaboration.communication_writer.model = reviewed_model(
            output(TEXT[language][selection], selected=selection == "run"), calls=calls
        )
        body = "@forge ¿cómo va?" if language == "es" else "@forge how is this going?"
        if selection == "clarify":
            body = "@forge reanuda este trabajo" if language == "es" else "@forge change the product"
        delivery = await writer_address(c, body, 82)
        result = await c.collaboration.process(delivery["id"])
        assert result["conversation"]["selection"] == selection
        assert result["feedback_status"] == "sent" and len(c.state["posts"]) == baseline + 1
        assert len(calls) == 2 and result["feedback_intent"]["communication"]["mode"] == "ai"
        assert TEXT[language][selection] in c.state["posts"][-1]["body"]
        assert result["source_snapshot"] == delivery["source_snapshot"] and result["command"] == delivery["command"]
        assert not c.db["journal"]
        await c.collaboration.process(delivery["id"])
        await c.collaboration.sync_feedback()
        assert len(calls) == 2 and len(c.state["posts"]) == baseline + 1


@pytest.mark.parametrize("fault", ["source", "membership", "context", "lost_reply"])
async def test_signed_ai_draft_keeps_native_custody_and_never_repeats_an_unknown_write(poll_case, tmp_path, fault):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        c.store.task_router = (await configured_store(tmp_path)).task_router
        calls = []
        delivery = await writer_address(c)

        def model(messages, info):
            if fault == "source":
                c.state["edited"] = True
            elif fault == "membership":
                c.db["memberships"]["member"].active = False
            elif fault == "context":
                c.db["inbox"][delivery["id"]].data["conversation"]["digest"] = "0" * 64
            else:
                c.state["lost_reply"] = True
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output(TEXT["en"]["missing"]))])

        c.collaboration.communication_writer.model = reviewed_model(model, calls=calls)
        result = await c.collaboration.process(delivery["id"])
        assert result["feedback_status"] == "needs_reconciliation"
        assert len(calls) == 2 and len(c.state["posts"]) == (1 if fault == "lost_reply" else 0)
        original = copy.deepcopy(result["feedback_intent"])
        await c.collaboration.sync_feedback()
        await c.collaboration.process(delivery["id"])
        assert len(calls) == 2 and len(c.state["posts"]) == (1 if fault == "lost_reply" else 0)
        assert (await c.collaboration.receipt(delivery["id"]))["feedback_intent"] == original
        assert not c.db["journal"]


async def test_spanish_run_change_during_generation_holds_without_native_fallback_or_new_call(poll_case, tmp_path):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        c.db["runs"][c.run_id].data.update(status="developing", worker_request={})
        await c.collaboration.process((await c.new_command("status"))["id"])
        c.store.task_router = (await configured_store(tmp_path)).task_router
        calls = []

        def model(messages, info):
            c.db["runs"][c.run_id].data["status"] = "failed"
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, output(TEXT["es"]["run"], selected=True))]
            )

        c.collaboration.communication_writer.model = reviewed_model(model, calls=calls)
        result = await c.collaboration.process((await writer_address(c, "@forge ¿cómo va?", 82))["id"])
        assert len(calls) == 2 and len(c.state["posts"]) == 1
        assert result["feedback_intent"]["communication"]["reason"] == "state_changed"
        assert result["feedback_status"] == "held" and result["feedback_intent"]["body"] == ""
        await c.collaboration.process(result["id"])
        await c.collaboration.sync_feedback()
        assert len(calls) == 2 and len(c.state["posts"]) == 1 and not c.db["journal"]


async def test_signed_spanish_status_uses_current_native_scope_and_the_configured_ai_writer(poll_case, tmp_path):
    from test_addressed_status_signed import grant, signed_address

    async with poll_case() as c:
        delivery, comment = await signed_address(c, body="@forge ¿cómo va?")
        c.state["comments"].append(comment)
        await grant(c, delivery)
        c.store.task_router = (await configured_store(tmp_path)).task_router
        calls = []

        def model(messages, info):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output(TEXT["es"]["missing"]))])

        c.collaboration.communication_writer.model = reviewed_model(model, calls=calls)
        result = await c.collaboration.process(delivery["id"])
        assert result["feedback_status"] == "sent" and len(calls) == 2 and len(c.state["posts"]) == 1
        assert result["feedback_intent"]["communication"]["mode"] == "ai"
        assert result["verification"] == delivery["verification"] and "transport" not in result["verification"]
        assert TEXT["es"]["missing"] in c.state["posts"][0]["body"] and not c.db["journal"]


async def test_mutating_request_on_linked_ticket_gets_ai_clarification_without_execution(poll_case, tmp_path):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        c.db["runs"][c.run_id].data.update(status="developing", worker_request={})
        await c.collaboration.process((await c.new_command("status"))["id"])
        c.store.task_router = (await configured_store(tmp_path)).task_router
        calls = []
        c.collaboration.communication_writer.model = reviewed_model(output(TEXT["es"]["clarify"]), calls=calls)
        reply = await c.collaboration.process((await writer_address(c, "@forge reanuda este trabajo", 93))["id"])
        assert reply["conversation"]["selection"] == "clarify"
        assert len(reply["conversation"]["candidate_ids"]) == 0 and reply.get("run_id") is None
        assert reply["feedback_status"] == "sent" and TEXT["es"]["clarify"] in c.state["posts"][-1]["body"]
        assert len(calls) == 2 and not c.db["journal"]
        assert c.db["runs"][c.run_id].data["status"] == "developing"
