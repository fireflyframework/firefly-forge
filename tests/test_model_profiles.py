# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import importlib
import json
import os

import pytest

from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration


def module():
    return importlib.import_module("firefly_forge.model_profiles")


def profiles(tmp_path):
    return module().ModelProfiles(ConfigurationStore(tmp_path))


async def test_named_models_credentials_are_distinct_private_and_frozen(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_AGENTIC_API_KEY", "ambient-not-selected")
    service = profiles(tmp_path)
    for profile_id, model, key in (
        ("planner", "openai-responses:model-a", "planner-secret"),
        ("builder", "anthropic:model-b", "builder-secret"),
    ):
        service.save(
            profile_id,
            module().ModelProfileInput(name=profile_id, provider="agentic", values={"model": model, "api_key": key}),
        )
    frozen = await service.snapshot("planner", "product")
    assert service.private.load_snapshot(frozen["config_revision"])["FORGE_AGENTIC_API_KEY"] == "planner-secret"
    service.save(
        "planner",
        module().ModelProfileInput(
            name="planner", provider="agentic", values={"model": "openai-responses:model-c", "api_key": "changed"}
        ),
    )
    assert service.validate_snapshot(frozen)["FORGE_AGENTIC_MODEL"] == "openai-responses:model-a"
    assert service.resolve("builder")[1]["FORGE_AGENTIC_API_KEY"] == "builder-secret"
    assert "secret" not in json.dumps(service.catalog()) and "ambient-not-selected" not in json.dumps(frozen)
    assert os.stat(service.path).st_mode & 0o777 == 0o600
    assert os.environ["FORGE_AGENTIC_API_KEY"] == "ambient-not-selected"


def test_new_profile_does_not_borrow_secret_without_explicit_inheritance(tmp_path):
    private = ConfigurationStore(tmp_path)
    private.update(
        "bridge", ProviderConfiguration(values={"token": "global-secret", "endpoint": "https://bridge.example"})
    )
    service = module().ModelProfiles(private)
    service.save("plain", module().ModelProfileInput(name="plain", provider="bridge"))
    assert not service.resolve("plain")[1]["FORGE_BRIDGE_TOKEN"]
    service.save("inherited", module().ModelProfileInput(name="inherited", provider="bridge", inherit_credentials=True))
    assert service.resolve("inherited")[1]["FORGE_BRIDGE_TOKEN"] == "global-secret"
    service.save("inherited", module().ModelProfileInput(name="retained", provider="bridge", values={"token": ""}))
    assert service.resolve("inherited")[1]["FORGE_BRIDGE_TOKEN"] == "global-secret"
    service.save("inherited", module().ModelProfileInput(name="cleared", provider="bridge", clear_secrets=["token"]))
    assert not service.resolve("inherited")[1]["FORGE_BRIDGE_TOKEN"]


async def test_native_dialogue_and_vision_never_silently_route_to_unsupported_provider(tmp_path):
    service = profiles(tmp_path)
    service.save("cli", module().ModelProfileInput(name="cli", provider="codex"))
    with pytest.raises(ValueError, match="task"):
        await service.snapshot("cli", "product")
    service.save("api", module().ModelProfileInput(name="api", provider="agentic", values={"api_key": "explicit"}))
    with pytest.raises(ValueError, match="verified"):
        await service.snapshot("api", "multimodal_qa")


async def test_verified_image_revision_matches_exact_selected_model_and_frozen_profile(tmp_path, monkeypatch):
    service = profiles(tmp_path)
    service.save(
        "vision",
        module().ModelProfileInput(
            name="vision",
            provider="bridge",
            values={"model": "bridge/visual", "endpoint": "https://bridge.example", "token": "explicit"},
        ),
    )
    from firefly_forge.providers import BridgeProvider

    async def capability(self, **kwargs):
        return {"status": "verified", "model": "bridge/visual", "config_revision": "a" * 64}

    monkeypatch.setattr(BridgeProvider, "image_input_capability", capability)
    frozen = await service.snapshot("vision", "multimodal_qa")
    assert frozen["image_input"]["config_revision"] == "a" * 64
    service.save(
        "vision", module().ModelProfileInput(name="vision", provider="bridge", values={"model": "bridge/other"})
    )
    with pytest.raises(ValueError, match="matching"):
        await service.snapshot("vision", "multimodal_qa")
    assert service.validate_snapshot(frozen)["FORGE_BRIDGE_MODEL"] == "bridge/visual"


@pytest.mark.parametrize(
    "values",
    [
        {"endpoint": "https://user:secret@api.example"},
        {"unknown": "value"},
        {"timeout": "90000"},
        {"model": "bad\nmodel"},
    ],
)
def test_profile_validation_never_mutates_provider_settings(tmp_path, values):
    service = profiles(tmp_path)
    before = service.private.resolve("agentic")
    with pytest.raises(ValueError):
        service.save("invalid", module().ModelProfileInput(name="invalid", provider="agentic", values=values))
    assert service.private.resolve("agentic") == before


async def test_private_snapshot_tampering_is_denied(tmp_path):
    service = profiles(tmp_path)
    service.save("api", module().ModelProfileInput(name="api", provider="agentic", values={"api_key": "explicit"}))
    frozen = await service.snapshot("api", "implementation")
    path = service.private.revisions / (frozen["config_revision"] + ".json")
    payload = service.private._read(path)
    payload["FORGE_AGENTIC_MODEL"] = "altered"
    service.private._write(path, payload)
    with pytest.raises(ValueError, match="digest"):
        service.validate_snapshot(frozen)


async def test_public_snapshot_cannot_upgrade_task_or_forge_image_capability(tmp_path):
    service = profiles(tmp_path)
    service.save("api", module().ModelProfileInput(name="api", provider="agentic", values={"api_key": "explicit"}))
    frozen = await service.snapshot("api", "implementation")
    for changed in (
        {"task": "multimodal_qa"},
        {"profile_id": "another"},
        {"image_input": {"status": "verified", "config_revision": "a" * 64}},
    ):
        with pytest.raises(ValueError, match="manifest"):
            service.validate_snapshot(frozen | changed)


def test_profile_public_fields_reject_known_credentials_and_symlink_store(tmp_path):
    service = profiles(tmp_path)
    with pytest.raises(ValueError, match="credentials"):
        service.save(
            "unsafe",
            module().ModelProfileInput(
                name="private-long-token", provider="agentic", values={"api_key": "private-long-token"}
            ),
        )
    destination = tmp_path / "unrelated.json"
    destination.write_text("{}")
    service.path.symlink_to(destination)
    with pytest.raises(ValueError, match="regular"):
        service.catalog()


async def test_profile_connection_probe_uses_selected_credentials_without_generation_or_vision_claim(
    tmp_path, monkeypatch
):
    service = profiles(tmp_path)
    service.save(
        "specific",
        module().ModelProfileInput(
            name="specific", provider="agentic", values={"model": "openai:model-specific", "api_key": "specific-secret"}
        ),
    )
    from firefly_forge import providers as provider_module

    async def probe(provider, config):
        assert provider == "agentic" and config.resolve(provider)["FORGE_AGENTIC_API_KEY"] == "specific-secret"
        return {"readiness": {"state": "connected", "detail": "Test fixture model listing"}}

    monkeypatch.setattr(provider_module, "test_provider_connection", probe)
    result = await service.test("specific")
    assert result["readiness"]["state"] == "connected" and result["generation_verified"] is False
    assert result["image_input"]["status"] == "unverified" and "specific-secret" not in json.dumps(result)
