# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native transaction protocol with an owned memory backend, not PostgreSQL lock proof."""

import asyncio
import copy
import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from test_agentic_checkpoint_activation_host import activated, result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_inflight_cancel import RUN


@pytest.fixture
async def continuity_case(tmp_path, monkeypatch, request):
    from firefly_forge import agentic_runtime, agentic_tool_custody
    from firefly_forge import invalid_read_continuities as native
    from firefly_forge.agent_roles import ProjectRoles, RoleRegistry
    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.providers import BridgeProvider
    from firefly_forge.workspace_tools import tool_catalog

    env = await activated(tmp_path, monkeypatch)
    advanced = getattr(request, "param", False)
    env.store.run.data.update(attempt=2, implementation_rounds=1, repair_budget=2)

    async def no_commands(_):
        return []

    env.journal.sandboxes = SimpleNamespace(for_run=no_commands)
    env.command_calls, env.evidence_calls = [], []
    if advanced:
        from test_sandbox_command_ordinals_host import Rows

        from firefly_forge.process import ProcessResult
        from firefly_forge.run_evidence import RunEvidence
        from firefly_forge.sandbox import SandboxExecutor
        from firefly_forge.sandbox_models import SandboxInput, capture_sandbox

        class Backend:
            async def run(self, config, argv, cwd, timeout, output_limit):
                env.command_calls.append(argv)
                return ProcessResult(argv, 0, "Controlled complete command")

        env.store.run.data["sandbox_snapshot"] = capture_sandbox(SandboxInput(command_limit=22))
        env.journal.sandboxes = Rows()
        env.engine.sandbox = SandboxExecutor(env.store, env.journal, SimpleNamespace(select=lambda _: Backend()))

        async def evidence(self, run_id, query, *, task_id):
            env.evidence_calls.append(query.model_dump())
            assert query.limit <= 10 and task_id == "implement"
            return {"items": [], "fixture": "controlled-read"}

        monkeypatch.setattr(RunEvidence, "for_worker", evidence)
    (env.workspace / "existing.py").write_text("VALUE = 1\n")
    env.store.run.data["role_snapshot"] = RoleRegistry(tmp_path / "continuity-roles").snapshot(
        ProjectRoles(
            disabled=["docs"],
            allowed_tools={
                "implementer": ["read_run_evidence", "read_file", "list_files"]
                + (["execute_command", "write_file"] if advanced else [])
            },
        ),
        tool_catalog=tool_catalog({"read_run_evidence": True, "execute_command": advanced}),
    )
    await env.journal.ensure_plan(RUN)
    async with env.store._unit():
        task = await env.journal._task(RUN, "implement")
        task.attempt = 2
        await env.journal.tasks.save(task)
    if advanced:
        original_event = env.store.event
        env.historical_rows = None

        async def seed_charged_history(run_id, stage, message):
            if stage == "provider" and env.historical_rows is None:
                env.historical_rows = []
                for index in range(11):
                    await env.engine.sandbox.execute(
                        RUN, env.workspace, env.workspace, ["retained-command-" + str(index + 1)], 2, "implement"
                    )
                env.historical_rows = copy.deepcopy([row.data for row in env.journal.sandboxes.rows])
                env.historical_wal = copy.deepcopy([row.data for row in env.journal.records.rows])
            await original_event(run_id, stage, message)

        monkeypatch.setattr(env.store, "event", seed_charged_history)
    posts = []

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        posts.append(request.content)
        assert len(posts) <= (7 if advanced else 4), "No original response or new model call may be repeated"
        if len(posts) == 1:
            response = result(tool=True)
            response["choices"][0]["message"]["tool_calls"][0]["function"] = {
                "name": "read_run_evidence",
                "arguments": '{"query":{"tool_name":"read_file"}}',
            }
        elif advanced and len(posts) in {2, 3, 4, 5, 6}:
            response = result(tool=True)
            call = response["choices"][0]["message"]["tool_calls"][0]
            call["id"] = "advanced-next-" + str(len(posts))
            name, arguments = {
                2: ("read_run_evidence", '{"query":{"kind":"journal","limit":2}}'),
                3: ("write_file", '{"path":"existing.py","content":"VALUE = 2\\n"}'),
                4: ("execute_command", '{"argv":["first-controlled-command"],"timeout":2}'),
                5: ("read_file", '{"path":"existing.py"}'),
                6: ("execute_command", '{"argv":["second-controlled-command"],"timeout":2}'),
            }[len(posts)]
            call["function"] = {"name": name, "arguments": arguments}
        elif len(posts) in {2, 3}:
            response = result(tool=True)
            call = response["choices"][0]["message"]["tool_calls"][0]
            call["id"] = "next-read-" + str(len(posts))
            call["function"] = (
                {"name": "read_file", "arguments": '{"path":"existing.py"}'}
                if len(posts) == 2
                else {
                    "name": "list_files",
                    "arguments": '{"path_prefix":""}',
                }
            )
        else:
            response = result(
                json.dumps(
                    {
                        "summary": "Retained evidence requires review",
                        "worker_feedback": {
                            "id": "continued-review",
                            "role_id": "implementer",
                            "task_id": "implement",
                            "attempt": 2,
                            "disposition": "clarification",
                            "summary": "Original source preserved",
                            "request": {
                                "id": "continued-review",
                                "kind": "product_definition",
                                "question": "Proceed?",
                                "context": "Fixture",
                            },
                        },
                        "qa_report": None,
                    }
                )
            )
        return httpx2.Response(200, json=response)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as http:
        model = BridgeProvider(env.provider.settings).model(http_client=http)
        monkeypatch.setattr(env.provider, "model", lambda **kwargs: model)

        class HistoricalInput:
            @classmethod
            def model_json_schema(cls):
                from pydantic_ai.tools import Tool

                async def old_query(query: dict[str, Any]) -> dict[str, Any]:
                    raise AssertionError("Schema-only historical signature")

                return Tool(old_query, name="read_run_evidence").tool_def.parameters_json_schema

            @classmethod
            def model_validate(cls, arguments):
                query = arguments["query"]
                return SimpleNamespace(
                    query=SimpleNamespace(model_dump=lambda **_: dict(query), limit=query.get("limit", 10))
                )

        custody_lookup = agentic_tool_custody.current_agentic_tool_custody
        with monkeypatch.context() as historical:
            # This producer predates durable tool custody; resumed execution uses the current implementation.
            historical.setattr(agentic_tool_custody, "current_agentic_tool_custody", lambda: None)
            historical.setattr(agentic_runtime, "WorkerEvidenceReadInput", HistoricalInput)
            render = agentic_runtime.prompt_text
            historical.setattr(
                agentic_runtime,
                "prompt_text",
                lambda name, **kwargs: render(
                    name, **({**kwargs, "version": "1.0.0"} if name == "tools/read_run_evidence" else kwargs)
                ),
            )
            await env.engine.execute_run(RUN)
        assert agentic_tool_custody.current_agentic_tool_custody is custody_lookup
        assert not any(row.kind.startswith("agentic_tool_") for row in env.journal.records.rows)
        frozen = await env.store.get_run(RUN)
        assert frozen["status"] == "failed" and frozen.get("bridge_completion_hold"), frozen.get("error")
        assert len(posts) == 1
        monkeypatch.setattr(native, "LEGACY_RUNTIME_CONTRACT", runtime_contract())
        monkeypatch.setattr(
            native, "current_profile_seal", lambda: {"profile_id": "fixture-qualified-v20", "digest": "8" * 64}
        )
        service = native.InvalidReadContinuities(env.store, env.journal, env.engine, env.requests)
        env.requests.invalid_read_continuities = service
        env.continuity = service
        env.posts = posts
        env.frozen = frozen
        write_policy(env.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=True, max_window_seconds=180))
        token = env.principal_context.set(env.principal)
        try:
            yield env
        finally:
            env.principal_context.reset(token)
    await env.model.client.close()


