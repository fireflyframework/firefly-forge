# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared real DI/API/PG controls; recorded App HTTP only, never live GitHub."""

import asyncio
import json
from contextlib import asynccontextmanager
from functools import partial
from uuid import uuid4

import pytest
from scm_polling_native_fixtures import native_poll  # noqa: F401
from sqlalchemy import text
from worker_reply_fixtures import authenticated_operator

from firefly_forge.access_persistence import ForgeMembershipRepository
from firefly_forge.client import ForgeClient
from firefly_forge.collaboration import Collaboration, CollaborationSettings
from firefly_forge.journal import Journal
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.scm_connections import SCMConnections
from firefly_forge.scm_poll_models import PollConfigurationV1
from firefly_forge.scm_polling import SCMPolling
from firefly_forge.store import Store


def test_native_baseline_and_page_failure_keep_inbox_cursor_atomic(native_poll, monkeypatch):  # noqa: F811
    c = native_poll
    old = "/forge respond " + json.dumps(
        {"run_id": c.run["id"], "request_id": "poll-native-question", "answer": "An old command must not run"}
    )
    c.state["comments"].extend([c.comment(11, old), c.comment(12, old, issue=4)])
    c.ready()
    before = c.http.portal.call(c.retained)
    assert {row["reason"] for row in before[2]} == {"baseline_existing", "unsupported_poll_issue"}
    assert c.state["posts"] == []
    assert not [row for row in c.http.portal.call(c.journal.entries, c.run["id"]) if row["kind"] == "worker_response"]
    c.state["clock"] += 3600
    c.state["comments"].append(c.comment(13, "New ordinary discussion"))
    c.http.portal.call(c.service.step, c.repo["id"])
    before_page = c.http.portal.call(c.retained)
    save = c.service.settings.save

    async def refuse_after_native_save(row):
        await save(row)
        raise RuntimeError("Owned failure after cursor save, before native commit")

    with monkeypatch.context() as patch:
        patch.setattr(c.service.settings, "save", refuse_after_native_save)
        with pytest.raises(RuntimeError, match="before native commit"):
            c.http.portal.call(c.service.step, c.repo["id"])
    assert c.http.portal.call(c.retained) == before_page
    c.http.portal.call(c.service.step, c.repo["id"])
    after = c.http.portal.call(c.retained)
    assert len(after[2]) == len(before[2]) + 1 and after[1]["cursor"]["ready"]
    c.state["clock"] += 3600
    for _ in range(2):
        c.http.portal.call(c.service.step, c.repo["id"])
    assert len(c.http.portal.call(c.retained)[2]) == len(after[2])


