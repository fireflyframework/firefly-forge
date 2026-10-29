# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recovery routes preserve authentication while sanitizing retained-record conflicts."""

from types import SimpleNamespace

import pytest

from firefly_forge.invalid_read_continuity_controllers import InvalidReadContinuityController
from firefly_forge.project_access import AccessDenied


@pytest.mark.parametrize("error", [ValueError("private retained input"), KeyError("private retained input")])
async def test_recovery_conflicts_never_export_original_values(error):
    async def candidate(*args):
        raise error

    controller = InvalidReadContinuityController(SimpleNamespace(recoveries=SimpleNamespace(candidate=candidate)))
    with pytest.raises(type(error)) as caught:
        await controller.recovery_candidate("a" * 32, "b" * 32)
    assert "private retained input" not in str(caught.value)


async def test_recovery_route_does_not_replace_authorization_failure():
    async def candidate(*args):
        raise AccessDenied("Current authority unavailable")

    controller = InvalidReadContinuityController(SimpleNamespace(recoveries=SimpleNamespace(candidate=candidate)))
    with pytest.raises(AccessDenied):
        await controller.recovery_candidate("a" * 32, "b" * 32)