async def admit(env, request_id="a" * 32):
    from firefly_forge.invalid_read_continuity_models import InvalidReadAdmissionInputV1

    candidate = await env.continuity.candidate(RUN)
    assert candidate["admission"] == "READY", candidate.get("holds")
    body = InvalidReadAdmissionInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        expected_run_digest=candidate["run_digest"],
        expected_journal_digest=candidate["journal_digest"],
        expected_policy_digest=candidate["lease_policy"]["digest"],
        valid_for_seconds=180,
        acknowledge_no_new_grant=True,
        acknowledge_original_consumption=True,
        acknowledge_renewed_same_attempt_lease=True,
    )
    return await env.continuity.request(RUN, request_id, body), body


async def test_native_admission_is_idempotent_and_does_not_reset_original_budget(continuity_case):
    env = continuity_case
    old = copy.deepcopy([r.data for r in env.journal.records.rows])
    ticket, body = await admit(env)
    replies = await asyncio.gather(*(env.continuity.request(RUN, ticket["id"], body) for _ in range(4)))
    assert all(reply == ticket for reply in replies)
    assert len(env.requests.records.rows) == 1
    assert [r.data for r in env.journal.records.rows][: len(old)] == old
    assert len([r for r in env.journal.records.rows if r.kind == "invalid_read_settlement"]) == 1
    assert len([r for r in env.journal.records.rows if r.kind == "invalid_read_claim"]) == 1
    current = await env.store.get_run(RUN)
    for key in (
        "attempt",
        "implementation_rounds",
        "repair_budget",
        "config_revision",
        "execution_actor",
    ):
        assert current.get(key) == env.frozen.get(key)
    assert ticket["lease"]["original_deadline_ms"] != ticket["lease"]["expires_at_ms"]
    assert len(env.posts) == 1


