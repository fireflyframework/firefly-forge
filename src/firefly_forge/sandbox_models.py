# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Frozen, non-secret execution policies; Docker selection never falls back to host execution."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def pinned_image(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._:/-]{0,255}@sha256:[a-f0-9]{64}", value) or "://" in value:
        raise ValueError("An OCI repository image pinned by sha256 digest is required")
    return value


def bounded_argv(value: list[str]) -> list[str]:
    if any(not part or "\x00" in part or len(part) > 8192 for part in value):
        raise ValueError("Command arguments must be bounded nonempty strings")
    return value


class BrowserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str = Field(max_length=512)
    start_argv: list[str] = Field(min_length=1, max_length=64)
    origin: str = Field(max_length=200)
    startup_timeout: int = Field(default=30, ge=1, le=120, strict=True)
    max_width: int = Field(default=2560, ge=320, le=2560, strict=True)
    max_height: int = Field(default=1440, ge=320, le=1440, strict=True)
    max_screenshot_bytes: int = Field(default=1_000_000, ge=1024, le=2_000_000, strict=True)
    max_calls: int = Field(default=8, ge=1, le=20, strict=True)

    _image = field_validator("image")(pinned_image)
    _argv = field_validator("start_argv")(bounded_argv)

    @field_validator("origin")
    @classmethod
    def loopback_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or not parsed.port
            or parsed.port < 1024
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Browser origin must be the declared container loopback server on port 1024 or higher")
        return value.rstrip("/")


class SandboxInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["host", "docker"] = "host"
    image: str | None = Field(default=None, max_length=512)
    context: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    workspace_transport: Literal["bind", "archive"] = "bind"
    workspace_max_bytes: int = Field(default=67_108_864, ge=1_048_576, le=268_435_456, strict=True)
    cpus: float = Field(default=1, ge=0.1, le=4, allow_inf_nan=False)
    memory_mb: int = Field(default=1024, ge=128, le=8192, strict=True)
    pids_limit: int = Field(default=128, ge=32, le=512, strict=True)
    timeout: int = Field(default=300, ge=1, le=3600, strict=True)
    max_output_bytes: int = Field(default=1_000_000, ge=1024, le=2_000_000, strict=True)
    network: Literal["none", "egress"] = "none"
    command_limit: int = Field(default=50, ge=1, le=100, strict=True)
    browser: BrowserInput | None = None
    internet_search: bool = False

    @model_validator(mode="after")
    def mode_controls(self) -> SandboxInput:
        if self.mode == "docker":
            if not self.image or not self.context:
                raise ValueError("Docker mode requires a pinned image and an explicit Docker context")
            pinned_image(self.image)
        elif self.image or self.context or self.browser or self.network != "none" or self.workspace_transport != "bind":
            raise ValueError("Image, context, browser and network policy require Docker mode")
        return self


def _digest(configuration: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def capture_sandbox(value: SandboxInput | dict[str, Any] | None) -> dict[str, Any]:
    config = SandboxInput.model_validate(value.model_dump() if isinstance(value, SandboxInput) else value or {})
    configuration = config.model_dump(mode="json")
    controls = ["timeout", "output"]
    if config.mode == "docker":
        controls += [
            "workspace_archive" if config.workspace_transport == "archive" else "workspace_mount",
            "read_only_root",
            "capability_drop",
            "no_new_privileges",
            "cpu",
            "memory",
            "pids",
            "network",
        ]
    return {
        "mode": config.mode,
        "configuration": configuration,
        "digest": _digest(configuration),
        "effective_network": config.network if config.mode == "docker" else "host",
        "enforced_controls": controls,
        "reason": (
            "Docker commands use the selected immutable image and bounded container controls"
            if config.mode == "docker"
            else "Trusted host execution uses the service account; CPU, memory and network isolation are not provided"
        ),
    }


def validate_sandbox_snapshot(snapshot: dict[str, Any]) -> SandboxInput:
    config = SandboxInput.model_validate(snapshot.get("configuration"))
    expected = capture_sandbox(config)
    for key in ("workspace_transport", "workspace_max_bytes"):
        if key not in snapshot.get("configuration", {}):
            expected["configuration"].pop(key)
    expected["digest"] = _digest(expected["configuration"])
    original_browser = snapshot.get("configuration", {}).get("browser")
    if isinstance(original_browser, dict) and "max_calls" not in original_browser:
        expected["configuration"]["browser"].pop("max_calls")
        expected["digest"] = _digest(expected["configuration"])
    if snapshot != expected:
        raise ValueError("Runner snapshot digest or policy metadata was changed")
    return config
