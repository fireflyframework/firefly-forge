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
                        "paragraphs": [
                            {"text": "Your request is queued. Execution has not started yet.", "fact_ids": ["state"]}
                        ]
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
    assert sent[0]["body"].startswith("Your request is queued.")
    assert "Queued for execution" in sent[0]["body"]


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
                    {"paragraphs": [{"text": "Your request is queued.", "fact_ids": ["state"]}]},
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
                    {"paragraphs": [{"text": "Your request is queued.", "fact_ids": ["state"]}]},
                )
            ]
        )

    service.communication_writer.model = FunctionModel(model)
    received(row, "status")
    await service.process(RECEIPT)
    assert sent == []
    assert row.data["feedback_status"] == "needs_reconciliation"
    assert row.data["feedback_intent"]["body"].startswith("Your request is queued.")


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
                        "paragraphs": [
                            {"text": "The request is queued. Execution has not started yet.", "fact_ids": ["state"]}
                        ]
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
                    {"paragraphs": [{"text": "The request is queued.", "fact_ids": ["state"]}]},
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
                        "paragraphs": [
                            {"text": "The recorded run is paused for a decision.", "fact_ids": ["state", "decision"]}
                        ]
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


async def test_human_question_has_recorded_context_choices_and_complete_reply_command(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    run["worker_request"].update(
        kind="product_definition",
        context="The customer has not selected an ownership model.",
        choices=["One owner per account — simpler access rules", "Shared owners — flexible handoff"],
    )
    received(row, "status")
    await service.process(RECEIPT)
    body = sent[0]["body"]
    assert "customer has not selected" in body
    assert "simpler access rules" in body and "flexible handoff" in body
    assert '/forge respond {"run_id":"' + run["id"] + '"' in body
    assert '"request_id":"question-1"' in body
    assert "stays paused" in body
    assert run["worker_request"]["status"] == "pending"
    assert run["status"] == "paused"


async def test_question_links_to_real_authenticated_run_destination_when_channel_reply_disabled(delegated_feedback, monkeypatch):
    service, row, run, sent, _ = delegated_feedback
    original = service.settings
    async def status_only(repository):
        settings = await original(repository)
        return {**settings, "channel_commands": ["status"]}
    service.settings = status_only
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "https://forge.example.test")
    received(row, "status")
    await service.process(RECEIPT)
    assert f"[Open agent questions](https://forge.example.test/?run={run['id']}&view=questions)" in sent[0]["body"]
    assert "/forge respond" not in sent[0]["body"]
    assert "keeps the run paused" in sent[0]["body"]


@pytest.mark.parametrize("origin", ["", "http://localhost:5173", "https://127.0.0.1", "https://forge.example.test/path", "https://user:password@forge.example.test", "https://forge.example.test/?token=private"])
def test_unusable_or_private_origin_never_becomes_a_github_image_or_broken_navigation_link(origin):
    from firefly_forge.collaboration_feedback import feedback_card
    run = {"id": "a" * 32, "status": "paused", "worker_request": {"id": "decision", "status": "pending", "question": "Which owner?", "context": "Choose the customer workflow.", "choices": ["One", "Shared"]}}
    body = feedback_card({"id": "b" * 32, "_feedback_origin": origin}, run, {}, "", lambda value: value)
    assert "[Open agent questions]" not in body
    assert "a" * 32 in body and "administrator" in body
    assert "![" not in body