async def test_api_source_review_and_admission_do_not_acquire_worker_ownership(continuity_case, monkeypatch):
    from firefly_forge.agentic_checkpoint_invocation import workspace_cut

    env = continuity_case
    ownership_calls = []

    async def api_has_no_execution_owner():
        ownership_calls.append(True)
        raise RuntimeError("PostgreSQL execution ownership was lost")

    monkeypatch.setattr(env.store, "assert_ownership", api_has_no_execution_owner)
    old = copy.deepcopy([row.data for row in env.journal.records.rows])
    candidate = await env.continuity.candidate(RUN)
    assert candidate["admission"] == "READY"
    assert not env.requests.records.rows and [row.data for row in env.journal.records.rows] == old
    ticket, _ = await admit(env)
    assert ticket["status"] == "queued" and not ownership_calls
    assert len(env.posts) == 1
    with pytest.raises(RuntimeError, match="execution ownership"):
        await workspace_cut(env.store, env.workspace)
    assert ownership_calls == [True]
    await env.continuity.execute(ticket["id"])
    assert (await env.continuity.get(RUN, ticket["id"]))["status"] == "failed"
    assert len(env.posts) == 1


@pytest.mark.parametrize("status", ["queued", "executing"])
async def test_api_source_review_refuses_remote_native_claim_with_no_local_active_task(continuity_case, status):
    from uuid import UUID

    env = continuity_case
    assert not env.engine.active
    env.requests.records.rows.append(
        SimpleNamespace(run_id=UUID(RUN), kind="git_delivery", status=status, request_key="remote-native-claim")
    )
    with pytest.raises(ValueError, match="quiescent|active|held"):
        await env.continuity.candidate(RUN)
    assert len(env.posts) == 1


@pytest.mark.parametrize(
    "change",
    [
        "identity",
        "caller",
        "configuration",
        "frozen_configuration",
        "run",
        "task",
        "journal",
        "active",
        "stopping",
        "cancelled",
    ],
)
async def test_api_source_read_custody_refuses_current_authority_or_native_cut_drift(continuity_case, change):
    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.project_access import AccessDenied

    env = continuity_case
    run = await env.store.get_run(RUN)
    guard = await env.continuity.restores.source_read_guard(
        run, env.principal, task_attempt=run["attempt"], invalid_read=True
    )
    active = None
    if change == "identity":
        env.user.enabled = False
    elif change == "caller":
        env.principal_context.set(None)
    elif change == "configuration":
        env.store.configuration.update("bridge", ProviderConfiguration(values={"model": "different-model"}))
    elif change == "frozen_configuration":
        revision = run["config_revision"]
        settings = env.store.configuration.load_snapshot(revision)
        env.store.configuration._write(
            env.store.configuration.revisions / (revision + ".json"), {**settings, "FORGE_BRIDGE_MODEL": "changed"}
        )
    elif change == "run":
        env.store.run.data["brief"] = "Different retained scope"
    elif change == "task":
        task = await env.journal._task(RUN, "implement")
        task.data["changed"] = True
    elif change == "journal":
        await env.journal._append(RUN, "implement", "read-custody-probe", "read_custody_probe", {"fixture": True})
    elif change == "active":
        active = asyncio.get_running_loop().create_future()
        env.engine.active[RUN] = active
    elif change == "stopping":
        env.engine.stopping = True
    else:
        env.store.run.data["cancel_request"] = {"status": "requested"}
    try:
        with pytest.raises((AccessDenied, ValueError)):
            await guard()
    finally:
        env.principal_context.set(env.principal)
        if active is not None:
            active.cancel()
            env.engine.active.pop(RUN)
    assert not env.requests.records.rows and len(env.posts) == 1


