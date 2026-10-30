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

REGISTRY_VERSION = 29
LOADER_PROFILE = "codex-exec-restricted-source/v30"
LOADER_MODULES = (
    "source_budget_allowances.py",
    "source_budget_output.py",
    "source_tool_budget.py",
    "tool_broker.py",
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
    "resources/prompts/v1/coding-runtime/source-list-1.0.2.yaml",
    "resources/prompts/v1/coding-runtime/source-read-1.0.1.yaml",
    "resources/prompts/v1/coding-runtime/source-search-1.0.2.yaml",
    "checkpoint_source_identity.py",
    "work_duration_models.py",
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
    budget_checks: tuple[str, ...] = ()
    budget_limits: tuple[tuple[str, int], ...] = ()


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
    source_budget_limits: tuple[tuple[str, int], ...] = ()
    admission_contract_digest: str = ""


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


SOURCE_BUDGET_CHECKS = (
    "legacy_read_limit10",
    "amended_read_limit",
    "shared_total_limit30",
    "definite_refusal_no_effect",
    "remaining_capabilities_exact",
    "unamended_activation_limit4",
    "amended_canonical_activation",
    "unknown_never_replayed",
    "known_completed_output_finite_activation",
)


def profile_payload(profile: CapabilityProfile) -> dict[str, Any]:
    value = asdict(profile)
    if profile.version < 9:
        if profile.admission_contract_digest:
            raise ValueError("Historical native profiles cannot acquire a new admission closure")
        value.pop("admission_contract_digest")
    if profile.version == 4 and profile.loader_profile == "codex-exec-restricted-source/v5":
        if profile.source_budget_limits or profile.proof.budget_checks or profile.proof.budget_limits:
            raise ValueError("Historical native profiles cannot acquire new budget authority")
        value.pop("source_budget_limits")
        value["proof"].pop("budget_checks")
        value["proof"].pop("budget_limits")
    return value


def historical_profile_seal(profile: CapabilityProfile) -> dict[str, Any]:
    if (
        profile not in _profiles()
        or (profile.version, profile.loader_profile)
        not in {
            (4, "codex-exec-restricted-source/v5"),
            (5, "codex-exec-restricted-source/v6"),
            (6, "codex-exec-restricted-source/v7"),
            (7, "codex-exec-restricted-source/v8"),
            (8, "codex-exec-restricted-source/v9"),
            (9, "codex-exec-restricted-source/v10"),
            (10, "codex-exec-restricted-source/v11"),
            (11, "codex-exec-restricted-source/v12"),
            (12, "codex-exec-restricted-source/v13"),
            (13, "codex-exec-restricted-source/v14"),
            (14, "codex-exec-restricted-source/v15"),
            (15, "codex-exec-restricted-source/v16"),
            (16, "codex-exec-restricted-source/v17"),
            (17, "codex-exec-restricted-source/v18"),
            (18, "codex-exec-restricted-source/v19"),
            (19, "codex-exec-restricted-source/v20"),
            (20, "codex-exec-restricted-source/v21"),
            (21, "codex-exec-restricted-source/v22"),
            (22, "codex-exec-restricted-source/v23"),
            (23, "codex-exec-restricted-source/v24"),
            (24, "codex-exec-restricted-source/v25"),
            (25, "codex-exec-restricted-source/v26"),
            (26, "codex-exec-restricted-source/v27"),
            (27, "codex-exec-restricted-source/v28"),
            (28, "codex-exec-restricted-source/v29"),
        }
        or profile.proof.contract_digest != profile_contract_digest(profile)
    ):
        raise ValueError("The immutable historical native profile is not registered provenance")
    return {
        "registry_version": profile.version,
        "profile_id": profile.profile_id,
        "profile_digest": digest(profile_payload(profile)),
        "catalog_digest": digest({"catalog": profile.catalog}),
        "proof_sha256": profile.proof.artifact_sha256,
        "loader_profile": profile.loader_profile,
    }


def source_budget_limits(profile: CapabilityProfile) -> dict[str, int]:
    require_profile(profile)
    limits = dict(profile.source_budget_limits)
    if (
        len(limits) != len(profile.source_budget_limits)
        or set(limits) != {"read_file_lifetime_limit", "activation_ceiling"}
        or type(limits["read_file_lifetime_limit"]) is not int
        or not 10 <= limits["read_file_lifetime_limit"] <= 30
        or type(limits["activation_ceiling"]) is not int
        or not 4 <= limits["activation_ceiling"] <= 32
    ):
        raise ValueError("The current native profile has no exact finite source budget qualification")
    return limits


def profile_contract_digest(profile: CapabilityProfile) -> str:
    contract = profile_payload(profile)
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
    from firefly_forge.source_budget_allowances import admission_contract_digest
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
        or not isinstance(profile.admission_contract_digest, str)
        or re.fullmatch(r"[a-f0-9]{64}", profile.admission_contract_digest) is None
        or profile.admission_contract_digest != admission_contract_digest()
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
        or proof.budget_checks != SOURCE_BUDGET_CHECKS
        or proof.budget_limits != profile.source_budget_limits
        or set(dict(profile.source_budget_limits)) != {"read_file_lifetime_limit", "activation_ceiling"}
        or len(profile.source_budget_limits) != 2
        or any(type(value) is not int for _, value in profile.source_budget_limits)
        or not 10 <= dict(profile.source_budget_limits).get("read_file_lifetime_limit", 0) <= 30
        or not 4 <= dict(profile.source_budget_limits).get("activation_ceiling", 0) <= 32
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
        if profile.version == REGISTRY_VERSION
        and profile.loader_profile == LOADER_PROFILE
        and profile.loader_hashes == loader_hashes()
        and profile.executable_sha256 == executable["sha256"]
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
        "profile_digest": digest(profile_payload(profile)),
        "catalog_digest": digest({"catalog": profile.catalog}),
        "proof_sha256": profile.proof.artifact_sha256,
        "loader_profile": profile.loader_profile,
    }
