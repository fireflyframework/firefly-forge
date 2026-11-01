# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled structured model outputs exercise the real Agentic drafting adapter."""

import json
from types import SimpleNamespace

import pytest
from communication_model_fixture import communication_phase, reviewed_model
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import FunctionModel

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
    calls = []
    model = reviewed_model(
        {
            "title": {"text": "Execution update", "fact_ids": ["state"]},
            "paragraphs": [
                {"text": "Implementation is underway. The recorded run remains in development.", "fact_ids": ["state"]}
            ],
        },
        calls=calls,
    )
    writer = CommunicationWriter(store, model=model)
    result = await writer.write({"status": "developing"}, "Native evidence", lambda value: value)
    assert result["communication"]["mode"] == "ai"
    assert "Implementation is underway" in result["body"]
    assert result["body"].startswith("### Execution update\n")
    assert "Native evidence" not in result["body"]
    assert result["communication"]["profile"]["profile_id"] == "updates"
    assert result["communication"]["facts"] == {"state": {"status": "developing"}}
    assert [phase for phase, _, _ in calls] == ["draft", "review"]
    assert all(info.function_tools == [] for _, _, info in calls)


async def test_bridge_uses_prompted_structured_output_without_new_provider_stack(tmp_path):
    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path, "bridge")
    output = {
        "title": {"text": "Execution update", "fact_ids": ["state"]},
        "paragraphs": [
            {"text": "The request is queued. Its execution results are not available yet.", "fact_ids": ["state"]}
        ],
    }
    writer = CommunicationWriter(store, model=reviewed_model(output))
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
async def test_unsupported_claims_are_corrected_once_while_invalid_framing_is_held(tmp_path, text, facts):
    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path)
    model = reviewed_model(
        {
            "title": {"text": "Execution update", "fact_ids": ["state"]},
            "paragraphs": [{"text": text, "fact_ids": facts}],
        },
        verdict="unsupported",
        issues=["unsupported_claim"],
        correction={
            "title": {"text": "Execution update", "fact_ids": ["state"]},
            "paragraphs": [
                {
                    "text": "The run remains in development. Saved checks include a failure.",
                    "fact_ids": ["state", "verification"],
                }
            ],
        },
    )
    writer = CommunicationWriter(store, model=model)
    result = await writer.write(
        {"status": "developing", "final_verification": [{"exit_code": 1}]}, "Native evidence", lambda value: value
    )
    structural = "@everyone" in text or facts == ["unknown_fact"]
    if structural:
        assert result["body"] == "" and result["communication"]["mode"] == "held"
        assert result["communication"]["reason"] == "invalid_output"
    else:
        assert result["communication"]["mode"] == "ai"
        assert result["communication"]["repair_attempted"] is True
        assert result["communication"]["review"]["verdict"] == "unsupported"
        assert "Saved checks include a failure" in result["body"]
        assert text not in result["body"]


async def test_failed_native_checks_cannot_be_treated_as_current_verification_after_recovery(tmp_path):
    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path)
    run = {
        "status": "developing",
        "provider_recovery": {"request_id": "a" * 32, "attempt": 5, "status": "consumed"},
        "final_verification": [{"exit_code": 0, "attempt": 4}],
    }
    model = reviewed_model(
        {
            "title": {"text": "Execution update", "fact_ids": ["state"]},
            "paragraphs": [{"text": "All checks passed.", "fact_ids": ["verification"]}],
        },
        verdict="unsupported",
        issues=["misleading_checks"],
        correction={
            "title": {"text": "Execution update", "fact_ids": ["state"]},
            "paragraphs": [
                {
                    "text": "The run remains in development. Saved results do not establish current verification.",
                    "fact_ids": ["state", "verification"],
                }
            ],
        },
    )
    result = await CommunicationWriter(store, model=model).write(run, "Native historical evidence", lambda value: value)
    assert result["communication"]["mode"] == "ai"
    assert result["communication"]["repair_attempted"] is True
    assert "do not establish current verification" in result["body"]
    assert result["communication"]["facts"]["verification"]["provenance"] == "retained"
    assert result["communication"]["reason"] == "generated"
    assert result["communication"]["review"]["issues"] == ["misleading_checks"]


