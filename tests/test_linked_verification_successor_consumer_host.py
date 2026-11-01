# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Known four-result hold -> explicit fresh seven-check successor, never old-result reuse."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_linked_verification_consumer_host import admit_and_execute

from firefly_forge import engine as engine_module
from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import LinkedVerificationInputV1
from firefly_forge.models import Command
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.verification_scope import build_profile, vitest_no_cache_argv, wrap_build_argv
from firefly_forge.verification_scope_attestation import PreparedBuildScope, VerificationScopeV1

pytest_plugins = ["test_git_linked_commit_host"]


@pytest.fixture(autouse=True)
def selected_cache_before_native_history(monkeypatch, original_git_consent):
    import test_invalid_read_native_protocol_host as protocol

    activate = protocol.activated

    async def legacy_workspace(*args):
        env = await activate(*args)
        root = env.workspace
        (root / ".gitignore").write_text("node_modules/\n")
        frontend = root / "frontend"
        tool = frontend / "node_modules/vitest"
        tool.mkdir(parents=True)
        (frontend / "package.json").write_text(
            json.dumps({"scripts": {"test": "node node_modules/vitest/vitest.mjs run --configLoader runner"}})
        )
        (tool / "package.json").write_text('{"name":"vitest","version":"4.1.11"}\n')
        (tool / "vitest.mjs").write_text("// Fixture manifest only; controlled backend does not run Node\n")
        cache = frontend / (".cache/vite/vitest/" + "a" * 40 + "/results.json")
        cache.parent.mkdir(parents=True)
        cache.write_text('{"version":"4.1.11","results":[]}\n')
        return env

    monkeypatch.setattr(protocol, "activated", legacy_workspace)


@pytest.fixture
async def seven_checks(linked_case, monkeypatch):
    c = linked_case
    mandatory = [Command(argv=["controlled-quality-check", str(i)], timeout=10) for i in range(2, 8)]
    mandatory[2] = Command(argv=["npm", "--prefix", "frontend", "run", "test"], timeout=10)
    monkeypatch.setattr(engine_module, "policy_commands", lambda *args: mandatory)
    inputs = c.api._inputs

    async def scoped_inputs(run, entries, workspace, current, *, actor, successor=None):
        original = await inputs(run, entries, workspace, current, actor=actor)
        value = original.witness.model_dump(mode="json")
        if successor is not None:
            value["scratch"] += "-" + digest(successor.model_dump(mode="json"))
        scratch = Path(value["scratch"])
        dependencies = [workspace / "frontend/node_modules"]
        logical = mandatory[2].argv
        physical = vitest_no_cache_argv(logical, workspace) if successor is not None else logical
        for ordinal in (1, 4):
            for part in ("home", "tmp"):
                (scratch / str(ordinal) / part).mkdir(mode=0o700, parents=True, exist_ok=True)
        value["checks"].append(
            dict(
                command_index=4,
                kind="node_build",
                argv=logical,
                wrapped_argv=wrap_build_argv(physical, build_profile(workspace, dependencies, scratch / "4")),
            )
        )
        value["commands_digest"] = digest({"commands": c.api_commands(run, workspace)})
        witness = VerificationScopeV1.model_validate(value)

        async def measure():
            await original.fence()
            return witness

        return PreparedBuildScope(witness, measure, original._fence, original._environment)

    c.api_commands = lambda run, workspace: [
        v.model_dump(mode="json") for v in engine_module.final_verification_commands(run, workspace)
    ]
    monkeypatch.setattr(c.api, "_inputs", scoped_inputs)
    backend = c.env.engine.sandbox.backends.select(None)
    cache = c.env.workspace / ("frontend/.cache/vite/vitest/" + "a" * 40 + "/results.json")

    class KnownResults:
        async def run(self, config, argv, cwd, timeout, maximum):
            result = await backend.run(config, argv, cwd, timeout, maximum)
            if len(c.calls) == 4:
                assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
                assert argv[-2:] != ["--", "--no-cache"]
                cache.write_text('{"version":"4.1.11","results":["known-old-run"]}\n')
            elif len(c.calls) == 8:
                assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
                assert argv[-2:] == ["--", "--no-cache"]
            return result

    monkeypatch.setattr(c.env.engine.sandbox.backends, "select", lambda _: KnownResults())
    return c


