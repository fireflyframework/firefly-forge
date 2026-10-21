# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native comment custody and communication routing; no live model or source control."""

import hashlib

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from test_communication_writer import configured_store
from test_scm_delegated_feedback import RECEIPT, received
from test_scm_delegated_feedback import delegated_feedback as delegated_feedback

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
from firefly_forge.task_routing import TaskRouting, TaskRoutingInput


async def test_communication_route_selects_explicit_bridge_without_implementation_fallback(tmp_path):
    profiles = ModelProfiles(ConfigurationStore(tmp_path))
    profiles.save(
        "project-updates",
        ModelProfileInput(
            name="Project updates",
            provider="bridge",
            values={"endpoint": "http://127.0.0.1:12345/v1", "token": "public-fixture-token"},
        ),
    )
    routing = TaskRouting(profiles)
    await routing.save(TaskRoutingInput(bindings={"communication": "project-updates"}))
    selected = await routing.resolve("communication")
    assert selected["provider"] == "bridge"
    assert selected["task"] == "communication"
    assert await routing.resolve("implementation") is None


async def test_exact_deterministic_body_is_retained_before_comment_transport(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    run.update(status="queued", worker_request={})
    received(row, "status")
    original_write = service._remote_write

    async def checked_write(repo, payload, issue, *, before_write=None):
        retained = row.data["feedback_intent"]
        assert retained["body"] == payload["body"]
        assert retained["body_sha256"] == hashlib.sha256(retained["body"].encode()).hexdigest()
        assert retained["communication"]["mode"] == "deterministic"
        assert retained["communication"]["reason"] == "unconfigured"
        return await original_write(repo, payload, issue, before_write=before_write)

    service._remote_write = checked_write
    await service.process(RECEIPT)
    assert len(sent) == 1
    assert row.data["feedback_status"] == "sent"


@pytest.mark.parametrize("kind", ["issue", "pull_request"])
async def test_ai_body_and_native_fact_profile_evidence_are_retained_before_transport(
    delegated_feedback, tmp_path, kind
):
    service, row, run, sent, _ = delegated_feedback
    configured = await configured_store(tmp_path)
    service.store.task_router = configured.task_router
    run.update(status="queued", worker_request={})
    observed = []

    def model(messages, info):
        observed.append(messages)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "title": {"text": "Execution update", "fact_ids": ["state"]},
                        "paragraphs": [
                            {"text": "Your request is queued. Execution has not started yet.", "fact_ids": ["state"]}
                        ],
                    },
                )
            ]
        )

    service.communication_writer.model = FunctionModel(model)
    original_write = service._remote_write

    async def checked_write(repo, payload, issue, *, before_write=None):
        intent = row.data["feedback_intent"]
        assert intent["body"] == payload["body"]
        assert intent["communication"]["mode"] == "ai"
        assert intent["communication"]["facts"] == {"state": {"status": "queued"}}
        assert intent["communication"]["profile"]["task"] == "communication"
        assert len(intent["communication"]["facts_sha256"]) == 64
        assert len(intent["communication"]["prompt_sha256"]) == 64
        return await original_write(repo, payload, issue, before_write=before_write)

    service._remote_write = checked_write
    received(row, "status")
    row.data["issue"]["kind"] = kind
    await service.process(RECEIPT)
    assert len(observed) == 1 and len(sent) == 1
    assert sent[0]["body"].startswith("### Execution update\n\nYour request is queued.")
    assert "Queued for execution" not in sent[0]["body"]


async def test_lost_ai_comment_response_keeps_exact_body_without_regeneration(delegated_feedback, tmp_path):
    service, row, run, sent, state = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    run.update(status="queued", worker_request={})
    calls = []

    def model(messages, info):
        calls.append(1)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "title": {"text": "Execution update", "fact_ids": ["state"]},
                        "paragraphs": [{"text": "Your request is queued.", "fact_ids": ["state"]}],
                    },
                )
            ]
        )

    service.communication_writer.model = FunctionModel(model)
    state["fail_delivery"] = True
    received(row, "status")
    await service.process(RECEIPT)
    assert row.data["feedback_status"] == "needs_reconciliation"
    original = row.data["feedback_intent"]
    await service.sync_feedback()
    await service.process(RECEIPT)
    assert len(calls) == 1 and len(sent) == 1
    assert row.data["feedback_intent"] == original
    assert original["body"] == sent[0]["body"]


async def test_authority_revoked_during_drafting_prevents_comment(delegated_feedback, tmp_path):
    service, row, run, sent, state = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    run.update(status="queued", worker_request={})

    def model(messages, info):
        state["deny"] = True
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "title": {"text": "Execution update", "fact_ids": ["state"]},
                        "paragraphs": [{"text": "Your request is queued.", "fact_ids": ["state"]}],
                    },
                )
            ]
        )

    service.communication_writer.model = FunctionModel(model)
    received(row, "status")
    await service.process(RECEIPT)
    assert sent == []
    assert row.data["feedback_status"] == "needs_reconciliation"
    assert row.data["feedback_intent"]["body"].startswith("### Execution update\n\nYour request is queued.")


async def test_run_advancing_during_ai_draft_uses_fresh_native_card_without_second_model_call(
    delegated_feedback, tmp_path
):
    service, row, run, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    run.update(status="queued", worker_request={})
    calls = []

    def model(messages, info):
        calls.append(1)
        run.update(status="developing", active_task="implement")
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "title": {"text": "Execution update", "fact_ids": ["state"]},
                        "paragraphs": [
                            {"text": "The request is queued. Execution has not started yet.", "fact_ids": ["state"]}
                        ],
                    },
                )
            ]
        )

    service.communication_writer.model = FunctionModel(model)
    received(row, "status")
    await service.process(RECEIPT)
    assert len(calls) == 1 and len(sent) == 1
    assert "Execution has not started yet" not in sent[0]["body"]
    assert "Implementing the requested change" in sent[0]["body"]
    assert row.data["feedback_status"] == "sent"
    assert row.data["feedback_intent"]["stage"] == "developing"
    assert row.data["feedback_intent"]["communication"]["reason"] == "state_changed"


