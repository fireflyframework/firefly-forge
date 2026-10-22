# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native comment custody and communication routing; no live model or source control."""

import hashlib

import pytest
from communication_model_fixture import reviewed_model
from pydantic_ai.messages import ModelResponse, ToolCallPart
from test_communication_writer import configured_store
from test_scm_delegated_feedback import RECEIPT, received
from test_scm_delegated_feedback import delegated_feedback as delegated_feedback
from test_scm_delegated_feedback import signed_reply_binding as signed_reply_binding
from test_scm_polling import poll_case as poll_case

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

    service.communication_writer.model = reviewed_model(model)
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

    service.communication_writer.model = reviewed_model(model)
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
    calls = []

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

    service.communication_writer.model = reviewed_model(model, calls=calls)
    received(row, "status")
    await service.process(RECEIPT)
    assert sent == []
    assert row.data["feedback_status"] == "needs_reconciliation"
    assert row.data["feedback_intent"] is None
    assert [phase for phase, *_ in calls] == ["draft", "review"]
    await service.feedback(RECEIPT)
    await service.sync_feedback()
    assert not sent and len(calls) == 2 and row.data["feedback_intent"] is None


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

    service.communication_writer.model = reviewed_model(model)
    received(row, "status")
    await service.process(RECEIPT)
    assert len(calls) == 1 and not sent
    assert row.data["feedback_status"] == "held"
    assert row.data["feedback_intent"]["stage"] == "developing"
    assert row.data["feedback_intent"]["communication"]["reason"] == "state_changed"
    assert row.data["feedback_status"] == "held" and row.data["feedback_intent"]["body"] == ""
    assert row.data["reason"] == "ai_communication_state_changed"
    await service.feedback(RECEIPT)
    await service.sync_feedback()
    assert len(calls) == 1 and not sent


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

    service.communication_writer.model = reviewed_model(model)
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

    service.communication_writer.model = reviewed_model(model)
    received(row, "status")
    await service.process(RECEIPT)
    assert len(calls) == 1 and not sent
    assert row.data["feedback_intent"]["communication"]["reason"] == "state_changed"
    assert row.data["feedback_status"] == "held" and row.data["feedback_intent"]["body"] == ""
    assert row.data["reason"] == "ai_communication_state_changed"
    await service.feedback(RECEIPT)
    await service.sync_feedback()
    assert len(calls) == 1 and not sent


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

    service.communication_writer.model = reviewed_model(model)
    received(row, "status")
    await service.process(RECEIPT)
    assert len(calls) == 1 and not sent
    assert row.data["feedback_intent"]["communication"]["reason"] == "state_changed"
    assert row.data["feedback_status"] == "held" and row.data["feedback_intent"]["body"] == ""
    assert row.data["reason"] == "ai_communication_state_changed"
    await service.feedback(RECEIPT)
    await service.sync_feedback()
    assert len(calls) == 1 and not sent


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
                        "title": {"text": "See @everyone", "fact_ids": ["state"]},
                        "paragraphs": [{"text": "The request is queued.", "fact_ids": ["state"]}],
                    },
                )
            ]
        )

    service.communication_writer.model = reviewed_model(model)
    received(row, "status")
    await service.process(RECEIPT)
    assert len(calls) == 1 and not sent
    assert row.data["feedback_intent"]["communication"]["reason"] == "state_changed"
    assert row.data["feedback_status"] == "held" and row.data["feedback_intent"]["body"] == ""
    assert row.data["reason"] == "ai_communication_state_changed"
    await service.feedback(RECEIPT)
    await service.sync_feedback()
    assert len(calls) == 1 and not sent