async def test_known_four_check_hold_has_explicit_all_fresh_seven_check_successor(seven_checks, monkeypatch):
    c = seven_checks
    original = copy.deepcopy(await c.env.store.get_run(c.env.run_id))
    posts = len(c.env.posts)
    old = await admit_and_execute(c)
    assert old["status"] == "needs_reconciliation", old
    assert old["progress"]["started"] == old["progress"]["completed"] == 4
    assert not old["progress"]["unknown"] and not old["progress"]["failed"]
    assert len(c.calls) == 4 and old["hold_diagnostic"]["phase"] == "post_result_source"
    old_row = await c.env.requests.records.find_by_id(UUID(old["request_id"]))
    old_data, old_events = copy.deepcopy(old_row.data), await c.api._events(old_row)
    candidate = await c.api.candidate(c.env.run_id, predecessor_request_id=old["request_id"])
    assert candidate["effect_budget"] == 7
    assert candidate["successor"]["predecessor_event_digest"] == old["event_digest"]
    assert candidate["execution_plan"][3]["physical_argv"][-2:] == ["--", "--no-cache"]
    assert len(c.calls) == 4
    body = LinkedVerificationInputV1(
        version=1,
        request_id=uuid4().hex,
        candidate_digest=candidate["digest"],
        successor=candidate["successor"],
        acknowledge_old_results_not_reused=True,
        acknowledge_all_fresh_checks=True,
        acknowledge_no_coding_or_repair=True,
        acknowledge_original_history_and_budget_preserved=True,
    )
    queued = await c.api.request(c.env.run_id, body)
    assert queued["status"] == "queued" and queued["progress"]["completed"] == 0
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await c.api.execute(body.request_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    result = await c.api.get(c.env.run_id, body.request_id)
    assert result["status"] == "completed", result
    assert result["progress"]["started"] == result["progress"]["completed"] == 7
    assert len(c.calls) == 11 and len(c.env.posts) == posts
    assert old_row.data == old_data and await c.api._events(old_row) == old_events
    current = await c.env.store.get_run(c.env.run_id)
    assert current["usage"] == original["usage"] and current["attempt"] == original["attempt"]
    task = next(t for t in (await c.env.journal.plan(c.env.run_id))["tasks"] if t["id"] == "verify")
    assert task["status"] == "done" and len(task["evidence"]["checks"]) == 7
    await c.api.validate_settlement(c.env.run_id, result["candidate"]["source_tree"], task)
    from firefly_forge import agentic_checkpoint_invocation, linked_verifications
    from firefly_forge.git_linked_verification import assert_linked_git_closed

    monkeypatch.setattr(
        agentic_checkpoint_invocation, "_runtime_material_digest", lambda: result["candidate"]["runtime_contract"]
    )
    monkeypatch.setattr(linked_verifications, "_policy_material_digest", lambda: result["candidate"]["policy_digest"])
    deliveries = SimpleNamespace(
        store=c.env.store, journal=c.env.journal, engine=c.env.engine, workspace=lambda _: c.env.workspace
    )
    assert await assert_linked_git_closed(deliveries, c.env.run_id) is not None
    from test_git_linked_commit_host import linked_git_case

    snapshot = c.env.engine._snapshot
    git_case = await linked_git_case(c, result, monkeypatch)
    monkeypatch.setattr(c.env.engine, "_snapshot", snapshot)
    base = original["base_revision"]

    def change_predecessor():
        old_row.data = {**old_row.data, "detail": "Changed after the last native graph read"}

    frontier = c.api._frontier
    armed = {"value": False}

    async def late_history(*args, **kwargs):
        result = await frontier(*args, **kwargs)
        if armed["value"]:
            armed["value"] = False
            change_predecessor()
        return result

    monkeypatch.setattr(c.api, "_frontier", late_history)
    git_case.after["action"] = lambda: armed.update(value=True)
    try:
        with pytest.raises((ValueError, AccessDenied), match="verification|settlement|history"):
            async with git_case.service._owned(c.env.run_id, original["execution_actor"]):
                await c.env.store.update(c.env.run_id, git_delivery_summary={"controlled": "late ancestor drift"})
    finally:
        git_case.after["action"] = None
        monkeypatch.setattr(c.api, "_frontier", frontier)
        old_row.data = copy.deepcopy(old_data)
    reached = []

    async def no_model(*args, **kwargs):
        pass

    async def git_gate(*args, **kwargs):
        receipt = await git_case.service.checkpoint(
            c.env.run_id,
            request_id=uuid4().hex,
            expected_head=base,
            expected_source_tree=(await c.env.store.get_run(c.env.run_id))["source_tree"],
            actor=original["execution_actor"],
            boundary=git_case.service,
            final=True,
        )
        from test_git_delivery_local import git

        assert git(c.env.workspace, "rev-parse", "HEAD^") == base
        assert git(c.env.workspace, "rev-parse", "HEAD") == receipt["revision"]
        reached.append(True)
        raise RuntimeError("Controlled existing Factory Git gate reached")

    monkeypatch.setattr(c.env.engine, "_independent_review", no_model)
    monkeypatch.setattr(c.env.engine, "_checkpoint_commit", git_gate)
    await c.env.journal.resume(c.env.run_id)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await c.env.engine.execute_run(c.env.run_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert reached == [True], (await c.env.store.get_run(c.env.run_id)).get("error")
    assert len(c.calls) == 11 and len(c.env.posts) == posts
