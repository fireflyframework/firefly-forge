# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Public same-run admission after passed native QA and an undispatched worker failure."""

# ruff: noqa: F811
import copy

import pytest
import test_final_outcome_epoch_scope_host as completed
from test_final_outcome_epoch_scope_host import completed_legacy_final, frozen_original_write_grant  # noqa: F401

pytest_plugins = ["test_invalid_read_retained_epoch_engine_host"]


@pytest.fixture(autouse=True)
def fresh_checkpoint_consumer():
    from firefly_forge.agent_checkpoints import AgentCheckpoints

    return AgentCheckpoints._consume


@pytest.fixture
async def failed_verified_entry(completed_legacy_final, monkeypatch):
    from firefly_forge import checkpoint_mode_runtime

    c = completed_legacy_final
    await completed.test_completed_final_allows_explicit_current_policy_adoption_and_checks_without_replay(
        c, monkeypatch
    )
    before = copy.deepcopy(await c.store.get_run(c.run_id))
    entries = copy.deepcopy(await c.journal.entries(c.run_id))

    async def refused(*args, **kwargs):
        raise ValueError("Fictional pre-dispatch authority preparation failure")

    with monkeypatch.context() as m:
        m.setattr(checkpoint_mode_runtime, "prepare_current_mode", refused)
        await c.engine.execute_run(c.run_id)
    current = await c.store.get_run(c.run_id)
    assert current["status"] == "failed"
    assert await c.journal.entries(c.run_id) == entries
    assert current["usage"] == before["usage"] and len(c.posts) == 3
    c.before_failed_resume = before
    return c


async def test_native_failed_verified_resume_queues_same_run_without_replay(failed_verified_entry):
    c = failed_verified_entry
    before = copy.deepcopy(await c.store.get_run(c.run_id))
    entries = copy.deepcopy(await c.journal.entries(c.run_id))
    task = await c.journal._task(c.run_id, "implement")
    previous_task = copy.deepcopy(vars(task))
    resumed = await c.journal.resume(c.run_id)
    assert resumed["id"] == c.run_id and resumed["status"] == "queued"
    assert resumed["usage"] == before["usage"]
    assert resumed["sandbox_snapshot"] == before["sandbox_snapshot"]
    assert resumed["verification_handoff"] == before["verification_handoff"]
    after = await c.journal.entries(c.run_id)
    assert after[:-1] == entries and after[-1]["kind"] == "resume"
    assert len(c.posts) == 3

    assert vars(await c.journal._task(c.run_id, "implement")) == previous_task
    assert after[-1]["payload"]["automatic"] is False
    assert after[-1]["payload"]["verified_pre_dispatch_resume"]["pre_dispatch_resume_digest"] == entries[-1]["digest"]
    with pytest.raises(ValueError, match="Only a paused"):
        await c.journal.resume(c.run_id)
    assert await c.journal.entries(c.run_id) == after


@pytest.mark.parametrize(
    "option", [{"auto": True}, {"agent": "bridge"}, {"agent": "agentic"}, {"model_profile_id": "alternate"}]
)
async def test_failed_resume_never_changes_provider_or_automatically_requeues(failed_verified_entry, option):
    c = failed_verified_entry
    before = copy.deepcopy(await c.journal.entries(c.run_id))
    with pytest.raises(ValueError, match="explicit unchanged-provider"):
        await c.journal.resume(c.run_id, **option)
    assert (await c.store.get_run(c.run_id))["status"] == "failed"
    assert await c.journal.entries(c.run_id) == before
    assert len(c.posts) == 3


