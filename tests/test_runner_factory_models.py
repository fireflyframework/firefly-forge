# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""New pool captures are explicit; older frozen policies never gain execution implicitly."""

import importlib
from pathlib import Path

import pytest

from firefly_forge.runner_controller_config import RunnerWorkerTransport
from firefly_forge.runner_pool_models import RunnerPoolProfile
from firefly_forge.sandbox_models import capture_sandbox

IMAGE = "example/worker@sha256:" + "a" * 64


def factory_models():
    try:
        return importlib.import_module("firefly_forge.runner_factory_models")
    except ModuleNotFoundError:
        pytest.fail("Explicit frozen factory pool policy is not implemented")


def test_new_capture_requires_approved_private_transport_and_keeps_old_binding_disabled():
    module = factory_models()
    profile = RunnerPoolProfile(
        profile_id="owned-factory", daemon_id="owned-engine", approved_images=(IMAGE,), transfer_image=IMAGE
    )
    selected = module.FactoryPoolSelection(profile=profile, image=IMAGE)
    transport = RunnerWorkerTransport(
        active=True,
        endpoint="https://127.0.0.1:9917",
        certificate=Path("/private/client.pem"),
        private_key=Path("/private/client.key"),
        ca=Path("/private/ca.pem"),
        token="test-only-private-controller-token-32-characters",
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
    )
    selected.require_transport(transport)
    snapshot = module.capture_factory_pool(selected)
    assert snapshot["mode"] == "pool" and snapshot["configuration"]["protocol"] == "forge-factory-pool-v1"
    assert "test-only-private-controller" not in str(snapshot) and "/private/" not in str(snapshot)
    assert module.validate_factory_pool(snapshot) == selected
    changed = {**snapshot, "effective_network": "egress"}
    with pytest.raises(ValueError, match="snapshot"):
        module.validate_factory_pool(changed)
    with pytest.raises(RuntimeError, match="inactive"):
        selected.require_transport(transport.model_copy(update={"active": False}))
    with pytest.raises(ValueError, match="profile"):
        selected.require_transport(transport.model_copy(update={"profile_revision": "b" * 64}))
    old = capture_sandbox(
        {
            "mode": "docker",
            "image": IMAGE,
            "context": "owned-context",
            "workspace_transport": "archive",
            "pool_binding": {"profile_id": profile.profile_id, "revision": profile.revision},
        }
    )
    assert old["enforced_controls"] == [] and old["effective_network"] == "unavailable"
    assert "pool" not in capture_sandbox(None)["configuration"]["mode"]


def test_factory_pool_capture_rejects_unapproved_image_and_budget_escalation():
    module = factory_models()
    profile = RunnerPoolProfile(
        profile_id="owned-factory", daemon_id="owned-engine", approved_images=(IMAGE,), transfer_image=IMAGE
    )
    for extra in (
        {"image": "example/other@sha256:" + "b" * 64},
        {"cpus": 4},
        {"timeout": 301},
        {"memory_mb": 2048},
        {"network": "egress"},
        {"docker_context": "default"},
    ):
        with pytest.raises(ValueError):
            module.FactoryPoolSelection.model_validate(
                {"profile": profile.model_dump(mode="json"), "image": IMAGE, **extra}
            )


def test_public_run_accepts_only_new_explicit_pool_policy_without_changing_legacy_default():
    from firefly_forge.models import RunInput

    module = factory_models()
    profile = RunnerPoolProfile(
        profile_id="owned", daemon_id="actual-engine", approved_images=(IMAGE,), transfer_image=IMAGE
    )
    selection = module.FactoryPoolSelection(profile=profile, image=IMAGE)
    request = RunInput(repository_id="a" * 32, brief="Isolated command", sandbox=selection.model_dump(mode="json"))
    assert isinstance(request.sandbox, module.FactoryPoolSelection)
    assert RunInput(repository_id="a" * 32, brief="Default").sandbox is None


def test_private_factory_worker_configuration_uses_native_config_and_rejects_public_files(tmp_path):
    import yaml

    module = factory_models()
    profile = RunnerPoolProfile(
        profile_id="owned", daemon_id="actual-engine", approved_images=(IMAGE,), transfer_image=IMAGE
    )
    path = tmp_path / "worker.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "forge": {
                    "runner": {
                        "worker": {
                            "git_binary": "/usr/bin/git",
                            "transport": {
                                "active": False,
                                "endpoint": "https://127.0.0.1:9917",
                                "certificate": str(tmp_path / "client.pem"),
                                "private_key": str(tmp_path / "client.key"),
                                "ca": str(tmp_path / "ca.pem"),
                                "token": "private-fixture-worker-token-32-chars",
                                "profile_id": profile.profile_id,
                                "profile_revision": profile.revision,
                            },
                        }
                    }
                }
            }
        )
    )
    path.chmod(0o600)
    cfg = module.load_factory_worker_configuration(path)
    assert cfg.git_binary == Path("/usr/bin/git") and not cfg.transport.active
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        module.load_factory_worker_configuration(path)
