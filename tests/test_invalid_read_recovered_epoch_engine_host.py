# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One real legacy policy/SDK/Engine path with owned memory rows and exact mock transport."""

import copy
import hashlib
import json

import test_invalid_read_native_protocol_host as protocol

pytest_plugins = ["test_invalid_read_retained_epoch_engine_host"]


async def test_recovered_retained_sdk_epoch_restarts_and_reaches_real_engine(retained_epoch_engine_case):
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
            record["compatibility"] = {"fireflyframework-agentic": "26.10.1", "pydantic-ai-slim": "2.58.0"}
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
    from types import MethodType

    from test_invalid_read_recovery_host import recovery_input

    await env.continuity._finish(ticket["id"], "failed", {"detail": "Controlled preclaim failure"})

    async def budget(self, key):
        rows = [r for r in self.rows if r.request_id == key]
        return len(rows), sum(len(json.dumps(r.data).encode()) for r in rows)

    env.requests.journal.custody_budget = MethodType(budget, env.requests.journal)

    async def pending():
        return [await env.journal._task(RUN, "implement")]

    env.journal.tasks.find_pending_recovery = pending
    assert await env.journal.recover() == 1
    candidate = await env.continuity.recoveries.candidate(RUN, ticket["id"])
    restored = BoundaryCheckpoint.decode(ticket["checkpoint"].encode(), ticket["checkpoint_digest"])
    assert candidate["usage"]["requests"] == restored.usage.requests
    assert candidate["usage"]["tool_calls"] == restored.usage.tool_calls
    await env.continuity.recoveries.request(RUN, ticket["id"], "b" * 32, recovery_input(candidate))
    await env.continuity.execute(ticket["id"])
    observed = await env.continuity.get(RUN, ticket["id"])
    event_kinds = [event["kind"] for event in await env.requests.events(ticket["id"])]
    assert observed["status"] == "completed", {
        "result": observed.get("result"),
        "model_posts": len(env.posts),
        "native_events": event_kinds,
        "legacy_runtime": ticket["policy_epoch"]["legacy_runtime_contract"],
    }
    from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed

    env.store.execution_requests = env.requests
    await assert_generic_dispatch_allowed(env.store, env.journal, await env.store.get_run(RUN))
    assert len(env.posts) == 4
    assert env.store.run.data["attempt"] == 2 and env.store.run.data["repair_budget"] == 2
    assert (await env.journal.entries(RUN))[: len(original)] == original
    assert event_kinds == [
        "request_admitted",
        "worker_failed",
        "request_recovered_before_claim",
        "worker_claim",
        "worker_completed",
    ]
    latest = [r.data["payload"] for r in env.journal.records.rows if r.kind == "agent_checkpoint"][-1]
    resumed = BoundaryCheckpoint.decode(latest["snapshot"].encode(), latest["checkpoint_digest"])
    assert resumed.record.usage["output_reasoning_tokens"] == 17
    assert resumed.record.usage["cache_write_1h_tokens"] == 5
    assert resumed.usage.requests == 3 and resumed.usage.tool_calls == 3
    assert resumed.resume_kwargs()["usage_limits"].request_limit == 100
    assert resumed.resume_kwargs()["usage_limits"].tool_calls_limit == 300
    await env.continuity.execute(ticket["id"])
    assert len(env.posts) == 4
