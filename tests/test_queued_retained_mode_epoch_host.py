# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A completed verification handoff must retain native epoch custody at queued Engine entry."""
# ruff: noqa: F811

import copy
import json
import traceback
from dataclasses import replace

import httpx2
import pytest
import test_final_outcome_epoch_scope_host as completed
from test_final_outcome_epoch_scope_host import completed_legacy_final, frozen_original_write_grant  # noqa: F401

pytest_plugins = ["test_invalid_read_retained_epoch_engine_host"]


@pytest.fixture(autouse=True)
def fresh_checkpoint_consumer():
    from firefly_forge.agent_checkpoints import AgentCheckpoints

    return AgentCheckpoints._consume


@pytest.mark.parametrize(
    "interrupted",
    [None, "artifact", "receipt"],
    ids=["complete", "uncertain-artifact-retry-held", "before-terminal-receipt-held"],
)
async def test_completed_final_queued_engine_reaches_qa_without_replaying_consumed_output(
    completed_legacy_final, monkeypatch, fresh_checkpoint_consumer, interrupted
):
    from test_agentic_checkpoint_activation_host import controlled_source_snapshot

    c = completed_legacy_final
    await completed.test_completed_final_allows_explicit_current_policy_adoption_and_checks_without_replay(
        c, monkeypatch
    )
    # A handoff authorizes a fresh ordinary round; the accepted final POST/consume stay immutable.
    from test_agentic_checkpoint_activation_host import result

    async def next_round(*args, **kwargs):
        return await type(c.provider).execute(c.provider, *args, **kwargs)

    def transport(request):
        if request.method == "GET":
            from test_bridge_completion_callbacks import CAPABILITY

            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        assert request.method == "POST"
        c.posts.append(request.content)
        response = result(
            json.dumps({"summary": "Existing source checks passed", "qa_report": None, "worker_feedback": None})
        )
        response["model"] = c.provider.settings["FORGE_BRIDGE_MODEL"]
        return httpx2.Response(200, json=response)

    from firefly_forge.agent_checkpoints import AgentCheckpoints

    monkeypatch.setattr(AgentCheckpoints, "_consume", fresh_checkpoint_consumer)
    monkeypatch.setattr(c.provider, "execute", next_round)
    c.provider.model().client._client._transport = httpx2.MockTransport(transport)
    from firefly_forge.bridge_completions import BridgeCompletions

    failures = []
    original_scope = BridgeCompletions.scope

    def scope(self, *args, **kwargs):
        bound = original_scope(self, *args, **kwargs)

        async def response(*values):
            try:
                await bound.response(*values)
            except Exception:
                failures.append(traceback.format_exc())
                raise

        return replace(bound, response=response)

    monkeypatch.setattr(BridgeCompletions, "scope", scope)
    c.store.run.data.setdefault("verification", [])
    c.store.run.data.setdefault("artifacts", [])
    before = copy.deepcopy(await c.store.get_run(c.run_id))
    entries = copy.deepcopy(await c.journal.entries(c.run_id))
    reached = []
    snapshots = []
    original_source = (c.workspace / "existing.py").read_bytes()

    async def snapshot(*args, **kwargs):
        snapshots.append(True)
        if interrupted == "receipt" and len(snapshots) == 1:
            raise RuntimeError("Controlled implementation receipt interruption")
        return ["existing.py"], controlled_source_snapshot("Retained verified source"), "c" * 40

    real_qa = c.engine._independent_review
    real_checkpoint = c.engine._checkpoint_commit

    async def qa(*args, **kwargs):
        reached.append("independent-review")
        await real_qa(*args, **kwargs)

    async def checkpoint(*args, **kwargs):
        reached.append("checkpoint")
        await real_checkpoint(*args, **kwargs)

    from firefly_forge import engine as engine_module

    original_checked = engine_module.checked
    git_calls = []

    async def checked(argv, cwd, *args, **kwargs):
        assert cwd == c.workspace
        if argv == ["git", "rev-parse", "HEAD"]:
            return "d" * 40
        if argv == ["git", "rev-parse", "HEAD^{tree}"]:
            return "c" * 40
        if argv[:2] == ["git", "-c"] and "commit" in argv:
            git_calls.append(argv)
            return ""
        return await original_checked(argv, cwd, *args, **kwargs)

    monkeypatch.setattr(engine_module, "checked", checked)

    async def artifact(*args, **kwargs):
        reached.append("artifact")
        if interrupted == "artifact" and reached.count("artifact") == 1:
            raise RuntimeError("Controlled artifact interruption")

    monkeypatch.setattr(c.engine, "_snapshot", snapshot)
    monkeypatch.setattr(c.engine, "_independent_review", qa)
    monkeypatch.setattr(c.engine, "_checkpoint_commit", checkpoint)
    monkeypatch.setattr(c.engine, "_artifact", artifact)
    await c.engine.execute_run(c.run_id)
    if interrupted:
        failed = await c.store.get_run(c.run_id)
        expected = (
            "Controlled artifact interruption"
            if interrupted == "artifact"
            else "Controlled implementation receipt interruption"
        )
        assert failed["status"] == "failed" and failed["error"] == expected
        spent = copy.deepcopy(failed["usage"])
        assert len(c.posts) == 4
        await c.engine.execute_run(c.run_id)
        assert (await c.store.get_run(c.run_id))["usage"] == spent
    current = await c.store.get_run(c.run_id)
    if interrupted:
        assert current["status"] == "failed"
        expected = (
            "Controlled implementation receipt interruption"
            if interrupted == "receipt"
            else "Task does not need reconciliation"
        )
        assert current["error"] == expected
        assert reached == (
            ["independent-review", "checkpoint", "artifact", "independent-review"] if interrupted == "artifact" else []
        )
    else:
        assert current["status"] == "succeeded", (current.get("error"), failures)
        assert reached == ["independent-review", "checkpoint", "artifact"]
    assert len(git_calls) == (0 if interrupted == "receipt" else 1)
    if interrupted != "receipt":
        assert current["checkpoint_revision"] == "d" * 40
        assert current["review"]["status"] == "not_requested"
    assert (c.workspace / "existing.py").read_bytes() == original_source
    assert len(c.posts) == 4
    for key, added in (("requests", 1), ("input_tokens", 11), ("output_tokens", 3)):
        assert current["usage"][key] == before["usage"][key] + added
    assert current["usage"]["runtime_report"]["summary"] == "Existing source checks passed"
    assert current["usage"]["retained_source"]["attempt"] == 3
    assert current["sandbox_snapshot"] == before["sandbox_snapshot"]
    assert current["tool_policy_snapshot"] == before["tool_policy_snapshot"]
    assert before["implementation_rounds"] == 2 and current["implementation_rounds"] == 3
    after = await c.journal.entries(c.run_id)
    assert after[: len(entries)] == entries
    original_consumes = [e for e in entries if e["kind"] == "agent_consume"]
    consumes = [e for e in after if e["kind"] == "agent_consume"]
    assert consumes[:-1] == original_consumes and len(consumes) == len(original_consumes) + 1
    assert consumes[-1]["payload"]["checkpoint_record_digest"] not in {
        e["payload"]["checkpoint_record_digest"] for e in original_consumes
    }
    from firefly_forge.checkpoint_mode_runtime import _CURRENT as mode
    from firefly_forge.invalid_read_epochs import _CURRENT as epoch
    from firefly_forge.unknown_command_proof import _COMPLETED_FINAL

    assert mode.get() is epoch.get() is _COMPLETED_FINAL.get() is None


