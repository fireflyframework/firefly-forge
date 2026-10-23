# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Package-owned qualification of a specific native source-edit capability profile."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from firefly_forge.journal import digest
from firefly_forge.source_edit_models import READ_TOOLS

REGISTRY_VERSION = 4
LOADER_PROFILE = "codex-exec-restricted-source/v5"
LOADER_MODULES = (
    "providers.py",
    "source_edit_cli_policy.py",
    "coding_cli_channel.py",
    "coding_check_transport.py",
    "source_edit_models.py",
    "native_configuration_rpc.py",
    "source_edit_filesystem_policy.py",
    "workspace_tools.py",
    "source_edit_continuations.py",
    "source_edit_model_catalog.py",
    "verification_models.py",
    "assurance.py",
    "prompts.py",
    "resources/prompts/v1/manifest.json",
    "resources/prompts/v1/coding-runtime/source-list-1.0.1.yaml",
    "resources/prompts/v1/coding-runtime/source-read-1.0.1.yaml",
    "resources/prompts/v1/coding-runtime/source-search-1.0.1.yaml",
)
NATIVE_CAPABILITIES = (
    ("apply_patch", "custom"),
    ("clock.curr_time", "function"),
    ("forge_coding.list_files", "function"),
    ("forge_coding.read_file", "function"),
    ("forge_coding.search_text", "function"),
    ("list_mcp_resource_templates", "function"),
    ("list_mcp_resources", "function"),
    ("read_mcp_resource", "function"),
)
NATIVE_OUTCOMES = (
    "list_files",
    "read_file",
    "search_text",
    "apply_patch",
    "curr_time",
    "list_mcp_resources",
    "list_mcp_resource_templates",
    "file_resource_denied",
    "https_resource_denied",
    "unknown_server_read_denied",
    "unknown_server_list_denied",
    "unknown_server_templates_denied",
    "exec_command_denied",
    "exec_alias_denied",
    "collaboration_tools_denied",
    "tool_search_denied",
    "source_reader_bounds_enforced",
)
FILESYSTEM_CHECKS = (
    "patch_add",
    "patch_update",
    "patch_move",
    "patch_delete",
    "private_read_denied",
    "private_write_denied",
    "outside_read_denied",
    "outside_write_denied",
    "case_alias_read_denied",
    "case_alias_write_denied",
    "public_env_example_allowed",
    "filesystem_groups_absent",
    "filesystem_streams_drained",
)


@dataclass(frozen=True)
class NativeQualificationProof:
    artifact_sha256: str
    contract_digest: str
    exit_code: int
    requests: int
    outcome_checks: tuple[str, ...]
    reader_calls: tuple[str, ...]
    group_absent: bool
    streams_drained: bool
    real_model_calls: int
    filesystem_policy_version: int
    filesystem_checks: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityProfile:
    version: int
    profile_id: str
    executable_sha256: str
    executable_size: int
    system: str
    machine: str
    model: str
    model_catalog_sha256: str
    native_provider: str
    policy_digest: str
    loader_profile: str
    loader_hashes: tuple[tuple[str, str], ...]
    runtime_versions: tuple[tuple[str, str], ...]
    catalog: tuple[tuple[str, str, str], ...]
    configuration_features: tuple[tuple[str, bool | None], ...]
    configuration_tools: tuple[tuple[str, bool], ...]
    proof: NativeQualificationProof


def _profiles() -> tuple[CapabilityProfile, ...]:
    """Load package-owned literals after qualification types are initialized."""
    from .source_edit_supported_profiles import PROFILES

    return PROFILES


def loader_hashes() -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()) for name in LOADER_MODULES
    )


def runtime_versions() -> tuple[tuple[str, str], ...]:
    return (
        ("python", platform.python_version()),
        *((name, importlib.metadata.version(name)) for name in ("mcp", "pydantic")),
    )


