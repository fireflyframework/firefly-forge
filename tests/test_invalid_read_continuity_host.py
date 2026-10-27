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
