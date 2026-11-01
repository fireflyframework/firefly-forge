# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Verified polling and persisted consent use a controlled native queue adapter, no live effects."""

import copy
import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from communication_model_fixture import reviewed_model
from linked_verification_fixtures import candidate as wire_candidate
from linked_verification_fixtures import receipt as wire_receipt
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from test_scm_natural_intake_host import prepared
from test_scm_polling import poll_case as poll_case

from firefly_forge.execution_requests import digest as request_digest
from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import LinkedVerificationCandidateV1, LinkedVerificationReceiptV1
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_linked_verification import SCMLinkedVerifications
from firefly_forge.scm_linked_verification_records import SCMVerificationRecordV1
from firefly_forge.scm_natural_models import NaturalDecision


async def start(c, monkeypatch):
    original_comment = c.comment
    c.comment = lambda number=10, body="Existing discussion": original_comment(
        number, "@forge, revisa cómo ejecutar las siete comprobaciones nuevas sin programar." if number == 99 else body
    )
    natural, receipt = await prepared(c, monkeypatch, intent="dialogue")
    import firefly_forge.scm_linked_verification as implementation
    import firefly_forge.scm_natural_tasks as tasks

    user = c.db["users"][c.user_id]
    user.api_roles = ["forge-admin"]
    user.api_roles_audience = "host-test-api"
    user.api_roles_issued_at = 1.0
    user.api_roles_expires_at = datetime.now(UTC) + timedelta(hours=1)
    c.store.execution_ownership_token = object()
    c.store.credential_registry = lambda: {}
    unit = object()
    monkeypatch.setattr(implementation, "current_unit_of_work", lambda: unit if c.state["depth"] else None)
    c.state.update(native_requests=[], native_status=None, native_unknown=False, candidate_calls=0)

    class NativeQueue:
        async def candidate(self, run_id):
            actor = CURRENT_PRINCIPAL.get()
            assert actor.authentication_method == "scm_mapping"
            await authority.review(actor, run_id)
            await ProjectAccess(c.store).authorize_actor(ProjectAccess.actor_snapshot(actor), c.project, "execute")
            c.state["candidate_calls"] += 1
            value = wire_candidate(effect_budget=7)
            value.update(
                run_id=run_id,
                actor_digest=digest(
                    {
                        "actor": ProjectAccess.actor_snapshot(actor),
                        "identity": {"issuer": actor.issuer, "subject": actor.subject},
                    }
                ),
            )
            value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
            return value

        async def request(self, run_id, body):
            actor = CURRENT_PRINCIPAL.get()
            record = SCMVerificationRecordV1.model_validate(c.db["inbox"][body.request_id].data["natural_verification"])
            grant = await authority.admit(actor, run_id, record.review.candidate, body)
            async with c.store._unit():
                await grant.current()
                grant.fence()
                c.state["native_requests"].append(body.model_dump(mode="json"))
                value = wire_receipt(record.review.candidate.model_dump(mode="json"))
                value.update(
                    request_id=body.request_id,
                    request=body.model_dump(mode="json"),
                    request_digest=request_digest(body.model_dump(mode="json")),
                )
                c.state["native_status"] = LinkedVerificationReceiptV1.model_validate(value).model_dump(mode="json")
            if c.state["native_unknown"]:
                raise OSError("Controlled response lost after native admission")
            return copy.deepcopy(c.state["native_status"])

        async def get(self, run_id, request_id):
            await authority.review(CURRENT_PRINCIPAL.get(), run_id)
            if c.state["native_status"] is None:
                raise KeyError("No operation result yet")
            assert request_id == c.state["native_status"]["request_id"]
            return copy.deepcopy(c.state["native_status"])

    authority = SCMLinkedVerifications(c.collaboration, NativeQueue())
    c.collaboration.linked_verification_authority = authority
    c.store.scm_linked_verification_authority = authority

    async def classify(message, context, settings):
        c.state["natural_calls"] += 1
        if "siete comprobaciones" in message:
            return NaturalDecision(
                intent="verify",
                message="Prepararé la revisión de esas comprobaciones.",
                language="es",
                verification_consent="request",
            )
        assert message == "sí, ejecuta esas comprobaciones"
        return NaturalDecision(
            intent="verify",
            message="He recibido tu confirmación.",
            language="es",
            verification_consent="approve",
            verification_affirmative_span=message,
        )

    monkeypatch.setattr(tasks, "classify", classify)

    def draft(messages, info):
        prompt = next(part.content for m in reversed(messages) for part in m.parts if isinstance(part, UserPromptPart))
        prefix = "Draft the project update using only this recorded fact packet:\n"
        facts, _ = json.JSONDecoder().raw_decode(prompt.removeprefix(prefix))
        assert "linked_verification" in facts and "factory_verification" not in facts
        keys = sorted(facts)
        c.state.setdefault("writer_facts", []).append(facts)
        card = dict(
            title={"text": "Comprobaciones de la fuente conservada", "fact_ids": ["linked_verification"]},
            paragraphs=[
                {
                    "text": "Esta operación conserva el trabajo anterior y no permite programar.",
                    "fact_ids": keys[n : n + 4],
                }
                for n in range(0, len(keys), 4)
            ],
            decision=None,
        )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, card)])

    c.collaboration.communication_writer.model = reviewed_model(draft)
    result = await c.collaboration.process(receipt)
    assert result["natural_turn"]["state"] == "verification_review", result
    assert result["feedback_status"] == "sent", result.get("reason")
    assert not c.state["native_requests"]
    bot = c.comment(101, result["feedback_intent"]["body"])
    bot["user"]["type"] = "Bot"
    c.state["comments"].append(bot)
    return authority, receipt


