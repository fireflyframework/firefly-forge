# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual API/worker/parent consumer with explicit fictional input and runner seams."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from types import MethodType, SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from pyfly.container import Container
from pyfly.web.adapters.starlette.controller import ControllerRegistrar
from starlette.applications import Starlette
from test_consumed_terminal_reconciliation_host import (
    test_reconcile_same_claim_without_replay_or_second_charge as accept,
)

from firefly_forge.controllers import OperatorErrors
from firefly_forge.execution_environment import execution_environment
from firefly_forge.identity import Principal
from firefly_forge.journal import Journal, digest
from firefly_forge.linked_verification_controllers import LinkedVerificationController
from firefly_forge.linked_verification_models import LinkedVerificationInputV1
from firefly_forge.linked_verifications import LinkedVerifications, final_commands
from firefly_forge.process import ProcessResult
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.verification_scope_attestation import PreparedBuildScope, VerificationScopeV1

pytest_plugins = ["test_consumed_terminal_reconciliation_host"]


@pytest.fixture
async def linked_case(consumed_terminal, monkeypatch):
    env = consumed_terminal
    await accept(env, monkeypatch, held=True)
    api = LinkedVerifications(env.store, env.journal, env.requests, env.engine)
    env.engine.linked_verifications = env.requests.linked_verifications = api
    calls, inputs_state = [], {"value": "original", "require_wrapped": False}
    expected_wrapped = ["fictional-qualified-wrapper", "-p", "deny-network", *env.store.run.data["commands"][0]["argv"]]
    scratch = env.workspace.parent / "fictional-input-scratch" / "1"
    for part in ("home", "tmp"):
        (scratch / part).mkdir(parents=True, exist_ok=True)

    async def event_rows(self, request_id, maximum):
        rows = await self.events(request_id)
        if len(rows) > maximum:
            raise ValueError("Linked verification event overflow")
        return rows

    env.requests.journal.linked_verification_events = MethodType(event_rows, env.requests.journal)

    async def fictional_inputs(run, entries, workspace, current, *, actor):
        # This proves consumer custody, not installed dependency/profile qualification.
        observed = inputs_state["value"]
        source = run["usage"]["retained_source"]
        value = dict(
            version=1,
            run_id=run["id"],
            handoff_intent_digest=source["handoff_intent_digest"],
            source_digest=source["source_digest"],
            commands_digest=digest({"commands": final_commands(run, workspace)}),
            scope_digest=source["scope_digest"],
            runtime_contract="1" * 64,
            dependency_digest=digest({"fixture": observed}),
            **{
                key: "2" * 64
                for key in (
                    "environment_digest",
                    "credential_digest",
                    "prerequisite_digest",
                    "wrapper_digest",
                    "declaration_digest",
                )
            },
            dependency_roots=[str(workspace)],
            scratch=str(scratch.parent),
            checks=[
                dict(
                    command_index=1,
                    kind="python_static",
                    argv=run["commands"][0]["argv"],
                    wrapped_argv=expected_wrapped,
                )
            ],
        )
        witness = VerificationScopeV1.model_validate(value)

        async def fence():
            if inputs_state["value"] != observed:
                raise ValueError("Controlled dependency input drift")

        async def measure():
            await fence()
            return witness

        return PreparedBuildScope(witness, measure, fence, lambda: {"CAPTURED_VERIFICATION_ENV": "exact-fixture"})

    monkeypatch.setattr(api, "_inputs", fictional_inputs)

    class Backend:
        async def run(self, config, argv, cwd, timeout, maximum):
            assert config.mode == "host"
            if argv == expected_wrapped:
                assert timeout == min(env.store.run.data["commands"][0]["timeout"], config.timeout)
            if inputs_state["require_wrapped"] and not calls:
                assert argv == expected_wrapped
                with execution_environment({"UNBOUND_ENV": "must-not-survive"}) as environment:
                    assert environment["CAPTURED_VERIFICATION_ENV"] == "exact-fixture"
                    assert "UNBOUND_ENV" not in environment
                    assert environment["HOME"] == str(scratch / "home")
                    assert environment["TMPDIR"] == str(scratch / "tmp")
            calls.append(list(argv))
            if "entered" in inputs_state:
                inputs_state["entered"].set()
                await inputs_state["release"].wait()
            if inputs_state.get("unknown"):
                raise RuntimeError("Controlled runner outcome is unknown")
            if inputs_state.get("drift_after_result"):
                inputs_state["value"] = "changed-after-effect"
            return ProcessResult(
                argv, inputs_state.get("exit_code", 0), inputs_state.get("output", "Controlled fresh native QA passed")
            )

    monkeypatch.setattr(env.engine.sandbox.backends, "select", lambda config: Backend())
    container = Container()
    container.register_instance(LinkedVerificationController, LinkedVerificationController(api))
    container.register_instance(OperatorErrors, OperatorErrors())
    context = SimpleNamespace(container=container, get_bean=container.resolve)
    app = Starlette(routes=ControllerRegistrar().collect_routes(context))
    return SimpleNamespace(env=env, api=api, app=app, calls=calls, inputs_state=inputs_state, wrapped=expected_wrapped)


