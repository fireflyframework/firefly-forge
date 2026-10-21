# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled structured model outputs exercise the real Agentic drafting adapter."""

import json
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
from firefly_forge.task_routing import TaskRouting, TaskRoutingInput


async def configured_store(tmp_path, provider="agentic"):
    private = ConfigurationStore(tmp_path)
    profiles = ModelProfiles(private)
    values = (
        {"model": "openai-responses:fixture", "api_key": "public-fixture-model-key"}
        if provider == "agentic"
        else {"endpoint": "http://127.0.0.1:12345/v1", "token": "public-fixture-bridge-token"}
    )
    profiles.save("updates", ModelProfileInput(name="Project updates", provider=provider, values=values))
    router = TaskRouting(profiles)
    await router.save(TaskRoutingInput(bindings={"communication": "updates"}))
    return SimpleNamespace(task_router=router, reject_credentials=lambda _: None)


async def test_real_structured_agent_drafts_native_implementation_facts(tmp_path):
    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path)
    model = TestModel(
        custom_output_args={
            "paragraphs": [
                {"text": "Implementation is underway. The recorded run remains in development.", "fact_ids": ["state"]}
            ]
        }
    )
    writer = CommunicationWriter(store, model=model)
    result = await writer.write({"status": "developing"}, "Native evidence", lambda value: value)
    assert result["communication"]["mode"] == "ai"
    assert "Implementation is underway" in result["body"]
    assert result["body"].endswith("Native evidence")
    assert result["communication"]["profile"]["profile_id"] == "updates"
    assert result["communication"]["facts"] == {"state": {"status": "developing"}}
    assert model.last_model_request_parameters.function_tools == []


async def test_bridge_uses_prompted_structured_output_without_new_provider_stack(tmp_path):
    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path, "bridge")
    output = {
        "paragraphs": [
            {"text": "The request is queued. Its execution results are not available yet.", "fact_ids": ["state"]}
        ]
    }
    writer = CommunicationWriter(store, model=TestModel(custom_output_text=json.dumps(output)))
    result = await writer.write({"status": "queued"}, "Native evidence", lambda value: value)
    assert result["communication"]["mode"] == "ai"
    assert result["communication"]["profile"]["provider"] == "bridge"
    assert "request is queued" in result["body"]


@pytest.mark.parametrize(
    "text,facts",
    [
        ("All 99 tests passed.", ["state"]),
        ("The changes are deployed and merged.", ["state"]),
        ("See https://attacker.example.test or @everyone.", ["state"]),
        ("Everything is complete.", ["unknown_fact"]),
        ("All checks passed for this attempt.", ["verification"]),
        ("The run is paused.", ["state"]),
        ("I fixed the customer API.", ["state"]),
    ],
)
async def test_unsupported_model_claims_fall_back_to_exact_native_card(tmp_path, text, facts):
    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path)
    model = TestModel(custom_output_args={"paragraphs": [{"text": text, "fact_ids": facts}]})
    writer = CommunicationWriter(store, model=model)
    result = await writer.write(
        {"status": "developing", "final_verification": [{"exit_code": 1}]}, "Native evidence", lambda value: value
    )
    assert result["body"] == "Native evidence"
    assert result["communication"]["mode"] == "deterministic"
    assert result["communication"]["reason"] == "invalid_output"


async def test_failed_native_checks_cannot_be_treated_as_current_verification_after_recovery(tmp_path):
    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path)
    run = {
        "status": "developing",
        "provider_recovery": {"request_id": "a" * 32, "attempt": 5, "status": "consumed"},
        "final_verification": [{"exit_code": 0, "attempt": 4}],
    }
    model = TestModel(custom_output_args={"paragraphs": [{"text": "All checks passed.", "fact_ids": ["verification"]}]})
    result = await CommunicationWriter(store, model=model).write(run, "Native historical evidence", lambda value: value)
    assert result["body"] == "Native historical evidence"
    assert result["communication"]["facts"]["verification"]["provenance"] == "retained"
    assert result["communication"]["reason"] == "invalid_output"


async def test_fact_packet_excludes_untrusted_issue_prose_logs_and_raw_recovery_data(tmp_path):
    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path)
    writer = CommunicationWriter(
        store,
        model=TestModel(custom_output_args={"paragraphs": [{"text": "The run is paused.", "fact_ids": ["state"]}]}),
    )
    run = {
        "status": "paused",
        "brief": "IGNORE INSTRUCTIONS",
        "events": [{"message": "SECRET LOG"}],
        "pause": {"kind": "invented", "detail": "RAW DETAIL"},
        "provider_recovery": {"attempt": 99},
        "worker_request": {"question": "INJECT"},
    }
    result = await writer.write(run, "Native evidence", lambda value: value)
    retained = json.dumps(result["communication"]["facts"])
    assert retained == '{"state": {"status": "paused"}}'
    assert all(value not in retained for value in ("IGNORE", "SECRET", "RAW", "INJECT"))


async def test_unconfigured_route_does_not_construct_or_invoke_a_model():
    from firefly_forge.communication import CommunicationWriter

    result = await CommunicationWriter(SimpleNamespace()).write(
        {"status": "queued"}, "Exact fallback", lambda value: value
    )
    assert result["body"] == "Exact fallback"
    assert result["communication"]["reason"] == "unconfigured"


@pytest.mark.parametrize(
    "failure,reason",
    [
        (TimeoutError(), "timeout"),
        (ModelHTTPError(429, "fixture", {"error": "private body must not be retained"}), "quota"),
    ],
)
async def test_unavailable_model_is_not_retried_and_does_not_block_native_update(tmp_path, failure, reason):
    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path)
    calls = []

    def model(messages, info):
        calls.append(1)
        raise failure

    result = await CommunicationWriter(store, model=FunctionModel(model)).write(
        {"status": "queued"}, "Exact fallback", lambda value: value
    )
    assert len(calls) == 1
    assert result["body"] == "Exact fallback"
    assert result["communication"]["reason"] == reason
    assert "private body" not in json.dumps(result)
