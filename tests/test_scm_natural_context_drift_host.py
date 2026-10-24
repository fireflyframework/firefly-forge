# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A complete read-only status classification may refresh once; unknown calls never replay."""

import copy

import pytest
from test_scm_natural_intake_host import prepared
from test_scm_polling import poll_case as poll_case

from firefly_forge.scm_natural_models import NaturalDecision
from firefly_forge.scm_natural_runtime import ConfirmedInvalidReply


async def setup(case, monkeypatch):
    natural, receipt = await prepared(case, monkeypatch, intent="status")
    case.db["runs"][case.run_id].data.update(status="validating", worker_request=None)
    case.state["baseline_posts"] = copy.deepcopy(case.state["posts"])
    return natural, receipt


@pytest.mark.parametrize("drift_at", ["provider", "final_lock"])
async def test_completed_status_refreshes_native_lifecycle_once_before_retaining_reply(
    poll_case, monkeypatch, drift_at
):
    async with poll_case() as case:
        natural, receipt = await setup(case, monkeypatch)
        calls = []
        original = natural._context
        reads = 0

        async def context(delivery, scopes):
            nonlocal reads
            if len(calls) == 1:
                reads += 1
                if drift_at == "final_lock" and reads == 2:
                    case.db["runs"][case.run_id].data["status"] = "failed"
            return await original(delivery, scopes)

        async def classify(message, context, settings):
            assert case.state["depth"] == 0
            calls.append(copy.deepcopy(context))
            if drift_at == "provider" and len(calls) == 1:
                case.db["runs"][case.run_id].data["status"] = "failed"
            return NaturalDecision(intent="status", message="Native status: " + context["candidates"][0]["run_status"])

        monkeypatch.setattr(natural, "_context", context)
        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        result = await natural.process(receipt)
        assert result["status"] == "processed", result
        turn = result["natural_turn"]
        assert turn["reply"] == "Native status: failed"
        assert turn["classifier_requests_reserved"] == 2 and len(calls) == 2
        assert [row["candidates"][0]["run_status"] for row in calls] == ["validating", "failed"]
        assert len(turn["classifier_attempts"]) == 2
        assert turn["classifier_attempts"][0]["decision"]["message"] == "Native status: validating"
        assert turn["classifier_attempts"][1]["decision"]["message"] == turn["reply"]
        assert not case.db["journal"] and case.state["posts"] == case.state["baseline_posts"]
        assert await type(natural)(case.collaboration).process(receipt) == result
        assert len(calls) == 2


async def test_repeated_progress_holds_confirmed_result_without_unbounded_refresh(poll_case, monkeypatch):
    async with poll_case() as case:
        natural, receipt = await setup(case, monkeypatch)
        calls = []

        async def classify(message, context, settings):
            calls.append(context)
            case.db["runs"][case.run_id].data["status"] = "running" if len(calls) == 1 else "failed"
            return NaturalDecision(intent="status", message="A complete controlled status response.")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        result = await natural.process(receipt)
        assert result["status"] == "needs_reconciliation" and len(calls) == 2
        assert result["natural_turn"]["classifier_outcome"] == "confirmed_valid"
        assert result["natural_turn"]["reply"] is None and result["feedback_status"] == "held"
        assert len(result["natural_turn"]["classifier_attempts"]) == 2
        assert await type(natural)(case.collaboration).process(receipt) == result
        assert len(calls) == 2


async def test_unknown_refresh_request_stays_unknown_without_restart_replay(poll_case, monkeypatch):
    async with poll_case() as case:
        natural, receipt = await setup(case, monkeypatch)
        calls = []

        async def classify(message, context, settings):
            calls.append(context)
            if len(calls) == 2:
                raise RuntimeError("Controlled lost provider outcome")
            case.db["runs"][case.run_id].data["status"] = "failed"
            return NaturalDecision(intent="status", message="Complete stale status.")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        result = await natural.process(receipt)
        assert result["status"] == "needs_reconciliation" and len(calls) == 2
        assert result["natural_turn"]["classifier_outcome"] == "unknown"
        assert result["natural_turn"]["classifier_requests_reserved"] == 2
        assert len(result["natural_turn"]["classifier_attempts"]) == 1
        assert await type(natural)(case.collaboration).process(receipt) == result
        assert len(calls) == 2 and case.state["posts"] == case.state["baseline_posts"]


