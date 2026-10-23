# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Optional execution surfaces disabled for the pinned-Codex source channel.

Managed requirements retain precedence. Incompatible required controls must
cause native CLI configuration admission to fail, never be bypassed.
"""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from firefly_forge.source_edit_filesystem_policy import (
    EMPTY_NATIVE_HOOKS,
    PROFILE_NAME,
    build_named_permissions,
    native_api_permissions,
    native_policy_overrides,
    typed_equal,
)
from firefly_forge.source_edit_qualification import CapabilityProfile

SOURCE_EDIT_LAUNCH: ContextVar[Any] = ContextVar("source_edit_launch", default=None)


@contextmanager
def source_edit_launch(server: Any, activation: Any, *, permissions_digest: str | None = None) -> Any:
    token = SOURCE_EDIT_LAUNCH.set(
        {"server": server, "activation": activation, "permissions_digest": permissions_digest}
    )
    try:
        yield
    finally:
        SOURCE_EDIT_LAUNCH.reset(token)


DISABLED_FEATURES = (
    "code_mode",
    "view_image",
    "deferred_executor",
    "token_budget",
    "sleep_tool",
    "send_message_to_user_async",
    "shell_tool",
    "hooks",
    "multi_agent",
    "multi_agent_v2",
    "multi_agent_v2_dynamic_tools",
    "apps",
    "plugins",
    "remote_plugin",
    "tool_suggest",
    "image_generation",
    "request_permissions_tool",
    "skill_mcp_dependency_install",
    "skill_env_var_dependency_prompt",
    "skill_search",
    "goals",
    "memories",
    "agent_message_board",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
)
SOURCE_ONLY_OVERRIDES = (
    *(f"features.{name}=false" for name in DISABLED_FEATURES),
    "agents.enabled=false",
    'web_search="disabled"',
    "notify=[]",
    "hooks={}",
    "skills.include_instructions=false",
    "skills.bundled.enabled=false",
    "cloud.skills.enabled=false",
    "features.skip_host_skill_discovery=true",
    "tools.update_plan.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
    "analytics.enabled=false",
    "feedback.enabled=false",
    'otel.exporter="none"',
    'otel.trace_exporter="none"',
    'otel.metrics_exporter="none"',
)
SOURCE_ONLY_ENV = {"CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED": "1"}
DISABLED_NATIVE_AGENTS: dict[str, bool | None] = {
    "enabled": False,
    "max_concurrent_threads_per_session": None,
    "max_depth": None,
    "default_subagent_model": None,
    "default_subagent_reasoning_effort": None,
    "job_max_runtime_seconds": None,
    "interrupt_message": None,
}


def configuration_projection(
    config: Any,
    requirements: Any,
    *,
    profile: CapabilityProfile,
    workspace: Path,
    private_roots: tuple[Path, ...],
) -> dict[str, Any]:
    """Project safe effective state only; never retain raw native configuration or requirements."""
    from firefly_forge.journal import digest
    from firefly_forge.source_edit_model_catalog import model_catalog_path, model_catalog_sha256
    from firefly_forge.source_edit_qualification import profile_seal, require_profile

    require_profile(profile)
    if not isinstance(config, dict) or requirements is not None and not isinstance(requirements, dict):
        raise ValueError("Native effective configuration is unavailable")
    required = requirements or {}
    permissions = build_named_permissions(workspace, private_roots)
    if (
        config.get("default_permissions") != PROFILE_NAME
        or config.get("approval_policy") != "never"
        or not typed_equal(config.get("agents"), DISABLED_NATIVE_AGENTS)
        or config.get("model_catalog_json") != str(model_catalog_path(profile.model))
        or not typed_equal(config.get("permissions"), native_api_permissions(permissions))
    ):
        raise ValueError("Native permissions do not match the current restricted workspace policy")

    def hooks_present(value: Any) -> bool:
        return any(hooks_present(item) for item in value.values()) if isinstance(value, dict) else bool(value)

    feature_requirements = required.get("featureRequirements")
    if feature_requirements is None:
        feature_requirements = {}
    if (
        not isinstance(feature_requirements, dict)
        or set(feature_requirements) - dict(profile.configuration_features).keys()
        or any(type(value) is not bool for value in feature_requirements.values())
    ):
        raise ValueError("Native managed feature requirements contain an unqualified capability")
    if (
        hooks_present(required.get("hooks"))
        or any(feature_requirements.get(key) is True for key in DISABLED_FEATURES)
        or feature_requirements.get("skip_host_skill_discovery") is False
    ):
        raise ValueError("Mandatory native controls conflict with source-only execution")
    features = config.get("features")
    if not isinstance(features, dict) or set(features) != dict(profile.configuration_features).keys():
        raise ValueError("Native effective feature state contains missing or unqualified capabilities")
    for key, expected in profile.configuration_features:
        value = features[key]
        if isinstance(value, dict) and set(value) == {"enabled"}:
            value = value["enabled"]
        if value is not expected:
            raise ValueError("Native effective features differ from the qualified capability profile")
    for key in DISABLED_FEATURES:
        value = features.get(key)
        if isinstance(value, dict):
            value = value.get("enabled")
        if value is not False:
            raise ValueError("An optional native execution capability remains enabled")
    tools = config.get("tools")
    # ApiConfig.ToolsV2 omits the two utility flags. Their absence is established
    # by the profile's frozen native catalog/invocation proof, not by this reply.
    if not typed_equal(tools, {"web_search": None}):
        raise ValueError("Native utility metadata differs from the qualified API contract")

    def setting(*path: str) -> Any:
        value: Any = config
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value

    if (
        config.get("model") != profile.model
        or config.get("model_provider") != profile.native_provider
        or features.get("skip_host_skill_discovery") is not True
        or config.get("notify") != []
        or not typed_equal(config.get("hooks"), EMPTY_NATIVE_HOOKS)
        or config.get("web_search") != "disabled"
        or config.get("mcp_servers") not in (None, {})
        or setting("skills", "include_instructions") is not False
        or setting("skills", "bundled", "enabled") is not False
        or setting("cloud", "skills", "enabled") is not False
        or setting("analytics", "enabled") is not False
        or setting("feedback", "enabled") is not False
        or any(setting("otel", key) != "none" for key in ("exporter", "trace_exporter", "metrics_exporter"))
    ):
        raise ValueError("Native autoload or another MCP server prevents source-only execution")
    return {
        "version": 5,
        "profile": profile_seal(profile),
        "configuration_digest": digest(
            {
                "features": features,
                "tools": tools,
                "model": profile.model,
                "model_provider": profile.native_provider,
                "model_catalog_path": str(model_catalog_path(profile.model)),
                "model_catalog_sha256": model_catalog_sha256(profile.model),
                "permissions": permissions,
                "approval_policy": "never",
                "agents": DISABLED_NATIVE_AGENTS,
                "hooks": EMPTY_NATIVE_HOOKS,
            }
        ),
        "permissions_digest": digest(permissions),
        "managed_execution_conflict": False,
    }


async def qualify_configuration(
    executable: dict[str, Any],
    timeout: float,
    *,
    profile: CapabilityProfile,
    workspace: Path,
    private_roots: tuple[Path, ...],
) -> dict[str, Any]:
    """Fixed no-model metadata operation, with no thread, tool or model request.

    The actual pinned startup/sentinel qualification is required before adopting
    this mode. Configuration replies stay in memory and never enter activity logs.
    """
    from firefly_forge.source_edit_qualification import qualified_profile

    if qualified_profile(executable, profile.model) != profile:
        raise ValueError("Native metadata requires the exact qualified executable profile")
    from firefly_forge.native_configuration_rpc import ConfigurationRPC
    from firefly_forge.providers import cli_dispatch_scope, run_process
    from firefly_forge.secrets import runtime_environment
    from firefly_forge.source_edit_model_catalog import model_catalog_override

    protocol = ConfigurationRPC(workspace)
    command = [executable["path"], "app-server", "--listen", "stdio://", "--strict-config"]
    command += [
        value
        for override in (
            *SOURCE_ONLY_OVERRIDES,
            *native_policy_overrides(workspace, private_roots),
            model_catalog_override(profile.model),
            'approval_policy="never"',
            "mcp_servers={}",
            "model=" + json.dumps(profile.model),
            "model_provider=" + json.dumps(profile.native_provider),
        )
        for value in ("-c", override)
    ]

    async def discard(_kind: str, _message: str) -> None:
        return None

    # A fixed metadata request does not consume a paid provider dispatch.
    with cli_dispatch_scope(None, executable=executable["path"]):
        result = await run_process(
            command,
            workspace.resolve(),
            discard,
            timeout=min(5, timeout),
            stdin_protocol=protocol,
            env=runtime_environment(SOURCE_ONLY_ENV),
        )
    outcome = result.process_outcome
    if (
        result.returncode != 0
        or outcome is None
        or not outcome.leader_waited
        or not outcome.streams_drained
        or not outcome.group_absent
    ):
        raise ValueError("Native no-model configuration qualification did not settle")
    try:
        replies = protocol.results
        if set(replies) != {1, 2, 3}:
            raise ValueError("Incomplete native metadata")
        return configuration_projection(
            replies[2]["config"],
            replies[3]["requirements"],
            profile=profile,
            workspace=workspace,
            private_roots=private_roots,
        )
    except (ValueError, KeyError, TypeError):
        raise ValueError("Native effective source-only policy was not established") from None
