# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One real legacy policy/SDK/Engine path with owned memory rows and exact mock transport."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
import test_invalid_read_native_protocol_host as protocol
from test_invalid_read_epochs_host import freeze_legacy

pytest_plugins = ["test_invalid_read_epochs_host"]


@pytest.fixture
async def retained_epoch_engine_case(legacy_epoch_case, tmp_path, monkeypatch):
    from firefly_forge.checkpoint_mode_runtime import effective_revision
    from firefly_forge.invalid_read_continuity import RETAINED_SDK_RUNTIME_CONTRACT
    from firefly_forge.invalid_read_epochs import epoch_scope, epoch_snapshot
    from firefly_forge.journal import digest
    from firefly_forge.unknown_command_proof import continuation_scope

    legacy = legacy_epoch_case
    import test_agentic_checkpoint_activation_host as activation
    import test_agentic_checkpoint_storage_host as storage
    import test_bridge_completion_journal_host as bridge_journal
    import test_bridge_inflight_cancel as bridge_fixture

    for fixture_module in (protocol, activation, storage, bridge_journal, bridge_fixture):
        monkeypatch.setattr(fixture_module, "RUN", legacy.run_id)
    activated = protocol.activated
    render = protocol.result

    def selected_result(*args, **kwargs):
        response = render(*args, **kwargs)
        response["model"] = legacy.store.configuration.load_snapshot(effective_revision(legacy.store.run.data))[
            "FORGE_BRIDGE_MODEL"
        ]
        return response

    monkeypatch.setattr(protocol, "result", selected_result)

    async def activate_with_legacy(*args):
        env = await activated(*args)
        env.principal = legacy.actor
        env.user.id, env.user.issuer, env.user.subject = legacy.actor.id, legacy.actor.issuer, legacy.actor.subject
        from firefly_forge.bridge_completions import BridgeCompletions
        from firefly_forge.project_access import AccessDenied

        async def identity(self, user_id):
            if user_id != env.user.id.hex or not env.user.enabled:
                raise AccessDenied("Controlled original identity is unavailable")
            return env.user

        monkeypatch.setattr(BridgeCompletions, "_identity", identity)
        real_execute = env.engine.execute_run
        real_plan, real_ensure = env.journal.plan, env.journal.ensure_plan
        installed = False

        async def plan(run_id):
            nonlocal installed
            if not installed:
                # Build the complete fictional retained scope before the SDK freezes any invocation.
                data = copy.deepcopy(env.store.run.data) | copy.deepcopy(legacy.store.run.data)
                data["role_snapshot"] = env.store.run.data["role_snapshot"]
                data["git_metadata_digest"] = env.store.run.data["git_metadata_digest"]
                data["resume_existing"] = True
                legacy.store.run.data = data
                env.store.configuration = legacy.store.configuration
                env.store.task_router = legacy.store.task_router
                env.store.run.data = {
                    k: v
                    for k, v in data.items()
                    if k not in {"checkpoint_mode_adoption", "original_selection_readmission"}
                }
                env.journal.plan, env.journal.ensure_plan = real_plan, real_ensure
                await real_ensure(run_id)
                prepared = await env.journal.intent(run_id, "prepare")
                await env.journal.receipt(
                    run_id, prepared["idempotency_key"], {"source": "owned fictional Git workspace"}
                )
                env.journal.plan = env.journal.ensure_plan = plan
                other_tasks = [row for row in env.journal.tasks.rows if row.task_id != "implement"]
                prefix = copy.deepcopy(env.journal.records.rows)
                rebound, previous = {}, prefix[-1].digest
                for row in legacy.store.records.rows:
                    if row.kind == "checkpoint_mode_intent":
                        break
                    old = row.data["digest"]
                    row.data.update(sequence=row.sequence + len(prefix), previous_hash=previous)
                    row.data["digest"] = previous = digest({k: v for k, v in row.data.items() if k != "digest"})
                    rebound[old] = previous
                for row in legacy.store.records.rows:
                    if row.kind in {"checkpoint_mode_adoption", "original_selection_readmission"}:
                        proof = row.data["payload"]["candidate"]["proof"]
                        proof.update(
                            {key: rebound.get(value, value) for key, value in proof.items() if isinstance(value, str)}
                        )
                        proof["digest"] = digest({k: v for k, v in proof.items() if k != "digest"})
                    row.sequence += len(prefix)
                    row.data["sequence"] = row.sequence
                legacy.store.records.rows = prefix + legacy.store.records.rows
                scope = continuation_scope(data)
                for row in legacy.store.records.rows:
                    if row.kind in {"checkpoint_mode_adoption", "original_selection_readmission"}:
                        row.data["payload"]["candidate"]["original_scope_digest"] = scope
                freeze_legacy(legacy)
                env.store.run.data = copy.deepcopy(legacy.store.run.data)
                env.store.configuration = legacy.store.configuration
                env.store.task_router = legacy.store.task_router
                env.journal.records.rows = copy.deepcopy(legacy.store.records.rows)
                env.journal.tasks.rows = other_tasks + copy.deepcopy(legacy.store.tasks.rows)
                env.store.configuration._write(
                    env.store.configuration.path,
                    {"bridge": env.store.configuration.load_snapshot(effective_revision(env.store.run.data))},
                )
                env.provider.settings = env.store.configuration.load_snapshot(effective_revision(env.store.run.data))
                installed = True
            return {
                "tasks": [
                    {**copy.deepcopy(r.data), "id": r.task_id, "status": r.status, "attempt": r.attempt}
                    for r in env.journal.tasks.rows
                ]
            }

        async def historical_producer(run_id):
            epoch = epoch_snapshot(
                env.store,
                await env.store.get_run(run_id),
                await env.journal.entries(run_id),
                legacy_runtime_contract=RETAINED_SDK_RUNTIME_CONTRACT,
            )
            assert epoch is not None
            # Only fixture production uses this real, validated historical scope. The test's
            # continuity admission and worker execution enter their own production scopes.
            with epoch_scope(epoch):
                await real_execute(run_id)
            env.engine.execute_run = real_execute

        env.journal.ensure_plan = plan
        env.journal.plan = plan
        env.engine.execute_run = historical_producer
        return env

    monkeypatch.setattr(protocol, "activated", activate_with_legacy)
    generator = protocol.continuity_case.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param=False))
    env = await anext(generator)
    try:
        yield env
    finally:
        await generator.aclose()


