# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native history ordering with fictional SCM/owner seams, not physical lease proof."""

from types import SimpleNamespace
from uuid import UUID

import pytest
from linked_verification_fixtures import candidate, receipt

from firefly_forge import linked_verifications as module
from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import LinkedVerificationCandidateV1, LinkedVerificationReceiptV1
from firefly_forge.project_access import AccessDenied

pytest_plugins = ["test_linked_verification_source_outputs_host"]


@pytest.mark.parametrize("revoke_after_locks", [False, True])
async def test_scm_successor_refreshes_current_history_locks_before_full_authority_fence(
    source_case, monkeypatch, revoke_after_locks
):
    p = source_case.prepared
    value = receipt(candidate(1), status="needs_reconciliation")
    value["progress"].update(started=1, completed=1, unknown=False)
    parent = LinkedVerificationReceiptV1.model_validate(value)
    p.run["attempt"] = 1
    child = p.candidate.model_dump(mode="json")
    child.update(
        run_digest=digest(p.run),
        successor=dict(
            predecessor_request_id=parent.request_id,
            predecessor_event_digest=parent.event_digest,
            parent_attempt=1,
            output_policy="vitest_no_cache_v1",
        ),
        execution_plan=[
            dict(
                ordinal=1,
                logical_command_digest="1" * 64,
                physical_argv=["fictional-wrapper", "--", "--no-cache"],
                output_policy="vitest_no_cache_v1",
            )
        ],
    )
    child["digest"] = digest({key: item for key, item in child.items() if key != "digest"})
    p.candidate = LinkedVerificationCandidateV1.model_validate(child)
    p.operation_id = "f" * 32
    p.history = (parent,)
    p.history_digest = module._history_digest(p.history)
    p.history_unit = object()
    unit = object()
    monkeypatch.setattr(module, "current_unit_of_work", lambda: unit)
    state = {"grant": True, "views": 0, "locks": 0}

    async def current():
        pass

    def view():
        state["views"] += 1

    def grant_fence():
        if not state["grant"]:
            raise AccessDenied("Current SCM grant was revoked")

    p.scm = SimpleNamespace(current=current, fence=grant_fence)
    monkeypatch.setattr(p, "_scm_view", view)
    row = SimpleNamespace(
        id=UUID(parent.request_id),
        run_id=UUID(parent.run_id),
        kind=parent.kind,
        status=parent.status,
        request_key="fictional",
        data=parent.model_dump(mode="json"),
    )

    async def history(run_id, *, exclude, lock):
        assert run_id == p.run["id"] and exclude == p.operation_id and lock is True
        assert state["views"] > 0
        state["locks"] += 1
        if revoke_after_locks:
            state["grant"] = False
        return [row], [parent]

    monkeypatch.setattr(p.service, "_history", history)
    if revoke_after_locks:
        with pytest.raises(AccessDenied, match="revoked"):
            await p.current()
    else:
        await p.current()
        assert p.history_unit is unit
        p.fence()
    assert state["locks"] == 1
