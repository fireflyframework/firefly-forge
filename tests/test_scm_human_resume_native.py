# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual DI, identity, consent and Journal; only remote SCM HTTP is recorded."""

import copy

import pytest
from scm_polling_native_fixtures import native_poll  # noqa: F401
from test_scm_poll_resume_native import observed_resume, prepared
from test_scm_resume_native import native_resume  # noqa: F401

from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_poll_resume import SCMPollResumeAuthority
from firefly_forge.scm_resume_service import SCMResumeAuthority


def exercise(case, service_type, delivery, monkeypatch, changed):
    service = case.http.app.state.runtime.context.get_bean(service_type)
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    original = case.http.portal.call(case.store.get_run, case.run["id"])
    observed = []
    authorize = getattr(service, "authorize_snapshot", None)

    async def revalidate(snapshot, project, permission):
        observed.append(copy.deepcopy(snapshot))
        if changed == "context":
            snapshot = {**snapshot, "scm_delegation": {**snapshot["scm_delegation"], "grant_digest": "0" * 64}}
        elif changed == "identity":
            async with case.store._unit():
                identities = ForgeIdentityUserRepository()
                identity = await identities.find_by_id(case.mapped.id)
                identity.enabled = False
                await identities.save(identity)
        await authorize(snapshot, project, permission)

    if authorize is not None:
        monkeypatch.setattr(service, "authorize_snapshot", revalidate)
    result = case.process(delivery)
    after = case.http.portal.call(case.journal.entries, case.run["id"])
    if changed:
        assert result["status"] in {"denied", "withheld"}
        assert after == before
        assert case.http.portal.call(case.store.get_run, case.run["id"])["status"] == "paused"
    else:
        assert result["status"] == "processed" and result["action_status"] == "completed", result
        assert [entry["kind"] for entry in after[len(before) :]] == ["resume", "scm_resume_receipt"]
        assert Journal.verify_chain(after)
        link = after[-2]["payload"]["human_clarification"]
        assert link["resuming_actor"] == {"user_id": case.mapped.id.hex, "authentication_method": "scm_mapping"}
        current = case.http.portal.call(case.store.get_run, case.run["id"])
        for field in ("execution_actor", "role_snapshot", "repair_budget", "execution_config", "retry_count"):
            assert current.get(field) == original.get(field)
        assert case.process(delivery)["action_result"] == result["action_result"]
        assert case.http.portal.call(case.journal.entries, case.run["id"]) == after

    async def cannot_reuse():
        assert CURRENT_PRINCIPAL.get() is None
        with pytest.raises(AccessDenied):
            await SCMActorAuthority(case.store).authorize_snapshot(observed[0], case.project["id"], "execute")

    assert observed
    case.http.portal.call(cannot_reuse)


@pytest.mark.parametrize("native_resume", ["implementation"], indirect=True)
@pytest.mark.parametrize("changed", [None, "context", "identity"])
def test_signed_bounded_human_resume_uses_verified_native_actor(native_resume, monkeypatch, changed):  # noqa: F811
    case = native_resume
    command = case.review(case.consent())
    delivery = case.receive("resume", command)
    exercise(case, SCMResumeAuthority, delivery, monkeypatch, changed)


@pytest.mark.parametrize("native_poll", ["implementation"], indirect=True)
@pytest.mark.parametrize("changed", [None, "context", "identity"])
def test_polling_bounded_human_resume_uses_verified_native_actor(native_poll, monkeypatch, changed):  # noqa: F811
    case = native_poll
    _, _, command = prepared(case)
    delivery = observed_resume(case, command)
    exercise(case, SCMPollResumeAuthority, delivery, monkeypatch, changed)
