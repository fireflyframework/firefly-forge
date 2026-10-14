# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Administrator transport configuration is separate from frozen public pool policy."""

import importlib
import ssl
from pathlib import Path

import pytest
from pydantic import ValidationError


def controller_models():
    try:
        return importlib.import_module("firefly_forge.runner_controller_config")
    except ModuleNotFoundError:
        pytest.fail("Private mTLS controller configuration is not implemented")


def test_worker_transport_rejects_http_authority_and_never_serializes_credentials_to_pool():
    module = controller_models()
    values = {
        "endpoint": "https://127.0.0.1:9917",
        "certificate": Path("/private/client.crt"),
        "private_key": Path("/private/client.key"),
        "ca": Path("/private/ca.crt"),
        "token": "test-only-controller-token-32-characters",
        "profile_id": "owned-local",
        "profile_revision": "a" * 64,
    }
    transport = module.RunnerWorkerTransport.model_validate(values)
    assert transport.active is False
    with pytest.raises(RuntimeError, match="inactive"):
        transport.require_active()
    assert "test-only-controller-token" not in repr(transport)
    for change in (
        {"endpoint": "http://127.0.0.1:9917"},
        {"endpoint": "https://user:secret@127.0.0.1:9917"},
        {"endpoint": "https://127.0.0.1:9917?token=secret"},
        {"private_key": "relative.key"},
        {"docker_context": "default"},
        {"verify_tls": False},
    ):
        with pytest.raises(ValidationError):
            module.RunnerWorkerTransport.model_validate({**values, **change})


def test_server_tls_requires_client_certificates_and_private_key_files(tmp_path, monkeypatch):
    module = controller_models()
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ca = tmp_path / "ca.pem"
    for path in (cert, key, ca):
        path.write_text("fixture-only-certificate-placeholder")
        path.chmod(0o600)
    value = module.RunnerServerTLS(certificate=cert, private_key=key, client_ca=ca)
    settings = value.uvicorn_settings()
    assert settings["ssl_cert_reqs"] == ssl.CERT_REQUIRED
    assert settings["ssl_keyfile"] == str(key)
    key.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        value.uvicorn_settings()
    key.chmod(0o600)
    alias = tmp_path / "alias.key"
    alias.symlink_to(key)
    with pytest.raises(ValueError, match="private"):
        module.RunnerServerTLS(certificate=cert, private_key=alias, client_ca=ca).uvicorn_settings()


def test_native_admin_configuration_binds_private_paths_and_environment_token(tmp_path, monkeypatch):
    import dataclasses

    import yaml
    from test_runner_controller import TOKEN, settings

    cfg = settings(tmp_path, "forge_test_runtime")
    document = {
        "forge": {
            "runner": {
                "controller": {
                    "authority": dataclasses.asdict(cfg.authority),
                    "runner_schema": cfg.runner_schema,
                    "factory_schema": cfg.factory_schema,
                    "tls": cfg.tls.model_dump(mode="json"),
                    "token": "${FORGE_RUNNER_CONTROLLER_TOKEN:}",
                    "port": 9917,
                }
            }
        }
    }
    document["forge"]["runner"]["controller"]["authority"]["profile"] = cfg.authority.profile.model_dump(mode="json")
    for key in ("docker_binary", "config_directory", "state_directory"):
        document["forge"]["runner"]["controller"]["authority"][key] = str(
            document["forge"]["runner"]["controller"]["authority"][key]
        )
    path = tmp_path / "owned-runner.yaml"
    path.write_text(yaml.safe_dump(document))
    path.chmod(0o600)
    monkeypatch.setenv("FORGE_RUNNER_CONTROLLER_TOKEN", TOKEN)
    try:
        runtime = importlib.import_module("firefly_forge.runner_controller_runtime")
    except ModuleNotFoundError:
        pytest.fail("Standalone native runner runtime configuration is not implemented")
    value = runtime.load_configuration(path)
    assert value.settings().active is False
    assert value.token.get_secret_value() == TOKEN
    assert TOKEN not in repr(value)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        runtime.load_configuration(path)
