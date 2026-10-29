# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated ownership liveness renews independently of a job deadline."""

import json

import pytest

from firefly_forge.runner_pool_owner import OwnerLiveness, owner_request

KEY = bytes(range(32))
EXECUTION = "a" * 32


def guardian():
    alarms = []
    return OwnerLiveness(EXECUTION, KEY, 30, alarms.append), alarms


def claim(owner):
    return owner.handle({"protocol": "forge-runner-owner-v2", "execution_id": EXECUTION, "operation": "claim"})


def message(operation, sequence, owner):
    return json.loads(owner_request(KEY, EXECUTION, operation, sequence, owner.challenge))


def test_secret_is_claimed_once_before_project_start_and_never_echoed_afterward():
    owner, alarms = guardian()
    assert claim(owner)["key"] == KEY.hex()
    with pytest.raises(ValueError):
        claim(owner)
    result = owner.handle(message("start", 1, owner))
    assert result["state"] == "running" and "key" not in result
    with pytest.raises(ValueError):
        claim(owner)
    assert alarms == [30]


def test_fresh_renewals_allow_arbitrary_job_duration_but_replays_do_not_extend_liveness():
    owner, alarms = guardian()
    claim(owner)
    owner.handle(message("start", 1, owner))
    for sequence in range(2, 7):
        latest = message("renew", sequence, owner)
        result = owner.handle(latest)
        assert result["state"] == "running" and result["sequence"] == sequence
    assert alarms == [30] * 6
    with pytest.raises(ValueError):
        owner.handle(latest)
    with pytest.raises(ValueError):
        owner.handle(message("renew", 2, owner))
    assert alarms == [30] * 6


@pytest.mark.parametrize(
    "change",
    [
        {"signature": "f" * 64},
        {"execution_id": "b" * 32},
        {"sequence": True},
        {"sequence": 0},
        {"operation": "unknown"},
        {"unexpected": "field"},
    ],
)
def test_untrusted_messages_cannot_start_or_keep_code_alive(change):
    owner, alarms = guardian()
    claim(owner)
    with pytest.raises(ValueError):
        owner.handle({**message("start", 1, owner), **change})
    assert alarms == [] and owner.state == "waiting"


def test_finished_work_has_a_collection_grace_not_an_extendable_work_lease():
    owner, alarms = guardian()
    claim(owner)
    owner.handle(message("start", 1, owner))
    owner.finish(7)
    result = owner.handle(message("renew", 2, owner))
    assert result["state"] == "drained" and result["exit_code"] == 7
    assert alarms == [30, 60]


def test_draining_is_finite_even_if_a_live_owner_keeps_polling():
    owner, alarms = guardian()
    claim(owner)
    owner.handle(message("start", 1, owner))
    owner.begin_drain()
    assert owner.handle(message("renew", 2, owner))["state"] == "draining"
    owner.finish(0)
    assert owner.handle(message("renew", 3, owner))["state"] == "drained"
    assert alarms == [30, 60]


def test_controller_accepts_only_the_exact_authenticated_guardian_ack():
    from firefly_forge.runner_pool_owner import owner_reply

    owner, _ = guardian()
    initial = claim(owner)
    assert owner_reply(json.dumps(initial).encode(), EXECUTION, claim=True)["key"] == KEY.hex()
    running = owner.handle(message("start", 1, owner))
    assert owner_reply(json.dumps(running).encode(), EXECUTION, sequence=1)["state"] == "running"
    for changed in ({"execution_id": "b" * 32}, {"sequence": 0}, {"state": "waiting"}, {"key": KEY.hex()}):
        with pytest.raises(ValueError):
            owner_reply(json.dumps({**running, **changed}).encode(), EXECUTION, sequence=1)
    owner.begin_drain()
    owner.finish(0)
    assert (
        owner_reply(json.dumps(owner.handle(message("renew", 2, owner))).encode(), EXECUTION, sequence=2)["exit_code"]
        == 0
    )