def policy_digest() -> str:
    from firefly_forge.source_edit_cli_policy import SOURCE_ONLY_ENV, SOURCE_ONLY_OVERRIDES
    from firefly_forge.source_edit_filesystem_policy import (
        FILESYSTEM_POLICY_VERSION,
        MAX_PRIVATE_PATHS,
        MAX_SCAN_DEPTH,
        MAX_SCAN_ENTRIES,
        env_private_patterns,
    )
    from firefly_forge.source_edit_model_catalog import CATALOG_SHA256
    from firefly_forge.workspace_tools import PRIVATE_BASENAMES, PRIVATE_KEY_SUFFIXES, PUBLIC_ENV_SUFFIXES

    return digest(
        {
            "overrides": SOURCE_ONLY_OVERRIDES,
            "environment": SOURCE_ONLY_ENV,
            "model_catalog_sha256": CATALOG_SHA256,
            "filesystem_version": FILESYSTEM_POLICY_VERSION,
            "filesystem_scan_bounds": (MAX_SCAN_ENTRIES, MAX_SCAN_DEPTH, MAX_PRIVATE_PATHS),
            "private_basenames": sorted(PRIVATE_BASENAMES),
            "private_key_suffixes": PRIVATE_KEY_SUFFIXES,
            "public_env_suffixes": PUBLIC_ENV_SUFFIXES,
            "private_env_patterns": env_private_patterns(),
        }
    )


def profile_contract_digest(profile: CapabilityProfile) -> str:
    contract = asdict(profile)
    contract.pop("proof")
    return digest(contract)


def require_profile(profile: CapabilityProfile) -> None:
    """An isolated catalog proof and a current config admission are separate gates."""
    if (
        not isinstance(profile, CapabilityProfile)
        or profile not in _profiles()
        or not isinstance(profile.proof, NativeQualificationProof)
    ):
        raise ValueError("Source-only execution requires a registered qualified native profile")
    from firefly_forge.source_edit_cli_policy import DISABLED_FEATURES
    from firefly_forge.source_edit_filesystem_policy import FILESYSTEM_POLICY_VERSION
    from firefly_forge.source_edit_model_catalog import model_catalog_sha256

    try:
        catalog_digest = model_catalog_sha256(profile.model)
    except ValueError:
        raise ValueError("The package-owned model catalog is no longer qualified") from None

    proof = profile.proof
    features = dict(profile.configuration_features)
    hashes = (profile.executable_sha256, proof.artifact_sha256, *(row[2] for row in profile.catalog))
    if (
        profile.version != REGISTRY_VERSION
        or proof.contract_digest != profile_contract_digest(profile)
        or not re.fullmatch(r"[a-z0-9_-]{1,100}", profile.profile_id)
        or any(re.fullmatch(r"[a-f0-9]{64}", value) is None for value in hashes)
        or not 0 < profile.executable_size <= 512 * 1024 * 1024
        or (profile.system, profile.machine) != (platform.system(), platform.machine())
        or not profile.model
        or profile.model_catalog_sha256 != catalog_digest
        or not profile.native_provider
        or profile.policy_digest != policy_digest()
        or profile.loader_profile != LOADER_PROFILE
        or profile.loader_hashes != loader_hashes()
        or profile.runtime_versions != runtime_versions()
        or tuple((name, kind) for name, kind, _checksum in profile.catalog) != NATIVE_CAPABILITIES
        or type(proof.exit_code) is not int
        or proof.exit_code != 0
        or type(proof.requests) is not int
        or not 1 <= proof.requests <= 512
        or proof.outcome_checks != NATIVE_OUTCOMES
        or proof.reader_calls != READ_TOOLS
        or proof.group_absent is not True
        or proof.streams_drained is not True
        or type(proof.real_model_calls) is not int
        or proof.real_model_calls != 0
        or type(proof.filesystem_policy_version) is not int
        or proof.filesystem_policy_version != FILESYSTEM_POLICY_VERSION
        or proof.filesystem_checks != FILESYSTEM_CHECKS
        or len(features) != len(profile.configuration_features)
        or any(features.get(key) is not False for key in DISABLED_FEATURES)
        or features.get("skip_host_skill_discovery") is not True
        or profile.configuration_tools != (("update_plan", False), ("experimental_request_user_input", False))
    ):
        raise ValueError("The qualified native build, loader, policy or capability proof is incomplete or changed")


def qualified_profile(executable: dict[str, Any], model: str) -> CapabilityProfile:
    selected = [
        profile
        for profile in _profiles()
        if profile.executable_sha256 == executable["sha256"]
        and profile.executable_size == executable["size"]
        and profile.model == model
    ]
    if len(selected) != 1:
        raise ValueError("Source-only execution requires a qualified native executable and model")
    require_profile(selected[0])
    return selected[0]


def profile_seal(profile: CapabilityProfile) -> dict[str, Any]:
    require_profile(profile)
    return {
        "registry_version": REGISTRY_VERSION,
        "profile_id": profile.profile_id,
        "profile_digest": digest(asdict(profile)),
        "catalog_digest": digest({"catalog": profile.catalog}),
        "proof_sha256": profile.proof.artifact_sha256,
        "loader_profile": profile.loader_profile,
    }
