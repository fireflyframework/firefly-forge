# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real native pause/answer/continuation; only external GitHub HTTP is recorded."""

import json
from uuid import uuid4

import pytest
from scm_polling_native_fixtures import native_poll  # noqa: F401
from worker_reply_fixtures import authenticated_operator

from firefly_forge.project_access import ProjectAccess


def prepared(case):
    case.ready()
    binding = case.consent()
    assert case.process(case.observe())["status"] == "processed"
    body = dict(
        version=3,
        transport="github_rest_poll_v1",
        request_id=uuid4().hex,
        expected_previous_digest=None,
        expected_project_binding_digest=binding["project_binding_digest"],
        expected_binding_request_id=binding["request"]["request_id"],
        expected_binding_digest=binding["digest"],
        component_ids=["api"],
        allowed_actions=["resume"],
        active=True,
        acknowledge_same_run_resume=True,
    )
    path = case.path + "/resume-grants"
    response = case.http.post(path, json=body)
    assert response.status_code == 201, response.text
    grant = response.json()
    review = case.http.get(
        path + "/review", params={"run_id": case.run["id"], "binding_request_id": body["expected_binding_request_id"]}
    )
    assert review.status_code == 200, review.text
    cut = review.json()
    command = dict(
        version=2,
        run_id=case.run["id"],
        expected_grant_digest=grant["digest"],
        expected_question_id=cut["question_id"],
        expected_response_digest=cut["response_digest"],
        expected_cut_digest=cut["digest"],
    )
    return binding, grant, command


def observed_resume(case, command):
    case.state["comments"].append(case.comment(50, "/forge resume " + json.dumps(command)))
    case.state["clock"] += 3600
    for _ in range(2):
        case.http.portal.call(case.service.step, case.repo["id"])
    return next(row for row in case.http.portal.call(case.retained)[2] if row["verification"]["resource_id"] == "50")


def test_polling_status_and_answer_consent_never_grants_continuation(native_poll):  # noqa: F811
    case = native_poll
    case.ready()
    case.consent()
    assert case.process(case.observe())["status"] == "processed"
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    command = dict(
        version=2,
        run_id=case.run["id"],
        expected_grant_digest="0" * 64,
        expected_question_id="poll-native-question",
        expected_response_digest=before[-1]["digest"],
        expected_cut_digest="1" * 64,
    )
    delivery = observed_resume(case, command)
    assert delivery["status"] == "received"
    result = case.process(delivery)
    assert result["status"] == "denied"
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before
    assert case.http.portal.call(case.store.get_run, case.run["id"])["status"] == "paused"


@pytest.mark.parametrize("changed", ["grant", "binding", "membership", "provider", "source", "unknown_effect"])
def test_current_polling_resume_scope_changes_refuse_without_dispatch(native_poll, changed):  # noqa: F811
    case = native_poll
    binding, grant, command = prepared(case)
    delivery = observed_resume(case, command)
    original = case.http.portal.call(case.store.get_run, case.run["id"])
    if changed == "grant":
        body = {
            **grant["request"],
            "request_id": uuid4().hex,
            "expected_previous_digest": grant["digest"],
            "active": False,
        }
        response = case.http.post(case.path + "/resume-grants", json=body)
        assert response.status_code == 201, response.text
    elif changed == "binding":
        body = {
            **binding["request"],
            "request_id": uuid4().hex,
            "expected_previous_digest": binding["digest"],
            "active": False,
        }
        response = case.http.post(case.path + "/bindings", json=body)
        assert response.status_code == 201, response.text
    elif changed == "membership":

        async def revoke():
            async with authenticated_operator(case.store):
                await ProjectAccess(case.store).set_membership(case.project["id"], case.mapped, "viewer")

        case.http.portal.call(revoke)
    elif changed == "provider":
        case.state["permission"] = "none"
    elif changed == "source":
        case.state["comments"][-1]["body"] += " edited"
    else:

        async def retain_unknown():
            async with case.store._unit():
                await case.journal._append(
                    case.run["id"],
                    "verify",
                    "unknown-native-tool",
                    "tool_intent",
                    {"tool": "owned-fixture-no-effect"},
                )

        case.http.portal.call(retain_unknown)
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    result = case.process(delivery)
    assert result["status"] in {"denied", "withheld"}
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before
    current = case.http.portal.call(case.store.get_run, case.run["id"])
    assert current["status"] == "paused"
    for key in ("repair_budget", "implementation_rounds", "model_selection", "execution_config", "base_revision"):
        assert current.get(key) == original.get(key)


def test_unknown_polling_resume_intent_is_held_without_replaying(native_poll):  # noqa: F811
    from firefly_forge.store import identifier

    case = native_poll
    _, _, command = prepared(case)
    delivery = observed_resume(case, command)

    async def retain_unknown():
        async with case.store._unit():
            row = await case.service.inbox.find_by_id(identifier(delivery["id"]))
            await case.collaboration._write_row(
                row, status="needs_reconciliation", action_status="writing", resume_intent={"known": False}
            )

    case.http.portal.call(retain_unknown)
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    result = case.process(delivery)
    assert result["status"] == "withheld"
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before
    exact = case.http.get("/api/v1/collaboration/receipts", params={"repository_id": case.repo["id"]})
    assert exact.status_code == 200
    assert next(row for row in exact.json() if row["id"] == delivery["id"])["action_status"] == "writing"