@pytest.mark.parametrize("invalid_first", [False, True])
async def test_one_correction_and_one_status_refresh_have_independent_finite_reservations(
    poll_case, monkeypatch, invalid_first
):
    async with poll_case() as case:
        natural, receipt = await setup(case, monkeypatch)
        calls = []

        async def classify(message, context, settings):
            assert case.state["depth"] == 0
            calls.append(copy.deepcopy(context))
            index = len(calls)
            if index == (1 if invalid_first else 2):
                raise ConfirmedInvalidReply([{"type": "literal_error", "loc": ["intent"]}])
            if index == (2 if invalid_first else 1):
                case.db["runs"][case.run_id].data["status"] = "failed"
            return NaturalDecision(intent="status", message="Status: " + context["candidates"][0]["run_status"])

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        result = await natural.process(receipt)
        assert result["status"] == "processed", result
        assert result["natural_turn"]["reply"] == "Status: failed"
        assert result["natural_turn"]["classifier_requests_reserved"] == 3 and len(calls) == 3
        assert len(result["natural_turn"]["classifier_attempts"]) == 3
        assert await type(natural)(case.collaboration).process(receipt) == result
        assert len(calls) == 3


@pytest.mark.parametrize("change", ["source", "question", "delivery", "non_status"])
async def test_completed_status_refresh_never_relaxes_source_question_or_effect_binding(poll_case, monkeypatch, change):
    async with poll_case() as case:
        natural, receipt = await setup(case, monkeypatch)
        calls = []
        original = natural._context

        async def context(delivery, scopes):
            result = await original(delivery, scopes)
            if calls and change in {"question", "delivery"}:
                result["candidates"][0]["question_digest" if change == "question" else "delivery"] = "changed"
            return result

        async def classify(message, context, settings):
            calls.append(context)
            case.db["runs"][case.run_id].data["status"] = "failed"
            if change == "source":
                case.state["edited"] = True
            return NaturalDecision(intent="answer" if change == "non_status" else "status", message="Complete reply.")

        monkeypatch.setattr(natural, "_context", context)
        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        result = await natural.process(receipt)
        assert result["status"] == "needs_reconciliation" and len(calls) == 1
        assert result["natural_turn"]["reply"] is None and not case.db["journal"]


async def test_status_refresh_cannot_become_a_product_effect(poll_case, monkeypatch):
    async with poll_case() as case:
        natural, receipt = await setup(case, monkeypatch)
        calls = []

        async def classify(message, context, settings):
            calls.append(context)
            case.db["runs"][case.run_id].data["status"] = "failed"
            return NaturalDecision(intent="status" if len(calls) == 1 else "product", message="Complete reply.")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        result = await natural.process(receipt)
        assert result["status"] == "needs_reconciliation" and len(calls) == 2
        assert result["natural_turn"]["classifier_outcome"] == "confirmed_valid"
        assert result["natural_turn"]["reply"] is None and not case.db["journal"]
        assert case.state["posts"] == case.state["baseline_posts"]


async def test_unknown_initial_provider_request_never_refreshes_despite_native_progress(poll_case, monkeypatch):
    async with poll_case() as case:
        natural, receipt = await setup(case, monkeypatch)
        calls = []

        async def classify(message, context, settings):
            calls.append(context)
            case.db["runs"][case.run_id].data["status"] = "failed"
            raise RuntimeError("Controlled unknown provider request")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        result = await natural.process(receipt)
        assert result["status"] == "needs_reconciliation" and len(calls) == 1
        assert result["natural_turn"]["classifier_outcome"] == "unknown"
        assert result["natural_turn"]["classifier_requests_reserved"] == 1
        assert await type(natural)(case.collaboration).process(receipt) == result
        assert len(calls) == 1 and case.state["posts"] == case.state["baseline_posts"]