@pytest.mark.parametrize("change", ["actor", "policy", "terminal", "entry-error"])
async def test_queued_completed_final_refuses_changed_authority_and_restores_context(
    completed_legacy_final, monkeypatch, change
):
    from firefly_forge.checkpoint_mode_runtime import _CURRENT as mode
    from firefly_forge.invalid_read_epochs import _CURRENT as epoch
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.unknown_command_proof import _COMPLETED_FINAL

    c = completed_legacy_final
    await completed.test_completed_final_allows_explicit_current_policy_adoption_and_checks_without_replay(
        c, monkeypatch
    )
    before = copy.deepcopy(await c.store.get_run(c.run_id))
    consumes = [copy.deepcopy(e) for e in await c.journal.entries(c.run_id) if e["kind"] == "agent_consume"]
    if change == "actor":
        c.user.enabled = False
    elif change == "policy":
        write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
    elif change == "terminal":
        row = next(r for r in c.journal.records.rows if r.kind == "terminal_completion_receipt")
        row.data["payload"]["proof"]["task_key"] = "forged-task"
    else:

        async def refused(*args, **kwargs):
            assert mode.get() is not None and epoch.get() is not None and _COMPLETED_FINAL.get() is not None
            raise RuntimeError("Controlled retained entry interruption")

        monkeypatch.setattr(c.engine, "_inspect_workspace", refused)
    await c.engine.execute_run(c.run_id)
    current = await c.store.get_run(c.run_id)
    assert current["status"] == "failed" and current["error"]
    assert len(c.posts) == 3
    assert current["usage"] == before["usage"]
    assert [copy.deepcopy(r.data) for r in c.journal.records.rows if r.kind == "agent_consume"] == consumes
    assert mode.get() is epoch.get() is _COMPLETED_FINAL.get() is None
