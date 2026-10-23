# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native question outbox and the existing AI feedback writer; only controlled SCM transports."""
# ruff: noqa: F401, F811

import copy
import importlib.util
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_scm_polling import poll_case
from test_scm_source_edit_service_host import observe, prepare

from firefly_forge.agent_roles import WorkerFeedback
from firefly_forge.journal import Journal, digest
from firefly_forge.source_edit_models import PROTOCOL


async def pending(c, monkeypatch):
    module, authority, body = await prepare(c, monkeypatch)
    candidate = await authority.workspaces.source_candidate(c.run_id)
    candidate["source_edit"]["protocol"] = PROTOCOL
    candidate["digest"] = digest({k: v for k, v in candidate.items() if k != "digest"})

    async def source_candidate(run):
        return copy.deepcopy(candidate)

    authority.workspaces.source_candidate = source_candidate
    body["activation"]["candidate_digest"] = candidate["digest"]
    grant = await authority.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
    delivery = await observe(c, grant)
    journal = c.collaboration.deployments.engine.journal

    async def activation():
        evidence = c.state["activations"][-1].evidence
        payload = dict(
            version=1,
            request=grant["request"]["activation"],
            actor=grant["admin_snapshot"],
            candidate=candidate,
            previous_task=candidate["previous_task"],
            scm_source_activation=evidence,
            expires_at_ms=9999999999999,
        )
        intent = await journal._append(
            c.run_id, "implement", body["activation"]["request_id"], "workspace_reconciliation_intent", payload
        )
        receipt = await journal._append(
            c.run_id,
            "implement",
            body["activation"]["request_id"],
            "workspace_reconciliation_receipt",
            payload | {"intent_digest": intent["digest"], "completed_cut": {}},
        )
        c.db["runs"][c.run_id].data["source_edit_continuation"] = {
            "request_id": body["activation"]["request_id"],
            "receipt_digest": receipt["digest"],
        }

    c.state["activation_hook"] = activation
    c.state["activation_unknown"] = True
    with pytest.raises(RuntimeError, match="unknown"):
        await authority.process(delivery["id"])
    original = copy.deepcopy(c.db["inbox"][delivery["id"]].data)
    feedback = WorkerFeedback(
        id="public-worker-feedback",
        role_id="implementer",
        task_id="implement",
        attempt=2,
        disposition="clarification",
        summary="The owner must choose the expected outcome",
        request={
            "id": "public-question",
            "kind": "other",
            "question": "Which result is expected?",
            "context": "Retained public context",
            "choices": ["Use the original scope", "Stop for review"],
        },
    )
    event = await journal._append(
        c.run_id, "implement", "public-question-feedback", "worker_feedback", feedback.model_dump(mode="json")
    )
    run = c.db["runs"][c.run_id].data
    run.update(
        status="paused",
        active_task="implement",
        pause={"kind": "worker_request"},
        role_snapshot={"max_requests": 3},
        worker_request={
            **feedback.request.model_dump(),
            "role_id": "implementer",
            "task_id": "implement",
            "attempt": 2,
            "status": "pending",
        },
    )

    async def entries(run_id):
        async with c.store._unit():
            rows = await journal.records.find_for_run(__import__("uuid").UUID(run_id))
        values = [copy.deepcopy(r.data) for r in rows]
        assert Journal.verify_chain(values)
        return values

    authority.workspaces.recoveries = SimpleNamespace(store=c.store, journal=journal, _entries=entries)
    c.collaboration.source_edit_authority = authority
    import firefly_forge.native_question_publications as producer

    ownership = object()
    c.store.execution_ownership_token = ownership

    @asynccontextmanager
    async def owned(store, token, **kwargs):
        assert store is c.store and token is ownership
        await store.assert_ownership()
        async with store._unit() as session:
            yield session
        await store.assert_ownership()

    monkeypatch.setattr(producer, "question_owned_unit", owned)
    original_request = c.scm._request

    async def request(method, url, **kwargs):
        if method == "POST" and url.endswith("/issues/3/comments"):
            import httpx

            c.state["posts"].append(copy.deepcopy(kwargs["json"]))
            hook = c.state.get("question_effect_hook")
            if hook:
                await hook()
            if c.state["lost_reply"]:
                raise httpx.ReadError("Controlled lost notification result")
            return httpx.Response(
                201, json={"id": 501, "html_url": "https://github.com/team/product/issues/3#issuecomment-501"}
            )
        return await original_request(method, url, **kwargs)

    monkeypatch.setattr(c.scm, "_request", request)
    return original, event