async def test_configured_ai_failure_is_retained_without_write_token_or_automatic_retry(delegated_feedback, tmp_path):
    from firefly_forge.communication import communication_facts
    from firefly_forge.product_provenance import content_digest

    service, row, run, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    run.update(status="queued", worker_request={})
    calls = []
    tokens = []

    async def held_writer(current, native, safe):
        calls.append(1)
        facts = communication_facts(current)
        return {
            "body": "",
            "communication": {"mode": "held", "reason": "quota", "facts": facts, "facts_sha256": content_digest(facts)},
        }

    async def forbidden_token(*args, **kwargs):
        tokens.append(1)
        pytest.fail("Held AI feedback must not prepare a write token")

    service.communication_writer.write = held_writer
    service.scm.issue_authentication = forbidden_token
    received(row, "status")
    await service.process(RECEIPT)
    assert row.data["feedback_status"] == "held" and row.data["reason"] == "ai_communication_quota"
    assert row.data["feedback_intent"]["body"] == "" and not sent and not tokens
    retained = row.data["feedback_intent"]
    await service.feedback(RECEIPT)
    await service.sync_feedback()
    await service.process(RECEIPT)
    assert row.data["feedback_intent"] == retained and len(calls) == 1 and not tokens and not sent


async def test_configured_ai_state_change_is_held_instead_of_posting_a_native_fallback(delegated_feedback, tmp_path):
    from firefly_forge.communication import communication_facts
    from firefly_forge.product_provenance import content_digest

    service, row, run, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    run.update(status="queued", worker_request={})
    calls = []
    tokens = []

    async def changed_writer(current, native, safe):
        calls.append(1)
        facts = communication_facts(current)
        run.update(status="developing", active_task="implement")
        return {
            "body": "Old AI wording",
            "communication": {
                "mode": "ai",
                "reason": "generated",
                "facts": facts,
                "facts_sha256": content_digest(facts),
            },
        }

    async def forbidden_token(*args, **kwargs):
        tokens.append(1)
        pytest.fail("Changed configured AI facts must not prepare a write token")

    service.communication_writer.write = changed_writer
    service.scm.issue_authentication = forbidden_token
    received(row, "status")
    await service.process(RECEIPT)
    assert row.data["feedback_status"] == "held" and row.data["reason"] == "ai_communication_state_changed"
    assert row.data["feedback_intent"]["body"] == "" and row.data["feedback_intent"]["stage"] == "developing"
    await service.feedback(RECEIPT)
    await service.sync_feedback()
    assert len(calls) == 1 and not sent and not tokens


async def test_new_authenticated_status_receipt_can_request_ai_after_a_held_narrative(poll_case, tmp_path):
    from pydantic_ai.models.function import FunctionModel

    async with poll_case() as c:
        await c.ready()
        await c.consent()
        c.db["runs"][c.run_id].data.update(status="developing", worker_request={})
        c.store.task_router = (await configured_store(tmp_path)).task_router
        failed_calls = []

        def unavailable(messages, info):
            failed_calls.append(1)
            raise TimeoutError()

        c.collaboration.communication_writer.model = FunctionModel(unavailable)
        first = await c.collaboration.process((await c.new_command("status", 20))["id"])
        assert first["feedback_status"] == "held" and not c.state["posts"]
        await c.collaboration.sync_feedback()
        assert len(failed_calls) == 1
        calls = []
        c.collaboration.communication_writer.model = reviewed_model(
            {
                "title": {"text": "Execution update", "fact_ids": ["state"]},
                "paragraphs": [{"text": "The work remains in development.", "fact_ids": ["state"]}],
            },
            calls=calls,
        )
        second = await c.collaboration.process((await c.new_command("status", 21))["id"])
        assert second["id"] != first["id"] and second["feedback_status"] == "sent"
        assert len(c.state["posts"]) == 1 and [phase for phase, _, _ in calls] == ["draft", "review"]
        assert (await c.collaboration.receipt(first["id"]))["feedback_status"] == "held"


async def test_human_question_has_recorded_context_choices_and_complete_reply_command(
    delegated_feedback, signed_reply_binding
):
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


