# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit public fixture profiles; never part of the production registry."""

import hashlib
import json
import platform
from dataclasses import replace
from pathlib import Path

from firefly_forge.provider_executable_adoption import executable_identity
from firefly_forge.source_edit_cli_policy import DISABLED_FEATURES, configuration_projection
from firefly_forge.source_edit_model_catalog import model_catalog_sha256
from firefly_forge.source_edit_models import READ_TOOLS
from firefly_forge.source_edit_qualification import (
    FILESYSTEM_CHECKS,
    LOADER_PROFILE,
    NATIVE_CAPABILITIES,
    NATIVE_OUTCOMES,
    REGISTRY_VERSION,
    SOURCE_BUDGET_CHECKS,
    CapabilityProfile,
    NativeQualificationProof,
    loader_hashes,
    policy_digest,
    profile_contract_digest,
    runtime_versions,
)


def fixture_profile(executable):
    identity = executable_identity(str(executable), executable.parent / "unused-workspaces")
    profile = CapabilityProfile(
        version=REGISTRY_VERSION,
        profile_id="explicit-public-test-fixture",
        executable_sha256=identity["sha256"],
        executable_size=identity["size"],
        system=platform.system(),
        machine=platform.machine(),
        model="fixture-model",
        model_catalog_sha256=model_catalog_sha256("fixture-model"),
        native_provider="fixture-provider",
        policy_digest=policy_digest(),
        loader_profile=LOADER_PROFILE,
        loader_hashes=loader_hashes(),
        runtime_versions=runtime_versions(),
        catalog=tuple(
            (name, kind, hashlib.sha256(("fixture:" + name).encode()).hexdigest()) for name, kind in NATIVE_CAPABILITIES
        ),
        configuration_features=tuple((name, False) for name in DISABLED_FEATURES)
        + (("skip_host_skill_discovery", True),),
        configuration_tools=(("update_plan", False), ("experimental_request_user_input", False)),
        source_budget_limits=(("read_file_lifetime_limit", 20), ("activation_ceiling", 5)),
        proof=NativeQualificationProof(
            contract_digest="",
            artifact_sha256=hashlib.sha256(
                b"Explicit public fixture, no actual native qualification claim"
            ).hexdigest(),
            exit_code=0,
            requests=154,
            outcome_checks=NATIVE_OUTCOMES,
            reader_calls=READ_TOOLS,
            group_absent=True,
            streams_drained=True,
            real_model_calls=0,
            filesystem_policy_version=2,
            filesystem_checks=FILESYSTEM_CHECKS,
            budget_checks=SOURCE_BUDGET_CHECKS,
            budget_limits=(("read_file_lifetime_limit", 20), ("activation_ceiling", 5)),
        ),
    )
    return replace(profile, proof=replace(profile.proof, contract_digest=profile_contract_digest(profile)))


def register_fixture_profile(monkeypatch, executable):
    from firefly_forge import source_edit_model_catalog, source_edit_qualification

    catalog = Path(executable).with_name("explicit-fixture-model-catalog.json")
    content = json.dumps({"models": [{"slug": "fixture-model", "tool_mode": "direct"}]}).encode()
    catalog.write_bytes(content)
    monkeypatch.setattr(source_edit_model_catalog, "CATALOG_MODEL", "fixture-model")
    monkeypatch.setattr(source_edit_model_catalog, "CATALOG_PATH", catalog)
    monkeypatch.setattr(source_edit_model_catalog, "CATALOG_SHA256", hashlib.sha256(content).hexdigest())

    profile = fixture_profile(Path(executable))
    monkeypatch.setattr(source_edit_qualification, "_profiles", lambda: (profile,))
    return profile


def api_configuration(profile, workspace, private_roots):
    from copy import deepcopy

    from firefly_forge.source_edit_cli_policy import DISABLED_NATIVE_AGENTS
    from firefly_forge.source_edit_filesystem_policy import (
        EMPTY_NATIVE_HOOKS,
        build_named_permissions,
        native_api_permissions,
    )
    from firefly_forge.source_edit_model_catalog import model_catalog_path

    config = {
        "features": dict(profile.configuration_features),
        "tools": {"web_search": None},
        "model": profile.model,
        "model_provider": profile.native_provider,
        "model_catalog_json": str(model_catalog_path(profile.model)),
        "notify": [],
        "hooks": deepcopy(EMPTY_NATIVE_HOOKS),
        "default_permissions": "forge_source_edit",
        "permissions": native_api_permissions(build_named_permissions(workspace, private_roots)),
        "approval_policy": "never",
        "web_search": "disabled",
        "mcp_servers": {},
        "agents": dict(DISABLED_NATIVE_AGENTS),
        "skills": {"include_instructions": False, "bundled": {"enabled": False}},
        "cloud": {"skills": {"enabled": False}},
        "analytics": {"enabled": False},
        "feedback": {"enabled": False},
        "otel": {"exporter": "none", "trace_exporter": "none", "metrics_exporter": "none"},
    }
    return config


def metadata_proof(profile, workspace, private_roots):
    return configuration_projection(
        api_configuration(profile, workspace, private_roots),
        None,
        profile=profile,
        workspace=workspace,
        private_roots=private_roots,
    )
