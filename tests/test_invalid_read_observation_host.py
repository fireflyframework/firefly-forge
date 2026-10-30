# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Unavailable local observations are explicit negative outcomes, never historical success."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

pytest_plugins = ["test_invalid_read_native_protocol_host"]


def test_unavailable_observation_policy_defaults_off():
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1

    assert "allow_unavailable_source_observations" in ContinuityLeasePolicyV1.model_fields
    assert ContinuityLeasePolicyV1(version=1).allow_unavailable_source_observations is False


def test_v2_requires_new_exact_acknowledgement_and_preserves_v1_wire():
    from firefly_forge import invalid_read_continuity_models as dto

    assert hasattr(dto, "InvalidReadAdmissionInputV2")
    base = dict(
        candidate_digest="1" * 64,
        expected_run_digest="2" * 64,
        expected_journal_digest="3" * 64,
        expected_policy_digest="4" * 64,
        valid_for_seconds=60,
        acknowledge_no_new_grant=True,
        acknowledge_original_consumption=True,
        acknowledge_renewed_same_attempt_lease=True,
    )
    assert (
        "acknowledge_unavailable_source_observations"
        not in dto.InvalidReadAdmissionInputV1(version=1, **base).model_dump()
    )
    for bad in (None, False, 1, "true"):
        with pytest.raises(ValueError):
            dto.InvalidReadAdmissionInputV2(version=2, **base, acknowledge_unavailable_source_observations=bad)
    accepted = dto.InvalidReadAdmissionInputV2(version=2, **base, acknowledge_unavailable_source_observations=True)
    assert accepted.version == 2


def add_sibling(env, name="list_files", arguments=None, *, retained_epoch=False):
    from firefly_forge.journal import ZERO_HASH, digest

    previous, rebound = ZERO_HASH, {}
    for row in env.journal.records.rows:
        data = row.data
        old_digest = data["digest"]
        if row.kind == "agent_checkpoint" and retained_epoch:
            from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload
            from firefly_forge.invalid_read_continuity import RETAINED_SDK_RUNTIME_CONTRACT

            payload = data["payload"]
            original = BoundaryCheckpoint.decode(payload["snapshot"].encode(), payload["checkpoint_digest"])
            snapshot = json.loads(original.canonical_payload)
            snapshot["compatibility"] = {"fireflyframework-agentic": "26.10.1", "pydantic-ai-slim": "2.58.0"}
            snapshot["contract"]["parameters"].update(
                _telemetry_mcp_url_ids=[], _telemetry_mcp_url_toolset_ids=[], _telemetry_mcp_url_tool_names=[]
            )
            snapshot["usage"].update(
                requests=7,
                tool_calls=11,
                input_tokens=200,
                output_tokens=100,
                cache_write_tokens=30,
                output_reasoning_tokens=17,
                cache_write_1h_tokens=5,
            )
            physical = _checkpoint_payload(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            )
            payload.update(snapshot=physical.decode(), checkpoint_digest=hashlib.sha256(physical).hexdigest())
            payload["invocation"]["contract"] = RETAINED_SDK_RUNTIME_CONTRACT
        if row.kind == "bridge_request_response":
            payload = data["payload"]
            response = json.loads(payload["response"])
            sibling = {
                "id": "original-source-observation",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments or {})},
            }
            response["choices"][0]["message"]["tool_calls"].insert(0, sibling)
            raw = json.dumps(response, separators=(",", ":"))
            payload.update(
                response=raw, response_sha256=hashlib.sha256(raw.encode()).hexdigest(), response_bytes=len(raw.encode())
            )
            original_response = payload
        if row.kind == "agent_consume":
            for key in ("checkpoint_record_digest", "response_record_digest"):
                data["payload"][key] = rebound.get(data["payload"][key], data["payload"][key])
            data["payload"].update(
                response_sha256=original_response["response_sha256"], response_bytes=original_response["response_bytes"]
            )
        data["previous_hash"] = previous
        data["digest"] = previous = digest({key: value for key, value in data.items() if key != "digest"})
        row.previous_hash, row.digest = data["previous_hash"], data["digest"]
        rebound[old_digest] = previous


