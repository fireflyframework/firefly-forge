# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A consumed invalid read can be inspected without repeating its native effect."""

import copy
import hashlib
import importlib
import importlib.util
import json
from typing import Any

import httpx2
import pytest
from fireflyframework_agentic.agents import FireflyAgent
from fireflyframework_agentic.config import FireflyAgenticConfig
from fireflyframework_agentic.exceptions import ToolError
from fireflyframework_agentic.tools import firefly_tool
from openai import AsyncOpenAI
from pydantic_ai import PromptedOutput
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary
from firefly_forge.agentic_checkpoint_invocation import invocation_cut
from firefly_forge.agentic_runtime import RuntimeReport
from firefly_forge.bridge_completions import _cut
from firefly_forge.journal import ZERO_HASH, Journal, digest
from firefly_forge.run_evidence import EvidenceQuery


def api():
    name = "firefly_forge.invalid_read_continuity"
    assert importlib.util.find_spec(name) is not None, "Consumed invalid-read continuity proof is missing"
    return importlib.import_module(name)


def changed_paths(left, right, prefix=""):
    if isinstance(left, dict) and isinstance(right, dict):
        return [
            path
            for key in left.keys() | right.keys()
            for path in changed_paths(left.get(key), right.get(key), prefix + "/" + key)
        ]
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [
            path
            for index, pair in enumerate(zip(left, right, strict=True))
            for path in changed_paths(*pair, prefix + "/" + str(index))
        ]
    return [] if left == right else [prefix]


def record(entries, run, kind, request, payload):
    value = dict(
        run_id=run["id"],
        task_id="implement",
        kind=kind,
        idempotency_key=request,
        sequence=len(entries) + 1,
        previous_hash=entries[-1]["digest"] if entries else ZERO_HASH,
        created_at="2026-10-26T00:00:00+00:00",
        payload=payload,
    )
    value["digest"] = digest(value)
    entries.append(value)
    return value


def rechain(entries):
    previous = ZERO_HASH
    for index, entry in enumerate(entries, 1):
        entry.update(sequence=index, previous_hash=previous)
        entry["digest"] = previous = digest({k: v for k, v in entry.items() if k != "digest"})