def test_polling_native_resume_and_completion_receipt_rollback_together(native_poll, monkeypatch):  # noqa: F811
    from firefly_forge.scm_poll_resume import SCMPollResumeAuthority

    case = native_poll
    _, _, command = prepared(case)
    delivery = observed_resume(case, command)
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    service = case.http.app.state.runtime.context.get_bean(SCMPollResumeAuthority)
    save = service._save_completion

    async def fail_after_save(result):
        await save(result)
        raise RuntimeError("Owned completion rollback fixture")

    monkeypatch.setattr(service, "_save_completion", fail_after_save)
    assert case.process(delivery)["status"] == "denied"
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before
    assert case.http.portal.call(case.store.get_run, case.run["id"])["status"] == "paused"


def test_lost_polling_resume_feedback_keeps_known_completion_and_holds_post(native_poll):  # noqa: F811
    case = native_poll
    _, _, command = prepared(case)
    delivery = observed_resume(case, command)
    case.state["lost_reply"] = True
    result = case.process(delivery)
    assert result["action_status"] == "completed" and result["feedback_status"] == "needs_reconciliation"
    entries = case.http.portal.call(case.journal.entries, case.run["id"])
    posts = len(case.state["posts"])
    replay = case.process(delivery)
    assert replay["action_result"] == result["action_result"]
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == entries
    assert len(case.state["posts"]) == posts


def test_polling_same_run_resume_needs_new_consent_and_keeps_original_completion(native_poll):  # noqa: F811
    case = native_poll
    case.ready()
    binding = case.consent()
    answered = case.process(case.observe())
    assert answered["status"] == "processed"
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    grant_path = case.path + "/resume-grants"
    body = dict(
        version=3,
        transport="github_rest_poll_v1",
        request_id=uuid4().hex,
        expected_previous_digest=None,
        expected_project_binding_digest=binding["project_binding_digest"],
        expected_binding_request_id=binding["request"]["request_id"],
        expected_binding_digest=binding["digest"],
        component_ids=["api"],
        allowed_actions=["resume"],
        active=True,
        acknowledge_same_run_resume=True,
    )
    configured = case.http.post(grant_path, json=body)
    assert configured.status_code == 201, configured.text
    grant = configured.json()
    assert grant["request"] == body and grant["version"] == 3
    assert case.http.post(grant_path, json=body).json() == grant
    assert case.http.get(grant_path + "/requests/" + body["request_id"]).json() == grant
    review = case.http.get(
        grant_path + "/review",
        params={"run_id": case.run["id"], "binding_request_id": body["expected_binding_request_id"]},
    )
    assert review.status_code == 200, review.text
    cut = review.json()
    command = dict(
        version=2,
        run_id=case.run["id"],
        expected_grant_digest=grant["digest"],
        expected_question_id=cut["question_id"],
        expected_response_digest=cut["response_digest"],
        expected_cut_digest=cut["digest"],
    )
    case.state["comments"].append(case.comment(50, "/forge resume " + json.dumps(command)))
    case.state["clock"] += 3600
    for _ in range(2):
        case.http.portal.call(case.service.step, case.repo["id"])
    delivery = next(
        row for row in case.http.portal.call(case.retained)[2] if row["verification"]["resource_id"] == "50"
    )
    assert delivery["status"] == "received"
    result = case.process(delivery)
    assert result["status"] == "processed" and result["action_status"] == "completed"
    assert result["run_id"] == case.run["id"]
    after = case.http.portal.call(case.journal.entries, case.run["id"])
    assert [row["kind"] for row in after[len(before) :]] == ["resume", "scm_resume_receipt"]
    assert case.process(result)["action_result"] == result["action_result"]
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == after
    assert case.http.portal.call(case.store.get_run, case.run["id"])["status"] == "queued"


@pytest.mark.parametrize("changed", ["source", "permission"])
def test_polling_resume_rechecks_remote_source_after_credential_wait(native_poll, monkeypatch, changed):  # noqa: F811
    case = native_poll
    _, _, command = prepared(case)
    delivery = observed_resume(case, command)
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    posts = len(case.state["posts"])
    admit = case.store.admit_credentials
    crossed = False

    async def change_after_admission(value):
        nonlocal crossed
        await admit(value)
        if not crossed and "grant" in value and value.get("command", {}).get("action") == "resume":
            crossed = True
            if changed == "source":
                case.state["comments"][-1]["body"] += " edited during credential admission"
            else:
                case.state["permission"] = "none"

    monkeypatch.setattr(case.store, "admit_credentials", change_after_admission)
    result = case.process(delivery)
    assert crossed
    assert result["status"] in {"denied", "withheld"}
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before
    assert case.http.portal.call(case.store.get_run, case.run["id"])["status"] == "paused"
    assert len(case.state["posts"]) == posts


@pytest.mark.parametrize("changed", ["source", "permission"])
def test_polling_feedback_rechecks_remote_source_after_credential_wait(native_poll, monkeypatch, changed):  # noqa: F811
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.scm_poll_resume import SCMPollResumeAuthority

    case = native_poll
    _, _, command = prepared(case)
    delivery = observed_resume(case, command)
    result = case.process(delivery)
    assert result["action_status"] == "completed"
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    posts = len(case.state["posts"])
    admit = case.store.admit_credentials
    crossed = False

    async def change_after_admission(value):
        nonlocal crossed
        await admit(value)
        if not crossed and value.get("id") == delivery["id"]:
            crossed = True
            if changed == "source":
                case.state["comments"][-1]["body"] += " edited during credential admission"
            else:
                case.state["permission"] = "none"

    monkeypatch.setattr(case.store, "admit_credentials", change_after_admission)
    service = case.http.app.state.runtime.context.get_bean(SCMPollResumeAuthority)
    with pytest.raises(AccessDenied):
        case.http.portal.call(service.authorize_feedback, delivery["id"])
    assert crossed
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before
    assert len(case.state["posts"]) == posts