async def answer(c, *, addressed=False):
    c.state["clock"] += 3600
    c.state["comments"].append(c.comment(102, ("@forge, " if addressed else "") + "sí, ejecuta esas comprobaciones"))
    for _ in range(3):
        await c.service.step(c.repository)
    return next(row.data["id"] for row in c.db["inbox"].values() if row.source_key == "comment:102")


async def test_real_owner_plain_reply_queues_once_and_status_preserves_independent_budget(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        receipt = await answer(c)
        result = await c.collaboration.process(receipt)
        assert result["natural_turn"]["state"] == "verification_current", result
        assert result["feedback_status"] == "sent", result.get("reason")
        assert len(c.state["native_requests"]) == 1
        request = c.state["native_requests"][0]
        assert request["request_id"] == proposal and all(
            request[k] is True for k in request if k.startswith("acknowledge")
        )
        record = SCMVerificationRecordV1.model_validate(c.db["inbox"][proposal].data["natural_verification"])
        assert record.grant.confirmation.human_message == "sí, ejecuta esas comprobaciones"
        assert record.review.candidate.effect_budget == 7
        assert (record.review.candidate.original_effect_budget, record.review.candidate.original_effect_count) == (
            22,
            18,
        )
        await c.collaboration.process(receipt)
        await authority.confirm(result, proposal)
        assert len(c.state["native_requests"]) == 1
        actor = await authority.actor(record)
        assert ProjectAccess.actor_snapshot(actor)["global_admin"] is False and actor.api_audience is None
        assert c.state["writer_facts"][-1]["linked_verification"]["progress"]["completed"] == 0


async def test_lost_native_response_uses_same_request_readback_without_another_post(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        c.state["native_unknown"] = True
        receipt = await answer(c)
        result = await c.collaboration.process(receipt)
        assert result["natural_turn"]["state"] == "verification_current"
        await authority.confirm(result, proposal)
        assert len(c.state["native_requests"]) == 1


@pytest.mark.parametrize("change", ["owner", "admin", "execute", "publication"])
async def test_changed_owner_authority_or_publication_never_admits_operation(poll_case, monkeypatch, change):  # noqa: F811
    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        receipt = await answer(c)
        if change == "owner":
            c.db["inbox"][receipt].data["actor"] = "8"
        elif change == "admin":
            c.db["users"][c.user_id].api_roles = []
        elif change == "execute":
            c.db["memberships"]["member"].role = "viewer"
        else:
            c.db["inbox"][proposal].data["feedback_status"] = "needs_reconciliation"
        with suppress(AccessDenied, ValueError):
            await c.collaboration.process(receipt)
        assert not c.state["native_requests"]


async def test_unknown_before_admission_is_read_only_on_repeated_confirmation(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        calls = []

        async def unknown(run_id, body):
            calls.append(body.request_id)
            raise OSError("Controlled response unknown before native record becomes readable")

        monkeypatch.setattr(authority.verifications, "request", unknown)
        receipt = await answer(c)
        result = await c.collaboration.process(receipt)
        assert result["natural_turn"]["state"] == "verification_unknown"
        assert result["feedback_status"] == "sent"
        await authority.confirm(result, proposal)
        assert calls == [proposal]
        assert c.state["writer_facts"][-1]["state"]["status"] == "unknown"


async def test_prepared_grant_binds_active_writer_not_incidental_task_and_revalidates_revocation(
    poll_case, monkeypatch
):  # noqa: F811
    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        result = await c.collaboration.process(await answer(c))
        record = SCMVerificationRecordV1.model_validate(c.db["inbox"][proposal].data["natural_verification"])
        actor = await authority.actor(record)
        grant = await authority.admit(actor, c.run_id, record.review.candidate, record.grant.request)
        grant.fence()
        async with c.store._unit():
            with pytest.raises(AccessDenied):
                grant.fence()
            await grant.current()
            grant.fence()
            # A shielded settlement Task still owns this same native UoW.
            import asyncio

            async def settled():
                grant.fence()

            await asyncio.create_task(settled())
        c.db["users"][c.user_id].api_roles = []
        with pytest.raises(AccessDenied):
            await grant.current()
        assert result["natural_turn"]["state"] == "verification_current"


async def test_record_tamper_and_foreign_candidate_cannot_reuse_owner_confirmation(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        result = await c.collaboration.process(await answer(c))
        raw = copy.deepcopy(c.db["inbox"][proposal].data["natural_verification"])
        for section, key, value in [
            ("base", "run_id", "f" * 32),
            ("review", "candidate", wire_candidate()),
            ("grant", "review_digest", "0" * 64),
        ]:
            changed = copy.deepcopy(raw)
            changed[section][key] = value
            with pytest.raises(ValueError):
                SCMVerificationRecordV1.model_validate(changed)
        record = SCMVerificationRecordV1.model_validate(raw)
        actor = await authority.actor(record)
        changed = record.review.candidate.model_dump()
        changed["effect_budget"] += 1
        changed["digest"] = digest({k: v for k, v in changed.items() if k != "digest"})
        with pytest.raises(AccessDenied):
            await authority.admit(
                actor, c.run_id, LinkedVerificationCandidateV1.model_validate(changed), record.grant.request
            )
        assert result["feedback_status"] == "sent"


async def test_revoked_current_mapping_refuses_old_review_and_never_creates_operation(poll_case, monkeypatch):  # noqa: F811
    from uuid import uuid4

    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        receipt = await answer(c)
        request = c.state["consent_request"]
        prior = await c.service.binding(c.project, c.repository, request.request_id)
        revoke = c.models.SCMPollActorBindingInputV2.model_validate(
            {
                **request.model_dump(),
                "request_id": uuid4().hex,
                "expected_previous_digest": prior["digest"],
                "active": False,
            }
        )
        await c.service.configure_binding(c.project, c.repository, revoke)
        result = await c.collaboration.process(receipt)
        assert result["status"] == "denied" and not c.state["native_requests"]
        record = SCMVerificationRecordV1.model_validate(c.db["inbox"][proposal].data["natural_verification"])
        with pytest.raises(AccessDenied):
            await authority.review(await authority.actor(record), c.run_id)


async def test_new_pending_question_after_classification_prevents_grant(poll_case, monkeypatch):  # noqa: F811
    from firefly_forge.scm_natural_tasks import SCMNaturalTasks

    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        receipt = await answer(c, addressed=True)
        delivery = await SCMNaturalTasks(c.collaboration).process(receipt)
        original = SCMNaturalTasks._context

        async def changed_context(self, observed, scopes):
            context = await original(self, observed, scopes)
            return {
                **context,
                "candidates": [
                    *context["candidates"],
                    {
                        **context["scope"],
                        "origin": "product_assistant_turn",
                        "run_id": None,
                        "question_id": "product-" + "f" * 32,
                        "question_digest": "e" * 64,
                        "question": "Should existing contacts be retained?",
                        "question_status": "pending",
                        "consumed": False,
                    },
                ],
            }

        monkeypatch.setattr(SCMNaturalTasks, "_context", changed_context)
        with pytest.raises(AccessDenied):
            await authority.confirm(delivery, proposal)
        assert not c.state["native_requests"]
        assert c.db["inbox"][proposal].data["natural_verification"]["grant"] is None


async def test_current_grant_rejects_changed_intake_context_without_replaying(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        receipt = await answer(c)
        await c.collaboration.process(receipt)
        record = SCMVerificationRecordV1.model_validate(c.db["inbox"][proposal].data["natural_verification"])
        actor = await authority.actor(record)
        c.db["inbox"][receipt].data["natural_turn"]["context"]["history"].append(
            {"human": "Changed context", "reply": None}
        )
        with pytest.raises(AccessDenied):
            await authority.admit(actor, c.run_id, record.review.candidate, record.grant.request)
        assert len(c.state["native_requests"]) == 1


async def test_queue_then_interrupted_marker_uses_durable_grant_readback(poll_case, monkeypatch):  # noqa: F811
    from firefly_forge.scm_natural_tasks import SCMNaturalTasks

    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        receipt = await answer(c)
        delivery = await SCMNaturalTasks(c.collaboration).process(receipt)
        queued = await authority.confirm(delivery, proposal)
        assert queued.status == "queued"
        interrupted = await c.collaboration.receipt(receipt)
        assert "linked_verification_proposal" not in interrupted["natural_turn"]
        packet = await authority.communication(interrupted)
        assert packet is not None and packet.receipt.status == "queued"
        assert packet.receipt.request_id == proposal
        await authority.apply(receipt)
        assert len(c.state["native_requests"]) == 1


async def test_confirmation_before_admission_has_typed_unknown_not_parent_facts(poll_case, monkeypatch):  # noqa: F811
    from firefly_forge.communication import CommunicationConversationV1, communication_facts
    from firefly_forge.scm_natural_tasks import SCMNaturalTasks

    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        delivery = await SCMNaturalTasks(c.collaboration).process(await answer(c))
        packet = await authority.communication(delivery)
        assert packet is not None and packet.phase == "unknown"
        facts = communication_facts(
            c.db["runs"][c.run_id].data,
            conversation=CommunicationConversationV1(selection="run", language="es", linked_runs=1),
            linked_verification=packet,
        )
        assert facts["linked_verification"]["request_id"] == proposal
        assert "factory_verification" not in facts and not c.state["native_requests"]


async def test_multiple_current_operations_use_explicit_typed_hold(poll_case, monkeypatch):  # noqa: F811
    from firefly_forge.communication import CommunicationConversationV1, communication_facts

    async with poll_case() as c:
        authority, proposal = await start(c, monkeypatch)
        row = c.db["inbox"][proposal]
        row.data["natural_turn"]["resolution"].update(state="status", run_id=c.run_id)
        row.data["natural_turn"]["context"]["linked_verifications"] = [
            {"proposal_id": value * 32, "run_id": c.run_id, "confirmed": True} for value in ("a", "b")
        ]
        result = await authority.apply(proposal)
        packet = await authority.communication(result)
        assert packet.phase == "held" and packet.hold_reason == "operation_selection_ambiguous"
        facts = communication_facts(
            c.db["runs"][c.run_id].data,
            conversation=CommunicationConversationV1(selection="run", language="en", linked_runs=1),
            linked_verification=packet,
        )
        assert facts["linked_verification"]["next_action"] == "ask_which_linked_verification_operation"
        assert "factory_verification" not in facts and not c.state["native_requests"]