async def test_known_pending_question_has_separate_durable_outbox_even_when_source_delivery_is_unknown(
    poll_case, monkeypatch
):
    assert importlib.util.find_spec("firefly_forge.native_question_publications"), (
        "A durable native question producer is missing"
    )
    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        assert record["native_question"]["feedback_digest"] == event["digest"]
        assert record["id"] != original["id"] and record["feedback_status"] == "pending"
        assert c.db["inbox"][original["id"]].data == original
        assert await producer.produce(c.run_id) == record
        await c.collaboration.feedback(record["id"])
        sent = await c.collaboration.receipt(record["id"])
        assert sent["feedback_status"] == "sent"
        assert len(c.state["posts"]) == 1 and "Which result is expected" in c.state["posts"][0]["body"]
        assert c.db["inbox"][original["id"]].data == original
        assert await producer.produce(c.run_id) == sent
        assert c.db["runs"][c.run_id].data["status"] == "paused"


@pytest.mark.parametrize("changed", ["answer", "membership", "identity", "binding", "source", "lease"])
async def test_revoked_or_changed_question_before_effect_never_posts(poll_case, monkeypatch, changed):
    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        native_write = c.collaboration.communication_writer.write

        async def write(*args, **kwargs):
            result = await native_write(*args, **kwargs)
            if changed == "answer":
                c.db["runs"][c.run_id].data["worker_request"]["status"] = "answered"
            elif changed == "membership":
                c.db["memberships"]["member"].active = False
            elif changed == "identity":
                c.db["users"][c.user_id].enabled = False
            elif changed == "binding":
                c.db["settings"][c.repository].data["allowed_commands"] = []
            elif changed == "source":
                c.state["edited"] = True
            else:
                c.state["ownership_lost"] = True
            return result

        monkeypatch.setattr(c.collaboration.communication_writer, "write", write)
        await c.collaboration.feedback(record["id"])
        assert c.state["posts"] == []
        assert c.db["inbox"][original["id"]].data == original
        held = await c.collaboration.receipt(record["id"])
        assert held["feedback_status"] in {"held", "writing"}
        if held["feedback_status"] == "held":
            assert held["feedback_intent"]["body"] == ""


async def test_unknown_new_notification_is_reserved_once_and_original_unknown_survives(poll_case, monkeypatch):
    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        c.state["lost_reply"] = True
        await c.collaboration.feedback(record["id"])
        held = await c.collaboration.receipt(record["id"])
        assert held["feedback_status"] == "needs_reconciliation"
        assert held["feedback_intent"]["body"] == c.state["posts"][0]["body"]
        assert await producer.produce(c.run_id) == held
        await c.collaboration.feedback(record["id"])
        assert len(c.state["posts"]) == 1
        assert c.db["inbox"][original["id"]].data == original


@pytest.mark.parametrize("outcome", ["posted", "not_posted"])
@pytest.mark.parametrize("lost", [False, True])
async def test_operator_notification_reconciliation_wins_late_success_or_exception(
    poll_case, monkeypatch, outcome, lost
):
    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        captured = {}

        async def reconcile():
            row = c.db["inbox"][record["id"]]
            row.data = {**row.data, "feedback_status": "needs_reconciliation"}
            captured["value"] = await c.collaboration.reconcile(record["id"], outcome, "Public checked notification")

        c.state["question_effect_hook"] = reconcile
        c.state["lost_reply"] = lost
        await c.collaboration.feedback(record["id"])
        assert await c.collaboration.receipt(record["id"]) == captured["value"]
        assert c.db["inbox"][original["id"]].data == original
        assert len(c.state["posts"]) == 1


