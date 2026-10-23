# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual polling, profiles, finite native functional admission and controlled architect persistence."""

import copy

from test_scm_poll_functional_service import setup as functional_setup
from test_scm_polling import poll_case as poll_case

from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
from firefly_forge.product_provenance import content_digest
from firefly_forge.scm_natural_models import NaturalDecision
from firefly_forge.task_routing import TaskRouting, TaskRoutingInput


async def prepared(case, monkeypatch, *, allowance=True):
    module, body = await functional_setup(case, monkeypatch)
    profiles = ModelProfiles(case.store.configuration)
    product = ModelProfileInput(
        name="Public product architect",
        provider="bridge",
        values={"model": "public-functional", "endpoint": "http://127.0.0.1:9", "token": "owned-functional-key"},
    )
    profiles.save("functional", product)
    profiles.save("functional", product)
    profiles.save(
        "natural",
        ModelProfileInput(
            name="Public coordinator",
            provider="agentic",
            values={"model": "openai-responses:public-natural", "api_key": "owned-natural-key"},
        ),
    )
    case.state["profile"].update(profiles._read("functional"))
    router = TaskRouting(profiles)
    await router.save(TaskRoutingInput(bindings={"product": "functional", "communication": "natural"}))
    case.store.task_router = router
    record = profiles._read("functional")
    body.update(
        expected_routing_digest=router.current()["digest"],
        expected_model_profile_revision=record["revision"],
        expected_model_profile_digest=content_digest({"provider": record["provider"], "settings": record["settings"]}),
    )
    if allowance:
        await case.state["functional"].configure(
            case.project, case.repository, module.SCMPollFunctionalGrantInputV1(**body)
        )
    case.state["coordinator_calls"] = 0

    async def classify(message, context, settings):
        case.state["coordinator_calls"] += 1
        assert settings["ASSISTANT_PROVIDER"] == "agentic"
        return NaturalDecision(intent="product", message="Coordinator handoff acknowledgement.")

    monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
    number = 100 + len(case.state["comments"])
    case.state["clock"] += 3600
    case.state["comments"].append(case.comment(number, "@forge, build a CRM with imports."))
    for _ in range(3):
        await case.service.step(case.repository)
    receipt = next(
        copy.deepcopy(row.data) for row in case.db["inbox"].values() if row.source_key == f"comment:{number}"
    )
    return module, body, receipt


async def test_product_handoff_reaches_existing_functional_service_with_fresh_ledger_once(poll_case, monkeypatch):
    async with poll_case() as case:
        _, _, receipt = await prepared(case, monkeypatch)
        original = copy.deepcopy(case.db["runs"][case.run_id].data)
        result = await case.collaboration.process(receipt["id"])
        assert result["natural_turn"]["state"] == "product_completed", result
        assert case.state["coordinator_calls"] == 1 and case.state["model_calls"] == 1
        children = [row.data for row in case.db["inbox"].values() if row.id.hex in result["natural_product_effects"]]
        assert {row["command"]["operation"] for row in children} == {"start", "turn"}
        turn = next(row for row in children if row["command"]["operation"] == "turn")
        assert turn["command"]["message"] == "build a CRM with imports."
        assert turn["action_result"]["intent"] == "dialogue" and turn["feedback_status"] == "sent"
        assert "Fictional functional reply" in turn["feedback_intent"]["body"]
        assert "Coordinator handoff acknowledgement" not in turn["feedback_intent"]["body"]
        assert await case.collaboration.process(receipt["id"]) == result
        assert case.state["model_calls"] == 1
        assert case.db["runs"][case.run_id].data == original and not case.db["journal"]


async def test_product_request_is_retained_without_borrowing_old_execution_consent(poll_case, monkeypatch):
    async with poll_case() as case:
        _, _, receipt = await prepared(case, monkeypatch, allowance=False)
        result = await case.collaboration.process(receipt["id"])
        assert result["natural_turn"]["state"] == "product_pending_permission", result
        assert not result.get("natural_product_effects")
        assert case.state["coordinator_calls"] == 1 and case.state.get("model_calls", 0) == 0
        assert result["feedback_status"] == "sent"


async def test_unknown_architect_result_keeps_original_intent_without_another_turn(poll_case, monkeypatch):
    async with poll_case() as case:
        _, _, receipt = await prepared(case, monkeypatch)
        case.state["unknown_model"] = True
        result = await case.collaboration.process(receipt["id"])
        assert result["natural_turn"]["state"] == "needs_reconciliation", result
        assert result["feedback_status"] == "held"
        assert case.state["coordinator_calls"] == case.state["model_calls"] == 1
        await case.collaboration.process(receipt["id"])
        assert case.state["coordinator_calls"] == case.state["model_calls"] == 1


async def test_existing_architect_allowance_cannot_authorize_changed_natural_handoff_policy(poll_case, monkeypatch):
    async with poll_case() as case:
        _, _, receipt = await prepared(case, monkeypatch)
        import firefly_forge.scm_natural_tasks as natural

        previous = natural.natural_policy_digest()
        monkeypatch.setattr(natural, "natural_policy_digest", lambda: "9" * 64 if previous != "9" * 64 else "8" * 64)
        result = await case.collaboration.process(receipt["id"])
        assert result["natural_turn"]["state"] == "product_pending_permission"
        assert case.state["coordinator_calls"] == 1 and case.state.get("model_calls", 0) == 0
        assert not result.get("natural_product_effects")
