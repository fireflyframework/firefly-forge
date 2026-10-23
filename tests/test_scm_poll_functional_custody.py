# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An awaited functional result cannot replace a newer native operator decision."""

import copy
from uuid import uuid4

import pytest
from test_scm_poll_functional_service import command, setup
from test_scm_polling import poll_case  # noqa: F401

from firefly_forge.project_access import CURRENT_PRINCIPAL


@pytest.mark.parametrize("outcome", ["completed", "not_completed"])
@pytest.mark.parametrize("returned", ["completed", "unconfirmed", "exception"])
async def test_operator_reconciliation_survives_late_assistant_result(poll_case, monkeypatch, outcome, returned):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        service = c.state["functional"]
        await service.configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        started = await service.process((await command(c, "start"))["id"])
        delivery = await command(
            c,
            "turn",
            conversation_id=started["action_result"]["conversation_id"],
            request_id=uuid4().hex,
            message="Public review fixture",
        )
        original = c.collaboration.product_assistant.turn
        captured = {}

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            token = CURRENT_PRINCIPAL.set(c.owner)
            try:
                captured["value"] = await c.collaboration.reconcile(
                    delivery["id"], outcome, "Public fixture operator decision"
                )
            finally:
                CURRENT_PRINCIPAL.reset(token)
            if returned == "exception":
                raise RuntimeError("Controlled late model failure")
            if returned == "unconfirmed":
                return {**result, "status": "needs_reconciliation"}
            return result

        c.collaboration.product_assistant.turn = delayed
        if returned == "exception":
            with pytest.raises(RuntimeError, match="Controlled late model failure"):
                await service.process(delivery["id"])
        else:
            await service.process(delivery["id"])
        assert await c.collaboration.receipt(delivery["id"]) == captured["value"]
        before = copy.deepcopy(c.db["settings"][c.repository].data)
        assert await service.process(delivery["id"]) == captured["value"]
        assert c.state["model_calls"] == 1
        assert c.db["settings"][c.repository].data == before


async def test_changed_writing_authority_is_not_replaced_by_late_model_result(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        service = c.state["functional"]
        await service.configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        started = await service.process((await command(c, "start"))["id"])
        delivery = await command(
            c,
            "turn",
            conversation_id=started["action_result"]["conversation_id"],
            request_id=uuid4().hex,
            message="Public authority fixture",
        )
        original = c.collaboration.product_assistant.turn
        captured = {}

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            row = c.db["inbox"][delivery["id"]]
            row.data = {**row.data, "functional_authority": {"grant_digest": "0" * 64, "binding_digest": "0" * 64}}
            captured["value"] = copy.deepcopy(row.data)
            return result

        c.collaboration.product_assistant.turn = changed
        assert await service.process(delivery["id"]) == captured["value"]
        assert c.state["model_calls"] == 1


async def test_operator_reconciliation_prevents_late_conversation_link(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        service = c.state["functional"]
        await service.configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        delivery = await command(c, "start")
        original = c.collaboration.product_assistant.create
        captured = {}

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            token = CURRENT_PRINCIPAL.set(c.owner)
            try:
                captured["value"] = await c.collaboration.reconcile(
                    delivery["id"], "not_completed", "Public fixture start outcome held"
                )
            finally:
                CURRENT_PRINCIPAL.reset(token)
            return result

        c.collaboration.product_assistant.create = changed
        assert await service.process(delivery["id"]) == captured["value"]
        assert c.db["settings"][c.repository].data[module.USAGE_KEY]["conversations"] == {}
        assert c.state.get("model_calls", 0) == 0
