# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed grants for factory-owned execution of frozen verification commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from firefly_forge.journal import digest


class AutomaticVerificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=True, strict=True)
    max_continuations: int = Field(default=1, ge=1, le=3, strict=True)


class VerificationAdoption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    pending_request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    commands_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    agent: Literal["codex", "claude", "bridge", "agentic"]


def verification_scope(run: dict[str, Any]) -> str:
    return digest(
        {
            key: run.get(key)
            for key in (
                "id",
                "project_id",
                "repository_id",
                "component_id",
                "component_snapshot",
                "execution_actor",
                "sandbox_snapshot",
                "publish",
            )
        }
    )


def verification_provider(store: Any, run: dict[str, Any], agent: str | None = None) -> str:
    settings = (
        store.configuration.resolve(agent) if agent else store.configuration.load_snapshot(run["config_revision"])
    )
    return digest({"agent": agent or run["agent"], "settings": settings})


def verification_source(workspace: Path) -> str:
    from firefly_forge.assurance import _manifest

    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("Verification requires the retained real workspace")
    return digest(
        {
            "protocol": "forge-verification-source-v1",
            "files": _manifest(workspace, original=True, extra_ignored=frozenset({".cache"})),
        }
    )


def check_verification_cut(store: Any, run: dict[str, Any], intent: dict[str, Any]) -> None:
    from firefly_forge.tool_broker import tool_policy_snapshot

    if (
        verification_scope(run) != intent["scope_digest"]
        or digest({"commands": run.get("commands", [])}) != intent["commands_digest"]
        or tool_policy_snapshot(store.configuration)["digest"] != intent["policy_digest"]
        or verification_provider(store, run, intent.get("agent")) != intent["provider_digest"]
        or (run.get("cancel_request") or {}).get("status") == "requested"
    ):
        raise ValueError("Automatic verification scope, policy or provider changed")
    request = run.get("worker_request", {})
    if (
        request.get("id") != intent["request_id"]
        or (request.get("status") == "pending" and digest(request) != intent["request_digest"])
        or (
            request.get("status") != "pending"
            and run.get("worker_response", {}).get("verification_handoff_id") != intent["id"]
        )
    ):
        raise ValueError("Automatic verification request changed")
    actual = verification_source(Path(intent["workspace"]))
    if actual != intent["source_digest"]:
        raise ValueError("Automatic verification source changed")


def automatic_verification_grant(snapshot: dict[str, Any] | None) -> AutomaticVerificationPolicy | None:
    if snapshot is None:
        return None
    payload = {key: value for key, value in snapshot.items() if key != "digest"}
    if (
        set(payload) != {"version", "revision", "settings"}
        or payload["version"] != 1
        or digest(payload) != snapshot.get("digest")
    ):
        raise ValueError("Automatic verification policy snapshot was changed")
    settings = payload["settings"]
    if not isinstance(settings, dict) or "automatic_verification" not in settings:
        return None
    policy = AutomaticVerificationPolicy.model_validate(settings["automatic_verification"])
    return policy if policy.enabled else None