@pytest.mark.parametrize("capacity", ["run", "pending"])
async def test_admin_can_expand_queue_capacity_but_atomic_admission_enforces_it(continuity_case, monkeypatch, capacity):
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicies, ContinuityLeasePolicyV1

    env = continuity_case
    configured = await ContinuityLeasePolicies(env.store).configure(
        ContinuityLeasePolicyV1(
            version=1, enabled=True, max_window_seconds=180, max_tickets_per_run=51, max_pending_tickets=501
        )
    )
    assert configured["settings"]["max_tickets_per_run"] == 51
    assert configured["settings"]["max_pending_tickets"] == 501
    counts = {"run": 51 if capacity == "run" else 50, "pending": 501 if capacity == "pending" else 500}

    async def historical_tickets(run_id, kind):
        return [SimpleNamespace(request_key="other-consumed-graph-" + str(i)) for i in range(min(counts["run"], 50))]

    async def ticket_count(run_id, kind):
        return counts["run"]

    async def pending_count():
        return counts["pending"]

    monkeypatch.setattr(env.requests.records, "for_run", historical_tickets)
    monkeypatch.setattr(env.requests.records, "count_for_run", ticket_count, raising=False)
    monkeypatch.setattr(env.requests.records, "pending_count", pending_count)
    old = copy.deepcopy([row.data for row in env.journal.records.rows])
    with pytest.raises(ValueError, match="queue is full"):
        await admit(env)
    assert not env.requests.records.rows
    assert [row.data for row in env.journal.records.rows] == old
    assert len(env.posts) == 1
    counts.update(run=50, pending=500)
    ticket, body = await admit(env)
    assert ticket["status"] == "queued" and len(env.requests.records.rows) == 1
    assert await env.continuity.request(RUN, ticket["id"], body) == ticket
    current = await env.store.get_run(RUN)
    assert current["attempt"] == 2 and current["implementation_rounds"] == 1 and current["repair_budget"] == 2
    assert len(env.posts) == 1


async def test_actual_engine_resumes_migrated_history_and_requests_only_next_completion(continuity_case):
    env = continuity_case
    ticket, _ = await admit(env)
    await env.continuity.execute(ticket["id"])
    current = await env.store.get_run(RUN)
    observed = await env.continuity.get(RUN, ticket["id"])
    assert len(env.posts) == 4

    request = json.loads(env.posts[1])
    assert request["messages"][-1]["role"] == "tool"
    assert json.loads(request["messages"][-1]["content"]) == {"status": "invalid_query"}
    assert observed["status"] == "completed", observed.get("result")
    assert current["status"] == "paused" and current["worker_feedback"]["id"] == "continued-review"
    assert current["usage"]["requests"] == 4
    assert current["attempt"] == env.frozen["attempt"] == 2
    assert current["implementation_rounds"] == env.frozen["implementation_rounds"] + 1 == 2
    assert current["repair_budget"] == 2
    consumes = [r for r in env.journal.records.rows if r.kind == "agent_consume"]
    assert len(consumes) == 4 and all(r.data["payload"]["version"] == 1 for r in consumes)
    await env.continuity.execute(ticket["id"])
    assert len(env.posts) == 4


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_actual_worker_contract_resumes_strict_read_and_two_file_command_cycles(continuity_case):
    env = continuity_case
    ticket, _ = await admit(env)
    before = await env.store.get_run(RUN)
    assert before["attempt"] == 2 and before["implementation_rounds"] == 1
    assert len(env.journal.sandboxes.rows) == 11
    assert before["sandbox_snapshot"]["configuration"]["command_limit"] == 22
    await env.continuity.execute(ticket["id"])
    observed = await env.continuity.get(RUN, ticket["id"])
    assert observed["status"] == "completed", (
        observed.get("result"),
        (await env.store.get_run(RUN)).get("error"),
        len(env.posts),
        env.command_calls,
    )
    assert len(env.posts) == 7 and len(env.evidence_calls) == 1
    assert env.evidence_calls[0]["limit"] == 2
    assert (env.workspace / "existing.py").read_text() == "VALUE = 2\n"
    assert len(env.journal.sandboxes.rows) == 13
    assert env.command_calls[-2:] == [["first-controlled-command"], ["second-controlled-command"]]
    assert [row.ordinal for row in env.journal.sandboxes.rows] == list(range(1, 14))
    assert [row.data for row in env.journal.sandboxes.rows][:11] == env.historical_rows
    assert [row.data for row in env.journal.records.rows][: len(env.historical_wal)] == env.historical_wal
    assert len({row.id for row in env.journal.sandboxes.rows}) == 13
    current = await env.store.get_run(RUN)
    assert current["attempt"] == 2 and current["repair_budget"] == 2
    assert current["usage"]["requests"] == 7
    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint

    checkpoint = [r.data["payload"] for r in env.journal.records.rows if r.kind == "agent_checkpoint"][-1]
    restored = BoundaryCheckpoint.decode(checkpoint["snapshot"].encode(), checkpoint["checkpoint_digest"])
    assert restored.usage.tool_calls == 6
    limits = restored.resume_kwargs()["usage_limits"]
    assert limits.request_limit == 100 and limits.tool_calls_limit == 300
    await env.continuity.execute(ticket["id"])
    assert len(env.posts) == 7
    assert len(env.journal.sandboxes.rows) == 13 and len(env.command_calls) == 13


