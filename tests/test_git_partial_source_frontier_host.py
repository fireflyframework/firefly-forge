# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real native consumed turns distinguish a stored source frontier from graph reentry."""

import copy
import json

import httpx2
import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import activated, result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests
from test_bridge_inflight_cancel import RUN


@pytest.fixture
async def frontier_case(tmp_path, monkeypatch):
    from firefly_forge.agent_roles import ProjectRoles, RoleRegistry
    from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary

    env = await activated(tmp_path, monkeypatch)
    env.store.run.data["role_snapshot"] = RoleRegistry(tmp_path / "frontier-roles").snapshot(
        ProjectRoles(disabled=["docs"], allowed_tools={"implementer": ["write_file", "read_file"]})
    )
    original = AgenticBoundary.capture
    captures = 0
    posts = []

    def capture(boundary, wire):
        nonlocal captures
        captures += 1
        if captures == 3:
            raise ValueError("Fictional checkpoint storage refusal before dispatch")
        return original(boundary, wire)

    monkeypatch.setattr(AgenticBoundary, "capture", capture)

    async def run(*, late_write=False):
        def transport(request):
            if request.method == "GET":
                return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
            posts.append(request.content)
            assert len(posts) <= 2
            body = result(tool=True)
            if len(posts) == 2:
                call = body["choices"][0]["message"]["tool_calls"][0]
                call["id"] = "second-call"
                if late_write:
                    call["function"]["arguments"] = json.dumps({"path": "service.py", "content": "VALUE = 2\n"})
                else:
                    call["function"] = {"name": "read_file", "arguments": json.dumps({"path": "service.py"})}
            return httpx2.Response(200, json=body)

        env.model.client._client._transport = httpx2.MockTransport(transport)
        env.model.client._client._mounts = {}
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.engine.execute_run(RUN)
        current = await env.store.get_run(RUN)
        assert current["status"] == "failed", current.get("error")
        assert len(posts) == 2
        entries = [copy.deepcopy(row.data) for row in env.journal.records.rows]
        task = next(t for t in (await env.journal.plan(RUN))["tasks"] if t["id"] == "implement")
        return current, task, entries

    env.run_frontier = run
    yield env
    await env.model.client.close()


async def test_stored_frontier_proof_keeps_failed_graph_and_all_consumes(frontier_case):
    from firefly_forge.git_partial_source_checkpoints import stored_frontier_proof

    run, task, entries = await frontier_case.run_frontier()
    before = copy.deepcopy((run, task, entries))
    proof = stored_frontier_proof(run, task, entries)
    assert proof["verification_status"] == "partial_unverified"
    assert proof["completion_count"] == 2
    assert proof["frontier_record_digest"] == next(
        e["digest"] for e in reversed(entries) if e["kind"] == "agent_checkpoint"
    )
    assert proof["later_tool_names"] == ["read_file"]
    assert "brief" not in proof["invocation"]
    assert (run, task, entries) == before
    assert run["implementation_pending"] is True and task["status"] == "running"


async def test_mutation_after_frontier_never_authorizes_partial_checkpoint(frontier_case):
    from firefly_forge.git_partial_source_checkpoints import stored_frontier_proof

    run, task, entries = await frontier_case.run_frontier(late_write=True)
    with pytest.raises(ValueError, match="read-only"):
        stored_frontier_proof(run, task, entries)


async def test_known_response_without_consume_is_still_not_settled(frontier_case):
    from firefly_forge.git_partial_source_checkpoints import stored_frontier_proof

    run, task, entries = await frontier_case.run_frontier()
    consume = max(index for index, entry in enumerate(entries) if entry["kind"] == "agent_consume")
    with pytest.raises(ValueError, match="completion closure"):
        stored_frontier_proof(run, task, entries[:consume])


async def test_old_runtime_contract_is_not_accepted_by_a_current_reader(frontier_case, monkeypatch):
    import firefly_forge.git_partial_source_checkpoints as partial

    run, task, entries = await frontier_case.run_frontier()
    monkeypatch.setattr(partial, "runtime_contract", lambda: "0" * 64)
    with pytest.raises(ValueError, match="runtime contract"):
        partial.stored_frontier_proof(run, task, entries)


