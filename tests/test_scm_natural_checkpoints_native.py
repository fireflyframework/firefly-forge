# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Registered API/real PostgreSQL consent; AI and GitHub are controlled inputs."""

from functools import partial
from uuid import uuid4

import pytest
from scm_polling_native_fixtures import native_poll as native_poll
from test_scm_natural_native import assert_original, observe, prepare
from worker_reply_fixtures import authenticated_operator

from firefly_forge.collaboration import CollaborationSettings
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.scm_natural_tasks import SCMNaturalTasks


def checkpoint_actor(case, receipt):
    return case.http.portal.call(partial(case.service.resolve_checkpoint, receipt, remote=False))


def test_native_checkpoint_interpretation_never_inherits_answer_authority(native_poll, monkeypatch):
    case = native_poll
    prepare(case, monkeypatch)
    case.process(case.observe("status"))
    original = case.http.portal.call(case.store.get_run, case.run["id"])
    wal = case.http.portal.call(case.journal.entries, case.run["id"])
    case.state["natural_intent"] = "checkpoint"
    delivery = observe(case, "@forge, save this progress as a draft pull request.")
    result = case.process(delivery)
    assert result["natural_turn"]["state"] == "checkpoint_pending_permission", result
    assert result["natural_turn"]["resolution"]["run_id"] == case.run["id"]
    assert result.get("natural_checkpoint") is None and result["feedback_status"] == "sent"
    assert case.process(delivery) == result and case.state["natural_calls"] == 1
    assert_original(case, original, wal)


def test_native_current_checkpoint_mapping_is_not_native_administrator_authority(native_poll, monkeypatch):
    case = native_poll
    prepare(case, monkeypatch)
    case.process(case.observe("status"))
    case.state["natural_intent"] = "checkpoint"
    old = case.process(observe(case, "@forge, save the current progress."))

    async def promote():
        async with authenticated_operator(case.store):
            await ProjectAccess(case.store).set_membership(case.project["id"], case.mapped, "maintainer")
        natural = SCMNaturalTasks(case.collaboration)
        scopes = await natural.conversations.scopes(old, remote=False)
        return await case.service._head(case.project["id"], case.repo["id"], scopes[0]["binding_key"])

    head = case.http.portal.call(promote)
    body = {
        **head["request"],
        "request_id": uuid4().hex,
        "expected_previous_digest": head["digest"],
        "allowed_actions": ["respond", "status", "checkpoint"],
    }
    response = case.http.post(case.path + "/bindings", json=body)
    assert response.status_code == 201, response.text
    settings = case.http.get(f"/api/v1/repositories/{case.repo['id']}/collaboration").json()
    settings = {key: value for key, value in settings.items() if key in CollaborationSettings.model_fields}
    settings["channel_commands"] = ["respond", "publish"]
    response = case.http.put(f"/api/v1/repositories/{case.repo['id']}/collaboration", json=settings)
    assert response.status_code == 200, response.text
    result = case.process(observe(case, "@forge, save the current progress as a draft."))
    actor = checkpoint_actor(case, result)
    assert actor.id == case.mapped.id and not ProjectAccess.is_admin(actor)
    assert actor.scm_delegation["run_id"] == case.run["id"]
    with pytest.raises(AccessDenied):
        checkpoint_actor(case, old)
    assert result["natural_turn"]["state"] == "checkpoint_pending_permission"
    assert result.get("natural_checkpoint") is None
