# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Only the explicit current run-owned protocol can admit a null work deadline."""

import pytest
from test_run_owned_authority_host import case
from test_run_owned_controller_host import initialized, renewal

from firefly_forge.runner_factory_models import FactoryPoolSelection, uncapped_pool_reason
from firefly_forge.work_duration_models import WorkDurationPolicyV2


def test_selected_uncapped_policy_is_available_without_inventing_an_administrator_deadline():
    _, _, run, _, _ = case(work_cap=None)
    selection = FactoryPoolSelection.model_validate(run["sandbox_snapshot"]["configuration"])
    assert selection.work_duration == selection.profile.work_duration == WorkDurationPolicyV2()
    assert uncapped_pool_reason(selection) is None


def test_run_owned_profile_cannot_capture_a_legacy_duration_selection():
    _, _, run, _, _ = case(work_cap=None)
    configuration = {**run["sandbox_snapshot"]["configuration"], "work_duration": None}
    with pytest.raises(ValueError, match="duration"):
        FactoryPoolSelection.model_validate(configuration)


async def test_controller_admits_and_renews_selected_uncapped_work_under_its_original_epoch(monkeypatch):
    executor, invocation, fence, clock, _, _, effects, _, _, _ = await initialized(monkeypatch, work_cap=None)
    result = await executor.accept(invocation, fence, b"fixture", b"")
    assert invocation.timeout is None and invocation.work_duration == WorkDurationPolicyV2()
    assert effects == ["daemon", "prepare", "retain", "submit"]
    ack = result["run_lease"]
    for _ in range(5):
        clock["now"] += 20
        ack = await executor.renew_run(invocation, fence, renewal(ack))
    assert ack["sequence"] == 5 and not executor.run_leases[invocation.execution_id].closed
