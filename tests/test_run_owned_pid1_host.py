# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PID1 control binds the run grant; authenticated status polling cannot renew it."""

import importlib
import importlib.util
import json

import pytest

KEY, EXECUTION, BINDING = bytes(range(32)), "a" * 32, "b" * 64


def module():
    assert importlib.util.find_spec("firefly_forge.runner_run_owner") is not None, "Run-bound PID1 renewal is missing"
    return importlib.import_module("firefly_forge.runner_run_owner")


def owner():
    alarms, clock = [], {"now": 0.0}
    value = module().RunOwnerLiveness(EXECUTION, BINDING, KEY, 30, alarms.append, clock=lambda: clock["now"])
    value.handle(
        {
            "protocol": "forge-runner-owner-v3",
            "execution_id": EXECUTION,
            "binding_digest": BINDING,
            "operation": "claim",
        }
    )
    return value, alarms, clock


def message(value, operation, sequence, grant, ttl):
    return json.loads(
        module().run_owner_request(KEY, EXECUTION, BINDING, operation, sequence, value.challenge, grant, ttl)
    )


def test_status_polling_never_extends_the_last_authorized_run_grant():
    value, alarms, clock = owner()
    value.handle(message(value, "start", 1, 0, 10))
    for sequence in range(2, 7):
        clock["now"] += 1
        reply = value.handle(message(value, "observe", sequence, 0, None))
        assert reply["sequence"] == sequence
    assert alarms == [10]
    clock["now"] = 10
    with pytest.raises(ValueError, match="expired"):
        value.handle(message(value, "renew", 7, 1, 30))
    assert alarms == [10]


def test_only_a_fresh_run_grant_can_renew_within_the_remaining_owner_ttl():
    value, alarms, clock = owner()
    value.handle(message(value, "start", 1, 0, 30))
    clock["now"] = 20
    with pytest.raises(ValueError):
        value.handle(message(value, "renew", 2, 0, 30))
    assert alarms == [30]
    value.handle(message(value, "observe", 2, 0, None))
    value.handle(message(value, "renew", 3, 1, 7))
    assert alarms == [30, 7]
    with pytest.raises(ValueError):
        value.handle(message(value, "renew", 4, 2, 31))


def test_delayed_signed_grant_cannot_restart_its_remaining_ttl_at_receipt():
    value, alarms, clock = owner()
    value.handle(message(value, "start", 1, 0, 30))
    clock["now"] = 5
    value.handle(message(value, "observe", 2, 0, None))
    delayed = message(value, "renew", 3, 1, 10)
    clock["now"] = 20
    with pytest.raises(ValueError, match="expired"):
        value.handle(delayed)
    assert alarms == [30]
    assert value.grant_sequence == 0


@pytest.mark.parametrize(
    "change",
    [
        {"binding_digest": "c" * 64},
        {"execution_id": "d" * 32},
        {"grant_sequence": True},
        {"ttl_seconds": True},
        {"signature": "f" * 64},
        {"sequence": 2},
        {"unexpected": 1},
    ],
)
def test_foreign_replayed_or_untyped_owner_frames_cannot_start_project_code(change):
    value, alarms, _ = owner()
    with pytest.raises(ValueError):
        value.handle({**message(value, "start", 1, 0, 30), **change})
    assert alarms == [] and value.state == "waiting"


def test_drain_status_is_finite_and_never_returns_the_private_key():
    value, alarms, _ = owner()
    value.handle(message(value, "start", 1, 0, 30))
    value.begin_drain()
    value.finish(3)
    reply = value.handle(message(value, "observe", 2, 0, None))
    assert reply["state"] == "drained" and reply["exit_code"] == 3 and "key" not in reply
    assert alarms == [30, 60]


@pytest.mark.parametrize(
    "change",
    [
        None,
        {"binding_digest": "f" * 64},
        {"grant_sequence": 3},
        {"protocol": "forge-runner-owner-status-v2"},
        {"unexpected": 1},
    ],
)
def test_controller_receipt_requires_exact_version_binding_and_grant_sequence(change):
    value, _, _ = owner()
    assert hasattr(module(), "run_owner_reply"), "Run-bound owner acknowledgement validator is missing"
    reply = value.handle(message(value, "start", 1, 0, 30))
    if change is not None:
        with pytest.raises(ValueError):
            module().run_owner_reply(
                json.dumps({**reply, **change}).encode(), EXECUTION, BINDING, sequence=1, grant_sequence=0
            )
    else:
        assert (
            module().run_owner_reply(json.dumps(reply).encode(), EXECUTION, BINDING, sequence=1, grant_sequence=0)
            == reply
        )
