# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit frozen runner inputs distinguish retained producers from new work."""

from typing import Any

from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.work_duration_models import WorkDurationPolicyV2


def runner_snapshot(
    *, policy: WorkDurationPolicyV2 | None = None, command_limit: int = 22, timeout: int = 300
) -> dict[str, Any]:
    value = {
        "sandbox_snapshot": capture_sandbox(
            SandboxInput(command_limit=command_limit, timeout=timeout, work_duration=policy)
        ),
        "runner_config_revision": None,
        "search_snapshot": None,
    }
    if policy is not None:
        value["work_duration"] = policy.model_dump(mode="json")
    return value
