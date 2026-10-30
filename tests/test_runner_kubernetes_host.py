# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Kubernetes API contracts are controlled here; cluster acceptance is separate."""

import copy
import importlib
from uuid import uuid4

import pytest
from test_runner_pool_dispatch import policy as docker_policy

from firefly_forge.runner_pool_models import RunnerPoolProfile


def kubernetes_policy():
    module = importlib.import_module("firefly_forge.runner_kubernetes_models")
    return module.KubernetesPoolPolicy(
        namespace="project-workers",
        namespace_uid=uuid4(),
        service_account="project-command",
        storage_class="private-csi",
        runtime_class="isolated-workers",
        node_names=("qualified-worker-1",),
        network_policy_name="command-deny-all",
        network_policy_uid=uuid4(),
        uid=10001,
        gid=10001,
        workspace_storage_mb=1024,
    )


def test_current_docker_profile_keeps_exact_legacy_serialization():
    before = docker_policy().model_dump(mode="json")
    saved = copy.deepcopy(before)
    assert RunnerPoolProfile.model_validate(before).model_dump(mode="json") == saved
    assert "backend" not in saved and "kubernetes" not in saved


def test_kubernetes_profile_has_explicit_protocol_and_frozen_isolation_policy():
    selected = docker_policy().model_dump(mode="json")
    selected.update(
        protocol="forge-kubernetes-pool-v1",
        backend="kubernetes",
        kubernetes=kubernetes_policy().model_dump(mode="json"),
    )
    profile = RunnerPoolProfile.model_validate(selected)
    assert profile.model_dump(mode="json") == selected
    assert profile.revision != docker_policy().revision
    with pytest.raises(ValueError, match="frozen"):
        profile.kubernetes.namespace = "other-project"
    for missing in ("protocol", "kubernetes"):
        value = copy.deepcopy(selected)
        value.pop(missing)
        with pytest.raises(ValueError):
            RunnerPoolProfile.model_validate(value)


def test_kubernetes_profile_cannot_use_docker_control_transport(tmp_path):
    from firefly_forge.runner_pool_dispatch import RunnerDockerAuthority

    selected = docker_policy().model_dump(mode="json")
    selected.update(
        protocol="forge-kubernetes-pool-v1",
        backend="kubernetes",
        kubernetes=kubernetes_policy().model_dump(mode="json"),
    )
    binary = tmp_path / "owned-docker-placeholder"
    binary.write_text("fixture; never executed")
    config, state = tmp_path / "config", tmp_path / "state"
    config.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="Docker"):
        RunnerDockerAuthority(
            profile=RunnerPoolProfile.model_validate(selected),
            docker_binary=binary,
            context="owned-test",
            config_directory=config,
            state_directory=state,
            namespace="forge-runner-test",
            uid=10001,
            gid=10001,
        )


def test_kubernetes_capacity_is_explicit_and_not_clamped_to_legacy_docker_limits():
    selected = docker_policy().model_dump(mode="json")
    selected.update(
        protocol="forge-kubernetes-pool-v1",
        backend="kubernetes",
        kubernetes=kubernetes_policy().model_dump(mode="json"),
        slots=256,
        project_slots=128,
        cpu_budget=2048.0,
        project_cpu_budget=1024.0,
        memory_budget_mb=2097152,
        project_memory_budget_mb=1048576,
        max_cpus=64.0,
        max_memory_mb=131072,
        max_pids=8192,
        work_duration={"protocol": "forge-work-duration-v2", "timeout_seconds": None},
        run_liveness="forge-run-owned-liveness-v1",
    )
    current = RunnerPoolProfile.model_validate(selected)
    assert current.slots == 256 and current.max_cpus == 64 and current.max_memory_mb == 131072
    current.validate_budget(
        image=current.approved_images[0],
        cpus=32,
        memory_mb=65536,
        pids_limit=4096,
        timeout=None,
        work_duration=current.work_duration,
        source_bytes=1024,
        max_output_bytes=1024,
    )
    legacy = {
        key: value
        for key, value in selected.items()
        if key not in {"backend", "kubernetes", "work_duration", "run_liveness"}
    }
    legacy["protocol"] = "forge-local-pool-v1"
    with pytest.raises(ValueError):
        RunnerPoolProfile.model_validate(legacy)


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace", "../other"),
        ("service_account", "default/admin"),
        ("node_names", ("*",)),
        ("uid", 0),
        ("workspace_storage_mb", True),
    ],
)
def test_kubernetes_policy_rejects_ambiguous_scope_or_privileged_identity(field, value):
    module = importlib.import_module("firefly_forge.runner_kubernetes_models")
    policy = kubernetes_policy().model_dump(mode="python")
    policy[field] = value
    with pytest.raises(ValueError):
        module.KubernetesPoolPolicy.model_validate(policy)


