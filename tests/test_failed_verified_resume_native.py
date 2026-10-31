# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Public same-run resume after native QA and a known undispatched failed entry."""

import copy

import pytest
import test_final_outcome_next_dispatch_native as next_dispatch

pytest_plugins = ["test_invalid_read_continuity_native"]


@pytest.mark.parametrize("outcome", ["success", "qa_red"])
async def test_failed_native_resume_retains_same_budget_and_reaches_next_dispatch(
    native_continuity, monkeypatch, outcome
):
    from firefly_forge.project_access import ProjectAccess

    accept = next_dispatch.accept_and_verify

    async def accept_then_fail(c, patch):
        await accept(c, patch)
        before = copy.deepcopy(await c.store.get_run(c.run_id))
        prefix = copy.deepcopy(await c.journal.entries(c.run_id))
        executions = copy.deepcopy(await c.journal.sandbox_executions(c.run_id))
        authorize = ProjectAccess.authorize_execution

        async def refuse_entry(self, run):
            if run.get("status") == "preparing":
                raise ValueError("Controlled pre-dispatch current-authority preparation failure")
            return await authorize(self, run)

        with patch.context() as failed:
            failed.setattr(ProjectAccess, "authorize_execution", refuse_entry)
            await c.engine.execute_run(c.run_id)
        current = await c.store.get_run(c.run_id)
        assert current["status"] == "failed"
        assert current["usage"] == before["usage"]
        assert await c.journal.entries(c.run_id) == prefix
        assert await c.journal.sandbox_executions(c.run_id) == executions
        resumed = await c.journal.resume(c.run_id)
        assert resumed["status"] == "queued" and resumed["id"] == c.run_id
        assert resumed["usage"] == before["usage"]
        assert resumed["implementation_rounds"] == before["implementation_rounds"]
        assert resumed["verification_handoff"] == before["verification_handoff"]
        after = await c.journal.entries(c.run_id)
        assert after[:-1] == prefix and after[-1]["kind"] == "resume"
        assert after[-1]["payload"]["automatic"] is False
        assert (
            after[-1]["payload"]["verified_pre_dispatch_resume"]["pre_dispatch_resume_digest"] == prefix[-1]["digest"]
        )
        with pytest.raises(ValueError, match="Only a paused"):
            await c.journal.resume(c.run_id)
        assert await c.journal.entries(c.run_id) == after

    monkeypatch.setattr(next_dispatch, "accept_and_verify", accept_then_fail)
    await next_dispatch.test_native_known_final_verification_queues_a_real_next_dispatch_without_prior_replay(
        native_continuity, monkeypatch, outcome
    )
