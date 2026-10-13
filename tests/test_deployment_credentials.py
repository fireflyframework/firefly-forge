# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest


def test_scoped_credentials_are_private_frozen_and_not_in_public_readback(tmp_path: Path):
    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.deployment_credentials import CredentialInput, DeploymentCredentials
    from firefly_forge.secrets import runtime_environment

    credentials = DeploymentCredentials(ConfigurationStore(tmp_path))
    result = credentials.configure(
        "aws-staging",
        CredentialInput(values={"AWS_ACCESS_KEY_ID": "example-id", "AWS_SECRET_ACCESS_KEY": "secret-first"}),
    )
    assert "secret-first" not in str(result) and result["configured_keys"]
    revision = credentials.snapshot("aws-staging")
    credentials.configure("aws-staging", CredentialInput(values={"AWS_SECRET_ACCESS_KEY": "secret-second"}))
    assert credentials.load(revision)["AWS_SECRET_ACCESS_KEY"] == "secret-first"
    assert not any(key.startswith("AWS_") for key in runtime_environment())
    assert "secret-first" not in str(credentials.catalog())
    assert all(file.stat().st_mode & 0o077 == 0 for file in credentials.directory.rglob("*.json"))


def test_deployment_profile_cannot_inject_factory_credentials_or_paths(tmp_path: Path):
    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.deployment_credentials import CredentialInput, DeploymentCredentials

    credentials = DeploymentCredentials(ConfigurationStore(tmp_path))
    for values in ({"FORGE_API_TOKEN": "secret"}, {"PYTHONPATH": "unsafe"}, {"HOME": "/other"}):
        with pytest.raises(ValueError):
            credentials.configure("staging", CredentialInput(values=values))
    with pytest.raises(ValueError):
        credentials.configure("../escape", CredentialInput(values={"DIGITALOCEAN_TOKEN": "secret"}))
    assert credentials.load(None) == {}


def test_file_credentials_snapshot_the_bytes_instead_of_mutable_host_paths(tmp_path):
    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.deployment_credentials import CredentialInput, DeploymentCredentials

    source = tmp_path / "account.json"
    source.write_text('{"private_key":"first-private-key"}')
    source.chmod(0o600)
    credentials = DeploymentCredentials(ConfigurationStore(tmp_path / "factory"))
    credentials.configure("gcp", CredentialInput(values={"GOOGLE_APPLICATION_CREDENTIALS": str(source)}))
    revision = credentials.snapshot("gcp")
    source.write_text('{"private_key":"replaced-private-key"}')
    copied = Path(credentials.load(revision)["GOOGLE_APPLICATION_CREDENTIALS"])
    assert copied != source and "first-private-key" in copied.read_text()
    assert copied.stat().st_mode & 0o077 == 0
    assert "first-private-key" in credentials.redaction_settings().values()