async def test_fact_packet_excludes_untrusted_issue_prose_logs_and_raw_recovery_data(tmp_path):
    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path)
    writer = CommunicationWriter(
        store,
        model=reviewed_model(
            {
                "title": {"text": "Execution update", "fact_ids": ["state"]},
                "paragraphs": [{"text": "The run is paused.", "fact_ids": ["state"]}],
            }
        ),
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
    assert result["body"] == ""
    assert result["communication"]["mode"] == "held"
    assert result["communication"]["reason"] == reason
    assert "private body" not in json.dumps(result)


@pytest.mark.parametrize(
    "checks,expected", [([{"exit_code": 0, "attempt": 1}], "retained"), ([{"exit_code": 0}], "unknown")]
)
async def test_ordinary_attempt_cannot_announce_retained_or_unattributed_checks_as_current(tmp_path, checks, expected):
    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path)
    model = reviewed_model(
        {
            "title": {"text": "Execution update", "fact_ids": ["state"]},
            "paragraphs": [{"text": "All checks passed.", "fact_ids": ["verification"]}],
        },
        verdict="unsupported",
        issues=["misleading_checks"],
        correction={
            "title": {"text": "Execution update", "fact_ids": ["state"]},
            "paragraphs": [
                {
                    "text": "The run remains in development. Saved results do not establish current verification.",
                    "fact_ids": ["state", "verification"],
                }
            ],
        },
    )
    result = await CommunicationWriter(store, model=model).write(
        {"status": "developing", "attempt": 2, "final_verification": checks}, "Native evidence", lambda value: value
    )
    assert result["communication"]["mode"] == "ai"
    assert result["communication"]["repair_attempted"] is True
    assert "All checks passed" not in result["body"]
    assert result["communication"]["facts"]["verification"]["provenance"] == expected
    assert result["communication"]["reason"] == "generated"
    assert result["communication"]["review"]["issues"] == ["misleading_checks"]


@pytest.mark.parametrize("family", ["openai-responses", "openai-chat", "anthropic", "google-gla"])
async def test_communication_request_disables_native_sdk_retries_without_changing_other_callers(family):
    from firefly_forge.providers import APIProvider

    provider = APIProvider(
        {"FORGE_AGENTIC_MODEL": family + ":public-fixture-model", "FORGE_AGENTIC_API_KEY": "public-fixture-key"}
    )
    default_model = provider.model()
    default_client = default_model._provider.client
    try:
        if family == "google-gla":
            assert default_client._api_client._http_options.retry_options is None
        else:
            assert default_client.max_retries == 2
    finally:
        if family == "google-gla":
            await default_client.aio.aclose()
        else:
            await default_client.close()
    model = provider.model(single_request=True)
    client = model._provider.client
    try:
        if family == "google-gla":
            assert client._api_client._http_options.retry_options.attempts == 1
        else:
            assert client.max_retries == 0
    finally:
        if family == "google-gla":
            await client.aio.aclose()
        else:
            await client.close()


@pytest.mark.parametrize(
    "run,allowed",
    [
        ({"status": "failed"}, ["state"]),
        ({"status": "developing", "active_task": "implement"}, ["stage", "state"]),
    ],
)
async def test_bridge_request_explains_exact_current_fact_ids(tmp_path, run, allowed):
    from pydantic_ai.messages import ModelResponse, TextPart

    from firefly_forge.communication import CommunicationWriter

    store = await configured_store(tmp_path, "bridge")
    prompts = []

    def model(messages, info):
        prompt = "\n".join(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(getattr(part, "content", None), str)
        )
        prompts.append(prompt)
        if communication_phase(info) == "review":
            return ModelResponse(
                parts=[
                    TextPart(
                        json.dumps(
                            {
                                "reader_checks": {
                                    "answers_question": True,
                                    "relevant_detail": True,
                                    "no_redundancy": True,
                                    "plain_language": True,
                                    "useful_excerpts": True,
                                },
                                "verdict": "supported",
                                "issues": [],
                                "fact_ids": ["state"],
                                "rationale": "The current state supports the exact cited message.",
                            }
                        )
                    )
                ]
            )
        # The first real Codex reply used a dotted path because its prompt did not
        # define the IDs. Exercise the exact adapter contract without a live call.
        fact_id = "state" if "Allowed fact_ids: " + json.dumps(allowed) in prompt else "state.status"
        text = "The recorded status is failed." if run["status"] == "failed" else "The run is in development."
        return ModelResponse(
            parts=[
                TextPart(
                    json.dumps(
                        {
                            "title": {"text": "Execution update", "fact_ids": ["state"]},
                            "paragraphs": [{"text": text, "fact_ids": [fact_id]}],
                        }
                    )
                )
            ]
        )

    result = await CommunicationWriter(store, model=FunctionModel(model)).write(
        run, "Exact native evidence", lambda value: value
    )
    assert result["communication"]["mode"] == "ai"
    assert len(prompts) == 2
    assert "Allowed fact_ids: " + json.dumps(allowed) in prompts[0]
    assert "top-level" in prompts[0] and "state.status" in prompts[0]
    assert result["body"].startswith("### Execution update\n")
    assert "Exact native evidence" not in result["body"]


def test_actual_dotted_fact_reply_remains_rejected_without_weakening_validation():
    from firefly_forge.communication import CommunicationCard, _validate_draft

    actual = CommunicationCard.model_validate(
        {
            "title": {"text": "Execution update", "fact_ids": ["state"]},
            "paragraphs": [{"text": "The recorded status is failed.", "fact_ids": ["state.status"]}],
        }
    )
    with pytest.raises(ValueError, match="unknown fact reference"):
        _validate_draft(actual, {"state": {"status": "failed"}})