@pytest.mark.parametrize(
    "change",
    [
        "ordinary-failure",
        "source",
        "actor",
        "owner",
        "usage-bool",
        "usage-empty",
        "usage-increased",
        "usage-float",
        "round-bool",
        "round-increased",
        "failed-qa",
        "task-running",
        "task-attempt",
        "task-key",
        "terminal",
        "chain",
        "sandbox-cleanup",
        "pending-effect",
        "new-intent",
        "policy",
        "publishing",
        "cancel",
        "ticket",
    ],
)
async def test_failed_resume_refuses_changed_custody_or_dispatched_work(failed_verified_entry, monkeypatch, change):
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.project_access import AccessDenied

    c = failed_verified_entry
    data = c.store.run.data
    task = await c.journal._task(c.run_id, "implement")
    if change == "ordinary-failure":
        data.pop("invalid_read_continuity")
    elif change == "source":
        (c.workspace / "existing.py").write_text("CHANGED = True\n")
    elif change == "actor":
        c.user.enabled = False
    elif change == "owner":

        async def owner_lost():
            raise RuntimeError("Controlled native execution ownership lost")

        monkeypatch.setattr(c.store, "assert_ownership", owner_lost)
    elif change.startswith("usage-"):
        if change == "usage-empty":
            data["usage"] = {}
        else:
            value = data["usage"]["requests"]
            data["usage"]["requests"] = {
                "usage-bool": False,
                "usage-increased": value + 1,
                "usage-float": float(value),
            }[change]
    elif change.startswith("round-"):
        data["implementation_rounds"] = False if change == "round-bool" else data["implementation_rounds"] + 1
    elif change == "failed-qa":
        data["verification_handoff"]["failed_commands"] = 1
    elif change == "task-running":
        task.status = "running"
    elif change == "task-attempt":
        task.attempt += 1
    elif change == "task-key":
        task.data["idempotency_key"] = "forged-task"
    elif change == "terminal":
        row = next(r for r in c.journal.records.rows if r.kind == "terminal_completion_receipt")
        row.data["payload"]["proof"]["task_key"] = "forged-task"
    elif change == "chain":
        c.journal.records.rows[-1].digest = "f" * 64
    elif change == "sandbox-cleanup":
        row = c.journal.sandboxes.rows[-1]
        row.data["effects"]["cleanup"]["status"] = "executing"
    elif change in {"pending-effect", "new-intent"}:
        await c.journal._append(
            c.run_id, "implement", "after-qa", "sandbox_intent" if change == "pending-effect" else "intent", {}
        )
    elif change == "policy":
        write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
    elif change == "publishing":
        data["publication_status"] = "publishing"
    elif change == "cancel":
        data["cancel_request"] = {"status": "requested"}
    elif change == "ticket":
        c.requests.records.rows[0].status = "needs_reconciliation"
        c.requests.records.rows[0].data["status"] = "needs_reconciliation"
    before = copy.deepcopy(data)
    entries = copy.deepcopy([r.data for r in c.journal.records.rows])
    with pytest.raises((ValueError, RuntimeError, AccessDenied)):
        await c.journal.resume(c.run_id)
    assert c.store.run.data == before
    assert [r.data for r in c.journal.records.rows] == entries
    assert len(c.posts) == 3


@pytest.mark.parametrize("change", ["source", "task", "usage", "ticket", "policy", "profile"])
async def test_failed_resume_rechecks_locked_admission(failed_verified_entry, monkeypatch, change):
    c = failed_verified_entry
    selected = c.journal._resume_selected

    async def late(*args, **kwargs):
        if change == "source":
            (c.workspace / "existing.py").write_text("LATE = True\n")
        elif change == "task":
            (await c.journal._task(c.run_id, "implement")).attempt += 1
        elif change == "usage":
            c.store.run.data["usage"]["requests"] += 1
        elif change == "ticket":
            c.requests.records.rows[0].data["status"] = "needs_reconciliation"
        elif change == "policy":
            from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

            write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
        else:
            from firefly_forge import invalid_read_continuities

            monkeypatch.setattr(invalid_read_continuities, "current_profile_seal", lambda: {"profile_id": "changed"})
        return await selected(*args, **kwargs)

    monkeypatch.setattr(c.journal, "_resume_selected", late)
    before = copy.deepcopy(await c.journal.entries(c.run_id))
    with pytest.raises(ValueError):
        await c.journal.resume(c.run_id)
    assert c.store.run.status == "failed"
    assert await c.journal.entries(c.run_id) == before
    assert len(c.posts) == 3


async def test_public_failed_resume_reaches_next_qa_checkpoint_without_replay(
    completed_legacy_final, monkeypatch, fresh_checkpoint_consumer
):
    import test_queued_retained_mode_epoch_host as queued

    from firefly_forge import checkpoint_mode_runtime

    c = completed_legacy_final
    execute = c.engine.execute_run
    entered = False

    async def recovered(run_id, **kwargs):
        nonlocal entered
        assert not entered
        entered = True
        before = copy.deepcopy(await c.store.get_run(run_id))
        prefix = copy.deepcopy(await c.journal.entries(run_id))

        async def refused(*args, **kwargs):
            raise ValueError("Controlled undispatched authority failure")

        with monkeypatch.context() as patch:
            patch.setattr(checkpoint_mode_runtime, "prepare_current_mode", refused)
            await execute(run_id, **kwargs)
        assert (await c.store.get_run(run_id))["status"] == "failed"
        assert await c.journal.entries(run_id) == prefix
        resumed = await c.journal.resume(run_id)
        assert resumed["usage"] == before["usage"] and len(c.posts) == 3
        await execute(run_id, **kwargs)

    monkeypatch.setattr(c.engine, "execute_run", recovered)
    await queued.test_completed_final_queued_engine_reaches_qa_without_replaying_consumed_output(
        c, monkeypatch, fresh_checkpoint_consumer, None
    )
    assert entered