def test_native_separate_consent_sdk_status_response_v3_and_exact_replay(native_poll, monkeypatch):  # noqa: F811
    c = native_poll
    c.ready()
    before_consent = c.observe("status")
    assert c.process(before_consent)["status"] == "denied" and c.state["posts"] == []
    binding = c.consent()
    replay = c.http.post(c.path + "/bindings", json=binding["request"])
    assert replay.status_code == 201 and replay.json() == binding
    assert binding["version"] == 2 and binding["verified_reference"]["transport"] == "github_rest_poll_v1"
    before = c.http.portal.call(c.journal.entries, c.run["id"])

    async def role(value):
        async with authenticated_operator(c.store):
            await ProjectAccess(c.store).set_membership(c.project["id"], c.mapped, value)

    c.http.portal.call(role, "viewer")
    status = c.process(c.observe("status"))
    assert status["status"] == "processed" and c.http.portal.call(c.journal.entries, c.run["id"]) == before
    c.http.portal.call(role, "developer")
    connection = ForgeClient("http://testserver", c.token)
    connection.http.close()
    connection.http = c.http
    request = c.http.request
    calls = []

    def native_request(method, path, **kwargs):
        calls.append((method, path))
        kwargs.pop("timeout", None)
        return request(method, "/api/v1/" + str(path).lstrip("/"), **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(c.http, "request", native_request)
        assert (
            connection.scm_polling_binding(c.project["id"], c.repo["id"], binding["request"]["request_id"]) == binding
        )
        heads = connection.scm_polling_heads(c.project["id"], c.repo["id"])
        assert heads["heads_only"] and heads["items"] == [binding]
        assert connection.scm_polling_status(c.project["id"], c.repo["id"])["ready"]
    assert [method for method, _ in calls] == ["GET", "GET", "GET"]
    original = c.http.portal.call(c.store.get_run, c.run["id"])
    delivery = c.observe()
    result = c.process(delivery)
    assert result["status"] == "processed" and c.process(delivery) == result
    entries = c.http.portal.call(c.journal.entries, c.run["id"])
    assert Journal.verify_chain(entries)
    answers = [entry for entry in entries if entry["kind"] == "worker_response"]
    assert len(answers) == 1
    actor = answers[0]["payload"]["responding_actor"]
    assert actor["version"] == 3 and actor["user_id"] == c.mapped.id.hex
    assert actor["provenance"]["binding_digest"] == binding["digest"]
    assert actor["provenance"]["transport"] == "github_rest_poll_v1"
    after = c.http.portal.call(c.store.get_run, c.run["id"])
    assert after["status"] == "paused" and after["worker_request"]["status"] == "answered"
    assert after["execution_actor"] == original["execution_actor"]
    assert not [row for row in entries if row["kind"] == "resume"]
    assert len(c.state["posts"]) == 2


def test_native_current_membership_and_late_credentials_rollback_answer(native_poll, monkeypatch):  # noqa: F811
    c = native_poll
    c.ready()
    c.consent()
    before = c.http.portal.call(c.journal.entries, c.run["id"])

    async def role(value):
        async with authenticated_operator(c.store):
            await ProjectAccess(c.store).set_membership(c.project["id"], c.mapped, value)

    c.http.portal.call(role, "viewer")
    assert c.process(c.observe())["status"] == "denied"
    assert c.http.portal.call(c.journal.entries, c.run["id"]) == before and c.state["posts"] == []
    c.http.portal.call(role, "developer")
    answer = "Owned late registry answer 369f"
    registry = c.store.credential_registry
    delivery = c.observe(answer=answer)
    append = c.journal._append

    async def register_after_append(*args, **kwargs):
        result = await append(*args, **kwargs)
        if len(args) > 3 and args[3] == "worker_response":
            monkeypatch.setattr(c.store, "credential_registry", lambda: {"OWNED_G01_API_KEY": answer})
        return result

    with monkeypatch.context() as patch:
        patch.setattr(c.journal, "_append", register_after_append)
        result = c.process(delivery)
    monkeypatch.setattr(c.store, "credential_registry", registry)
    assert result["status"] == "needs_reconciliation" and c.state["posts"] == []
    assert result["action_status"] == "needs_reconciliation"
    assert c.http.portal.call(c.journal.entries, c.run["id"]) == before
    assert c.http.portal.call(c.store.get_run, c.run["id"])["worker_request"]["status"] == "pending"


def test_native_final_feedback_credentials_and_lost_reply_never_repost(native_poll, monkeypatch):  # noqa: F811
    c = native_poll
    c.ready()
    c.consent()
    authenticate = c.scm.issue_authentication

    async def revoke_after_token(*args, **kwargs):
        result = await authenticate(*args, **kwargs)
        async with c.store._unit():
            records = ForgeMembershipRepository()
            for row in await records.for_user(c.mapped.id):
                if row.project_id.hex == c.project["id"]:
                    row.active = False
                    await records.save(row)
        return result

    revoked_delivery = c.observe("status")
    with monkeypatch.context() as patch:
        patch.setattr(c.scm, "issue_authentication", revoke_after_token)
        refused = c.process(revoked_delivery)
    assert refused["feedback_status"] == "needs_reconciliation" and c.state["posts"] == []

    async def restore_membership():
        async with authenticated_operator(c.store):
            await ProjectAccess(c.store).set_membership(c.project["id"], c.mapped, "developer")

    c.http.portal.call(restore_membership)
    reconciled = c.http.post(
        f"/api/v1/collaboration/receipts/{refused['id']}/reconcile",
        json={"outcome": "not_posted", "note": "The transport recorded no POST before membership was revoked."},
    )
    assert reconciled.status_code == 200 and reconciled.json()["feedback_status"] == "not_sent"
    fictional = "Owned final feedback credential 471f"
    registry = c.store.credential_registry

    delivery = c.observe("status")
    authenticate = c.scm.issue_authentication

    async def register_after_token(*args, **kwargs):
        result = await authenticate(*args, **kwargs)
        monkeypatch.setattr(c.store, "credential_registry", lambda: {"OWNED_G01_API_KEY": fictional})
        return result

    with monkeypatch.context() as patch:
        patch.setattr(c.scm, "issue_authentication", register_after_token)
        guarded = c.process(delivery)
    monkeypatch.setattr(c.store, "credential_registry", registry)
    assert guarded["feedback_status"] == "needs_reconciliation" and c.state["posts"] == []
    c.http.portal.call(c.collaboration.sync_feedback)
    assert c.state["posts"] == []
    reconciled = c.http.post(
        f"/api/v1/collaboration/receipts/{guarded['id']}/reconcile",
        json={"outcome": "not_posted", "note": "The credential guard refused the body before the transport POST."},
    )
    assert reconciled.status_code == 200 and reconciled.json()["feedback_status"] == "not_sent"
    c.state["lost_reply"] = True
    unknown_delivery = c.observe("status")
    unknown = c.process(unknown_delivery)
    assert unknown["feedback_status"] == "needs_reconciliation" and len(c.state["posts"]) == 1
    c.process(unknown_delivery)
    c.http.portal.call(c.collaboration.sync_feedback)
    assert len(c.state["posts"]) == 1
    assert not [row for row in c.http.portal.call(c.journal.entries, c.run["id"]) if row["kind"] == "worker_response"]


async def exercise_wal_fence(c, monkeypatch, *, switch_project):
    """Two actual Store connections, observed PostgreSQL blocking, no transport under locks."""
    second = Store(c.store.path)
    await second.initialize()
    second_scm = SCMConnections(second, client=c.scm._client)
    second_collaboration = Collaboration(second, second_scm)
    second_polling = SCMPolling(second, second_scm)
    second_polling.collaboration = second_collaboration
    second_collaboration.polling = second_polling
    reached, release, pid_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    blocking_pid = waiting_pid = None
    operation = contender = None
    original_append = c.journal._append
    unit = second._unit

    async def hold_after_append(*args, **kwargs):
        nonlocal blocking_pid
        result = await original_append(*args, **kwargs)
        if len(args) > 3 and args[3] == "worker_response":
            async with c.store._unit() as session:
                blocking_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
        return result

    @asynccontextmanager
    async def observed_unit(*args, **kwargs):
        nonlocal waiting_pid
        async with unit(*args, **kwargs) as session:
            waiting_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            pid_ready.set()
            yield session

    async def configure_other_project():
        async with authenticated_operator(second):
            return await second_polling.configure(other["id"], c.repo["id"], body)

    async def disable_collaboration():
        async with authenticated_operator(second):
            return await second_collaboration.configure(c.repo["id"], CollaborationSettings(enabled=False))

    try:
        if switch_project:
            async with authenticated_operator(c.store):
                other = await Projects(c.store).configure(
                    ProjectDefinition(
                        name="Second explicit polling project",
                        description="The single repository polling policy moves only after the original WAL commit",
                        topology="monorepo",
                        owners=["Owner"],
                        components=[
                            dict(id="api", name="API", repository_id=c.repo["id"], responsibility="Serve requests")
                        ],
                    )
                )
                await ProjectAccess(c.store).set_membership(other["id"], c.owner, "owner")
                await ProjectAccess(c.store).set_membership(other["id"], c.mapped, "developer")
                setup = await c.service.setup(other["id"], c.repo["id"])
            body = PollConfigurationV1(
                version=1,
                request_id=uuid4().hex,
                expected_previous_digest=setup["expected_previous_digest"],
                expected_project_binding_digest=setup["project_binding_digest"],
                expected_connection_revision=setup["connection_revision"],
                enabled=True,
            )
        with monkeypatch.context() as patch:
            patch.setattr(c.journal, "_append", hold_after_append)
            delivery = c.pending_delivery
            operation = asyncio.create_task(c.collaboration.process(delivery["id"]))
            await asyncio.wait_for(reached.wait(), 5)
            patch.setattr(second, "_unit", observed_unit)
            contender = asyncio.create_task(configure_other_project() if switch_project else disable_collaboration())
            await asyncio.wait_for(pid_ready.wait(), 5)
            async with asyncio.timeout(5):
                while True:
                    async with c.store._unit() as session:
                        blockers = await session.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid})
                    if blocking_pid in (blockers or []):
                        break
                    assert not contender.done(), "The setter crossed the in-flight native WAL membership fence"
                    await asyncio.sleep(0.01)
            assert not contender.done() and waiting_pid != blocking_pid
            release.set()
            outcomes = await asyncio.gather(operation, contender, return_exceptions=True)
            assert isinstance(outcomes[1], dict), outcomes
            assert not isinstance(outcomes[0], BaseException), outcomes
        entries = await c.journal.entries(c.run["id"])
        assert Journal.verify_chain(entries)
        assert len([row for row in entries if row["kind"] == "worker_response"]) == 1
        current_run = await c.store.get_run(c.run["id"])
        assert current_run["execution_actor"] == c.run["execution_actor"]
        assert current_run["status"] == "paused" and current_run["worker_request"]["status"] == "answered"
        with pytest.raises(AccessDenied):
            await c.service.authorize_snapshot(c.pending_snapshot, c.project["id"], "execute")
    finally:
        release.set()
        await asyncio.gather(*[task for task in [operation, contender] if task is not None], return_exceptions=True)
        await second.close()


