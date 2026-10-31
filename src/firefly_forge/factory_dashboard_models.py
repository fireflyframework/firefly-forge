# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read observations have an explicit scope; their counters never grant execution."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RUN_BUCKETS: dict[str, tuple[str, ...]] = {
    "queued": ("queued", "pending"),
    "active": (
        "running",
        "cloning",
        "preparing",
        "generating",
        "developing",
        "executing",
        "verifying",
        "validating",
        "publishing",
        "deploying",
    ),
    "needs_action": ("paused", "interrupted", "needs_reconciliation", "awaiting_input"),
    "completed": ("succeeded", "completed"),
    "failed": ("failed",),
    "cancelled": ("cancelled",),
    "expired": ("expired",),
    "quarantined": ("quarantined",),
}
STAGES = ("prepare", "implement", "verify", "checkpoint", "publish", "deploy", "health")


def run_bucket(status: str) -> str:
    return next((name for name, values in RUN_BUCKETS.items() if status in values), "unknown")


class DashboardScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["factory", "project", "standalone", "run"] = "factory"
    project_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def exact_scope(self) -> DashboardScope:
        if (self.project_id is not None) != (self.kind == "project"):
            raise ValueError("A product-project scope requires exactly its project identity")
        if (self.run_id is not None) != (self.kind == "run"):
            raise ValueError("A run scope requires exactly its recorded run identity")
        return self


class DashboardPageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    limit: int = Field(default=40, ge=1, le=200, strict=True)
    cursor: str | None = Field(default=None, max_length=2048)
    bucket: (
        Literal[
            "queued", "active", "needs_action", "completed", "failed", "cancelled", "expired", "quarantined", "unknown"
        ]
        | None
    ) = None


def scope_digest(scope: DashboardScope, actor: str, *, search: str = "", bucket: str | None = None) -> str:
    value = {"scope": scope.model_dump(), "actor": actor, "search": search, "bucket": bucket}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cursor_encode(binding: str, position: str) -> str:
    return (
        base64.urlsafe_b64encode(
            json.dumps({"version": 1, "binding": binding, "position": position}, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )


def cursor_decode(value: str | None, binding: str) -> str | None:
    if value is None:
        return None
    try:
        parsed: Any = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError):
        raise ValueError("Invalid dashboard page cursor") from None
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"version", "binding", "position"}
        or type(parsed["version"]) is not int
        or parsed["version"] != 1
        or parsed["binding"] != binding
        or not isinstance(parsed["position"], str)
    ):
        raise ValueError("The dashboard page belongs to another scope or identity")
    return parsed["position"]
