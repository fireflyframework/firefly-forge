# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Run ownership renews independently of work age and cannot cross authority scopes."""

import importlib
import importlib.util

import pytest
from pydantic import ValidationError

PROTOCOL = "forge-run-owned-liveness-v1"


def module():
    assert importlib.util.find_spec("firefly_forge.runner_run_liveness") is not None, "Run-scoped ownership is missing"
    return importlib.import_module("firefly_forge.runner_run_liveness")


def binding():
    return module().RunLivenessBindingV1(
        task_id="verify",
        attempt=2,
        snapshot_digest="a" * 64,
        actor_digest="b" * 64,
        run_cut_digest="c" * 64,
        factory_fence_digest="d" * 64,
        worker_epoch="e" * 32,
    )


def lease():
    clock = {"now": 0.0}
    value = module().RunLease("f" * 64, binding(), "1" * 32, 30, clock=lambda: clock["now"])
    return value, clock


def renewal(value):
    return module().RunRenewalV1.model_validate(
        {**value.ack(), "protocol": "forge-run-renewal-v1", "sequence": value.sequence + 1}
    )


def test_healthy_authorized_renewals_have_no_work_age_ceiling():
    value, clock = lease()
    for number in range(1, 302):
        clock["now"] += 20
        ack = value.renew(renewal(value))
        assert ack["sequence"] == number
        assert 29 < value.remaining() <= 30
    assert clock["now"] > 3600
    assert set(ack) == {"protocol", "invocation_digest", "worker_epoch", "controller_epoch", "sequence", "challenge"}


@pytest.mark.parametrize(
    "change",
    [
        {"invocation_digest": "0" * 64},
        {"worker_epoch": "2" * 32},
        {"controller_epoch": "3" * 32},
        {"sequence": 2},
        {"challenge": "4" * 32},
    ],
)
def test_foreign_or_out_of_order_renewal_never_extends_the_current_run(change):
    value, clock = lease()
    request = renewal(value).model_dump(mode="json")
    before = value.ack()
    clock["now"] = 20
    with pytest.raises(ValueError):
        value.renew(module().RunRenewalV1.model_validate({**request, **change}))
    assert value.ack() == before and value.remaining() == 10


def test_expired_or_revoked_run_cannot_be_revived_by_a_live_factory():
    value, clock = lease()
    request = renewal(value)
    clock["now"] = 30
    with pytest.raises(ValueError, match="expired"):
        value.renew(request)
    with pytest.raises(ValueError):
        value.remaining()
    other, _ = lease()
    other.stop()
    with pytest.raises(ValueError, match="closed"):
        other.renew(renewal(other))


def test_replayed_renewal_cannot_refresh_ownership_and_new_controller_has_no_authority():
    value, clock = lease()
    request = renewal(value)
    value.renew(request)
    clock["now"] = 20
    with pytest.raises(ValueError):
        value.renew(request)
    assert value.remaining() == 10
    with pytest.raises(ValueError):
        module().validate_run_ack(value.ack(), "f" * 64, binding(), controller_epoch="9" * 32, sequence=1)


def test_control_delay_cannot_commit_a_renewal_after_the_previous_owner_expired():
    ticks = iter([0.0, 29.0, 31.0])
    value = module().RunLease("f" * 64, binding(), "1" * 32, 30, clock=lambda: next(ticks))
    before = value.ack()
    with pytest.raises(ValueError, match="expired"):
        value.renew(renewal(value))
    assert value.ack() == before


@pytest.mark.parametrize(
    "change", [{"attempt": True}, {"attempt": 0}, {"task_id": "deploy"}, {"worker_epoch": "short"}, {"unexpected": 1}]
)
def test_run_binding_is_strict_and_cannot_grant_a_foreign_stage(change):
    with pytest.raises(ValidationError):
        module().RunLivenessBindingV1.model_validate({**binding().model_dump(mode="json"), **change})


def test_profile_selection_is_explicit_and_legacy_serialized_bytes_stay_exact():
    from test_runner_pools import invocation, profile

    from firefly_forge.runner_pool_models import RunnerInvocation
    from firefly_forge.work_duration_models import WorkDurationPolicyV2

    old = profile()
    assert "run_liveness" not in old.model_dump(mode="json")
    assert old.revision == "b8dca8854ba8c41f6b3d9707787716e5ee84570cf33b6264f0c347b09f1e92e8"
    assert "run_liveness" not in invocation(old).model_dump(mode="json")
    selected = profile(work_duration=WorkDurationPolicyV2(timeout_seconds=864000), run_liveness=PROTOCOL)
    request = invocation(
        selected, timeout=864000, work_duration=WorkDurationPolicyV2(timeout_seconds=864000), run_liveness=binding()
    )
    selected.validate_invocation(request)
    with pytest.raises(ValueError):
        selected.validate_invocation(
            RunnerInvocation.model_validate({**request.model_dump(mode="json"), "run_liveness": None})
        )
