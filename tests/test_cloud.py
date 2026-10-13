# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from firefly_forge.cloud import CloudRecipeInput, CloudRecipes
from firefly_forge.configuration import ConfigurationStore

IMAGE = "ghcr.io/example/inventory@sha256:" + "a" * 64
CONFIG = {
    "aws": {
        "region": "us-east-1",
        "vpc_id": "vpc-1234",
        "subnet_id": "subnet-1234",
        "image_id": "ami-1234",
        "state_bucket": "forge-state",
    },
    "azure": {
        "region": "eastus",
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "state_resource_group": "tfstate",
        "state_storage_account": "forgestate",
        "state_container": "tfstate",
    },
    "gcp": {
        "project": "forge-project",
        "region": "us-central1",
        "zone": "us-central1-a",
        "state_bucket": "forge-state",
    },
    "digitalocean": {
        "region": "nyc3",
        "state_bucket": "forge-state",
        "state_region": "us-east-1",
        "state_endpoint": "https://nyc3.digitaloceanspaces.com",
        "ssh_key_id": "123456",
    },
    "vps": {"host": "203.0.113.5"},
    "onprem": {"host": "192.0.2.8"},
}


def request(provider="aws", **values):
    return CloudRecipeInput(
        provider=provider,
        repository_id="project",
        name="inventory",
        image=IMAGE,
        domain="inventory.example.com",
        ssh_public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIqI4910CfGV/VLbLTy6XXLKZwm/HZQSG/N0iAG0D29c",
        admin_cidr="203.0.113.8/32",
        credential_profile="cloud-fixture",
        config=CONFIG[provider],
        **values,
    )


def manager(tmp_path):
    store = Mock(path=tmp_path / "factory.db", configuration=ConfigurationStore(tmp_path))
    store.repository = AsyncMock(return_value={"id": "project"})
    deployments = Mock(configure_target=AsyncMock())
    return CloudRecipes(store, deployments), deployments


@pytest.mark.parametrize("provider", list(CONFIG))
async def test_recipe_has_immutable_images_real_health_and_explicit_effects(tmp_path, provider):
    recipes, _ = manager(tmp_path)
    result = await recipes.render(request(provider, previous_image=IMAGE.replace("a" * 64, "b" * 64)))
    assert result["validation"]["infrastructure_applied"] is False
    assert result["target"]["recipe_files"] == result["files"]
    assert result["target"]["verification"][0]["argv"][:2] == ["curl", "--fail"]
    assert result["target"]["rollback_commands"] and result["target"]["rollback_verification"]
    playbook = json.loads(result["files"]["deploy.playbook.json"])
    assert IMAGE in json.dumps(playbook)
    assert "StrictHostKeyChecking=yes" in json.dumps(result["target"])
    if provider in {"aws", "azure", "gcp", "digitalocean"}:
        terraform = json.loads(result["files"]["main.tf.json"])
        assert terraform["terraform"]["backend"]
        assert "required_providers" in terraform["terraform"]
        assert any("apply" in command["argv"] for command in result["target"]["commands"])
    else:
        assert "main.tf.json" not in result["files"]


@pytest.mark.parametrize(
    "changes",
    [
        {"image": "ghcr.io/example/inventory:latest"},
        {"domain": "host; echo stolen"},
        {"admin_cidr": "0.0.0.0/0"},
        {"health_path": "/health?token=private"},
        {"config": {"region": "us-east-1", "token": "private-token"}},
    ],
)
def test_rejects_mutable_images_injection_and_embedded_credentials(changes):
    values = request().model_dump()
    values.update(changes)
    with pytest.raises(ValidationError):
        CloudRecipeInput.model_validate(values)


async def test_render_does_not_save_or_configure_and_export_is_private(tmp_path):
    recipes, deployments = manager(tmp_path)
    await recipes.render(request())
    deployments.configure_target.assert_not_called()
    saved = await recipes.save(request())
    archive = await recipes.export(saved["id"])
    assert archive.is_file() and archive.stat().st_mode & 0o077 == 0
    import zipfile

    with zipfile.ZipFile(archive) as source:
        assert "main.tf.json" in source.namelist()
        assert not any(name.startswith("/") or ".." in Path(name).parts for name in source.namelist())
    await recipes.configure_target(saved["id"], "production")
    deployments.configure_target.assert_awaited_once()
    with pytest.raises(ValueError):
        await recipes.export("../escape")


async def test_export_refuses_a_hardlink_without_overwriting_an_external_file(tmp_path):
    import os

    recipes, _ = manager(tmp_path)
    saved = await recipes.save(request())
    external = tmp_path / "unrelated.txt"
    external.write_text("Preserve unrelated data")
    os.link(external, recipes.directory / (saved["id"] + ".zip"))
    with pytest.raises(ValueError, match="private"):
        await recipes.export(saved["id"])
    assert external.read_text() == "Preserve unrelated data"


async def test_enabling_a_recipe_requires_explicit_credentials_and_project_resource_names_are_distinct(tmp_path):
    recipes, deployments = manager(tmp_path)
    first = request()
    second = first.model_copy(update={"repository_id": "another-project"})
    a = json.loads((await recipes.render(first))["files"]["main.tf.json"])
    b = json.loads((await recipes.render(second))["files"]["main.tf.json"])
    assert (
        a["resource"]["aws_key_pair"]["operator"]["key_name"] != b["resource"]["aws_key_pair"]["operator"]["key_name"]
    )
    first.credential_profile = None
    saved = await recipes.save(first)
    with pytest.raises(ValueError, match="credential profile"):
        await recipes.configure_target(saved["id"], "production")
    deployments.configure_target.assert_not_called()