@pytest.mark.parametrize("retained_epoch", [False, True])
@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_explicit_negative_observation_resumes_with_two_old_tools_charged_once(
    continuity_case, retained_epoch, monkeypatch
):
    from test_bridge_inflight_cancel import RUN

    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
    from firefly_forge.invalid_read_continuity_models import InvalidReadAdmissionInputV1, InvalidReadAdmissionInputV2
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    env = continuity_case
    add_sibling(env, retained_epoch=retained_epoch)

    async def no_rows(_):
        return []

    async def no_unknown(_):
        return False

    env.engine.tool_broker = SimpleNamespace(records=SimpleNamespace(for_run=no_rows), unresolved=no_unknown)
    with pytest.raises(ValueError, match="observation|sibling"):
        await env.continuity.candidate(RUN)
    write_policy(
        env.store.configuration,
        ContinuityLeasePolicyV1(
            version=1, enabled=True, max_window_seconds=180, allow_unavailable_source_observations=True
        ),
    )
    before = copy.deepcopy([row.data for row in env.journal.records.rows])
    candidate = await env.continuity.candidate(RUN)
    assert candidate["admission"] == "READY" and candidate["proof"]["version"] == 2
    assert len(env.posts) == 1 and len(env.journal.sandboxes.rows) == 11
    observation = candidate["proof"]["unavailable_observations"][0]
    assert observation["tool_call_id"] == "original-source-observation"
    assert observation["outcome"] == {
        "status": "observation_unavailable",
        "reason": "original_read_result_not_retained",
    }
    base = dict(
        candidate_digest=candidate["digest"],
        expected_run_digest=candidate["run_digest"],
        expected_journal_digest=candidate["journal_digest"],
        expected_policy_digest=candidate["lease_policy"]["digest"],
        valid_for_seconds=180,
        acknowledge_no_new_grant=True,
        acknowledge_original_consumption=True,
        acknowledge_renewed_same_attempt_lease=True,
    )
    with pytest.raises(ValueError, match="acknowledg|version"):
        await env.continuity.request(RUN, "a" * 32, InvalidReadAdmissionInputV1(version=1, **base))
    body = InvalidReadAdmissionInputV2(version=2, **base, acknowledge_unavailable_source_observations=True)
    ticket = await env.continuity.request(RUN, "a" * 32, body)
    assert await env.continuity.request(RUN, "a" * 32, body) == ticket
    assert len(env.posts) == 1
    execution_errors = []
    execute = env.continuity._execute

    async def diagnose(ticket):
        try:
            return await execute(ticket)
        except Exception as error:
            execution_errors.append(type(error).__name__ + ": " + str(error))
            raise

    monkeypatch.setattr(env.continuity, "_execute", diagnose)
    await env.continuity.execute(ticket["id"])
    state = await env.continuity.get(RUN, ticket["id"])
    assert state["status"] == "completed", (state.get("result"), execution_errors)
    assert len(env.posts) == 7 and len(env.journal.sandboxes.rows) == 13
    assert [row.data for row in env.journal.sandboxes.rows][:11] == env.historical_rows
    assert [row.data for row in env.journal.records.rows][: len(before)] == before
    latest = [row.data["payload"] for row in env.journal.records.rows if row.kind == "agent_checkpoint"][-1]
    resumed = BoundaryCheckpoint.decode(latest["snapshot"].encode(), latest["checkpoint_digest"])
    assert resumed.usage.requests == (13 if retained_epoch else 6)
    assert resumed.usage.tool_calls == (18 if retained_epoch else 7)
    if retained_epoch:
        assert resumed.record.usage["output_reasoning_tokens"] == 17
        assert resumed.record.usage["cache_write_1h_tokens"] == 5
        assert resumed.usage.output_tokens >= 100 and resumed.usage.cache_write_tokens == 30
    assert resumed.resume_kwargs()["usage_limits"].request_limit == 100
    assert resumed.resume_kwargs()["usage_limits"].tool_calls_limit == 300
    await env.continuity.execute(ticket["id"])
    assert len(env.posts) == 7 and len(env.journal.sandboxes.rows) == 13


def test_typed_sdk_uses_v2_route_and_never_retries_missing_acknowledgement():
    import httpx

    from firefly_forge.client import ForgeClient

    assert hasattr(ForgeClient, "request_invalid_read_continuity")
    calls = []

    def transport(request):
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"status": "queued"})

    body = dict(
        version=2,
        candidate_digest="1" * 64,
        expected_run_digest="2" * 64,
        expected_journal_digest="3" * 64,
        expected_policy_digest="4" * 64,
        valid_for_seconds=60,
        acknowledge_no_new_grant=True,
        acknowledge_original_consumption=True,
        acknowledge_renewed_same_attempt_lease=True,
        acknowledge_unavailable_source_observations=True,
    )
    with ForgeClient("https://forge.invalid", "fictional-owned-token") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(transport))
        client.request_invalid_read_continuity("a" * 32, "b" * 32, body)
        assert calls[0][1] == "/api/v1/runs/" + "a" * 32 + "/invalid-read-continuity/requests-v2/" + "b" * 32
        assert calls[0][2] == body
        body.pop("acknowledge_unavailable_source_observations")
        with pytest.raises(ValueError):
            client.request_invalid_read_continuity("a" * 32, "b" * 32, body)
        assert len(calls) == 1