@pytest.mark.parametrize(
    "field,value",
    [
        ("checkpoint", "{}"),
        ("checkpoint_digest", "0" * 64),
        ("settings_digest", "0" * 64),
        ("current_profile", {}),
        ("command_custody_digest", "0" * 64),
        ("lease_generation", "0" * 32),
    ],
)
async def test_ticket_immutable_fields_are_bound_to_native_admission(continuity_case, field, value):
    env = continuity_case
    ticket, _ = await admit(env)
    env.requests.records.rows[0].data[field] = value
    with pytest.raises(ValueError, match="seal|admission"):
        await env.continuity.get(RUN, ticket["id"])
    assert len(env.posts) == 1


@pytest.mark.parametrize(
    "field,value", [("brief", "Changed scope"), ("repair_budget", 99), ("commands", ["new command"])]
)
async def test_changed_protected_execution_scope_cannot_claim(continuity_case, field, value):
    env = continuity_case
    ticket, _ = await admit(env)
    env.store.run.data[field] = value
    await env.continuity.execute(ticket["id"])
    assert (await env.continuity.get(RUN, ticket["id"]))["status"] == "failed"
    assert len(env.posts) == 1


async def test_revoked_policy_stops_queued_lease_and_cannot_be_replayed(continuity_case):
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    env = continuity_case
    ticket, body = await admit(env)
    write_policy(env.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
    await env.continuity.execute(ticket["id"])
    observed = await env.continuity.request(RUN, ticket["id"], body)
    assert observed["status"] == "failed" and len(env.posts) == 1


async def test_disabled_policy_preserves_candidate_and_never_admits(continuity_case):
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    env = continuity_case
    write_policy(env.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
    candidate = await env.continuity.candidate(RUN)
    assert candidate["admission"] == "HOLD" and "lease_policy_disabled" in candidate["holds"]
    assert not env.requests.records.rows and len(env.posts) == 1


@pytest.mark.parametrize(
    "change",
    [
        "binding_scope",
        "claim_task",
        "claim_lease",
        "checkpoint",
        "candidate",
        "version",
        "unknown_field",
        "claim_version",
    ],
)
async def test_settlement_semantics_refuse_rehashed_incompatible_claims(continuity_case, change):
    from firefly_forge.invalid_read_settlements import settled_requests
    from firefly_forge.journal import digest

    env = continuity_case
    await admit(env)
    entries = copy.deepcopy([row.data for row in env.journal.records.rows])
    settlement, claim = entries[-2:]
    payload = settlement["payload"]
    if change == "binding_scope":
        payload["binding"]["brief"] = "Different task"
    elif change == "claim_task":
        claim["payload"]["task_key"] = "0" * 32
    elif change == "claim_lease":
        claim["payload"]["expires_at_ms"] += 1
    elif change == "checkpoint":
        payload["checkpoint_digest"] = "0" * 64
    elif change == "version":
        payload["version"] = 2
    elif change == "unknown_field":
        payload["unreviewed"] = True
    elif change == "claim_version":
        claim["payload"]["version"] = True
    else:
        payload["candidate_digest"] = "0" * 64
    payload["digest"] = digest({k: v for k, v in payload.items() if k != "digest"})
    with pytest.raises(ValueError):
        settled_requests(settlement, entries)


@pytest.mark.parametrize("change", ["cancelled", "source", "expired", "profile", "identity", "configuration"])
async def test_current_execution_fences_refuse_before_next_model(continuity_case, monkeypatch, change):
    from firefly_forge import invalid_read_continuities as native
    from firefly_forge import invalid_read_tickets as tickets

    env = continuity_case
    ticket, _ = await admit(env)
    if change == "cancelled":
        env.store.run.data["cancel_request"] = {"status": "requested"}
    elif change == "source":
        (env.workspace / "existing.py").write_text("CHANGED = True\n")
    elif change == "expired":
        monkeypatch.setattr(tickets.time, "time", lambda: ticket["lease"]["expires_at_ms"] / 1000 + 1)
    elif change == "profile":
        monkeypatch.setattr(native, "current_profile_seal", lambda: {"profile_id": "changed", "digest": "0" * 64})
    elif change == "identity":
        env.user.enabled = False
    else:
        from firefly_forge.configuration import ProviderConfiguration

        env.store.configuration.update("bridge", ProviderConfiguration(values={"model": "changed/model"}))
    await env.continuity.execute(ticket["id"])
    assert (await env.requests.get(ticket["id"]))["status"] == "failed"
    assert len(env.posts) == 1


async def test_owner_loss_after_one_claim_is_terminal_and_never_requeued(continuity_case, monkeypatch):
    env = continuity_case
    ticket, _ = await admit(env)

    async def lost_owner(_):
        raise RuntimeError("Controlled loss after claim before any next request")

    monkeypatch.setattr(env.engine, "continue_agent_checkpoint", lost_owner)
    await env.continuity.execute(ticket["id"])
    assert (await env.continuity.get(RUN, ticket["id"]))["status"] == "needs_reconciliation"
    await env.continuity.recover()
    await env.continuity.execute(ticket["id"])
    assert len(env.posts) == 1
    assert len([r for r in env.journal.records.rows if r.kind == "invalid_read_claim"]) == 1


async def test_operator_context_loss_during_final_admission_scan_cannot_commit(continuity_case, monkeypatch):
    from firefly_forge.project_access import AccessDenied

    env = continuity_case
    original = env.store.admit_credentials

    async def lost_context(value):
        await original(value)
        if isinstance(value, list) and value and isinstance(value[0], dict) and "input" in value[0]:
            env.principal_context.set(None)

    monkeypatch.setattr(env.store, "admit_credentials", lost_context)
    try:
        with pytest.raises(AccessDenied):
            await admit(env)
        assert not env.requests.records.rows
        assert not any(row.kind == "invalid_read_settlement" for row in env.journal.records.rows)
    finally:
        env.principal_context.set(env.principal)
    assert len(env.posts) == 1


@pytest.mark.parametrize("change", ["permission", "caller", "task", "journal", "configuration"])
async def test_api_source_inspection_rechecks_custody_during_streaming(continuity_case, monkeypatch, change):
    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.project_access import AccessDenied

    env = continuity_case
    scanner = env.store.source_patch_scanner
    changed = False

    async def racing_scanner(*args, **kwargs):
        nonlocal changed
        result = await scanner(*args, **kwargs)
        if not changed:
            changed = True
            if change == "permission":
                env.user.enabled = False
            elif change == "caller":
                env.principal_context.set(None)
            elif change == "task":
                task = await env.journal._task(RUN, "implement")
                task.attempt += 1
            elif change == "journal":
                await env.journal._append(RUN, "implement", "mid-scan-probe", "read_custody_probe", {"fixture": True})
            else:
                env.store.configuration.update("bridge", ProviderConfiguration(values={"model": "mid-scan-change"}))
        return result

    monkeypatch.setattr(env.store, "source_patch_scanner", racing_scanner)
    try:
        with pytest.raises((AccessDenied, ValueError)):
            await env.continuity.candidate(RUN)
    finally:
        env.principal_context.set(env.principal)
    assert changed and not env.requests.records.rows and len(env.posts) == 1


async def test_consumed_hold_lookup_cannot_hide_behind_paginated_ticket_history(continuity_case, monkeypatch):
    from uuid import UUID, uuid4

    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.invalid_read_tickets import KIND

    env = continuity_case
    write_policy(
        env.store.configuration,
        ContinuityLeasePolicyV1(version=1, enabled=True, max_window_seconds=180, max_tickets_per_run=100),
    )
    hold = env.frozen["bridge_completion_hold"]["request_id"]
    for index in range(51):
        env.requests.records.rows.append(
            SimpleNamespace(
                id=uuid4(),
                run_id=UUID(RUN),
                kind=KIND,
                status="failed",
                request_key=hold if index == 0 else "other-hold-" + str(index),
                data={},
            )
        )

    async def paginated(run_id, kind):
        return env.requests.records.rows[-50:]

    monkeypatch.setattr(env.requests.records, "for_run", paginated)
    before = copy.deepcopy([row.data for row in env.journal.records.rows])
    with pytest.raises(ValueError, match="already has a migration ticket"):
        await admit(env)
    assert len(env.requests.records.rows) == 51
    assert [row.data for row in env.journal.records.rows] == before and len(env.posts) == 1


async def test_api_admission_rechecks_frozen_config_inside_native_writer(continuity_case, monkeypatch):
    env = continuity_case
    original = env.store.admit_credentials
    changed = False

    async def mutate_after_preparation(value):
        nonlocal changed
        await original(value)
        if not changed and isinstance(value, list) and value and isinstance(value[0], dict) and "input" in value[0]:
            changed = True
            revision = env.frozen["config_revision"]
            settings = env.store.configuration.load_snapshot(revision)
            env.store.configuration._write(
                env.store.configuration.revisions / (revision + ".json"),
                {**settings, "FORGE_BRIDGE_MODEL": "late-change"},
            )

    before = copy.deepcopy([row.data for row in env.journal.records.rows])
    monkeypatch.setattr(env.store, "admit_credentials", mutate_after_preparation)
    with pytest.raises(ValueError, match="configuration"):
        await admit(env)
    assert changed and not env.requests.records.rows
    assert [row.data for row in env.journal.records.rows] == before and len(env.posts) == 1


async def test_concurrent_identical_admission_returns_known_receipt_after_read_custody_conflict(
    continuity_case, monkeypatch
):
    env = continuity_case
    prepare = env.continuity._prepare
    request = env.continuity.request
    selected = []
    raced = False

    async def capture_request(run_id, request_id, body):
        selected.append((run_id, request_id, body))
        return await request(run_id, request_id, body)

    async def concurrent_prepare(run_id):
        nonlocal raced
        if selected and not raced:
            raced = True
            await request(*selected[0])
        return await prepare(run_id)

    monkeypatch.setattr(env.continuity, "request", capture_request)
    monkeypatch.setattr(env.continuity, "_prepare", concurrent_prepare)
    ticket, _ = await admit(env)
    assert raced and ticket["status"] == "queued" and len(env.requests.records.rows) == 1
    assert len([row for row in env.journal.records.rows if row.kind == "invalid_read_claim"]) == 1
    assert len(env.posts) == 1


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_retained_sdk_compatibility_witness_is_bound_to_native_settlement(continuity_case):
    import hashlib

    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload
    from firefly_forge.invalid_read_continuity import RETAINED_SDK_RUNTIME_CONTRACT
    from firefly_forge.invalid_read_settlements import settled_requests
    from firefly_forge.journal import ZERO_HASH, digest

    env = continuity_case
    previous, replaced = ZERO_HASH, {}
    original_physical = None
    for row in env.journal.records.rows:
        data = row.data
        old_digest = data["digest"]
        if row.kind == "agent_checkpoint":
            payload = data["payload"]
            checkpoint = BoundaryCheckpoint.decode(payload["snapshot"].encode(), payload["checkpoint_digest"])
            record = json.loads(checkpoint.canonical_payload)
            record["compatibility"]["pydantic-ai-slim"] = "2.58.0"
            record["contract"]["parameters"].update(
                _telemetry_mcp_url_ids=[], _telemetry_mcp_url_toolset_ids=[], _telemetry_mcp_url_tool_names=[]
            )
            for definition in record["contract"]["parameters"]["function_tools"]:
                assert "tool_kind" in definition and definition["tool_kind"] is None
            record["usage"].update(
                input_tokens=200,
                output_tokens=100,
                cache_write_tokens=30,
                output_reasoning_tokens=17,
                cache_write_1h_tokens=5,
            )
            original_physical = _checkpoint_payload(
                json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            )
            payload.update(
                snapshot=original_physical.decode(), checkpoint_digest=hashlib.sha256(original_physical).hexdigest()
            )
            payload["invocation"]["contract"] = RETAINED_SDK_RUNTIME_CONTRACT
        if row.kind == "agent_consume":
            for key in ("checkpoint_record_digest", "response_record_digest"):
                data["payload"][key] = replaced.get(data["payload"][key], data["payload"][key])
        data["previous_hash"] = previous
        data["digest"] = previous = digest({k: v for k, v in data.items() if k != "digest"})
        replaced[old_digest] = previous
        row.digest, row.previous_hash = previous, data["previous_hash"]
    from firefly_forge.invalid_read_continuity import prepare_invalid_read, reconstruct_and_migrate
    from firefly_forge.known_worker_completions import _task

    reordered = copy.deepcopy([row.data for row in env.journal.records.rows])
    changed = next(entry["payload"] for entry in reordered if entry["kind"] == "agent_checkpoint")
    source_record = BoundaryCheckpoint(changed["snapshot"].encode(), changed["checkpoint_digest"]).record.model_dump(
        mode="json"
    )
    assert len(source_record["contract"]["parameters"]["function_tools"]) > 1
    source_record["contract"]["parameters"]["function_tools"].reverse()
    reordered_raw = _checkpoint_payload(
        json.dumps(source_record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    changed.update(snapshot=reordered_raw.decode(), checkpoint_digest=hashlib.sha256(reordered_raw).hexdigest())
    previous, rebound = ZERO_HASH, {}
    for entry in reordered:
        old_digest = entry["digest"]
        if entry["kind"] == "agent_consume":
            for key in ("checkpoint_record_digest", "response_record_digest"):
                entry["payload"][key] = rebound.get(entry["payload"][key], entry["payload"][key])
        entry["previous_hash"] = previous
        entry["digest"] = previous = digest({k: v for k, v in entry.items() if k != "digest"})
        rebound[old_digest] = previous
    prepared = prepare_invalid_read(
        await env.store.get_run(RUN), _task(await env.journal._task(RUN, "implement")), reordered
    )
    with pytest.raises(ValueError, match="wire|Restored model state"):
        await reconstruct_and_migrate(prepared)
    assert len(env.posts) == 1
    before = copy.deepcopy([row.data for row in env.journal.records.rows])
    assert await env.journal.entries(RUN) == before
    historical_row = next(row for row in env.journal.records.rows if row.kind == "agent_checkpoint")
    intact_digest = historical_row.digest
    historical_row.digest = "0" * 64
    try:
        with pytest.raises(ValueError, match="custody"):
            await env.journal.entries(RUN)
    finally:
        historical_row.digest = intact_digest
    assert await env.journal.entries(RUN) == before
    ticket, _ = await admit(env)
    entries = copy.deepcopy([row.data for row in env.journal.records.rows])
    assert entries[: len(before)] == before
    settlement = entries[-2]
    assert settled_requests(settlement, entries) == [env.frozen["bridge_completion_hold"]["request_id"]]
    witness = settlement["payload"]["reconstruction"]["compatibility_migration"]
    assert witness["source_checkpoint"] == hashlib.sha256(original_physical).hexdigest()
    assert witness["source_runtime"] == RETAINED_SDK_RUNTIME_CONTRACT
    original_tools = BoundaryCheckpoint(
        original_physical, hashlib.sha256(original_physical).hexdigest()
    ).record.contract["parameters"]["function_tools"]
    assert [item["name"] for item in witness["source_function_tool_kinds"]] == [tool["name"] for tool in original_tools]
    assert all(item["source_present"] is True for item in witness["source_function_tool_kinds"])
    assert all(value == [] for value in witness["source_telemetry"].values())
    for change in ("missing", "target", "derived_checkpoint"):
        altered = copy.deepcopy(entries)
        payload = altered[-2]["payload"]
        reconstruction = payload["reconstruction"]
        if change == "missing":
            reconstruction.pop("compatibility_migration")
        elif change == "target":
            reconstruction["compatibility_migration"]["target"]["pydantic-ai-slim"] = "2.54.0"
        else:
            reconstruction["compatibility_migration"]["derived_checkpoint"] = "0" * 64
        reconstruction["digest"] = digest({k: v for k, v in reconstruction.items() if k != "digest"})
        payload["digest"] = digest({k: v for k, v in payload.items() if k != "digest"})
        with pytest.raises(ValueError, match="compatibility"):
            settled_requests(altered[-2], altered)
    await env.continuity.execute(ticket["id"])
    assert len(env.posts) == 7
    observed = await env.continuity.get(RUN, ticket["id"])
    assert observed["status"] == "completed", (observed.get("result"), (await env.store.get_run(RUN)).get("error"))
    assert len(env.journal.sandboxes.rows) == 13
    assert [row.data for row in env.journal.sandboxes.rows][:11] == env.historical_rows
    latest = [row.data["payload"] for row in env.journal.records.rows if row.kind == "agent_checkpoint"][-1]
    resumed = BoundaryCheckpoint.decode(latest["snapshot"].encode(), latest["checkpoint_digest"])
    assert resumed.record.usage["output_reasoning_tokens"] == 17
    assert resumed.record.usage["cache_write_1h_tokens"] == 5
    assert resumed.usage.requests == 6 and resumed.usage.tool_calls == 6
    assert resumed.resume_kwargs()["usage_limits"].request_limit == 100
    assert resumed.resume_kwargs()["usage_limits"].tool_calls_limit == 300
    await env.continuity.execute(ticket["id"])
    assert len(env.posts) == 7 and len(env.journal.sandboxes.rows) == 13
    assert [row.data for row in env.journal.records.rows][: len(before)] == before