async def test_question_links_to_real_authenticated_run_destination_when_channel_reply_disabled(
    delegated_feedback, monkeypatch
):
    service, row, run, sent, _ = delegated_feedback
    original = service.settings

    async def status_only(repository):
        settings = await original(repository)
        return {**settings, "channel_commands": ["status"]}

    service.settings = status_only
    service.browser_origin = "https://forge.example.test"
    received(row, "status")
    await service.process(RECEIPT)
    assert f"[Open agent questions](https://forge.example.test/?run={run['id']}&view=questions)" in sent[0]["body"]
    assert "/forge respond" not in sent[0]["body"]
    assert "keeps the run paused" in sent[0]["body"]


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "http://localhost:5173",
        "https://127.0.0.1",
        "https://forge.example.test/path",
        "https://user:password@forge.example.test",
        "https://forge.example.test/?token=private",
        "https://forge.example.test:broken",
        "https://[invalid",
        "https://forge.example.test\n",
    ],
)
def test_unusable_or_private_origin_never_becomes_a_github_image_or_broken_navigation_link(origin):
    from firefly_forge.collaboration_feedback import feedback_card

    run = {
        "id": "a" * 32,
        "status": "paused",
        "worker_request": {
            "id": "decision",
            "status": "pending",
            "question": "Which owner?",
            "context": "Choose the customer workflow.",
            "choices": ["One", "Shared"],
        },
    }
    body = feedback_card({"id": "b" * 32, "_feedback_origin": origin}, run, {}, "", lambda value: value)
    assert "[Open agent questions]" not in body
    assert "a" * 32 in body and "administrator" in body
    assert "![" not in body


async def test_reply_permission_removed_during_draft_never_advertises_an_unusable_answer_command(
    delegated_feedback, tmp_path, signed_reply_binding
):
    service, row, run, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    run["worker_request"].update(kind="design", context="A product decision is needed", choices=["One", "Shared"])
    original = service.settings
    permission = {"enabled": True}
    calls = []

    async def settings(repository):
        value = await original(repository)
        return {**value, "channel_commands": ["respond", "status"] if permission["enabled"] else ["status"]}

    service.settings = settings

    def model(messages, info):
        permission["enabled"] = False
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
                            "question": run["worker_request"]["question"],
                            "context": "A product decision is needed",
                            "choices": ["One", "Shared"],
                        },
                    },
                )
            ]
        )

    service.communication_writer.model = reviewed_model(model, calls=calls)
    received(row, "status")
    await service.process(RECEIPT)
    assert [phase for phase, *_ in calls] == ["draft", "review"]
    assert not sent
    assert row.data["feedback_status"] == "held"
    assert row.data["feedback_intent"]["body"] == ""
    assert row.data["feedback_intent"]["communication"]["reason"] == "channel_changed"
    assert row.data["reason"] == "ai_communication_channel_changed"
    await service.feedback(RECEIPT)
    await service.sync_feedback()
    assert len(calls) == 2 and not sent


@pytest.mark.parametrize("transport", ["github_rest_poll_v1", "signed_webhook_v1"])
async def test_native_membership_downgrade_during_draft_holds_reply_without_post_or_repeat(
    delegated_feedback, tmp_path, monkeypatch, signed_reply_binding, transport
):
    import json
    from types import SimpleNamespace
    from uuid import UUID

    from pydantic_ai.messages import TextPart

    from firefly_forge.access_persistence import ForgeMembershipRepository
    from firefly_forge.scm_actor_authority import SCMActorAuthority

    service, row, run, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    role = {"value": "developer"}
    run["worker_request"].update(kind="design", context="A human decision is needed", choices=[])
    calls = []

    async def memberships(_repository, user_id):
        assert user_id.hex == "6" * 32
        return [SimpleNamespace(project_id=UUID(run["project_id"]), role=role["value"], revision=1)]

    async def head(*args):
        return await SCMActorAuthority(service.store, service.scm)._head(*args)

    monkeypatch.setattr(ForgeMembershipRepository, "for_user", memberships)

    async def authorize(delivery):
        assert delivery["id"] == RECEIPT and delivery["command"]["action"] == "status"

    if transport == "github_rest_poll_v1":
        service.polling = SimpleNamespace(_head=head, authorize_feedback=authorize, authorize_feedback_local=authorize)

    def draft(messages, info):
        role["value"] = "viewer"
        payload = {
            "title": {"text": "Execution update", "fact_ids": ["state"]},
            "paragraphs": [{"text": "The run is paused for your decision.", "fact_ids": ["state", "decision"]}],
            "decision": {
                "question": run["worker_request"]["question"],
                "context": run["worker_request"]["context"],
                "choices": [],
            },
        }
        part = ToolCallPart(info.output_tools[0].name, payload) if info.output_tools else TextPart(json.dumps(payload))
        return ModelResponse(parts=[part])

    service.communication_writer.model = reviewed_model(draft, calls=calls)
    received(row, "status")
    row.data["verification"] = {"transport": transport}
    await service.process(RECEIPT)
    assert [phase for phase, *_ in calls] == ["draft", "review"]
    assert not sent
    assert row.data["feedback_status"] == "held"
    assert row.data["feedback_intent"]["body"] == ""
    assert row.data["feedback_intent"]["communication"]["reason"] == "channel_changed"
    await service.feedback(RECEIPT)
    await service.sync_feedback()
    assert len(calls) == 2 and not sent