async def test_selected_ai_writes_actual_question_and_retains_profile_before_post(poll_case, monkeypatch, tmp_path):
    from communication_model_fixture import reviewed_model
    from test_communication_writer import configured_store

    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        c.store.task_router = (await configured_store(tmp_path / "models")).task_router
        calls = []
        c.collaboration.communication_writer.model = reviewed_model(
            {
                "title": {"text": "Owner decision needed", "fact_ids": ["decision"]},
                "paragraphs": [
                    {
                        "text": "The implementation worker is waiting for your decision.",
                        "fact_ids": ["state", "decision"],
                    }
                ],
                "decision": {
                    "question": "Which result is expected?",
                    "context": "Retained public context",
                    "choices": ["Use the original scope", "Stop for review"],
                },
            },
            calls=calls,
        )
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        await c.collaboration.feedback(record["id"])
        sent = await c.collaboration.receipt(record["id"])
        assert sent["feedback_status"] == "sent", sent
        assert sent["feedback_intent"]["communication"]["mode"] == "ai"
        assert [phase for phase, _, _ in calls] == ["draft", "review"]
        assert "Which result is expected?" in c.state["posts"][0]["body"]
        assert sent["feedback_intent"]["body"] == c.state["posts"][0]["body"]
        assert c.db["inbox"][original["id"]].data == original


async def test_selected_ai_failure_has_empty_body_and_zero_remote_effect(poll_case, monkeypatch, tmp_path):
    from pydantic_ai.models.function import FunctionModel
    from test_communication_writer import configured_store

    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        c.store.task_router = (await configured_store(tmp_path / "models")).task_router

        def broken(messages, info):
            raise RuntimeError("Controlled AI provider failure")

        c.collaboration.communication_writer.model = FunctionModel(broken)
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        await c.collaboration.feedback(record["id"])
        held = await c.collaboration.receipt(record["id"])
        assert held["feedback_status"] == "held"
        assert held["feedback_intent"]["communication"]["mode"] == "held"
        assert held["feedback_intent"]["body"] == ""
        assert c.state["posts"] == []
        assert c.db["inbox"][original["id"]].data == original


@pytest.mark.parametrize("field", ["issue", "run", "feedback", "binding", "connection"])
async def test_forged_original_provenance_never_creates_a_notification(poll_case, monkeypatch, field):
    from firefly_forge.native_question_publications import NativeQuestionPublications
    from firefly_forge.project_access import AccessDenied

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        if field == "issue":
            c.db["inbox"][original["id"]].data["issue"]["number"] = 999
        elif field == "run":
            c.db["inbox"][original["id"]].data["command"]["run_id"] = "9" * 32
        elif field == "feedback":
            c.db["runs"][c.run_id].data["worker_request"]["question"] = "Forged question"
        elif field == "binding":
            c.db["inbox"][original["id"]].data["source_edit_authority"]["binding_digest"] = "9" * 64
        else:
            original_scope = c.scm.delegation_scope
            monkeypatch.setattr(
                c.scm, "delegation_scope", lambda repo: {**original_scope(repo), "connection_revision": 999}
            )
        with pytest.raises((AccessDenied, ValueError)):
            await NativeQuestionPublications(c.collaboration).produce(c.run_id)
        assert c.state["posts"] == []
        assert not [e for e in c.db["journal"].values() if e.kind == "native_question_reserved"]


async def test_natural_reply_to_new_notification_saves_answer_without_resume_or_expired_grant(
    poll_case, monkeypatch, tmp_path
):
    from datetime import UTC, datetime, timedelta

    from test_communication_writer import configured_store

    from firefly_forge.native_question_publications import NativeQuestionPublications
    from firefly_forge.scm_natural_models import NaturalDecision

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        import firefly_forge.scm_source_edit as source_service
        from firefly_forge.project_access import AccessDenied

        monkeypatch.setattr(source_service, "utc_now", lambda: datetime.now(UTC) + timedelta(hours=2))
        with pytest.raises(AccessDenied, match="expired"):
            await c.collaboration.source_edit_authority._context(original)
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        await c.collaboration.feedback(record["id"])
        sent = await c.collaboration.receipt(record["id"])
        assert sent["feedback_status"] == "sent"
        c.store.task_router = (await configured_store(tmp_path / "models")).task_router
        seen = []

        async def classify(message, context, settings):
            seen.append(context)
            assert context["candidates"][0]["origin"] == "worker_journal"
            assert context["candidates"][0]["publication"]["receipt_id"] == record["id"]
            return NaturalDecision(intent="answer", message="Your answer is retained.")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        stamp = datetime.now(UTC)
        bot = c.comment(501, sent["feedback_intent"]["body"])
        bot["user"]["type"] = "Bot"
        bot.update(created_at=stamp.isoformat(), updated_at=stamp.isoformat())
        human = c.comment(502, "Use the original scope.")
        human.update(
            created_at=(stamp + timedelta(seconds=1)).isoformat(), updated_at=(stamp + timedelta(seconds=1)).isoformat()
        )
        c.state["comments"].extend([bot, human])
        c.state["clock"] += 3600
        for _ in range(3):
            await c.service.step(c.repository)
        reply = next(r.data for r in c.db["inbox"].values() if r.source_key == "comment:502")
        assert reply["status"] == "received", reply
        result = await c.collaboration.process(reply["id"])
        assert result["natural_turn"]["state"] == "answer_saved_pending_resume", result
        responses = [e for e in c.db["journal"].values() if e.kind == "worker_response"]
        assert len(responses) == 1 and responses[0].data["payload"]["answer"] == "Use the original scope."
        assert c.db["runs"][c.run_id].data["status"] == "paused"
        assert c.db["inbox"][original["id"]].data == original
        assert len(seen) == 1
        assert not [e for e in c.db["journal"].values() if e.kind in {"resume", "provider_dispatch_intent"}]