@pytest.fixture
async def consumed_read(request):
    run = dict(
        id="a" * 32,
        project_id="b" * 32,
        repository_id="c" * 32,
        agent="bridge",
        status="failed",
        active_task="implement",
        attempt=2,
        implementation_pending=True,
        implementation_rounds=1,
        repair_attempts=2,
        config_revision="d" * 32,
        source_tree="e" * 64,
        execution_actor={"user_id": "f" * 32},
        execution_config={"endpoint": "http://localhost:1", "model": "bridge/default"},
        model_selection={"profile_id": "codex-0163-darwin-arm64-source-v19"},
        frozen_read_budget={"spent": 11, "limit": 22},
    )
    task = dict(id="implement", status="needs_reconciliation", attempt=2, idempotency_key="9" * 32)
    body = json.dumps(
        {
            "id": "chatcmpl-fictional-consumed",
            "object": "chat.completion",
            "created": 1,
            "model": "bridge/default",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "invalid-read-once",
                                "type": "function",
                                "function": {
                                    "name": "read_run_evidence",
                                    "arguments": '{"query":{"tool_name":"read_file"}}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        },
        separators=(",", ":"),
    ).encode()
    boundary = AgenticBoundary(lambda: {"files_written": ["existing.py"]})
    captures, wires, callback = [], [], []

    async def old_reader(query: dict[str, Any]) -> dict[str, Any]:
        EvidenceQuery.model_validate(query)
        callback.append(query)
        return {"items": []}

    from firefly_forge.profiles import build_instruction
    from firefly_forge.prompts import prompt_text

    tool = firefly_tool(
        "read_run_evidence", description=prompt_text("tools/read_run_evidence", version="1.0.0"), auto_register=False
    )(old_reader)

    def transport(request):
        wires.append(request.content)
        captures.append(boundary.capture(request.content))
        return httpx2.Response(200, content=body)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as http:
        client = AsyncOpenAI(
            base_url="http://localhost:1/v1", api_key="fictional-local-only", max_retries=0, http_client=http
        )
        model = OpenAIChatModel(
            "bridge/default",
            provider=OpenAIProvider(openai_client=client),
            profile=OpenAIModelProfile(openai_unsupported_model_settings=["max_tokens"]),
        )
        agent = FireflyAgent(
            name="continuity-fixture",
            model=model,
            output_type=PromptedOutput(RuntimeReport),
            instructions=build_instruction("", "pyfly", "none") + prompt_text("implementation/tool-rules"),
            tools=[tool],
            auto_register=False,
            config=FireflyAgenticConfig(default_temperature=None, rate_limit_max_retries=0),
            capabilities=[boundary],
        )
        with boundary.invocation(), pytest.raises(ToolError):
            from pydantic_ai.usage import RunUsage

            await agent.run(
                "Fictional retained brief",
                usage_limits=UsageLimits(request_limit=100, tool_calls_limit=300),
                usage=RunUsage(requests=7, tool_calls=11, input_tokens=101, output_tokens=43)
                if getattr(request, "param", False)
                else None,
            )
    assert not callback and len(captures) == 1
    checkpoint = captures[0]
    request = "fbr1_" + "1" * 32
    entries = []
    intent = record(
        entries,
        run,
        "bridge_request_intent",
        request,
        {
            "version": 1,
            "run_id": run["id"],
            "task_id": "implement",
            "project_id": run["project_id"],
            "repository_id": run["repository_id"],
            "task_key": task["idempotency_key"],
            "task_attempt": 2,
            "profile_id": run["model_selection"]["profile_id"],
            "config_field": "config_revision",
            "config_revision": run["config_revision"],
            "scope_digest": _cut(run, "config_revision"),
            "descriptor": {
                "request_id": request,
                "principal_id": "fictional-actor",
                "model": "bridge/default",
                "endpoint": "http://localhost:1/v1",
                "wire_sha256": checkpoint.wire_sha256,
                "wire_bytes": len(wires[0]),
                "image_revision": None,
            },
        },
    )
    run["bridge_completion_hold"] = {
        "version": 1,
        "reason": "graph_checkpoint_unavailable",
        "request_id": request,
        "intent_digest": intent["digest"],
    }
    saved = record(
        entries,
        run,
        "agent_checkpoint",
        request,
        {
            "version": 2,
            "request_id": request,
            "intent_digest": intent["digest"],
            "checkpoint_digest": checkpoint.digest,
            "snapshot": checkpoint.payload.decode(),
            "invocation": {
                "invocation_id": "2" * 32,
                "ordinal": 1,
                "predecessor": None,
                "brief": "Fictional retained brief",
                "backend": "pyfly",
                "frontend": "none",
                "round": 2,
                "deadline_ms": 1,
                "grants": ["read_run_evidence"],
                "contract": "3" * 64,
                "run_cut": invocation_cut(run),
                "workspace": {"path": "/fictional/owned", "head": "4" * 40, "metadata": "5" * 64, "source": "6" * 64},
            },
        },
    )
    response = record(
        entries,
        run,
        "bridge_request_response",
        request,
        {
            "version": 1,
            "request_id": request,
            "intent_digest": intent["digest"],
            "response": body.decode(),
            "response_sha256": hashlib.sha256(body).hexdigest(),
            "response_bytes": len(body),
        },
    )
    record(
        entries,
        run,
        "agent_consume",
        request,
        {
            "version": 1,
            "request_id": request,
            "intent_digest": intent["digest"],
            "checkpoint_record_digest": saved["digest"],
            "response_record_digest": response["digest"],
            "response_sha256": response["payload"]["response_sha256"],
            "response_bytes": len(body),
        },
    )
    assert Journal.verify_chain(entries)
    return run, task, entries


async def test_consumed_invalid_read_has_inspection_proof_without_resetting_original(consumed_read):
    run, task, entries = consumed_read
    old = copy.deepcopy(consumed_read)
    prepared = api().prepare_invalid_read(run, task, entries, accepted_runtime_contract="3" * 64)
    assert prepared.proof["consume_record_digest"] == entries[-1]["digest"]
    assert prepared.proof["tool_call_id"] == "invalid-read-once"
    assert prepared.proof["result"] == {"status": "invalid_query"}
    assert prepared.proof["admission"] == "HOLD"
    assert "continuation_lease_policy_unavailable" in prepared.proof["holds"]
    assert "original_deadline_expired" in prepared.proof["holds"]
    assert (run, task, entries) == old


@pytest.mark.parametrize(
    "change", ["missing_consume", "duplicate", "bad_hold", "wrong_task", "contract", "changed_response"]
)
async def test_incomplete_or_wrong_original_custody_is_refused(consumed_read, change):
    run, task, entries = copy.deepcopy(consumed_read)
    if change == "missing_consume":
        entries.pop()
    elif change == "duplicate":
        entries.append(copy.deepcopy(entries[0]))
    elif change == "bad_hold":
        run["bridge_completion_hold"]["intent_digest"] = "0" * 64
    elif change == "wrong_task":
        task["attempt"] = 1
    elif change == "contract":
        entries[1]["payload"]["invocation"]["contract"] = "0" * 64
    else:
        entries[2]["payload"]["response"] = entries[2]["payload"]["response"].replace("read_file", "write_file")
    rechain(entries)
    with pytest.raises(ValueError):
        api().prepare_invalid_read(run, task, entries, accepted_runtime_contract="3" * 64)


async def test_offline_reconstruction_and_strict_migration_keep_history_and_charge_once(consumed_read, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("No socket or external callback is allowed")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    run, task, entries = consumed_read
    before = copy.deepcopy(consumed_read)
    prepared = api().prepare_invalid_read(run, task, entries, accepted_runtime_contract="3" * 64)
    result = await api().reconstruct_and_migrate(prepared)
    repeated = await api().reconstruct_and_migrate(prepared)
    assert repeated.derived.digest == result.derived.digest, changed_paths(
        result.derived.record.model_dump(), repeated.derived.record.model_dump()
    )
    assert repeated.migrated.digest == result.migrated.digest
    assert result.derived.usage.requests == result.derived.usage.tool_calls == 1
    assert result.derived.usage.input_tokens == 11 and result.derived.usage.output_tokens == 3
    assert result.migrated.record.history == result.derived.record.history
    assert result.migrated.history[-1].timestamp == result.derived.history[-1].timestamp
    assert result.migrated.record.usage == result.derived.record.usage
    assert result.migrated.record.limits == result.derived.record.limits
    assert result.migrated.state == {"files_written": ["existing.py"]}
    assert result.migrated.history[-1].parts[-1].content == {"status": "invalid_query"}
    assert result.receipt["external_requests"] == result.receipt["native_tool_callbacks"] == 0
    assert result.receipt["known_consumption_reused"] == entries[-1]["digest"]
    assert result.receipt["admission"] == "HOLD"
    assert (run, task, entries) == before


def change_completion(entries, update):
    response, consume = entries[2], entries[3]
    value = json.loads(response["payload"]["response"])
    update(value)
    raw = json.dumps(value, separators=(",", ":"))
    response["payload"].update(
        response=raw, response_sha256=hashlib.sha256(raw.encode()).hexdigest(), response_bytes=len(raw.encode())
    )
    rechain(entries[:3])
    consume["payload"].update(
        response_record_digest=response["digest"],
        response_sha256=response["payload"]["response_sha256"],
        response_bytes=response["payload"]["response_bytes"],
    )
    rechain(entries)


@pytest.mark.parametrize(
    "change",
    [
        "valid_query",
        "sibling",
        "write",
        "unknown",
        "duplicate_json",
        "non_object",
        "final_text",
        "extra_text",
        "audio",
        "missing_usage",
    ],
)
async def test_only_one_statically_invalid_object_read_is_eligible(consumed_read, change):
    run, task, entries = copy.deepcopy(consumed_read)

    def update(completion):
        choice = completion["choices"][0]
        message = choice["message"]
        call = message["tool_calls"][0]
        if change == "valid_query":
            call["function"]["arguments"] = '{"query":{"limit":1}}'
        elif change == "sibling":
            message["tool_calls"].append({**copy.deepcopy(call), "id": "another-call"})
        elif change == "write":
            call["function"]["name"] = "write_file"
        elif change == "unknown":
            call["function"]["name"] = "unknown_tool"
        elif change == "duplicate_json":
            call["function"]["arguments"] = '{"query":{},"query":{"tool_name":"read_file"}}'
        elif change == "non_object":
            call["function"]["arguments"] = '{"query":[]}'
        elif change == "final_text":
            choice.update(finish_reason="stop")
            message.update(tool_calls=None, content="Final")
        elif change == "extra_text":
            message["content"] = "Unproven accompanying text"
        elif change == "missing_usage":
            completion.pop("usage")
        else:
            message["audio"] = {"id": "audio-unproven", "data": "AAAA", "expires_at": 1, "transcript": "Unproven"}

    change_completion(entries, update)
    assert Journal.verify_chain(entries)
    with pytest.raises(ValueError):
        api().prepare_invalid_read(run, task, entries, accepted_runtime_contract="3" * 64)


async def test_public_projection_is_detached_and_runtime_pin_cannot_drift(consumed_read, monkeypatch):
    run, task, entries = consumed_read
    prepared = api().prepare_invalid_read(run, task, entries, accepted_runtime_contract="3" * 64)
    projection = prepared.proof
    projection["result"]["status"] = "accepted"
    assert prepared.proof["result"] == {"status": "invalid_query"}
    monkeypatch.setattr(api(), "LEGACY_QUERY_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="historical input validator"):
        api().prepare_invalid_read(run, task, entries, accepted_runtime_contract="3" * 64)


async def test_even_unexpired_checkpoint_has_no_implicit_continuation_lease(consumed_read):
    import time

    run, task, entries = copy.deepcopy(consumed_read)
    entries[1]["payload"]["invocation"]["deadline_ms"] = int(time.time() * 1000) + 60000
    rechain(entries[:3])
    entries[3]["payload"]["checkpoint_record_digest"] = entries[1]["digest"]
    entries[3]["payload"]["response_record_digest"] = entries[2]["digest"]
    rechain(entries)
    proof = api().prepare_invalid_read(run, task, entries, accepted_runtime_contract="3" * 64).proof
    assert proof["admission"] == "HOLD"
    assert proof["holds"] == ["continuation_lease_policy_unavailable"]


def test_admission_acknowledgements_require_boolean_true():
    from pydantic import ValidationError

    from firefly_forge.invalid_read_continuity_models import InvalidReadAdmissionInputV1

    with pytest.raises(ValidationError):
        InvalidReadAdmissionInputV1(
            version=1,
            candidate_digest="a" * 64,
            expected_run_digest="b" * 64,
            expected_journal_digest="c" * 64,
            expected_policy_digest="d" * 64,
            valid_for_seconds=30,
            acknowledge_renewed_same_attempt_lease=True,
            acknowledge_no_new_grant=1,
            acknowledge_original_consumption=True,
        )


@pytest.mark.parametrize("change", ["unknown_reference", "empty_result"])
async def test_proof_requires_exact_consumed_reference_and_negative_result(consumed_read, change):
    from pydantic import ValidationError

    from firefly_forge.invalid_read_continuity_models import InvalidReadProofV1

    proof = api().prepare_invalid_read(*consumed_read, accepted_runtime_contract="3" * 64).proof
    if change == "unknown_reference":
        proof["completions"][0]["unreviewed"] = "extra"
    else:
        proof["result"] = {}
    with pytest.raises(ValidationError):
        InvalidReadProofV1.model_validate(proof)


@pytest.mark.parametrize(
    "change", ["instructions", "settings", "grants", "description", "return_schema", "other_schema"]
)
async def test_contract_migration_refuses_every_unapproved_adapter_change(consumed_read, change):
    from firefly_forge.invalid_read_contract import actual_worker_contract, compatible_contract

    prepared = api().prepare_invalid_read(*consumed_read, accepted_runtime_contract="3" * 64)
    original = prepared.checkpoint.record.contract
    target = await actual_worker_contract(prepared)
    compatible_contract(original, target)
    if change == "instructions":
        target["parameters"]["instruction_parts"][0]["content"] += "Changed instructions"
    elif change == "settings":
        target["settings"] = {"temperature": 0.2}
    elif change == "grants":
        target["parameters"]["function_tools"].clear()
    elif change == "description":
        target["parameters"]["function_tools"][0]["description"] += "Unknown description"
    elif change == "return_schema":
        target["parameters"]["function_tools"][0]["return_schema"] = {"type": "string"}
    else:
        target["parameters"]["function_tools"][0]["parameters_json_schema"]["additionalProperties"] = True
    with pytest.raises(ValueError):
        compatible_contract(original, target)


@pytest.mark.parametrize("consumed_read", [True], indirect=True)
async def test_public_sdk_prior_usage_survives_reconstruction_without_new_limits(consumed_read):
    prepared = api().prepare_invalid_read(*consumed_read, accepted_runtime_contract="3" * 64)
    assert prepared.checkpoint.usage.requests == 7 and prepared.checkpoint.usage.tool_calls == 11
    result = await api().reconstruct_and_migrate(prepared)
    assert result.migrated.usage.requests == 8 and result.migrated.usage.tool_calls == 12
    assert result.migrated.usage.input_tokens == 112 and result.migrated.usage.output_tokens == 46
    assert result.migrated.record.limits == prepared.checkpoint.record.limits
    assert result.migrated.record.usage == result.derived.record.usage
    assert result.migrated.record.history == result.derived.record.history
    limits = result.migrated.resume_kwargs()["usage_limits"]
    assert limits.request_limit == 100 and limits.tool_calls_limit == 300


@pytest.mark.parametrize(
    "contract",
    [
        "f3cc412b382858ee8b4e9c9534a1dba20fcc582936d07e96a620c9f7a0c247f6",
    ],
)
async def test_registered_historical_dependency_epoch_preserves_original_custody(consumed_read, contract):
    run, task, entries = copy.deepcopy(consumed_read)
    entries[1]["payload"]["invocation"]["contract"] = contract
    rechain(entries)
    entries[3]["payload"].update(
        checkpoint_record_digest=entries[1]["digest"], response_record_digest=entries[2]["digest"]
    )
    rechain(entries)
    before = copy.deepcopy((run, task, entries))
    prepared = api().prepare_invalid_read(run, task, entries)
    assert prepared.proof["runtime_contract"] == contract
    assert prepared.proof["attempt"] == 2
    assert prepared.proof["admission"] == "HOLD"
    assert prepared.proof["consume_record_digest"] == entries[-1]["digest"]
    assert (run, task, entries) == before


@pytest.mark.parametrize("contract", ["0" * 64, "3" * 64])
async def test_unregistered_historical_dependency_epoch_is_refused(consumed_read, contract):
    run, task, entries = copy.deepcopy(consumed_read)
    entries[1]["payload"]["invocation"]["contract"] = contract
    rechain(entries)
    entries[3]["payload"].update(
        checkpoint_record_digest=entries[1]["digest"], response_record_digest=entries[2]["digest"]
    )
    rechain(entries)
    with pytest.raises(ValueError, match="runtime"):
        api().prepare_invalid_read(run, task, entries)


@pytest.mark.parametrize("tool_kind_present", [False, True])
@pytest.mark.parametrize(("url_ids", "tool_names"), [(None, None), ([], []), (None, []), ([], None)])
async def test_retained_sdk_epoch_derives_new_checkpoint_without_widening_restore(
    consumed_read, url_ids, tool_names, tool_kind_present
):
    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload

    run, task, entries = copy.deepcopy(consumed_read)
    saved = entries[1]["payload"]
    original = BoundaryCheckpoint.decode(saved["snapshot"].encode(), saved["checkpoint_digest"])
    record = json.loads(original.canonical_payload)
    record["compatibility"]["pydantic-ai-slim"] = "2.58.0"
    record["contract"]["parameters"].update(
        _telemetry_mcp_url_ids=url_ids, _telemetry_mcp_url_toolset_ids=[], _telemetry_mcp_url_tool_names=tool_names
    )
    for definition in record["contract"]["parameters"]["function_tools"]:
        if not tool_kind_present:
            assert definition.pop("tool_kind") is None
    record["usage"].update(
        input_tokens=200, output_tokens=100, cache_write_tokens=30, output_reasoning_tokens=17, cache_write_1h_tokens=5
    )
    historical = _checkpoint_payload(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    saved.update(snapshot=historical.decode(), checkpoint_digest=hashlib.sha256(historical).hexdigest())
    saved["invocation"]["contract"] = "0b48cf3f13632a7f182edb904ae97060f585f8907a737b695422eb8f4582786d"
    rechain(entries)
    entries[3]["payload"].update(
        checkpoint_record_digest=entries[1]["digest"], response_record_digest=entries[2]["digest"]
    )
    rechain(entries)
    before = copy.deepcopy((run, task, entries))
    with pytest.raises(ValueError, match="runtime"):
        BoundaryCheckpoint.decode(historical, saved["checkpoint_digest"])
    prepared = api().prepare_invalid_read(run, task, entries)
    assert prepared.proof["checkpoint_digest"] == saved["checkpoint_digest"]
    assert prepared.checkpoint.digest != saved["checkpoint_digest"]
    derived_record = json.loads(prepared.checkpoint.canonical_payload)
    expected = copy.deepcopy(record)
    expected["compatibility"] = original.record.compatibility
    for key in ("_telemetry_mcp_url_ids", "_telemetry_mcp_url_toolset_ids", "_telemetry_mcp_url_tool_names"):
        expected["contract"]["parameters"].pop(key)
    for definition in expected["contract"]["parameters"]["function_tools"]:
        definition["tool_kind"] = None
    assert derived_record == expected
    assert prepared.checkpoint.record.usage["output_reasoning_tokens"] == 17
    assert prepared.checkpoint.record.usage["cache_write_1h_tokens"] == 5
    result = await api().reconstruct_and_migrate(prepared)
    assert result.receipt["original_checkpoint"] == saved["checkpoint_digest"]
    assert result.receipt["compatibility_migration"]["source_checkpoint"] == saved["checkpoint_digest"]
    assert result.receipt["compatibility_migration"]["derived_checkpoint"] == prepared.checkpoint.digest
    provenance = {
        "_telemetry_mcp_url_ids": url_ids,
        "_telemetry_mcp_url_toolset_ids": [],
        "_telemetry_mcp_url_tool_names": tool_names,
    }
    witness = result.receipt["compatibility_migration"]
    source_tools = [
        {"name": tool["name"], "source_present": tool_kind_present, "source_value": None, "target_value": None}
        for tool in record["contract"]["parameters"]["function_tools"]
    ]
    assert witness["version"] == 3
    assert witness["source_function_tool_kinds"] == source_tools
    assert witness["source_function_tool_kinds_sha256"] == digest({"tools": source_tools})
    assert witness["contract_projection"]["function_tool_kinds"] == source_tools
    assert witness["source_telemetry"] == provenance
    assert witness["source_telemetry_sha256"] == digest(provenance)
    assert witness["contract_projection"]["remove_exact_provenance"] == provenance
    assert result.derived.record.usage["output_reasoning_tokens"] == 17
    assert result.derived.record.usage["cache_write_1h_tokens"] == 5
    assert result.derived.usage.requests == original.usage.requests + 1
    assert result.derived.usage.tool_calls == original.usage.tool_calls + 1
    assert result.receipt["external_requests"] == result.receipt["native_tool_callbacks"] == 0
    assert (run, task, entries) == before
    with pytest.raises(ValueError, match="runtime"):
        BoundaryCheckpoint.decode(historical, saved["checkpoint_digest"])


@pytest.mark.parametrize(
    "change",
    [
        "unknown_source",
        "unknown_target",
        "history_loss",
        "wire",
        "mixed_runtime",
        "telemetry",
        "telemetry_type",
        "unknown_parameter",
        "unknown_tool_field",
        "collision",
        "tool_kind",
        "tool_kind_bool",
        "tool_kind_number",
        "tool_kind_list",
        "tool_kind_object",
        "instruction",
        "schema",
    ],
)
async def test_historical_sdk_migration_refuses_unregistered_or_changed_frontiers(consumed_read, monkeypatch, change):
    from firefly_forge import agentic_checkpoint_boundary as codec

    run, task, entries = copy.deepcopy(consumed_read)
    saved = entries[1]["payload"]
    original = codec.BoundaryCheckpoint.decode(saved["snapshot"].encode(), saved["checkpoint_digest"])
    record = json.loads(original.canonical_payload)
    record["compatibility"]["pydantic-ai-slim"] = "2.59.0" if change == "unknown_source" else "2.58.0"
    record["usage"].update(output_reasoning_tokens=0, cache_write_1h_tokens=0)
    record["contract"]["parameters"].update(
        _telemetry_mcp_url_ids=None, _telemetry_mcp_url_toolset_ids=[], _telemetry_mcp_url_tool_names=None
    )
    for definition in record["contract"]["parameters"]["function_tools"]:
        assert definition.pop("tool_kind") is None
    parameters = record["contract"]["parameters"]
    if change == "telemetry":
        parameters["_telemetry_mcp_url_ids"] = ["unregistered-provenance"]
    elif change == "telemetry_type":
        parameters["_telemetry_mcp_url_toolset_ids"] = {}
    elif change == "unknown_parameter":
        parameters["unknown"] = None
    elif change == "unknown_tool_field":
        parameters["function_tools"][0]["unknown"] = None
    elif change == "collision":
        parameters["function_tools"].append(copy.deepcopy(parameters["function_tools"][0]))
    elif change == "tool_kind":
        parameters["function_tools"][0]["tool_kind"] = "tool-search"
    elif change.startswith("tool_kind_"):
        parameters["function_tools"][0]["tool_kind"] = {
            "tool_kind_bool": False,
            "tool_kind_number": 0,
            "tool_kind_list": [],
            "tool_kind_object": {},
        }[change]
    elif change == "instruction":
        parameters["instruction_parts"][0]["content"] += " Changed instruction"
    elif change == "schema":
        parameters["function_tools"][0]["parameters_json_schema"]["properties"]["query"]["description"] = (
            "Unapproved schema"
        )
    raw = codec._checkpoint_payload(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    saved.update(snapshot=raw.decode(), checkpoint_digest=hashlib.sha256(raw).hexdigest())
    saved["invocation"]["contract"] = api().RETAINED_SDK_RUNTIME_CONTRACT
    rechain(entries)
    entries[3]["payload"].update(
        checkpoint_record_digest=entries[1]["digest"], response_record_digest=entries[2]["digest"]
    )
    rechain(entries)
    if change == "unknown_target":
        monkeypatch.setattr(
            codec, "_compatibility", lambda: {"fireflyframework-agentic": "26.10.1", "pydantic-ai-slim": "2.54.0"}
        )
    elif change == "history_loss":
        serialize = codec.serialize_model_messages
        monkeypatch.setattr(codec, "serialize_model_messages", lambda messages: serialize(messages) + [{}])
    elif change == "mixed_runtime":
        grouped = api()._unsettled(entries, run["id"])
        extra = copy.deepcopy(grouped["agent_checkpoint"][0])
        extra["payload"]["invocation"]["contract"] = api().LEGACY_RUNTIME_CONTRACT
        grouped["agent_checkpoint"].append(extra)
        monkeypatch.setattr(api(), "_unsettled", lambda *args: grouped)
    elif change == "wire":
        record["wire_sha256"] = "0" * 64
        raw = codec._checkpoint_payload(
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
        saved.update(snapshot=raw.decode(), checkpoint_digest=hashlib.sha256(raw).hexdigest())
        rechain(entries)
        entries[3]["payload"].update(
            checkpoint_record_digest=entries[1]["digest"], response_record_digest=entries[2]["digest"]
        )
        rechain(entries)
    with pytest.raises(ValueError):
        prepared = api().prepare_invalid_read(run, task, entries)
        await api().reconstruct_and_migrate(prepared)


def test_public_usage_preserves_and_increments_known_reasoning_counter_without_double_counting():
    from pydantic_ai.usage import RunUsage, UsageLimits

    from firefly_forge.agentic_checkpoint_boundary import LIMITS, USAGE, _budgets

    raw = USAGE.dump_python(
        RunUsage(requests=7, tool_calls=11, input_tokens=200, cache_write_tokens=30, output_tokens=100), mode="json"
    )
    raw.update(output_reasoning_tokens=17, cache_write_1h_tokens=5)
    usage = USAGE.validate_json(json.dumps(raw))
    increment = USAGE.dump_python(
        RunUsage(requests=1, tool_calls=1, input_tokens=50, cache_write_tokens=4, output_tokens=30), mode="json"
    )
    increment.update(output_reasoning_tokens=3, cache_write_1h_tokens=2)
    usage.incr(USAGE.validate_json(json.dumps(increment)))
    actual = USAGE.dump_python(usage, mode="json")
    assert actual == {
        **raw,
        "requests": 8,
        "tool_calls": 12,
        "input_tokens": 250,
        "cache_write_tokens": 34,
        "output_tokens": 130,
        "output_reasoning_tokens": 20,
        "cache_write_1h_tokens": 7,
    }
    limits = LIMITS.dump_python(UsageLimits(request_limit=100, tool_calls_limit=300), mode="json")
    _budgets(actual, limits)


@pytest.mark.parametrize("counter", ["output_reasoning_tokens", "cache_write_1h_tokens"])
@pytest.mark.parametrize("change", [True, -1, 2**53, 1.5, "17", "unknown", "missing"])
def test_known_optional_reasoning_counter_keeps_strict_counter_and_field_validation(change, counter):
    from pydantic_ai.usage import RunUsage, UsageLimits

    from firefly_forge.agentic_checkpoint_boundary import LIMITS, USAGE, _budgets

    usage = USAGE.dump_python(RunUsage(), mode="json")
    usage[counter] = change
    if change == "unknown":
        usage[counter] = 17
        usage["arbitrary_counter"] = 1
    elif change == "missing":
        usage.pop("requests")
        usage[counter] = 17
    limits = LIMITS.dump_python(UsageLimits(request_limit=100, tool_calls_limit=300), mode="json")
    with pytest.raises(ValueError):
        _budgets(usage, limits)


@pytest.mark.parametrize("change", [None, "unknown_version", "digest", "canonical_schema", "custody", "counter"])
async def test_historical_credential_text_is_lossless_scanned_and_never_resumable(consumed_read, change):
    from types import SimpleNamespace

    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload
    from firefly_forge.secrets import ephemeral_credentials, redact_environment
    from firefly_forge.store import Store

    _, _, entries = consumed_read
    saved = entries[1]["payload"]
    current = BoundaryCheckpoint.decode(saved["snapshot"].encode(), saved["checkpoint_digest"])
    record = json.loads(current.canonical_payload)
    record["compatibility"]["pydantic-ai-slim"] = "2.59.0" if change == "unknown_version" else "2.58.0"
    record["usage"].update(output_reasoning_tokens=17, cache_write_1h_tokens=5)
    marker = "fictional-retained-credential-for-current-scanner-only"
    record["contract"]["parameters"]["_telemetry_mcp_url_ids"] = [marker]
    if change == "canonical_schema":
        record["unknown_record_field"] = 1
    elif change == "counter":
        record["usage"]["cache_write_1h_tokens"] = True
    raw = _checkpoint_payload(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    checksum = "0" * 64 if change == "digest" else hashlib.sha256(raw).hexdigest()
    if change is not None:
        with pytest.raises(ValueError):
            BoundaryCheckpoint.credential_text(raw, checksum, historical_custody=change != "custody")
        return
    text = BoundaryCheckpoint.credential_text(raw, checksum, historical_custody=True)
    assert json.loads(text) == record and marker in text
    source = SimpleNamespace(
        credential_registry=lambda: {"FIXTURE_TOKEN": marker},
        configuration=SimpleNamespace(resolve=lambda _: {}),
    )
    with pytest.raises(ValueError, match="configured credentials"):
        Store.reject_credentials(source, text)
    with ephemeral_credentials({"FIXTURE_TOKEN": marker}):
        assert marker not in redact_environment(text)
    with pytest.raises(ValueError, match="runtime"):
        BoundaryCheckpoint.decode(raw, checksum)
    with pytest.raises(ValueError, match="custody"):
        BoundaryCheckpoint.credential_text(raw, checksum)


async def test_migration_history_schema_failure_hides_retained_input(consumed_read):
    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload

    _, _, entries = consumed_read
    payload = entries[1]["payload"]
    checkpoint = BoundaryCheckpoint.decode(payload["snapshot"].encode(), payload["checkpoint_digest"])
    record = json.loads(checkpoint.canonical_payload)
    record["compatibility"]["pydantic-ai-slim"] = "2.58.0"
    record["usage"].update(output_reasoning_tokens=0, cache_write_1h_tokens=0)
    marker = "fictional-retained-input-must-not-appear-in-errors"
    record["history"] = record["request_history"] = [{"kind": marker}]
    raw = _checkpoint_payload(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    with pytest.raises(ValueError, match="^Checkpoint native history schema differs$") as raised:
        BoundaryCheckpoint.migrate_public_compatibility(raw, hashlib.sha256(raw).hexdigest())
    assert marker not in str(raised.value)
