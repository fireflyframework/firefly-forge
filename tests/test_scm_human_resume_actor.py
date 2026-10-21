# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Journal actor handling; persistence and SCM source checks use controlled host fixtures."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_human_clarification_continuation_host import (
    answered_question,
    clarification_setup,  # noqa: F401
)

from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_poll_resume import SCMPollResumeAuthority
from firefly_forge.scm_resume_service import SCMResumeAuthority


@pytest.mark.parametrize("service_type", [SCMResumeAuthority, SCMPollResumeAuthority])
@pytest.mark.parametrize("changed", [None, "revoked", "context", "identity"])
async def test_verified_scm_resume_uses_real_identity_and_scoped_authority(
    clarification_setup,  # noqa: F811
    monkeypatch,
    service_type,
    changed,
):
    store, journal, identity_actor = await answered_question(clarification_setup)
    project = uuid4().hex
    store.run.data.update(project_id=project, component_id="api")
    store.schema = "host_fixture"
    store.scm_resume_authorities = {}
    revoked = False

    async def execute(*args):
        pass

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(execute=execute)

    async def original(*args):
        pass

    store._unit = unit
    monkeypatch.setattr(ProjectAccess, "authorize_execution", original)

    async def memberships(*args):
        return [SimpleNamespace(project_id=UUID(project), role="developer")]

    monkeypatch.setattr("firefly_forge.access_persistence.ForgeMembershipRepository.for_user", memberships)
    delivery = {"id": uuid4().hex, "command": {"run_id": store.run.id.hex, "action": "resume"}}
    context = {
        "run": copy.deepcopy(store.run.data),
        "user_id": identity_actor.id.hex,
        "binding": {"digest": "a" * 64},
        "grant": {"digest": "b" * 64},
    }

    class Authority(service_type):
        async def _delivery(self, receipt_id):
            assert receipt_id == delivery["id"]
            return delivery

        async def _context(self, value):
            assert value is delivery
            if revoked:
                raise AccessDenied("Current resume grant revoked")
            return context

    service = Authority(store, None, journal, None, None, None)
    monkeypatch.setattr("firefly_forge.scm_resume_service.current_unit_of_work", lambda: object())
    delivery.update(status="needs_reconciliation", action_status="writing")
    store.scm_resume_authorities[service.actor_transport] = service
    observed = []
    authorize = service.authorize_snapshot

    async def guarded(snapshot, *args):
        observed.append(copy.deepcopy(snapshot))
        nonlocal revoked
        if changed == "revoked":
            revoked = True
        elif changed == "context":
            snapshot = {**snapshot, "scm_delegation": {**snapshot["scm_delegation"], "grant_digest": "c" * 64}}
        elif changed == "identity":
            store.identity_enabled = False
        await authorize(snapshot, *args)

    monkeypatch.setattr(service, "authorize_snapshot", guarded)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        if changed:
            with pytest.raises(AccessDenied):
                await service._resume(delivery, context)
            assert store.run.status == "paused"
            assert not any(row.kind == "resume" for row in journal.records.rows)
        else:
            result = await service._resume(delivery, context)
            assert result["status"] == "queued"
            link = next(
                row.data["payload"]["human_clarification"] for row in journal.records.rows if row.kind == "resume"
            )
            assert link["resuming_actor"] == {
                "user_id": identity_actor.id.hex,
                "authentication_method": "scm_mapping",
            }
        assert CURRENT_PRINCIPAL.get() is None
        if observed:
            with pytest.raises(AccessDenied):
                await SCMActorAuthority(store).authorize_snapshot(observed[0], project, "execute")
    finally:
        CURRENT_PRINCIPAL.reset(token)
