# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real native custody and one linked source service/CLI question; GitHub HTTP is recorded."""

import asyncio
import copy
import json
import os
from contextvars import Context
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from scm_polling_native_fixtures import native_poll  # noqa: F401
from sqlalchemy import text

from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest
from firefly_forge.journal import digest
from firefly_forge.native_question_publications import INTENT, RECEIPT, NativeQuestionPublications
from firefly_forge.project_access import ProjectAccess
from firefly_forge.source_edit_models import PROTOCOL
from firefly_forge.store import Store, identifier


def prepare(case, *, retained=None):
    if retained is None:
        case.ready()
        binding = case.consent()
        grant = "b" * 64
        number = 100 + len(case.state["comments"])
        command = dict(version=1, run_id=case.run["id"], expected_grant_digest=grant)
        case.state["comments"].append(case.comment(number, "/forge source_edit " + json.dumps(command)))
        case.state["clock"] += 3600
        for _ in range(2):
            case.http.portal.call(case.service.step, case.repo["id"])
        source = next(
            row for row in case.http.portal.call(case.retained)[2] if row["verification"]["resource_id"] == str(number)
        )

    async def seed():
        # Seed reviewed history only: this fixture does not execute a CLI or a source effect.
        async with case.store._unit():
            row = await case.service.inbox.find_by_id(identifier(source["id"]))
            evidence = dict(
                version=1,
                transport="github_rest_poll_source_edit_v1",
                receipt_id=source["id"],
                grant_digest=grant,
                binding_digest=binding["digest"],
                verified_reference_digest=digest(source["verification"]),
                native_user_id=case.mapped.id.hex,
            )
            await case.collaboration._write_row(
                row,
                status="needs_reconciliation",
                action_status="needs_reconciliation",
                feedback_status="held",
                source_edit_authority=evidence,
            )
            original = copy.deepcopy(row.data)
            request_id = uuid4().hex
            candidate = dict(
                run_id=case.run["id"],
                actor=ProjectAccess.actor_snapshot(case.owner),
                previous_task={"attempt": 1},
                source_edit={"protocol": PROTOCOL},
            )
            candidate["digest"] = digest(candidate)
            request = dict(
                version=1,
                request_id=request_id,
                candidate_digest=candidate["digest"],
                acknowledge_environment_only=True,
                acknowledge_resume_existing_task=True,
                acknowledge_existing_read_scope=True,
                acknowledge_no_commands=True,
            )
            payload = dict(
                request=request,
                actor=ProjectAccess.actor_snapshot(case.owner),
                candidate=candidate,
                previous_task=candidate["previous_task"],
                scm_source_activation=evidence,
                expires_at_ms=1,
            )
            intent = await case.journal._append(
                case.run["id"], "implement", request_id, "workspace_reconciliation_intent", payload
            )
            receipt = await case.journal._append(
                case.run["id"],
                "implement",
                request_id,
                "workspace_reconciliation_receipt",
                {**payload, "intent_digest": intent["digest"]},
            )
            feedback = WorkerFeedback(
                id="native-publication-feedback",
                role_id="implementer",
                task_id="implement",
                attempt=1,
                disposition="clarification",
                summary="An owner decision is pending",
                request=WorkerRequest(
                    id="native-publication-question",
                    kind="other",
                    question="Keep the original scope?",
                    context="The original task needs this decision.",
                    choices=["Keep it", "Stop for review"],
                ),
            )
            await case.journal._append(
                case.run["id"],
                "implement",
                "native-publication-feedback",
                "worker_feedback",
                feedback.model_dump(mode="json"),
            )
            await case.store.update(
                case.run["id"],
                status="paused",
                active_task="implement",
                source_edit_continuation={"request_id": request_id, "receipt_digest": receipt["digest"]},
                worker_request={
                    **feedback.request.model_dump(),
                    "status": "pending",
                    "role_id": "implementer",
                    "task_id": "implement",
                    "attempt": 1,
                },
                pause={"kind": "worker_request"},
            )
            return original, await case.store.get_run(case.run["id"])

    original, run = case.http.portal.call(seed) if retained is None else retained
    producer = NativeQuestionPublications(case.collaboration)
    transport = case.scm._client
    witnesses = []

    async def provider(request):
        from pyfly.data.transaction import current_unit_of_work

        if (
            current_unit_of_work() is None
            or request.method != "POST"
            or not request.url.path.endswith("/issues/3/comments")
        ):
            return await transport.send(request)
        # Observe the exact owned native connection and independently committed write-ahead intent.
        async with case.store._unit() as session:
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
        assert pid == case.store._ownership_pid
        async with case.store._engine.connect() as independent:
            committed = await independent.scalar(
                text(f"SELECT count(*) FROM \"{case.store.schema}\".forge_journal WHERE kind='native_question_intent'")
            )
        assert committed == 1
        body = json.loads(request.content)
        case.state["posts"].append(body)
        witnesses.append(dict(pid=pid, lease_pid=case.store._ownership_pid, intent_committed=True))
        if fixture := os.environ.get("NATIVE_SQL_FIXTURE"):
            with (Path(fixture) / "logs/native-question-witnesses.jsonl").open("a") as output:
                output.write(json.dumps({"run_id": case.run["id"], **witnesses[-1]}, sort_keys=True) + "\n")
        if hook := case.state.get("question_http_hook"):
            await hook()
        if case.state["lost_reply"]:
            raise httpx.ReadError("Public notification response loss", request=request)
        return httpx.Response(
            201, json={"id": 501, "html_url": "https://github.com/team/product/issues/3#issuecomment-501"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    case.scm._client = client
    return SimpleNamespace(producer=producer, original=original, run=run, witnesses=witnesses, client=client)


def completed_pending(case, tmp_path, monkeypatch):
    """Join real native output admission and scoped CLI feedback to the recorded App fixture."""
    import test_source_edit_native
    from test_source_completed_output_native import completed
    from test_source_completed_output_service_host import request as output_request
    from worker_reply_fixtures import authenticated_operator

    from firefly_forge import engine as engine_module
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL
    from firefly_forge.runtime_roles import RuntimeRoles
    from firefly_forge.scm import checked, clone
    from firefly_forge.scm_source_edit_models import SealedSourceEditAdmission
    from firefly_forge.source_result_readmission import SourceCompletedOutputReadmissions
    from firefly_forge.workspace_reconciliations import WorkspaceReconciliations

    case.ready()
    binding = case.consent()
    state = {}
    source_store = None
    create_run = None
    original_register = test_source_edit_native.register_fixture_profile

    async def local_repository(body):
        assert body.name == "Public environment fixture"
        local = Path(body.url).resolve(strict=True)
        assert local.is_dir() and (local / ".git").is_dir()
        state["local_source"] = local
        return copy.deepcopy(case.repo)

    async def scoped_run(body):
        assert body.repository_id == case.repo["id"]
        await source_store.update(case.run["id"], status="cancelled", worker_request=None)
        run = await create_run(body.model_copy(update={"project_id": case.project["id"], "component_id": "api"}))
        case.run.clear()
        case.run.update(run)
        return run

    async def local_clone(repository, destination, branch, *, env=None, revision=None):
        assert repository["id"] == case.repo["id"] and repository["url"] == case.repo["url"]
        assert env is None and revision is None
        result = await clone({**repository, "url": str(state["local_source"])}, destination, branch)
        await checked(["git", "remote", "set-url", "origin", repository["url"]], destination)
        assert (await checked(["git", "remote", "get-url", "origin"], destination)).strip() == repository["url"]
        return result

    question = {
        "summary": "The original source task requires the owner's decision",
        "feedback": {
            "id": "native-publication-feedback",
            "role_id": "implementer",
            "task_id": "implement",
            "attempt": 4,
            "disposition": "clarification",
            "summary": "An owner decision is pending",
            "request": {
                "id": "native-publication-question",
                "kind": "other",
                "question": "Keep the original scope?",
                "context": "The original task needs this decision.",
                "choices": ["Keep it", "Stop for review"],
            },
        },
    }

    def question_profile(patch, executable):
        program = executable.read_text()
        target = '"Changed public source through the retained source-only channel"'
        assert program.count(target) == 1
        executable.write_text(program.replace(target, json.dumps(json.dumps(question))))
        return original_register(patch, executable)

    async def scm_activate(service, run_id, body):
        assert "source" not in state, "Only the original SCM activation is substituted"
        grant = "b" * 64
        number = 100 + len(case.state["comments"])
        command = dict(version=1, run_id=run_id, expected_grant_digest=grant)
        case.state["comments"].append(case.comment(number, "/forge source_edit " + json.dumps(command)))
        case.state["clock"] += 3600
        for _ in range(2):
            await case.service.step(case.repo["id"])
        source = next(r for r in (await case.retained())[2] if r["verification"]["resource_id"] == str(number))
        evidence = dict(
            version=1,
            transport="github_rest_poll_source_edit_v1",
            receipt_id=source["id"],
            grant_digest=grant,
            binding_digest=binding["digest"],
            verified_reference_digest=digest(source["verification"]),
            native_user_id=case.mapped.id.hex,
        )
        async with source_store._unit():
            row = await case.service.inbox.find_by_id(identifier(source["id"]))
            await case.collaboration._write_row(
                row,
                status="needs_reconciliation",
                action_status="needs_reconciliation",
                feedback_status="held",
                source_edit_authority=evidence,
            )
            state["source"] = copy.deepcopy(row.data)
        candidate = await service.source_candidate(run_id)
        assert candidate["digest"] == body.candidate_digest
        caller = Principal(
            case.mapped.id,
            case.mapped.issuer,
            case.mapped.subject,
            authentication_method="scm_mapping",
            scm_delegation={"transport": "github_rest_poll_source_edit_v1"},
        )

        async def verified():
            assert CURRENT_PRINCIPAL.get() == caller
            await source_store.assert_ownership()
            assert await case.collaboration.receipt(source["id"]) == state["source"]

        admission = SealedSourceEditAdmission(
            run_id=run_id,
            native_actor=candidate["actor"],
            caller=caller,
            request=body,
            evidence=evidence,
            verify=verified,
        )
        token = CURRENT_PRINCIPAL.set(caller)
        try:
            return await service.activate_source_edit_from_scm(admission)
        finally:
            CURRENT_PRINCIPAL.reset(token)

    async def join():
        nonlocal source_store, create_run
        api_store = case.store
        assert api_store.runtime_roles.role == "api" and api_store.runtime_roles.started
        with pytest.raises(RuntimeError, match="API role"):
            api_store.runtime_roles.require_execution()
        await api_store.release_ownership()
        source_store = Store(api_store.path)
        source_store.configuration = api_store.configuration
        runtime = RuntimeRoles(source_store, "worker")
        source_store.runtime_roles = runtime
        try:
            await source_store.initialize()
            await runtime.start()
            runtime.require_execution()
            await source_store.acquire_ownership()
            create_run = source_store.create_run
            patch.setattr(source_store, "add_repository", local_repository)
            patch.setattr(source_store, "create_run", scoped_run)
            patch.setattr(case.service, "store", source_store)
            patch.setattr(case.collaboration, "store", source_store)
            async with authenticated_operator(source_store):
                env, parent, catalog = await completed(source_store, tmp_path, patch)
                service = SourceCompletedOutputReadmissions(env.service)
                review = await service.candidate(env.run_id, parent["request"]["request_id"], str(catalog))
                body = output_request(review, str(catalog))
                receipt = await service.admit(env.run_id, parent["request"]["request_id"], body)
                assert receipt["effect"]["no_environment_effect"] is True
                assert receipt["candidate"]["source_edit"]["remaining_commands"] == 7
                assert review["observation"]["implementation_success"] is False
                assert review["observation"]["process_exit_attested"] is False
                env.engine.configured_provider = True
                await env.engine.execute_run(env.run_id)
                run = await source_store.get_run(env.run_id)
                assert run["status"] == "paused", run.get("error")
                assert run["worker_request"]["id"] == "native-publication-question"
                assert run["worker_request"]["attempt"] == 4 and run["implementation_rounds"] == 4
                assert run["source_edit_continuation"]["request_id"] == body.request_id
                tools = await env.engine.tool_broker.invocations(env.run_id)
                assert [r["admission"]["tool"] for r in tools] == ["list_files", "read_file", "search_text"]
                assert all(r["status"] == "succeeded" for r in tools)
                assert len(env.checks) == len(await env.journal.sandbox_executions(env.run_id)) == 7
                assert await case.collaboration.receipt(state["source"]["id"]) == state["source"]
                return state["source"], run

        finally:
            await runtime.stop()
            await source_store.close()
            await api_store.acquire_ownership()
            assert api_store.runtime_roles.role == "api" and api_store.runtime_roles.started
            with pytest.raises(RuntimeError, match="API role"):
                api_store.runtime_roles.require_execution()

    with monkeypatch.context() as patch:
        patch.setattr(engine_module, "clone", local_clone)
        patch.setattr(test_source_edit_native, "register_fixture_profile", question_profile)
        patch.setattr(WorkspaceReconciliations, "activate_source_edit", scm_activate)
        return case.http.portal.call(join)


async def unchanged(case, env):
    assert await case.collaboration.receipt(env.original["id"]) == env.original
    assert await case.store.get_run(case.run["id"]) == env.run


@pytest.mark.parametrize("native_poll", ["implementation"], indirect=True)
@pytest.mark.parametrize(
    "outcome",
    ["sent", "unknown", "answer_during_draft", "identity_during_draft", "drain_before_post", "drain_after_post"],
)
def test_native_question_outbox_exact_effect_or_refusal(native_poll, monkeypatch, outcome):  # noqa: F811
    case = native_poll
    env = prepare(case)

    async def run():
        try:
            record = await env.producer.produce(case.run["id"])
            case.state["lost_reply"] = outcome == "unknown"
            writer = case.collaboration.communication_writer.write

            async def draft(*args, **kwargs):
                result = await writer(*args, **kwargs)
                if outcome == "answer_during_draft":
                    request = (await case.store.get_run(case.run["id"]))["worker_request"]
                    await case.store.update(case.run["id"], worker_request={**request, "status": "answered"})
                elif outcome == "identity_during_draft":
                    async with case.store._unit():
                        user = await case.service.users.find_by_id(case.mapped.id)
                        user.enabled = False
                        await case.service.users.save(user)
                return result

            monkeypatch.setattr(case.collaboration.communication_writer, "write", draft)
            if outcome == "drain_before_post":
                current = case.collaboration.communication_writer.assert_current
                guards = 0

                def drain(evidence):
                    nonlocal guards
                    current(evidence)
                    guards += 1
                    if guards == 2:
                        case.store.execution_stopping = True

                monkeypatch.setattr(case.collaboration.communication_writer, "assert_current", drain)
            elif outcome == "drain_after_post":

                async def drain_after():
                    case.store.execution_stopping = True

                case.state["question_http_hook"] = drain_after
            await env.producer.publish(record["id"])
            result = await case.collaboration.receipt(record["id"])
            expected = {
                "sent": "sent",
                "unknown": "needs_reconciliation",
                "answer_during_draft": "held",
                "identity_during_draft": "held",
                "drain_before_post": "writing",
                "drain_after_post": "sent",
            }[outcome]
            assert result["feedback_status"] == expected
            assert len(case.state["posts"]) == int(outcome in {"sent", "unknown", "drain_after_post"})
            await env.producer.publish(record["id"])
            if outcome in {"sent", "unknown", "drain_after_post"}:
                if outcome != "drain_after_post":
                    assert await env.producer.produce(case.run["id"]) == result
                assert env.witnesses[0]["pid"] == env.witnesses[0]["lease_pid"]
                assert result["feedback_intent"]["body"] == case.state["posts"][0]["body"]
                await unchanged(case, env)
                async with case.store._unit():
                    assert await case.store.run_records.find_native_worker_questions() == []
            else:
                if outcome != "drain_before_post":
                    assert result["feedback_intent"]["body"] == ""
                assert await case.collaboration.receipt(env.original["id"]) == env.original
        finally:
            await env.client.aclose()

    case.http.portal.call(run)


@pytest.mark.parametrize("native_poll", ["implementation"], indirect=True)
def test_native_concurrent_question_producers_and_writers_have_one_identity_and_post(native_poll):  # noqa: F811
    case = native_poll
    env = prepare(case)

    async def run():
        try:
            records = await asyncio.gather(*(env.producer.produce(case.run["id"]) for _ in range(3)))
            assert len({r["id"] for r in records}) == 1
            await asyncio.gather(*(env.producer.publish(records[0]["id"]) for _ in range(3)))
            assert len(case.state["posts"]) == 1
            entries = await case.journal.entries(case.run["id"])
            assert len([e for e in entries if e["kind"] == "native_question_reserved"]) == 1
            assert len([e for e in entries if e["kind"] == RECEIPT]) == 1
            await unchanged(case, env)
        finally:
            await env.client.aclose()

    case.http.portal.call(run)


@pytest.mark.parametrize("native_poll", ["implementation"], indirect=True)
def test_native_question_intent_rollback_never_reaches_http(native_poll, monkeypatch):  # noqa: F811
    case = native_poll
    env = prepare(case)

    async def run():
        try:
            record = await env.producer.produce(case.run["id"])
            append = case.journal._append

            async def fail(*args, **kwargs):
                event = await append(*args, **kwargs)
                if args[3] == INTENT:
                    raise RuntimeError("Public intent rollback fixture")
                return event

            monkeypatch.setattr(case.journal, "_append", fail)
            await env.producer.publish(record["id"])
            assert (await case.collaboration.receipt(record["id"]))["feedback_status"] == "held"
            assert not [e for e in await case.journal.entries(case.run["id"]) if e["kind"] == INTENT]
            assert case.state["posts"] == []
            await unchanged(case, env)
        finally:
            await env.client.aclose()

    case.http.portal.call(run)


@pytest.mark.parametrize("native_poll", ["implementation"], indirect=True)
@pytest.mark.parametrize("after_post", [False, True])
def test_native_question_lease_takeover_preserves_successor_custody(native_poll, monkeypatch, after_post):  # noqa: F811
    case = native_poll
    env = prepare(case)

    async def run():
        successor = None
        captured = {}
        try:
            record = await env.producer.produce(case.run["id"])

            async def takeover_body():
                nonlocal successor
                if after_post:
                    async with case.store._engine.connect() as connection:
                        assert await connection.scalar(
                            text("SELECT pg_terminate_backend(:pid)"), {"pid": case.store._ownership_pid}
                        )
                else:
                    await case.store.release_ownership()
                successor = Store(case.store.path)
                await successor.initialize()
                await successor.acquire_ownership()
                from firefly_forge.collaboration_persistence import ForgeWebhookInboxRepository

                async with successor._unit():
                    repo = ForgeWebhookInboxRepository()
                    row = await repo.find_by_id(identifier(record["id"]))
                    row.data = {
                        **row.data,
                        "feedback_status": "needs_reconciliation",
                        "reason": "public_successor_custody",
                    }
                    await repo.save(row)
                    captured["value"] = copy.deepcopy(row.data)

            async def takeover():
                await asyncio.create_task(takeover_body(), context=Context())

            if after_post:
                case.state["question_http_hook"] = takeover
            else:
                original = case.collaboration.communication_writer.write

                async def draft(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    await takeover()
                    return result

                monkeypatch.setattr(case.collaboration.communication_writer, "write", draft)
            await env.producer.publish(record["id"])
            assert await case.collaboration.receipt(record["id"]) == captured["value"]
            assert len(case.state["posts"]) == int(after_post)
            assert await case.collaboration.receipt(env.original["id"]) == env.original
        finally:
            if successor is not None:
                await successor.close()
            await env.client.aclose()

    case.http.portal.call(run)


@pytest.mark.parametrize("native_poll", ["implementation"], indirect=True)
def test_native_new_question_publication_accepts_natural_answer_without_resuming(native_poll, tmp_path, monkeypatch):  # noqa: F811
    case = native_poll
    env = prepare(case, retained=completed_pending(case, tmp_path, monkeypatch))
    before_wal = case.http.portal.call(case.journal.entries, case.run["id"])

    async def publish():
        record = await env.producer.produce(case.run["id"])
        await env.producer.publish(record["id"])
        return await case.collaboration.receipt(record["id"])

    try:
        sent = case.http.portal.call(publish)
        assert sent["feedback_status"] == "sent"
        from firefly_forge.model_profiles import ModelProfileInput
        from firefly_forge.scm_natural_models import NaturalDecision
        from firefly_forge.task_routing import TaskRoutingInput

        router = case.store.task_router
        router.profiles.save(
            "notification-answer",
            ModelProfileInput(
                name="Public question coordinator",
                provider="agentic",
                values={"model": "openai-responses:public-fixture", "api_key": "owned-native-question-fixture-key"},
            ),
        )
        case.http.portal.call(router.save, TaskRoutingInput(bindings={"communication": "notification-answer"}))

        async def classify(message, context, settings):
            assert context["candidates"][0]["origin"] == "worker_journal"
            assert context["candidates"][0]["publication"]["receipt_id"] == sent["id"]
            return NaturalDecision(intent="answer", message="Your answer is retained.")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        stamp = datetime.now(UTC)
        bot = case.comment(501, sent["feedback_intent"]["body"])
        bot["user"]["type"] = "Bot"
        bot.update(created_at=stamp.isoformat(), updated_at=stamp.isoformat())
        human = case.comment(502, "Keep it.")
        human.update(
            created_at=(stamp + timedelta(seconds=1)).isoformat(), updated_at=(stamp + timedelta(seconds=1)).isoformat()
        )
        case.state["comments"].extend([bot, human])
        case.state["clock"] += 3600
        for _ in range(2):
            case.http.portal.call(case.service.step, case.repo["id"])
        reply = next(r for r in case.http.portal.call(case.retained)[2] if r["verification"]["resource_id"] == "502")
        result = case.process(reply)
        assert result["natural_turn"]["state"] == "answer_saved_pending_resume", result
        assert result["feedback_status"] == "sent", result
        current = case.http.portal.call(case.store.get_run, case.run["id"])
        assert current["status"] == "paused" and current["worker_request"]["status"] == "answered"
        entries = case.http.portal.call(case.journal.entries, case.run["id"])
        assert entries[: len(before_wal)] == before_wal
        added = entries[len(before_wal) :]
        assert len([e for e in added if e["kind"] == "worker_response"]) == 1
        assert not [e for e in added if e["kind"] in {"resume", "provider_dispatch_intent"}]
        assert case.http.portal.call(case.collaboration.receipt, env.original["id"]) == env.original
    finally:
        case.http.portal.call(env.client.aclose)
