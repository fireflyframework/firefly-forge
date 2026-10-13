# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json
import os

import pytest

from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration
from firefly_forge.models import RunInput
from firefly_forge.skills import inject_skills, skill_catalog, validate_skills, workflow_catalog


def test_private_configuration_preserves_secrets_and_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_BRIDGE_TOKEN", "environment-secret")
    settings = ConfigurationStore(tmp_path)
    settings.update(
        "bridge", ProviderConfiguration(values={"endpoint": "http://127.0.0.1:8766", "token": "private-token"})
    )
    snapshot = settings.snapshot("bridge")
    settings.update("bridge", ProviderConfiguration(values={"token": "", "model": "bridge/other"}))
    assert settings.resolve("bridge")["FORGE_BRIDGE_TOKEN"] == "private-token"
    settings.update("bridge", ProviderConfiguration(values={"token": "new-private-token"}))
    assert settings.load_snapshot(snapshot["config_revision"])["FORGE_BRIDGE_TOKEN"] == "private-token"
    assert "private-token" not in json.dumps(snapshot)
    assert "environment-secret" not in json.dumps(settings.public("bridge"))
    assert settings.public("bridge")["token_configured"] is True
    assert os.stat(settings.path).st_mode & 0o777 == 0o600
    assert all(os.stat(p).st_mode & 0o777 == 0o600 for p in settings.revisions.glob("*.json"))


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@api.example/v1",
        "https://api.example/?token=secret",
        "file:///etc/passwd",
        "http://api.example/v1",
        "https://api.example/#secret",
    ],
)
def test_unsafe_configuration_endpoints_rejected(tmp_path, url):
    with pytest.raises(ValueError):
        ConfigurationStore(tmp_path).update("bridge", ProviderConfiguration(values={"endpoint": url}))


def test_secret_store_rejects_symlink_and_explicit_clear(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGE_BRIDGE_TOKEN", raising=False)
    settings = ConfigurationStore(tmp_path)
    settings.update("bridge", ProviderConfiguration(values={"token": "private-token"}))
    settings.update("bridge", ProviderConfiguration(clear_secrets=["token"]))
    assert not settings.public("bridge")["token_configured"]
    settings.path.unlink()
    destination = tmp_path / "unrelated.json"
    destination.write_text("{}")
    settings.path.symlink_to(destination)
    with pytest.raises(ValueError, match="regular"):
        settings.public("bridge")


def test_skills_are_validated_and_actually_injected():
    request = RunInput(repository_id="repo", brief="Inventory")
    assert request.skills == ["implementation", "verification", "security"]
    with pytest.raises(ValueError, match="Unknown skill"):
        validate_skills(["imaginary"])
    brief = inject_skills("Inventory", ["security"])
    assert "security" in brief and "credentials" in brief
    assert "Skills" not in inject_skills("Inventory", [])
    assert skill_catalog()["skills"]
    assert any(s["id"] == "repairing" for s in workflow_catalog()["stages"])


def test_unsafe_environment_url_is_never_public_or_queued(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_BRIDGE_URL", "https://user:private-uri-password@api.example/?token=private-query")
    settings = ConfigurationStore(tmp_path)
    public = settings.public("bridge")
    assert "private" not in json.dumps(public)
    assert public["endpoint_invalid"] is True
    with pytest.raises(ValueError, match="Endpoint"):
        settings.snapshot("bridge")
