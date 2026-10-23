# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual profile snapshots change invocation revision even when routing stays current."""

import pytest
from test_scm_natural_intake_host import prepared
from test_scm_polling import poll_case as poll_case

from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
from firefly_forge.task_routing import TaskRouting, TaskRoutingInput


async def test_actual_model_profile_snapshot_is_rechecked_without_creating_a_new_invocation(poll_case, monkeypatch):
    async with poll_case() as case:
        natural, receipt = await prepared(case, monkeypatch, intent="dialogue")
        profiles = ModelProfiles(case.store.configuration)
        profiles.save(
            "natural",
            ModelProfileInput(
                name="Public natural route",
                provider="agentic",
                values={"model": "openai-responses:public-fixture", "api_key": "owned-controlled-natural-fixture-key"},
            ),
        )
        router = TaskRouting(profiles)
        await router.save(TaskRoutingInput(bindings={"communication": "natural"}))
        case.store.task_router = router
        before = set(profiles.manifests.glob("*.json"))
        result = await natural.process(receipt)
        assert result["status"] == "processed", result.get("reason")
        assert result["natural_turn"]["classifier_outcome"] == "confirmed_valid"
        assert len(set(profiles.manifests.glob("*.json")) - before) == 1
        assert case.state["natural_calls"] == 1


@pytest.mark.parametrize("change", ["profile", "routing"])
async def test_current_route_drift_holds_once_without_reclassification(poll_case, monkeypatch, change):
    async with poll_case() as case:
        natural, receipt = await prepared(case, monkeypatch, intent="dialogue")
        router = case.store.task_router

        def drift():
            if change == "profile":
                router.profiles.save(
                    "natural",
                    ModelProfileInput(
                        name="Changed route",
                        provider="agentic",
                        values={"model": "openai-responses:changed-public-fixture", "api_key": "owned-fixture-key"},
                    ),
                )
            else:
                router.private._write(router.path, {"revision": 99, "bindings": {}})

        case.state["natural_hook"] = drift
        result = await natural.process(receipt)
        assert result["status"] == "needs_reconciliation"
        assert case.state["natural_calls"] == 1
        assert await type(natural)(case.collaboration).process(receipt) == result
        assert case.state["natural_calls"] == 1


@pytest.mark.parametrize("change", ["profile", "routing"])
async def test_retained_feedback_rechecks_actual_route_without_another_snapshot(poll_case, monkeypatch, change):
    async with poll_case() as case:
        natural, receipt = await prepared(case, monkeypatch, intent="dialogue")
        result = await natural.process(receipt)
        router = case.store.task_router
        before = set(router.profiles.manifests.glob("*.json"))
        await natural.authorize_feedback(result, remote=True)
        assert set(router.profiles.manifests.glob("*.json")) == before
        if change == "profile":
            router.profiles.save(
                "natural",
                ModelProfileInput(
                    name="Changed route",
                    provider="agentic",
                    values={"model": "openai-responses:changed-public-fixture", "api_key": "owned-fixture-key"},
                ),
            )
        else:
            await router.save(TaskRoutingInput(bindings={}))
        with pytest.raises(ValueError, match="changed before delivery"):
            await natural.authorize_feedback(result, remote=True)
        assert set(router.profiles.manifests.glob("*.json")) == before
        assert case.state["natural_calls"] == 1


async def test_confirmed_correction_reuses_one_actual_profile_snapshot(poll_case, monkeypatch):
    from firefly_forge.scm_natural_models import NaturalDecision
    from firefly_forge.scm_natural_runtime import ConfirmedInvalidReply

    async with poll_case() as case:
        natural, receipt = await prepared(case, monkeypatch, intent="dialogue")
        router = case.store.task_router
        before = set(router.profiles.manifests.glob("*.json"))
        calls = []

        async def classify(message, context, settings):
            calls.append(settings.copy())
            if len(calls) == 1:
                raise ConfirmedInvalidReply([{"type": "literal_error", "loc": ["intent"]}])
            return NaturalDecision(intent="dialogue", message="A complete controlled response.")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        result = await natural.process(receipt)
        assert result["status"] == "processed"
        assert result["natural_turn"]["classifier_requests_reserved"] == 2
        assert len(calls) == 2 and calls[0] == calls[1]
        assert len(set(router.profiles.manifests.glob("*.json")) - before) == 1