async def test_changed_communication_profile_blocks_outbound_write_after_draft(delegated_feedback, tmp_path):
    service, row, run, sent, _ = delegated_feedback
    configured = await configured_store(tmp_path)
    service.store.task_router = configured.task_router
    run.update(status="queued", worker_request={})

    def model(messages, info):
        configured.task_router.profiles.save(
            "updates",
            ModelProfileInput(
                name="Revised profile", provider="agentic", values={"model": "openai-responses:fixture-2"}
            ),
        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "title": {"text": "Execution update", "fact_ids": ["state"]},
                        "paragraphs": [{"text": "The request is queued.", "fact_ids": ["state"]}],
                    },
                )
            ]
        )

    service.communication_writer.model = FunctionModel(model)
    received(row, "status")
    await service.process(RECEIPT)
    assert not sent
    assert row.data["feedback_status"] == "needs_reconciliation"
    assert row.data["feedback_intent"]["communication"]["mode"] == "ai"


async def test_question_answered_during_draft_drops_old_question_without_choosing_an_answer(
    delegated_feedback, tmp_path
):
    service, row, run, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    run.update(
        status="paused",
        worker_request={
            "id": "question-1",
            "status": "pending",
            "kind": "design",
            "question": "Which owner?",
            "context": "A product decision is needed",
            "choices": ["One", "Shared"],
        },
    )
    calls = []

    def model(messages, info):
        calls.append(1)
        run["worker_request"]["status"] = "answered"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "title": {"text": "Execution update", "fact_ids": ["state"]},
                        "paragraphs": [
                            {"text": "The recorded run is paused for a decision.", "fact_ids": ["state", "decision"]}
                        ],
                        "decision": {
                            "question": "Which owner?",
                            "context": "A product decision is needed",
                            "choices": ["One", "Shared"],
                        },
                    },
                )
            ]
        )

    service.communication_writer.model = FunctionModel(model)
    received(row, "status")
    await service.process(RECEIPT)
    assert len(calls) == 1 and len(sent) == 1
    assert "Which owner?" not in sent[0]["body"]
    assert "The recorded run is paused for a decision." not in sent[0]["body"]
    assert row.data["feedback_intent"]["communication"]["reason"] == "state_changed"


def test_real_pyfly_api_container_registers_existing_profile_and_routing_contracts_without_startup(tmp_path):
    from firefly_forge.api import FactoryConfiguration, create_runtime
    from firefly_forge.model_profile_controllers import ModelProfileController

    runtime = create_runtime(tmp_path, "public-controlled-bootstrap-token-32-characters", role="api")
    assert runtime.context.container.contains_type(ModelProfileController)
    assert runtime.context.container.contains_type(FactoryConfiguration)


async def test_existing_native_profile_controller_exposes_and_saves_communication_route(tmp_path):
    from types import SimpleNamespace

    from firefly_forge.model_profile_controllers import ModelProfileController

    configured = await configured_store(tmp_path, "bridge")
    router = configured.task_router

    async def admission(_value):
        return None

    controller = ModelProfileController(router.profiles, router, SimpleNamespace(admit_credentials=admission))
    assert "communication" in (await controller.profiles_catalog())[0]["tasks"]
    current = await controller.save_routing(
        TaskRoutingInput(bindings={"communication": "updates", "product": "updates"})
    )
    assert current["bindings"] == {"communication": "updates", "product": "updates"}
    assert controller.task_routing() == current


async def test_reported_blocker_changing_during_draft_discards_stale_explanation(delegated_feedback, tmp_path):
    from test_human_project_updates_host import stopped_run

    service, row, run, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    preserved_id = run["id"]
    run.update(stopped_run(), id=preserved_id)
    calls = []

    def model(messages, info):
        calls.append(1)
        run["error"] = "A different recorded failure"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "title": {"text": "Execution update", "fact_ids": ["state"]},
                        "paragraphs": [
                            {
                                "text": (
                                    "A source-safety guard stopped this execution. "
                                    "Its recorded check batch contains 1 failure."
                                ),
                                "fact_ids": ["state", "blocker", "check_batch"],
                            }
                        ],
                    },
                )
            ]
        )

    service.communication_writer.model = FunctionModel(model)
    received(row, "status")
    await service.process(RECEIPT)
    assert len(calls) == 1 and len(sent) == 1
    assert "A source-safety guard stopped this execution." not in sent[0]["body"]
    assert row.data["feedback_intent"]["communication"]["reason"] == "state_changed"


async def test_invalid_model_output_does_not_publish_a_native_card_from_before_the_run_changed(
    delegated_feedback, tmp_path
):
    service, row, run, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    run.update(status="queued", worker_request={})
    calls = []

    def model(messages, info):
        calls.append(1)
        run.update(status="developing", active_task="implement")
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "title": {"text": "Everything is merged", "fact_ids": ["state"]},
                        "paragraphs": [{"text": "The request is queued.", "fact_ids": ["state"]}],
                    },
                )
            ]
        )

    service.communication_writer.model = FunctionModel(model)
    received(row, "status")
    await service.process(RECEIPT)
    assert len(calls) == 1 and len(sent) == 1
    assert "Implementing the requested change" in sent[0]["body"]
    assert "Queued for execution" not in sent[0]["body"]
    assert row.data["feedback_intent"]["communication"]["reason"] == "state_changed"