@pytest.mark.parametrize("transport", ["github_rest_poll_v1", "signed_webhook_v1"])
@pytest.mark.parametrize(
    "case,expected",
    [
        ("status_only", []),
        ("respond", ["respond"]),
        ("revoked", []),
        ("changed_binding", []),
        ("channel_disabled", []),
        ("native_viewer", []),
        ("native_reviewer", []),
        ("native_other_project", []),
        ("native_suspended", []),
    ],
)
async def test_question_reply_affordance_requires_exact_current_binding_and_channel(
    delegated_feedback, monkeypatch, case, expected, transport
):
    from types import SimpleNamespace
    from uuid import UUID

    from firefly_forge.access_persistence import ForgeMembershipRepository
    from firefly_forge.identity import Principal
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.scm_actor_authority import SCMActorAuthority

    service, row, run, _, _ = delegated_feedback
    original = service.settings

    async def memberships(_repository, user_id):
        assert user_id.hex == "6" * 32
        if case == "native_other_project":
            return [SimpleNamespace(project_id=UUID("9" * 32), role="developer", revision=1)]
        role = {"native_viewer": "viewer", "native_reviewer": "reviewer"}.get(case, "developer")
        return [SimpleNamespace(project_id=UUID(run["project_id"]), role=role, revision=1)]

    async def identity(_repository, user_id):
        assert user_id.hex == "6" * 32
        return SimpleNamespace(enabled=case != "native_suspended")

    monkeypatch.setattr(ForgeMembershipRepository, "for_user", memberships)
    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)

    async def settings(repository):
        value = await original(repository)
        return {**value, "channel_commands": ["status"] if case == "channel_disabled" else ["status", "respond"]}

    async def resolve(_authority, delivery):
        assert delivery["id"] == RECEIPT
        return Principal(
            UUID("6" * 32),
            "owned-host",
            "operator",
            authentication_method="scm_mapping",
            scm_delegation={"binding_key": "current-key", "binding_digest": "a" * 64},
        )

    async def head(project, repository, key):
        assert (project, repository, key) == (run["project_id"], run["repository_id"], "current-key")
        return {
            "digest": "b" * 64 if case == "changed_binding" else "a" * 64,
            "request": {
                "active": case != "revoked",
                "allowed_actions": ["status"] if case == "status_only" else ["status", "respond"],
            },
        }

    async def signed_head(_authority, *args):
        return await head(*args)

    service.settings = settings
    service.polling = SimpleNamespace(_head=head)
    monkeypatch.setattr(SCMActorAuthority, "resolve", resolve)
    monkeypatch.setattr(SCMActorAuthority, "_head", signed_head)
    received(row, "status")
    row.data["verification"] = {"transport": transport}
    context = await service._feedback_context(row.data, run)
    assert context["_feedback_actions"] == expected
    assert "resume" not in context["_feedback_actions"]


async def test_reply_context_refresh_uses_retained_poll_observation_without_presentation_fields(
    delegated_feedback, monkeypatch, signed_reply_binding
):
    from types import SimpleNamespace
    from uuid import UUID

    from firefly_forge.identity import Principal
    from firefly_forge.scm_actor_authority import SCMActorAuthority

    service, row, run, _, _ = delegated_feedback
    received(row, "status")
    row.data["verification"] = {"transport": "github_rest_poll_v1"}
    observed = []

    async def resolve(_authority, delivery):
        assert delivery == await service.receipt(RECEIPT)
        assert "_feedback_actions" not in delivery and "_feedback_origin" not in delivery
        observed.append(delivery)
        return Principal(
            UUID("6" * 32),
            "owned-host",
            "operator",
            authentication_method="scm_mapping",
            scm_delegation={"binding_key": "current-key", "binding_digest": "a" * 64},
        )

    async def head(*_args):
        return {"digest": "a" * 64, "request": {"active": True, "allowed_actions": ["status", "respond"]}}

    service.polling = SimpleNamespace(_head=head)
    monkeypatch.setattr(SCMActorAuthority, "resolve", resolve)
    data = {**row.data, "_feedback_actions": ["respond"], "_feedback_origin": "https://forge.example.test"}
    assert (await service._feedback_context(data, run))["_feedback_actions"] == ["respond"]
    assert len(observed) == 1


