# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Checkpoint routes hide retained payloads while preserving authorization failures."""

from types import SimpleNamespace

import pytest

from firefly_forge.project_access import AccessDenied


@pytest.mark.parametrize("operation", ["candidate", "request", "status"])
@pytest.mark.parametrize("error", [ValueError("private checkpoint wire"), KeyError("private checkpoint wire")])
async def test_checkpoint_route_conflicts_do_not_export_private_checkpoint_values(operation, error):
    from firefly_forge.checkpoint_continuation_controllers import CheckpointContinuationController

    async def fail(*args):
        raise error

    controller = CheckpointContinuationController(SimpleNamespace(checkpoints=SimpleNamespace(**{operation: fail})))
    arguments = ("a" * 32, "b" * 32, "c" * 32, {}) if operation == "request" else ("a" * 32, "b" * 32)
    with pytest.raises(type(error)) as caught:
        await getattr(controller, operation)(*arguments)
    assert "private checkpoint wire" not in str(caught.value)


@pytest.mark.parametrize("operation", ["candidate", "request", "status"])
async def test_checkpoint_route_preserves_current_authorization_failure(operation):
    from firefly_forge.checkpoint_continuation_controllers import CheckpointContinuationController

    async def fail(*args):
        raise AccessDenied("Current authority unavailable")

    controller = CheckpointContinuationController(SimpleNamespace(checkpoints=SimpleNamespace(**{operation: fail})))
    arguments = ("a" * 32, "b" * 32, "c" * 32, {}) if operation == "request" else ("a" * 32, "b" * 32)
    with pytest.raises(AccessDenied):
        await getattr(controller, operation)(*arguments)