async def test_real_retained_policy_epoch_and_sdk_checkpoint_reaches_same_attempt_engine(retained_epoch_engine_case):
    from test_bridge_inflight_cancel import RUN

    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload
    from firefly_forge.invalid_read_continuity import RETAINED_SDK_RUNTIME_CONTRACT
    from firefly_forge.invalid_read_epochs import epoch_snapshot
    from firefly_forge.journal import ZERO_HASH, digest

    env = retained_epoch_engine_case
    assert len(env.posts) == 1
    previous, rebound = ZERO_HASH, {}
    for row in env.journal.records.rows:
        value = row.data
        old = value["digest"]
        if row.kind == "agent_checkpoint":
            payload = value["payload"]
            checkpoint = BoundaryCheckpoint.decode(payload["snapshot"].encode(), payload["checkpoint_digest"])
            record = json.loads(checkpoint.canonical_payload)
            record["compatibility"]["pydantic-ai-slim"] = "2.58.0"
            record["contract"]["parameters"].update(
                _telemetry_mcp_url_ids=[], _telemetry_mcp_url_toolset_ids=[], _telemetry_mcp_url_tool_names=[]
            )
            record["usage"].update(
                input_tokens=200,
                output_tokens=100,
                cache_write_tokens=30,
                output_reasoning_tokens=17,
                cache_write_1h_tokens=5,
            )
            physical = _checkpoint_payload(
                json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            )
            payload.update(snapshot=physical.decode(), checkpoint_digest=hashlib.sha256(physical).hexdigest())
            payload["invocation"]["contract"] = RETAINED_SDK_RUNTIME_CONTRACT
        if row.kind == "agent_consume":
            for key in ("checkpoint_record_digest", "response_record_digest"):
                value["payload"][key] = rebound.get(value["payload"][key], value["payload"][key])
        value["previous_hash"] = previous
        value["digest"] = previous = digest({k: v for k, v in value.items() if k != "digest"})
        row.digest, row.previous_hash = previous, value["previous_hash"]
        rebound[old] = previous
    original = copy.deepcopy(await env.journal.entries(RUN))
    epoch = epoch_snapshot(
        env.store, await env.store.get_run(RUN), original, legacy_runtime_contract=RETAINED_SDK_RUNTIME_CONTRACT
    )
    assert epoch is not None and epoch["legacy_runtime_contract"] == RETAINED_SDK_RUNTIME_CONTRACT
    ticket, _ = await protocol.admit(env)
    assert ticket["policy_epoch"] is not None
    assert ticket["policy_epoch"]["legacy_runtime_contract"] == RETAINED_SDK_RUNTIME_CONTRACT
    entries = await env.journal.entries(RUN)
    assert entries[: len(original)] == original
    assert [e["kind"] for e in entries[-2:]] == ["invalid_read_settlement", "invalid_read_claim"]
    assert len(env.posts) == 1
    await env.continuity.execute(ticket["id"])
    observed = await env.continuity.get(RUN, ticket["id"])
    event_kinds = [event["kind"] for event in await env.requests.events(ticket["id"])]
    assert observed["status"] == "completed", {
        "result": observed.get("result"),
        "model_posts": len(env.posts),
        "native_events": event_kinds,
        "legacy_runtime": ticket["policy_epoch"]["legacy_runtime_contract"],
    }
    assert len(env.posts) == 4
    assert env.store.run.data["attempt"] == 2 and env.store.run.data["repair_budget"] == 2
    assert (await env.journal.entries(RUN))[: len(original)] == original
    assert event_kinds == ["request_admitted", "worker_claim", "worker_completed"]
    latest = [r.data["payload"] for r in env.journal.records.rows if r.kind == "agent_checkpoint"][-1]
    resumed = BoundaryCheckpoint.decode(latest["snapshot"].encode(), latest["checkpoint_digest"])
    assert resumed.record.usage["output_reasoning_tokens"] == 17
    assert resumed.record.usage["cache_write_1h_tokens"] == 5
    assert resumed.usage.requests == 3 and resumed.usage.tool_calls == 3
    assert resumed.resume_kwargs()["usage_limits"].request_limit == 100
    assert resumed.resume_kwargs()["usage_limits"].tool_calls_limit == 300
    await env.continuity.execute(ticket["id"])
    assert len(env.posts) == 4