def test_kubernetes_invocation_and_factory_selection_use_explicit_new_resource_protocol(tmp_path):
    from test_runner_pool_dispatch import request

    from firefly_forge import runner_pool_models as models
    from firefly_forge.runner_factory_models import FactoryPoolSelection
    from firefly_forge.runner_pool_bundle import pack_project_bundle

    selected = docker_policy().model_dump(mode="json")
    selected.update(
        protocol="forge-kubernetes-pool-v1",
        backend="kubernetes",
        kubernetes=kubernetes_policy().model_dump(mode="json"),
        slots=256,
        project_slots=128,
        cpu_budget=2048.0,
        project_cpu_budget=1024.0,
        memory_budget_mb=2097152,
        project_memory_budget_mb=1048576,
        max_cpus=64.0,
        max_memory_mb=131072,
        max_pids=8192,
        work_duration={"protocol": "forge-work-duration-v2", "timeout_seconds": 300},
    )
    profile = RunnerPoolProfile.model_validate(selected)
    source, git = tmp_path / "source", tmp_path / "git"
    source.mkdir()
    git.mkdir()
    (source / "main.py").write_text("print(1)\n")
    (git / "HEAD").write_text("ref: refs/heads/forge-snapshot\n")
    archive, bundle = pack_project_bundle(source, git, 1048576)
    raw = request(profile).model_dump(mode="json")
    raw.update(
        protocol="forge-kubernetes-project-invocation-v1",
        bundle=bundle.model_dump(mode="json"),
        archive_digest=bundle.archive_digest,
        source_digest=bundle.source_digest,
        source_bytes=len(archive),
        cpus=32.0,
        memory_mb=65536,
        pids_limit=4096,
        work_duration={"protocol": "forge-work-duration-v2", "timeout_seconds": 300},
    )
    invocation = models.parse_invocation(raw)
    assert invocation.model_dump(mode="json") == raw
    profile.validate_invocation(invocation)
    with pytest.raises(ValueError):
        models.RunnerProjectInvocation.model_validate({**raw, "protocol": "forge-project-invocation-v1"})
    selection = FactoryPoolSelection.model_validate(
        {
            "protocol": "forge-factory-kubernetes-pool-v1",
            "profile": profile.model_dump(mode="json"),
            "image": invocation.image,
            "cpus": 32.0,
            "memory_mb": 65536,
            "pids_limit": 4096,
            "work_duration": {"protocol": "forge-work-duration-v2", "timeout_seconds": 300},
        }
    )
    assert selection.cpus == invocation.cpus and selection.memory_mb == invocation.memory_mb
    with pytest.raises(ValueError):
        FactoryPoolSelection.model_validate({**selection.model_dump(mode="json"), "protocol": "forge-factory-pool-v1"})


def test_kubernetes_profile_cannot_admit_a_legacy_invocation_without_its_protocol():
    from test_runner_pool_dispatch import request

    selected = docker_policy().model_dump(mode="json")
    selected.update(
        protocol="forge-kubernetes-pool-v1",
        backend="kubernetes",
        kubernetes=kubernetes_policy().model_dump(mode="json"),
    )
    profile = RunnerPoolProfile.model_validate(selected)
    with pytest.raises(ValueError, match="Kubernetes invocation protocol"):
        profile.validate_invocation(request(profile))