@pytest.mark.parametrize(
    "field,value",
    [("version", True), ("feedback_digest", "invalid"), ("run_id", "not-a-run"), ("extra", "unsupported")],
)
def test_internal_question_reference_rejects_untyped_or_extra_fields(field, value):
    from pydantic import ValidationError

    from firefly_forge.native_question_publications import NativeQuestionReferenceV1

    value_map = dict(
        version=1,
        kind="native_worker_question_v1",
        run_id="a" * 32,
        question_id="public-question",
        feedback_digest="b" * 64,
        activation_digest="c" * 64,
        source_receipt_id="d" * 32,
        source_authority_digest="e" * 64,
        verified_reference_digest="f" * 64,
        binding_digest="1" * 64,
        native_user_id="2" * 32,
        native_identity_digest="3" * 64,
        question_cut="4" * 64,
    )
    value_map[field] = value
    with pytest.raises(ValidationError):
        NativeQuestionReferenceV1.model_validate(value_map)


async def test_sync_produces_question_from_held_original_without_existing_feedback_row(poll_case, monkeypatch):
    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)

        async def pending_runs():
            return [c.db["runs"][c.run_id]]

        monkeypatch.setattr(c.store.run_records, "find_native_worker_questions", pending_runs, raising=False)
        await c.collaboration.sync_feedback()
        notifications = [r.data for r in c.db["inbox"].values() if r.data.get("native_question")]
        assert len(notifications) == 1 and notifications[0]["feedback_status"] == "sent"
        assert c.db["inbox"][original["id"]].data == original
        await c.collaboration.sync_feedback()
        assert len(c.state["posts"]) == 1


async def test_forged_outbox_without_native_reservation_never_drafts_or_posts(poll_case, monkeypatch):
    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        row = c.db["inbox"][record["id"]]
        old = row.id.hex
        from uuid import uuid4

        row.id = uuid4()
        row.data = {**row.data, "id": row.id.hex}
        c.db["inbox"][row.id.hex] = row
        c.db["inbox"].pop(old)
        await c.collaboration.feedback(row.id.hex)
        assert c.state["posts"] == []
        assert row.data["feedback_status"] == "pending"


async def test_confirmed_reference_requires_exact_immutable_intent_body(poll_case, monkeypatch):
    from firefly_forge.native_question_publications import NativeQuestionPublications
    from firefly_forge.project_access import AccessDenied

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        await c.collaboration.feedback(record["id"])
        sent = await c.collaboration.receipt(record["id"])
        sent["feedback_intent"]["body"] = "An unrelated published question"
        with pytest.raises(AccessDenied):
            await producer.authorize_reference(sent, await c.store.get_run(c.run_id))


async def test_revoked_notification_before_draft_is_explicitly_held_not_silent_pending(poll_case, monkeypatch):
    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        record = await NativeQuestionPublications(c.collaboration).produce(c.run_id)
        c.db["users"][c.user_id].enabled = False
        await c.collaboration.feedback(record["id"])
        held = await c.collaboration.receipt(record["id"])
        assert held["feedback_status"] == "held"
        assert held["feedback_intent"]["body"] == ""
        assert c.state["posts"] == []