def test_owned_launcher_bounds_live_output_before_container_completion():
    import ast
    import io
    from types import SimpleNamespace

    from firefly_forge.runner_pool_launcher import owned_launcher

    program = owned_launcher()
    compile(program, "owned-launcher", "exec")
    output = next(
        node for node in ast.parse(program).body if isinstance(node, ast.FunctionDef) and node.name == "output"
    )
    killed, failed = [], []
    retained = io.BytesIO()
    namespace = {
        "process": SimpleNamespace(pid=42, stdout=io.BytesIO(b"x" * 2048)),
        "config": {"max_output_bytes": 1024},
        "sys": SimpleNamespace(stdout=SimpleNamespace(buffer=retained)),
        "os": SimpleNamespace(killpg=lambda *args: killed.append(args)),
        "signal": SimpleNamespace(SIGKILL=9),
        "output_failed": SimpleNamespace(set=lambda: failed.append(True)),
    }
    exec(compile(ast.Module(body=[output], type_ignores=[]), "actual-owned-output", "exec"), namespace)
    namespace["output"]()
    assert retained.getvalue() == b"x" * 1024 and killed == [(42, 9)] and failed == []


def test_owned_launcher_output_failure_stops_code_and_refuses_an_exit_receipt():
    import ast
    import io
    from types import SimpleNamespace

    from firefly_forge.runner_pool_launcher import owned_launcher

    class Broken:
        def write(self, chunk):
            raise OSError("controlled output transport lost")

    output = next(
        node for node in ast.parse(owned_launcher()).body if isinstance(node, ast.FunctionDef) and node.name == "output"
    )
    killed, failed = [], []
    namespace = {
        "process": SimpleNamespace(pid=42, stdout=io.BytesIO(b"x")),
        "config": {"max_output_bytes": 1024},
        "sys": SimpleNamespace(stdout=SimpleNamespace(buffer=Broken())),
        "os": SimpleNamespace(killpg=lambda *args: killed.append(args)),
        "signal": SimpleNamespace(SIGKILL=9),
        "output_failed": SimpleNamespace(set=lambda: failed.append(True)),
    }
    exec(compile(ast.Module(body=[output], type_ignores=[]), "actual-owned-output", "exec"), namespace)
    namespace["output"]()
    assert failed == [True] and killed == [(42, 9)]


def test_unused_owner_signature_expires_as_a_control_message_without_renewing_work():
    clock = {"value": 0.0}
    alarms = []
    owner = OwnerLiveness(EXECUTION, KEY, 30, alarms.append, clock=lambda: clock["value"])
    initial = claim(owner)
    frame = json.loads(owner_request(KEY, EXECUTION, "start", 1, initial["challenge"]))
    clock["value"] = 30
    with pytest.raises(ValueError):
        owner.handle(frame)
    assert alarms == [] and owner.state == "waiting"


def test_owner_renewals_cross_the_old_hour_ceiling_without_a_work_timer():
    clock = {"value": 0.0}
    alarms = []
    owner = OwnerLiveness(EXECUTION, KEY, 30, alarms.append, clock=lambda: clock["value"])
    claim(owner)
    for sequence in range(1, 2002):
        clock["value"] += 2
        owner.handle(message("start" if sequence == 1 else "renew", sequence, owner))
    assert clock["value"] > 3600 and owner.state == "running"
    assert alarms == [30] * 2001


def test_healthy_owner_can_renew_after_slow_control_within_its_sealed_grace():
    clock = {"value": 0.0}
    alarms = []
    owner = OwnerLiveness(EXECUTION, KEY, 30, alarms.append, clock=lambda: clock["value"])
    claim(owner)
    owner.handle(message("start", 1, owner))
    clock["value"] = 4
    assert owner.handle(message("renew", 2, owner))["state"] == "running"
    assert alarms == [30, 30]