@pytest.fixture
async def partial_service(frontier_case, monkeypatch, request):
    from types import SimpleNamespace

    from firefly_forge.git_deliveries import GitDeliveries
    from firefly_forge.git_delivery_policy import GitDeliveryPolicies
    from firefly_forge.git_partial_source_checkpoints import GitPartialSourceCheckpoints

    env = frontier_case
    if getattr(request, "param", None) == "compressed":
        env.store.run.data["brief"] = "Controlled source custody context. " * 1200
    policy = dict(version=1, enabled=True, every_units=1, on_pause=True, remote="local", max_checkpoints=50)
    if getattr(request, "param", None) == "draft":
        policy["remote"] = "draft_pr"
    from test_git_delivery_local import git

    env.store.run.data.update(
        git_delivery_policy=policy, git_delivery_policy_digest="b" * 64, branch="forge/" + RUN[:12]
    )
    git(env.workspace, "checkout", "-qb", "forge/" + RUN[:12])
    if getattr(request, "param", None) == "draft":
        from firefly_forge.git_workspace import metadata_digest

        env.partial_remote = env.workspace.parent / "partial-remote.git"
        env.partial_remote.mkdir()
        git(env.partial_remote, "init", "--bare", "-q")
        git(env.workspace, "remote", "add", "origin", str(env.partial_remote))
        env.store.run.data["git_metadata_digest"] = metadata_digest(env.workspace)
    if getattr(request, "param", None) == "compressed":
        from firefly_forge import agentic_tool_custody

        # This legacy compressed-history fixture predates separate tool-result receipts.
        lookup = agentic_tool_custody.current_agentic_tool_custody
        with monkeypatch.context() as historical:
            historical.setattr(agentic_tool_custody, "current_agentic_tool_custody", lambda: None)
            await env.run_frontier()
        assert agentic_tool_custody.current_agentic_tool_custody is lookup
    else:
        await env.run_frontier()
    state = {"allowed": True}

    async def authorize(run, actor, permission):
        assert run["id"] == RUN and actor == env.store.run.data["execution_actor"]
        if not state["allowed"]:
            raise PermissionError("Controlled current authority revocation")

    async def policy_snapshot(_self, _project):
        return {"policy": copy.deepcopy(policy), "digest": "b" * 64}

    async def empty(_):
        return []

    monkeypatch.setattr(GitDeliveryPolicies, "snapshot", policy_snapshot)
    env.journal.sandboxes = SimpleNamespace(for_run=empty)
    env.store.source_budget_admission_unit = env.store._unit
    from firefly_forge.tool_persistence import ForgeToolInvocationRepository

    monkeypatch.setattr(ForgeToolInvocationRepository, "for_run", lambda _self, run_id: empty(run_id))
    deliveries = GitDeliveries(
        env.store, env.engine, env.journal, workspace=lambda _: env.workspace, authorize=authorize
    )
    service = GitPartialSourceCheckpoints(deliveries, tool_records=SimpleNamespace(for_run=empty))
    env.partial, env.partial_state = service, state
    return env


async def test_native_candidate_compares_actual_source_with_its_stored_frontier(partial_service):
    env = partial_service
    old = copy.deepcopy(await env.store.get_run(RUN))
    candidate = await env.partial.candidate(RUN, old["execution_actor"])
    assert candidate["workspace"] == candidate["proof"]["invocation"]["workspace"]
    assert candidate["proof"]["later_tool_names"] == ["read_file"]
    assert await env.store.get_run(RUN) == old


async def test_candidate_source_drift_and_late_revocation_are_not_bypassed(partial_service):
    env = partial_service
    actor = env.store.run.data["execution_actor"]
    source = env.workspace / "service.py"
    original = source.read_bytes()
    source.write_text("VALUE = 99\n")
    with pytest.raises(ValueError, match="Current source differs"):
        await env.partial.candidate(RUN, actor)
    source.write_bytes(original)
    env.partial_state["allowed"] = False
    with pytest.raises(PermissionError, match="revocation"):
        await env.partial.candidate(RUN, actor)


