# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""CLI feedback keeps its transaction; incomplete Bridge output cannot dispatch."""

import pytest
from test_native_terminal_completion_host import observe_boundary


@pytest.mark.parametrize("checkpoint_due", [True, False])
async def test_missing_normalized_report_cannot_reach_git_or_verification(tmp_path, monkeypatch, checkpoint_due):
    async def configure(env):
        execute = env.provider.execute

        async def missing(*args, **kwargs):
            result = await execute(*args, **kwargs)
            result.usage.pop("runtime_report", None)
            return result

        monkeypatch.setattr(env.provider, "execute", missing)

    observed = await observe_boundary(
        tmp_path, monkeypatch, checkpoint_due, configure=configure, expected_boundary=False
    )
    assert observed["run"]["bridge_completion_hold"]
    assert "normalized RuntimeReport" in observed["run"]["error"]
    assert not any(entry["kind"] == "terminal_completion_receipt" for entry in observed["entries"])


async def test_source_budget_scope_refuses_bridge_before_any_effect():
    from firefly_forge.source_edit_continuations import source_edit_scope

    with pytest.raises(ValueError, match="official Codex"):
        await source_edit_scope(None, {"agent": "bridge"})


@pytest.mark.parametrize("checkpoint_due", [True, False])
async def test_historical_held_normal_acceptance_cannot_escape_to_git_or_qa(tmp_path, monkeypatch, checkpoint_due):
    async def configure(env):
        receipt = env.journal.receipt

        async def historical(run, key, evidence, *, checkpoint=None):
            if "implementation_round" in evidence:
                env.store.run.data.get("usage", {}).pop("runtime_report", None)
            return await receipt(run, key, evidence, checkpoint=checkpoint)

        monkeypatch.setattr(env.journal, "receipt", historical)

    observed = await observe_boundary(
        tmp_path, monkeypatch, checkpoint_due, configure=configure, expected_boundary=False
    )
    assert observed["task"]["status"] == "done"
    assert observed["run"]["bridge_completion_hold"]
    assert "before Git or verification" in observed["run"]["error"]
    assert not any(entry["kind"] == "terminal_completion_receipt" for entry in observed["entries"])


@pytest.mark.parametrize("agent", ["codex", "claude"])
async def test_cli_feedback_keeps_its_existing_writer_unit(tmp_path, monkeypatch, agent):
    from pyfly.data.transaction import current_unit_of_work
    from test_agentic_checkpoint_activation_host import activated
    from test_bridge_inflight_cancel import RUN

    from firefly_forge.agent_roles import WorkerFeedback

    env = await activated(tmp_path, monkeypatch)
    env.store.run.data.update(agent=agent, status="developing")
    try:
        await env.journal.intent(RUN, "implement")
        feedback = WorkerFeedback(
            id="native-cli-result",
            role_id="implementer",
            task_id="implement",
            attempt=1,
            disposition="completed",
            summary="CLI source work is retained",
        )
        async with env.store._unit():
            unit = current_unit_of_work()
            assert unit is not None
            record = await env.journal.worker_feedback(RUN, feedback)
            assert current_unit_of_work() is unit
        assert record["kind"] == "worker_feedback"
        assert not any(row.kind == "terminal_completion_receipt" for row in env.journal.records.rows)
    finally:
        await env.model.client.close()
