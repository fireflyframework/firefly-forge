# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native private configuration and SDK controls only; no database or sockets."""

import hashlib
import json

import httpx2
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration
from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
from firefly_forge.providers import BridgeProvider, ProviderError

ENV = "FORGE_BRIDGE_COMPLETION_RECOVERY"


def test_old_absent_selection_does_not_backfill_a_frozen_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    private = ConfigurationStore(tmp_path)
    private.update("bridge", ProviderConfiguration(values={"endpoint": "http://localhost:19191", "token": "fictional"}))
    saved = private.snapshot("bridge")
    path = private.revisions / (saved["config_revision"] + ".json")
    original = path.read_bytes()
    assert ENV not in private.resolve("bridge")
    assert "completion_recovery" not in saved["execution_config"]
    assert ENV not in private.load_snapshot(saved["config_revision"])
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(original).digest()


def test_required_choice_is_persisted_and_frozen_through_native_configuration(tmp_path):
    private = ConfigurationStore(tmp_path)
    result = private.update("bridge", ProviderConfiguration(values={"completion_recovery": "required"}))
    assert result["completion_recovery"] == "required"
    frozen = private.snapshot("bridge")
    private.update("bridge", ProviderConfiguration(values={"completion_recovery": "legacy"}))
    assert private.load_snapshot(frozen["config_revision"])[ENV] == "required"
    assert frozen["execution_config"]["completion_recovery"] == "required"
    assert private.resolve("bridge")[ENV] == "legacy"


async def test_required_choice_is_persisted_and_frozen_in_a_named_model(tmp_path):
    profiles = ModelProfiles(ConfigurationStore(tmp_path))
    profiles.save(
        "recoverable",
        ModelProfileInput(
            name="Recoverable",
            provider="bridge",
            values={"completion_recovery": "required", "endpoint": "http://localhost:19191", "token": "fictional"},
        ),
    )
    assert profiles.resolve("recoverable")[1][ENV] == "required"
    record = profiles.catalog()[0]
    assert record["config"]["completion_recovery"] == "required"
    frozen = await profiles.snapshot("recoverable", "implementation")
    profiles.save(
        "recoverable",
        ModelProfileInput(name="Legacy next", provider="bridge", values={"completion_recovery": "legacy"}),
    )
    assert profiles.validate_snapshot(frozen)[ENV] == "required"
    assert frozen["execution_config"]["completion_recovery"] == "required"


async def test_absent_and_explicit_legacy_emit_the_same_sdk_body_without_capability_get():
    observed = []
    for extra in [{}, {ENV: "legacy"}]:
        model = BridgeProvider(
            {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional", **extra}
        ).model()
        calls = []

        def respond(request, calls=calls):
            calls.append(request)
            return httpx2.Response(
                200,
                json={
                    "id": "fictional",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "bridge/default",
                    "choices": [
                        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Fictional"}}
                    ],
                },
            )

        model.client._client._transport = httpx2.MockTransport(respond)
        model.client._client._mounts = {}
        try:
            await model.request(
                [ModelRequest(parts=[UserPromptPart("Same legacy context")])], None, ModelRequestParameters()
            )
            assert len(calls) == 1 and calls[0].method == "POST"
            assert "idempotency-key" not in calls[0].headers
            assert "x-bridge-completion-recovery" not in calls[0].headers
            observed.append(calls[0].content)
        finally:
            await model.client.close()
    assert observed[0] == observed[1]
    assert json.loads(observed[0])["model"] == "bridge/default"


async def test_required_selection_cannot_dispatch_against_an_unqualified_legacy_bridge():
    calls = []
    model = None
    caught = None

    def respond(request):
        calls.append(request)
        if request.method == "GET":
            return httpx2.Response(200, json={"version": "0.2.13", "protocolVersion": 2})
        return httpx2.Response(
            200,
            json={
                "id": "fictional",
                "object": "chat.completion",
                "created": 1,
                "model": "bridge/default",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Fictional"}}
                ],
            },
        )

    try:
        model = BridgeProvider(
            {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional", ENV: "required"}
        ).model()
        model.client._client._transport = httpx2.MockTransport(respond)
        model.client._client._mounts = {}
        await model.request([ModelRequest(parts=[UserPromptPart("Required recovery")])], None, ModelRequestParameters())
    except ProviderError as error:
        caught = error
    finally:
        if model is not None:
            await model.client.close()
    assert not [request for request in calls if request.method == "POST"]
    assert caught is not None


async def test_old_named_snapshot_digest_and_bytes_remain_valid_without_selection(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    profiles = ModelProfiles(ConfigurationStore(tmp_path))
    profiles.save(
        "old",
        ModelProfileInput(
            name="Old", provider="bridge", values={"endpoint": "http://localhost:19191", "token": "fictional"}
        ),
    )
    frozen = await profiles.snapshot("old", "implementation")
    original = json.dumps(frozen, sort_keys=True)
    revision = profiles.private.revisions / (frozen["config_revision"] + ".json")
    manifest = profiles.manifests / (frozen["config_revision"] + ".json")
    before = (revision.read_bytes(), manifest.read_bytes())
    assert ENV not in profiles.validate_snapshot(frozen)
    assert "completion_recovery" not in frozen["execution_config"]
    assert set(frozen["execution_config"]) == {"model", "endpoint", "token_configured", "timeout"}
    assert json.dumps(frozen, sort_keys=True) == original
    assert before == (revision.read_bytes(), manifest.read_bytes())


@pytest.mark.parametrize("mode", ["", "Required", " required", "auto", "disabled"])
def test_invalid_selection_cannot_be_saved_or_snapshotted(tmp_path, monkeypatch, mode):
    private = ConfigurationStore(tmp_path)
    with pytest.raises(ValueError, match="legacy or required"):
        private.update("bridge", ProviderConfiguration(values={"completion_recovery": mode}))
    monkeypatch.setenv(ENV, mode)
    with pytest.raises(ValueError, match="legacy or required"):
        private.snapshot("bridge")
    profiles = ModelProfiles(private)
    with pytest.raises(ValueError, match="legacy or required"):
        profiles.save("bad", ModelProfileInput(name="Bad", provider="bridge"))


def test_named_profile_cannot_trim_an_invalid_recovery_choice_into_admission(tmp_path):
    profiles = ModelProfiles(ConfigurationStore(tmp_path))
    with pytest.raises(ValueError, match="legacy or required"):
        profiles.save(
            "trimmed",
            ModelProfileInput(name="Trimmed", provider="bridge", values={"completion_recovery": " required "}),
        )