@pytest.mark.parametrize("unknown", [False, True])
async def test_notification_outcome_has_separate_immutable_native_receipt(poll_case, monkeypatch, unknown):
    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        record = await NativeQuestionPublications(c.collaboration).produce(c.run_id)
        c.state["lost_reply"] = unknown
        await c.collaboration.feedback(record["id"])
        current = await c.collaboration.receipt(record["id"])
        outcomes = [e.data["payload"] for e in c.db["journal"].values() if e.kind == "native_question_receipt"]
        assert len(outcomes) == 1
        assert outcomes[0]["receipt_id"] == record["id"]
        assert outcomes[0]["outcome"] == ("unknown" if unknown else "sent")
        assert outcomes[0]["feedback_digest"] == event["digest"]
        assert outcomes[0]["remote"] == (None if unknown else current["feedback_remote"])
        assert c.db["inbox"][original["id"]].data == original


async def test_full_existing_inbox_refuses_notification_without_partial_wal(poll_case, monkeypatch):
    import firefly_forge.native_question_publications as module
    from firefly_forge.project_access import AccessDenied

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)

        async def full(store, repository):
            return 5000, 0

        monkeypatch.setattr(module, "inbox_size", full, raising=False)
        with pytest.raises(AccessDenied, match="retention"):
            await module.NativeQuestionPublications(c.collaboration).produce(c.run_id)
        assert not [e for e in c.db["journal"].values() if e.kind == "native_question_reserved"]
        assert c.state["posts"] == []


@pytest.mark.parametrize(
    "remote",
    [
        {},
        {"id": True, "html_url": "https://github.com/team/product/issues/3#issuecomment-1"},
        {"id": 501, "html_url": "https://github.com/team/product/issues/999#issuecomment-501"},
    ],
)
async def test_malformed_2xx_notification_identity_is_unknown_and_never_retried(poll_case, monkeypatch, remote):
    import httpx

    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        transport = c.scm._request

        async def changed(method, url, **kwargs):
            response = await transport(method, url, **kwargs)
            if method == "POST" and url.endswith("/issues/3/comments"):
                return httpx.Response(201, json=remote)
            return response

        monkeypatch.setattr(c.scm, "_request", changed)
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        await producer.publish(record["id"])
        held = await c.collaboration.receipt(record["id"])
        assert held["feedback_status"] == "needs_reconciliation"
        assert held.get("feedback_remote") is None
        await producer.publish(record["id"])
        assert len(c.state["posts"]) == 1
        assert c.db["inbox"][original["id"]].data == original


async def test_losing_concurrent_claim_cannot_clear_active_publisher_marker(poll_case, monkeypatch):
    import asyncio

    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        read = c.collaboration.receipt
        both = asyncio.Event()
        drafting = asyncio.Event()
        finish = asyncio.Event()
        arrived = 0

        async def receipt(value):
            nonlocal arrived
            result = await read(value)
            if value == record["id"] and arrived < 2:
                arrived += 1
                if arrived == 2:
                    both.set()
                await both.wait()
            return result

        monkeypatch.setattr(c.collaboration, "receipt", receipt)
        write = c.collaboration.communication_writer.write

        async def draft(*args, **kwargs):
            drafting.set()
            await finish.wait()
            return await write(*args, **kwargs)

        monkeypatch.setattr(c.collaboration.communication_writer, "write", draft)
        tasks = [asyncio.create_task(producer.publish(record["id"])) for _ in range(2)]
        try:
            await asyncio.wait_for(drafting.wait(), 2)
            await asyncio.wait_for(asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED), 2)
            assert record["id"] in c.collaboration._active_writes
        finally:
            finish.set()
            await asyncio.gather(*tasks)
        assert len(c.state["posts"]) == 1


async def test_drain_at_final_guard_blocks_notification_without_post(poll_case, monkeypatch):
    from firefly_forge.native_question_publications import NativeQuestionPublications

    async with poll_case() as c:
        original, event = await pending(c, monkeypatch)
        record = await NativeQuestionPublications(c.collaboration).produce(c.run_id)
        c.store.execution_stopping = False
        current = c.collaboration.communication_writer.assert_current
        calls = 0

        def drain(evidence):
            nonlocal calls
            current(evidence)
            calls += 1
            if calls == 2:
                c.store.execution_stopping = True

        monkeypatch.setattr(c.collaboration.communication_writer, "assert_current", drain)
        await c.collaboration.feedback(record["id"])
        assert calls == 2 and c.state["posts"] == []
        assert c.db["inbox"][original["id"]].data == original