async def admit(c):
    env = c.env
    candidate = await c.api.candidate(env.run_id)
    body = LinkedVerificationInputV1(
        version=1,
        request_id=uuid4().hex,
        candidate_digest=candidate["digest"],
        acknowledge_all_fresh_checks=True,
        acknowledge_no_coding_or_repair=True,
        acknowledge_original_history_and_budget_preserved=True,
    )
    await c.api.request(env.run_id, body)
    return body


async def admit_and_execute(c):
    env = c.env
    body = await admit(c)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await c.api.execute(body.request_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    return await c.api.get(env.run_id, body.request_id)


async def test_linked_verification_seals_and_executes_required_policy_and_framework_checks(linked_case, monkeypatch):
    from firefly_forge import engine as engine_module
    from firefly_forge.models import Command

    c = linked_case
    configured = copy.deepcopy(c.env.store.run.data["commands"])
    # Control the existing command producers, not the linked operation's selection.
    monkeypatch.setattr(
        engine_module, "policy_commands", lambda snapshot, backend: [Command(argv=["required-policy"], timeout=10)]
    )
    monkeypatch.setattr(
        engine_module, "framework_commands", lambda run: [Command(argv=["required-framework"], timeout=10)]
    )
    candidate = await c.api.candidate(c.env.run_id)
    assert candidate["effect_budget"] == len(configured) + 2
    result = await admit_and_execute(c)
    assert result["status"] == "completed", result
    assert c.calls == [c.wrapped, ["required-policy"], ["required-framework"]]
    assert result["settlement"]["checks_count"] == len(configured) + 2


async def test_linked_verification_dispatches_qualified_wrapper_and_captured_environment(linked_case):
    c = linked_case
    c.inputs_state["require_wrapped"] = True
    result = await admit_and_execute(c)
    assert result["status"] == "completed", result
    assert c.calls == [c.wrapped]


async def test_linked_verification_preserves_oidc_admin_consent_audience_on_worker(linked_case):
    c, user = linked_case, linked_case.env.user
    user.api_roles, user.api_roles_audience = ["forge-admin"], "linked-fixture-api"
    user.api_roles_issued_at, user.api_roles_expires_at = 1.0, datetime.now(UTC) + timedelta(hours=1)
    actor = Principal(
        user.id,
        user.issuer,
        user.subject,
        authentication_method="oidc_bearer",
        api_roles=frozenset({"forge-admin"}),
        api_audience="linked-fixture-api",
    )
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        result = await admit_and_execute(c)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert result["status"] == "completed", result
    assert len(c.calls) == len(c.env.store.run.data["commands"])


async def test_linked_fresh_verification_api_worker_and_parent_reach_git_gate_without_coding(linked_case, monkeypatch):
    c, env = linked_case, linked_case.env
    original = copy.deepcopy(await env.store.get_run(env.run_id))
    prefix = copy.deepcopy(await env.journal.entries(env.run_id))
    sandbox_rows = copy.deepcopy(await env.journal.sandbox_executions(env.run_id))
    route = f"/api/v1/runs/{env.run_id}/linked-verifications"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url="http://127.0.0.1") as client:
        observed = await client.get(route + "/candidate")
        assert observed.status_code == 200, observed.text
        body = LinkedVerificationInputV1(
            version=1,
            request_id=uuid4().hex,
            candidate_digest=observed.json()["digest"],
            acknowledge_all_fresh_checks=True,
            acknowledge_no_coding_or_repair=True,
            acknowledge_original_history_and_budget_preserved=True,
        )
        queued = await client.post(route, json=body.model_dump(mode="json"))
        assert queued.status_code == 201 and queued.json()["status"] == "queued", queued.text
        assert (await client.post(route, json=body.model_dump(mode="json"))).json() == queued.json()
        assert not c.calls and await env.store.get_run(env.run_id) == original
        with pytest.raises(ValueError, match="cannot be replayed"):
            await env.journal.resume(env.run_id)
        token = CURRENT_PRINCIPAL.set(None)
        try:
            await c.api.execute(body.request_id)
        finally:
            CURRENT_PRINCIPAL.reset(token)
        result = await client.get(route + "/" + body.request_id)
        assert result.status_code == 200 and result.json()["status"] == "completed", result.text
        assert result.json()["settlement"]["checks_count"] == len(original["commands"])
        assert c.calls == [c.wrapped, *[command["argv"] for command in original["commands"][1:]]]
        assert (await client.post(route, json=body.model_dump(mode="json"))).json() == result.json()
        await c.api.execute(body.request_id)
        assert c.calls == [c.wrapped, *[command["argv"] for command in original["commands"][1:]]]
    after = await env.store.get_run(env.run_id)
    assert after["usage"] == original["usage"]
    assert after["attempt"] == after["implementation_rounds"] == original["attempt"]
    assert after["verification_handoff"] == original["verification_handoff"]
    assert await env.journal.sandbox_executions(env.run_id) == sandbox_rows
    entries = await env.journal.entries(env.run_id)
    assert entries[: len(prefix)] == prefix and Journal.verify_chain(entries)
    assert all(check["execution"] == "linked_verification" for check in after["final_verification"])
    posts, calls = len(env.posts), len(c.calls)
    reached = []

    async def no_independent_model(*args, **kwargs):
        pass

    async def git_gate(*args, **kwargs):
        reached.append(True)
        raise RuntimeError("Controlled existing Factory Git gate reached")

    monkeypatch.setattr(env.engine, "_independent_review", no_independent_model)
    monkeypatch.setattr(env.engine, "_checkpoint_commit", git_gate)
    await env.journal.resume(env.run_id)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await env.engine.execute_run(env.run_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    current = await env.store.get_run(env.run_id)
    assert reached == [True], current.get("error")
    assert "Factory Git gate reached" in current["error"]
    assert len(env.posts) == posts and len(c.calls) == calls
    assert current["usage"] == original["usage"]


async def test_revoked_queued_consent_is_held_once_before_any_effect(linked_case):
    c = linked_case
    body = await admit(c)
    original = copy.deepcopy(await c.env.store.get_run(c.env.run_id))
    c.env.user.enabled = False
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await c.api.execute(body.request_id)
        await c.api.execute(body.request_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    operation = await c.env.requests.get(body.request_id)
    assert operation["status"] == "needs_reconciliation"
    assert not c.calls
    assert await c.env.store.get_run(c.env.run_id) == original


async def test_known_result_guard_hold_retains_safe_phase_and_cause(linked_case, monkeypatch):
    c = linked_case
    inputs = c.api._inputs

    async def sensitive_guard(*args, **kwargs):
        prepared = await inputs(*args, **kwargs)
        original_fence = prepared._fence

        async def fence():
            try:
                await original_fence()
            except ValueError as error:
                raise ValueError("sensitive-fixture-value-must-not-be-published") from error

        return PreparedBuildScope(prepared.witness, prepared._measure, fence, prepared._environment)

    monkeypatch.setattr(c.api, "_inputs", sensitive_guard)
    c.inputs_state["drift_after_result"] = True
    result = await admit_and_execute(c)
    assert result["status"] == "needs_reconciliation"
    assert result["progress"]["started"] == result["progress"]["completed"] == 1
    assert result["progress"]["unknown"] is False
    assert result["hold_diagnostic"] == {
        "phase": "post_result_inputs",
        "cause_code": "input_scope_changed",
        "exception_type": "ValueError",
    }
    assert "sensitive-fixture-value" not in str(result)
    async with c.env.store._unit():
        row = await c.env.requests.records.find_by_id(UUID(result["request_id"]))
        events = await c.api._events(row)
    assert events[-1]["evidence"]["diagnostic"] == result["hold_diagnostic"]


@pytest.mark.parametrize("outcome", ["failed", "unknown", "drift_after_result"])
async def test_verification_failure_or_unknown_never_replays_or_settles_parent(linked_case, outcome):
    c = linked_case
    original = copy.deepcopy(await c.env.store.get_run(c.env.run_id))
    prefix = copy.deepcopy(await c.env.journal.entries(c.env.run_id))
    c.inputs_state.update({"exit_code": 1} if outcome == "failed" else {outcome: True})
    result = await admit_and_execute(c)
    assert result["status"] == ("failed" if outcome == "failed" else "needs_reconciliation"), result
    assert result["progress"]["started"] == 1
    assert result["progress"]["completed"] == (0 if outcome == "unknown" else 1)
    assert result["progress"]["unknown"] is (outcome == "unknown")
    assert result["progress"]["next_ordinal"] is None
    assert "settlement" not in result
    if outcome == "failed":
        assert result["last_failure"]["ordinal"] == 1
        assert result["last_failure"]["exit_code"] == 1
        assert result["last_failure"]["output_preview"] == "Controlled fresh native QA passed"
        assert result["last_failure"]["output_truncated"] is False
    else:
        assert "last_failure" not in result
    await c.api.execute(result["request_id"])
    assert len(c.calls) == 1
    assert await c.env.store.get_run(c.env.run_id) == original
    assert await c.env.journal.entries(c.env.run_id) == prefix
    with pytest.raises(ValueError, match="cannot be replayed"):
        await c.env.journal.resume(c.env.run_id)


async def test_long_verification_releases_global_publication_lock_but_keeps_repository_slot(linked_case):
    c = linked_case
    body = await admit(c)
    entered, release = asyncio.Event(), asyncio.Event()
    c.inputs_state.update(entered=entered, release=release)

    async def worker():
        token = CURRENT_PRINCIPAL.set(None)
        try:
            await c.api.execute(body.request_id)
        finally:
            CURRENT_PRINCIPAL.reset(token)

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(entered.wait(), 90)
        assert c.env.engine.active[c.env.run_id] is task
        assert c.env.engine.active_projects[c.env.run_id] == c.env.store.run.data["repository_id"]
        assert not await c.env.engine.restore_capacity(c.env.run_id)
        assert not c.env.engine.publication_lock.locked()
        async with c.env.engine.publication_lock:
            assert not task.done()
    finally:
        release.set()
        await task
    assert c.env.run_id not in c.env.engine.active


async def test_current_execute_permission_cannot_borrow_original_host_administrator(linked_case, monkeypatch):
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    c = linked_case

    async def not_administrator(self, actor):
        return False

    monkeypatch.setattr(ProjectAccess, "current_administrator", not_administrator)
    with pytest.raises(AccessDenied, match="administrator"):
        await c.api.candidate(c.env.run_id)
    assert not c.calls


async def test_delivery_rejects_changed_parent_check_projection_despite_valid_operation_ledger(linked_case):
    from firefly_forge.checkpoint_mode_runtime import mode_execution_scope

    c = linked_case
    result = await admit_and_execute(c)
    assert result["status"] == "completed", result
    task = next(task for task in (await c.env.journal.plan(c.env.run_id))["tasks"] if task["id"] == "verify")
    c.env.store.run.data["final_verification"] = copy.deepcopy(task["evidence"]["checks"])
    c.env.store.run.data["final_verification"][0]["exit_code"] = 1
    async with mode_execution_scope(c.env.journal, c.env.run_id):
        with pytest.raises(ValueError, match="linked receipt"):
            await c.api.validate_settlement(c.env.run_id, result["settlement"]["source_tree"], task)


async def test_queued_operation_wakes_actual_owned_worker_lifecycle(linked_case, monkeypatch):
    c, finished = linked_case, asyncio.Event()
    body = await admit(c)
    settle = c.api._settle

    async def settled(*args):
        await settle(*args)
        finished.set()

    monkeypatch.setattr(c.api, "_settle", settled)
    for name in (
        "runner_continuations",
        "agent_restores",
        "invalid_read_continuities",
        "git_deliveries",
        "github_projects",
    ):
        monkeypatch.setattr(c.env.requests, name, None)
    c.env.requests.runtime.executes_jobs = True
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await c.env.requests.start()
        await asyncio.wait_for(finished.wait(), 90)
        await asyncio.gather(*c.env.requests.active.values())
    finally:
        await c.env.requests.stop()
        CURRENT_PRINCIPAL.reset(token)
    assert c.env.requests.poller.done()
    assert all(task.done() for task in c.env.requests.active.values())
    assert (await c.api.get(c.env.run_id, body.request_id))["status"] == "completed"
    assert len(c.calls) == 1


async def test_restart_holds_durable_executing_intent_without_replaying_it(linked_case):
    from firefly_forge.checkpoint_mode_runtime import mode_execution_scope
    from firefly_forge.execution_requests import digest as request_digest

    c = linked_case
    body = await admit(c)
    original = copy.deepcopy(await c.env.store.get_run(c.env.run_id))
    actor = CURRENT_PRINCIPAL.get()
    async with mode_execution_scope(c.env.journal, c.env.run_id):
        async with c.env.engine.publication_lock:
            assert await c.env.engine.reserve_agent_restore(c.env.run_id)
        prepared = await c.api._prepare(c.env.run_id, actor)
        try:
            operation = await c.env.requests.get(body.request_id)
            await c.api._write(
                prepared,
                body.request_id,
                "linked_verification_claimed",
                {
                    "request_digest": operation["request_digest"],
                },
            )
            await c.api._write(
                prepared,
                body.request_id,
                "linked_verification_command_intent",
                {
                    "ordinal": 1,
                    "command_digest": request_digest(operation["commands"][0]),
                    "inputs_digest": operation["inputs_digest"],
                },
            )
        finally:
            prepared.close()
            c.env.engine.release_agent_restore(c.env.run_id)
    await c.api.recover()
    await c.api.recover()
    result = await c.api.get(c.env.run_id, body.request_id)
    assert result["status"] == "needs_reconciliation" and result["progress"]["unknown"]
    assert result["progress"]["started"] == 1 and result["progress"]["completed"] == 0
    assert "last_failure" not in result
    await c.api.execute(body.request_id)
    assert not c.calls and await c.env.store.get_run(c.env.run_id) == original


async def test_preparation_rejects_changed_original_scope_usage_and_unsupported_transport_before_effects(linked_case):
    from firefly_forge.sandbox_models import SandboxInput, capture_sandbox

    c = linked_case
    original = copy.deepcopy(c.env.store.run.data)
    for field, value in (
        ("commands", [{"argv": ["foreign-check"], "timeout": 10}]),
        ("usage", {**original["usage"], "requests": 999}),
        (
            "sandbox_snapshot",
            capture_sandbox(SandboxInput(mode="docker", image="fixture@sha256:" + "1" * 64, context="owned-fixture")),
        ),
    ):
        c.env.store.run.data = {**copy.deepcopy(original), field: value}
        with pytest.raises(ValueError):
            await c.api.candidate(c.env.run_id)
        assert not c.calls
    c.env.store.run.data = original


async def test_failure_preview_is_currently_redacted_byte_bounded_and_tied_to_actual_event(linked_case, monkeypatch):
    c = linked_case
    c.inputs_state.update(exit_code=2, output="fake-sensitive-value" + "é" * 2000 + " Actual failed assertion")
    monkeypatch.setattr(c.env.engine, "credential_registry", lambda: {"QA_API_KEY": "fake-sensitive-value"})
    result = await admit_and_execute(c)
    failure = result["last_failure"]
    assert result["status"] == "failed" and failure["exit_code"] == 2
    assert len(failure["output_preview"].encode()) <= 2000
    assert failure["output_preview"].endswith("Actual failed assertion")
    assert failure["output_truncated"] is True and "fake-sensitive-value" not in failure["output_preview"]
    events = await c.env.requests.events(result["request_id"])
    event = next(event for event in events if event["digest"] == failure["event_digest"])
    assert event["kind"] == "linked_verification_command_result"
    assert event["evidence"]["exit_code"] == failure["exit_code"]
    assert "fake-sensitive-value" not in event["evidence"]["output"]


async def test_new_owner_cannot_execute_previous_owners_queued_consent(linked_case):
    c = linked_case
    body = await admit(c)
    original = copy.deepcopy(await c.env.store.get_run(c.env.run_id))
    c.env.store.execution_ownership_token = object()
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await c.api.execute(body.request_id)
        await c.api.execute(body.request_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    result = await c.api.get(c.env.run_id, body.request_id)
    assert result["status"] == "needs_reconciliation"
    assert result["progress"]["started"] == result["progress"]["completed"] == 0
    assert result["progress"]["next_ordinal"] is None
    assert not c.calls and await c.env.store.get_run(c.env.run_id) == original
