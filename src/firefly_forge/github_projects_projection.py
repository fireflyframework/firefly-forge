# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A run outcome is scoped progress, not authority to declare its product complete."""

from typing import Any

from firefly_forge.github_projects_models import BoardRunProjection


def project_run(run: dict[str, Any]) -> BoardRunProjection:
    operation = run.get("operation", "build")
    source = run.get("source_checkpoint_snapshot") or {}
    scope = (
        "checkpoint_verification"
        if operation == "test" and source
        else "checkpoint_maintenance"
        if operation == "maintain" and source
        else "verification"
        if operation == "test"
        else "maintenance"
        if operation == "maintain"
        else "implementation"
    )
    state = run["status"]
    if state == "succeeded":
        # Native run receipts certify their admitted unit. Neither a test child nor
        # an implementation unit grants whole-product acceptance authority.
        state = "paused"
    if state == "campaign_blocked":
        state = "paused"
    return BoardRunProjection.model_validate(
        {
            "run_id": run["id"],
            "state": state,
            "operation": operation,
            "delivery_scope": scope,
            "source_checkpoint": source.get("digest"),
            "product_completion_verified": False,
        }
    )