def test_native_project_switch_waits_for_original_journal_commit(native_poll, monkeypatch):  # noqa: F811
    c = native_poll
    c.ready()
    c.consent()
    c.pending_delivery = c.observe()
    principal = c.http.portal.call(c.service.resolve, c.pending_delivery)
    c.pending_snapshot = ProjectAccess.actor_snapshot(principal)
    c.http.portal.call(partial(exercise_wal_fence, c, monkeypatch, switch_project=True))


def test_native_disable_waits_for_original_journal_commit(native_poll, monkeypatch):  # noqa: F811
    c = native_poll
    c.ready()
    c.consent()
    c.pending_delivery = c.observe()
    principal = c.http.portal.call(c.service.resolve, c.pending_delivery)
    c.pending_snapshot = ProjectAccess.actor_snapshot(principal)
    c.http.portal.call(partial(exercise_wal_fence, c, monkeypatch, switch_project=False))


@pytest.mark.parametrize("change", ["source", "permission"])
def test_native_feedback_refuses_remote_change_during_token_preparation(native_poll, monkeypatch, change):  # noqa: F811
    c = native_poll
    c.ready()
    binding = c.consent()
    delivery = c.observe("status")
    before = c.http.portal.call(c.journal.entries, c.run["id"])
    authenticate = c.scm.issue_authentication
    local = c.service.authorize_feedback_local
    crossed = False
    checks_after_wait = 0

    async def change_after_token(*args, **kwargs):
        nonlocal crossed
        result = await authenticate(*args, **kwargs)
        assert not crossed
        crossed = True
        if change == "source":
            c.state["comments"][-1]["body"] += " edited while issue token was prepared"
        else:
            c.state["permission"] = "none"
        return result

    async def native_local(data):
        nonlocal checks_after_wait
        await local(data)
        if crossed:
            checks_after_wait += 1

    monkeypatch.setattr(c.scm, "issue_authentication", change_after_token)
    monkeypatch.setattr(c.service, "authorize_feedback_local", native_local)
    result = c.process(delivery)
    assert crossed and checks_after_wait > 0
    assert c.state["posts"] == []
    assert result["status"] == "processed" and result["feedback_status"] == "needs_reconciliation"
    exact = c.http.portal.call(c.collaboration.receipt, delivery["id"])
    assert exact == result
    c.http.portal.call(c.collaboration.sync_feedback)
    c.http.portal.call(c.collaboration.sync_feedback)
    assert c.http.portal.call(c.collaboration.receipt, delivery["id"]) == exact
    assert c.state["posts"] == []
    assert c.http.get(c.path + "/bindings/requests/" + binding["request"]["request_id"]).json() == binding
    with pytest.raises(AccessDenied):
        c.http.portal.call(c.service.authorize_feedback, exact)
    assert c.http.portal.call(c.journal.entries, c.run["id"]) == before
    assert c.http.portal.call(c.store.get_run, c.run["id"])["status"] == "paused"