async def test_owner_commits_real_partial_git_without_clearing_failed_graph(partial_service):
    from test_git_delivery_local import git

    from firefly_forge.git_partial_source_models import GitPartialSourceConsentV1

    env = partial_service
    parent = copy.deepcopy(await env.store.get_run(RUN))
    actor = parent["execution_actor"]
    candidate = await env.partial.candidate(RUN, actor)
    consent = GitPartialSourceConsentV1.model_validate(
        {
            "version": 1,
            "candidate_digest": candidate["digest"],
            **{
                key: candidate[key]
                for key in [
                    "policy_digest",
                    "expected_run_digest",
                    "journal_digest",
                    "source_digest",
                    "frontier_record_digest",
                ]
            },
            "acknowledge_partial_unverified": True,
            "acknowledge_preserve_failed_graph": True,
            "acknowledge_no_execution_grant": True,
        }
    )
    checkpoint = env.partial.d
    await env.partial.admit(RUN, "a" * 32, actor, consent)
    receipt = await checkpoint.checkpoint(
        RUN,
        request_id="a" * 32,
        expected_head=candidate["expected_head"],
        expected_source_digest=candidate["source_digest"],
        actor=actor,
    )
    assert receipt["verification_status"] == "partial_unverified"
    assert git(env.workspace, "log", "-1", "--format=%s").startswith("[UNVERIFIED]")
    assert receipt["parent_revision"] == candidate["expected_head"]
    assert git(env.workspace, "show", "HEAD:service.py") == "VALUE = 1"
    current = await env.store.get_run(RUN)
    for key in (
        "status",
        "active_task",
        "implementation_pending",
        "bridge_completion_hold",
        "implementation_rounds",
        "attempt",
    ):
        assert current.get(key) == parent.get(key)
    assert current["checkpoint_revision"] == receipt["revision"]
    count = len(env.journal.records.rows)
    repeated = await checkpoint.checkpoint(
        RUN,
        request_id="a" * 32,
        expected_head=candidate["expected_head"],
        expected_source_digest=candidate["source_digest"],
        actor=actor,
    )
    assert repeated["checkpoint_digest"] == receipt["checkpoint_digest"]
    assert repeated["revision"] == receipt["revision"] == git(env.workspace, "rev-parse", "HEAD")
    assert len(env.journal.records.rows) == count
    with pytest.raises(ValueError, match="one unverified checkpoint"):
        await checkpoint.checkpoint(RUN, request_id="b" * 32, expected_head=receipt["revision"], actor=actor)
    with pytest.raises(ValueError, match="no final-ready"):
        await checkpoint.checkpoint(
            RUN, request_id="a" * 32, expected_head=receipt["revision"], actor=actor, final=True
        )


async def test_initial_index_change_is_refused_instead_of_treated_as_owned_staging(partial_service):
    from test_git_delivery_local import git

    env = partial_service
    actor = env.store.run.data["execution_actor"]
    candidate = await env.partial.candidate(RUN, actor)
    git(env.workspace, "add", "service.py")
    with pytest.raises(ValueError, match="frontier changed"):
        await env.partial.revalidate(RUN, actor, candidate, staged_tree=None)


@pytest.mark.parametrize("partial_service", ["compressed"], indirect=True)
async def test_decoded_checkpoint_documents_use_real_current_credential_guard(partial_service):
    env = partial_service
    canary = "Wrote service.py (10 bytes)"
    entries = await env.partial._entries(RUN)
    checkpoints = [e for e in entries if e["kind"] == "agent_checkpoint"]
    assert json.loads(checkpoints[-1]["payload"]["snapshot"])["version"] == 3
    assert canary not in json.dumps(entries)
    env.store.credential_registry = lambda: {"TOKEN": canary}
    with pytest.raises(ValueError, match="configured or historical credential"):
        await env.partial.candidate(RUN, env.store.run.data["execution_actor"])


async def test_hold_pointer_must_identify_the_original_native_completion(frontier_case):
    from firefly_forge.git_partial_source_checkpoints import stored_frontier_proof

    run, task, entries = await frontier_case.run_frontier()
    run["bridge_completion_hold"]["intent_digest"] = "0" * 64
    with pytest.raises(ValueError, match="original native completion"):
        stored_frontier_proof(run, task, entries)