async def test_pure_observation_allowlist_refuses_writable_network_ambiguous_and_invalid_siblings(continuity_case):
    from openai.types.chat import ChatCompletion

    from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2
    from firefly_forge.invalid_read_continuity import historical_checkpoint, unavailable_source_observations

    env = continuity_case
    entries = [row.data for row in env.journal.records.rows]
    checkpoint = CheckpointPayloadV2.model_validate(
        next(e["payload"] for e in entries if e["kind"] == "agent_checkpoint")
    )
    captured = historical_checkpoint(checkpoint)
    response = next(e["payload"] for e in entries if e["kind"] == "bridge_request_response")
    original = json.loads(response["response"])
    for name, arguments in (
        ("write_file", {}),
        ("execute_command", {}),
        ("internet_search", {}),
        ("external_mcp", {}),
        ("read_file", {"path": "existing.py"}),
        ("unknown", {}),
        ("list_files", {"page_size": True}),
        ("list_files", []),
    ):
        data = copy.deepcopy(original)
        sibling = {
            "id": "unavailable-source-call",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        data["choices"][0]["message"]["tool_calls"].append(sibling)
        calls = ChatCompletion.model_validate(data).choices[0].message.tool_calls
        with pytest.raises(ValueError):
            unavailable_source_observations(calls, checkpoint.invocation, captured, response["response_sha256"])
    for duplicate in (True, False):
        data = copy.deepcopy(original)
        first = data["choices"][0]["message"]["tool_calls"][0]
        sibling = copy.deepcopy(first)
        if not duplicate:
            sibling["id"] += "-second-rejected-read"
        data["choices"][0]["message"]["tool_calls"].append(sibling)
        calls = ChatCompletion.model_validate(data).choices[0].message.tool_calls
        with pytest.raises(ValueError, match="unique|exactly one"):
            unavailable_source_observations(calls, checkpoint.invocation, captured, response["response_sha256"])
    assert len(env.posts) == 1 and not env.requests.records.rows


def test_native_tool_custody_preserves_unrelated_known_rows_and_refuses_unknown_or_mutated_receipts():
    from test_invalid_read_continuity_host import record

    from firefly_forge.invalid_read_continuity import native_tool_custody
    from firefly_forge.journal import digest

    run, entries, rows = {"id": "a" * 32}, [], []
    for i, tool in enumerate(("internet_search", "knowledge_lookup"), 1):
        key = str(i) * 32
        admission = dict(
            run_id=run["id"],
            task_id="implement",
            tool=tool,
            correlation_id=key,
            arguments={"query": "fictional"},
            source={},
            configuration={},
        )
        result = {"invocation_id": key, "items": []}
        record(entries, run, "tool_intent", key, admission)
        record(
            entries, run, "tool_receipt", key, dict(correlation_id=key, tool=tool, status="succeeded", result=result)
        )
        rows.append(
            dict(
                id=str(i + 2) * 32,
                run_id=run["id"],
                task_id="implement",
                tool=tool,
                correlation_id=key,
                status="succeeded",
                data=dict(
                    admission=admission,
                    request_digest=digest(admission),
                    created_at="2026-10-27T00:00:00Z",
                    completed_at="2026-10-27T00:00:01Z",
                    lease_expires_at=1,
                    result=result,
                ),
            )
        )
    before = copy.deepcopy((entries, rows))
    assert native_tool_custody(run["id"], entries, rows) == digest({"native_tools": rows})
    for variant in ("unknown", "missing_row", "missing_receipt", "result", "request", "native_column", "duplicate"):
        history, altered = copy.deepcopy(before)
        if variant == "unknown":
            altered[0]["status"] = "unknown"
        elif variant == "missing_row":
            altered.pop()
        elif variant == "missing_receipt":
            history.pop()
        elif variant == "result":
            altered[0]["data"]["result"]["items"] = ["untrusted"]
        elif variant == "request":
            altered[0]["data"]["admission"]["arguments"]["query"] = "changed"
        elif variant == "native_column":
            altered[0]["tool"] = "execute_command"
        else:
            altered[1]["correlation_id"] = altered[0]["correlation_id"]
        with pytest.raises(ValueError):
            native_tool_custody(run["id"], history, altered)
    assert (entries, rows) == before