async def test_addressed_read_context_never_creates_execution_authority_or_offers_answer_actions(
    delegated_feedback, monkeypatch
):
    from firefly_forge.scm_actor_authority import SCMActorAuthority

    service, row, run, _, _ = delegated_feedback
    received(row, "status")
    row.data["command"]["adapter"] = "addressed_status_v1"
    row.data["verification"] = {"transport": "github_rest_poll_v1"}
    service.browser_origin = "https://forge.example.test"

    async def forbidden(*_args):
        pytest.fail("A read-only conversation must not acquire an execution principal")

    monkeypatch.setattr(SCMActorAuthority, "resolve", forbidden)
    context = await service._feedback_context(row.data, run)
    assert context == {"_feedback_origin": "https://forge.example.test", "_feedback_actions": []}


async def test_origin_changed_during_authentication_holds_exact_intent_without_post_or_regeneration(
    delegated_feedback, tmp_path
):
    service, row, run, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    run.update(status="queued", worker_request={})
    service.browser_origin = "https://forge.example.test"
    calls = []
    service.communication_writer.model = reviewed_model(
        {
            "title": {"text": "Execution update", "fact_ids": ["state"]},
            "paragraphs": [{"text": "Your request is queued.", "fact_ids": ["state"]}],
        },
        calls=calls,
    )
    original_authentication = service.scm.issue_authentication
    original_write = service._remote_write
    denials = []

    async def checked_write(*args, **kwargs):
        from firefly_forge.project_access import AccessDenied

        try:
            return await original_write(*args, **kwargs)
        except AccessDenied as error:
            denials.append(str(error))
            raise

    service._remote_write = checked_write

    async def authentication(*args, **kwargs):
        result = await original_authentication(*args, **kwargs)
        service.browser_origin = "https://revised.example.test"
        return result

    service.scm.issue_authentication = authentication
    received(row, "status")
    await service.process(RECEIPT)
    assert not sent
    assert denials == ["The feedback destination or reply permission changed before publication"]
    assert row.data["feedback_status"] == "needs_reconciliation"
    intent = row.data["feedback_intent"]
    assert intent["body"].startswith("### Execution update\n\nYour request is queued.")
    assert intent["body_sha256"] == hashlib.sha256(intent["body"].encode()).hexdigest()
    assert [phase for phase, *_ in calls] == ["draft", "review"]
    await service.feedback(RECEIPT)
    await service.sync_feedback()
    assert row.data["feedback_intent"] == intent and not sent and len(calls) == 2


@pytest.mark.parametrize("transport", ["github_rest_poll_v1", "signed_webhook_v1"])
async def test_separate_resume_authority_does_not_borrow_legacy_answer_affordance(
    delegated_feedback, monkeypatch, transport
):
    from firefly_forge.scm_actor_authority import SCMActorAuthority

    service, row, run, _, _ = delegated_feedback
    received(row, "status")
    row.data["command"].update(version=2, action="resume")
    row.data["verification"] = {"transport": transport}
    service.browser_origin = "https://forge.example.test"

    async def forbidden(*_args):
        pytest.fail("Separate continuation authority must not resolve a legacy execution mapping")

    monkeypatch.setattr(SCMActorAuthority, "resolve", forbidden)
    assert await service._feedback_context(row.data, run) == {
        "_feedback_origin": "https://forge.example.test",
        "_feedback_actions": [],
    }


async def test_nonproject_legacy_reply_context_preserves_repository_channel_settings(delegated_feedback):
    service, row, run, _, _ = delegated_feedback
    received(row, "status")
    run.pop("project_id")
    context = await service._feedback_context(row.data, run)
    assert context["_feedback_actions"] == ["respond", "cancel"]
