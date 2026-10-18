# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed grants for factory-owned execution of frozen verification commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from firefly_forge.journal import digest


class AutomaticVerificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=True, strict=True)
    max_continuations: int = Field(default=1, ge=1, le=3, strict=True)


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
