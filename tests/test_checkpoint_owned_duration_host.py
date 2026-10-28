# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned duration checkpoints preserve legacy bytes and native custody."""

import copy

import pytest
from test_agentic_checkpoint_storage_host import setup
from test_bridge_inflight_cancel import RUN

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2, InvocationBinding
from firefly_forge.checkpoint_work_duration import (
    binding_deadline,
    binding_remaining,
    checkpoint_payload,
    invocation_binding,
    selected_work_duration,
    validate_binding_duration,
)
from firefly_forge.work_duration_models import WorkDurationPolicyV2


def legacy_binding():
    return dict(
        invocation_id="a" * 32,
        ordinal=1,
        predecessor=None,
        brief="Fictional owned work",
        backend="python",
        frontend="react",
        round=1,
        deadline_ms=2000000000000,
        grants=["read_file"],
        contract="b" * 64,
        run_cut="c" * 64,
        workspace={"path": "owned", "head": "a", "metadata": "b", "source": "c"},
    )


def owned_binding(timeout=None):
    value = legacy_binding()
    value.pop("deadline_ms")
    return {
        **value,
        "protocol": "forge-owned-checkpoint-v3",
        "work_duration": WorkDurationPolicyV2(timeout_seconds=timeout).model_dump(),
        "work_started_at_ms": 1000,
        "work_deadline_ms": None if timeout is None else 1000 + timeout * 1000,
    }


def duration_run(timeout=None):
    policy = WorkDurationPolicyV2(timeout_seconds=timeout).model_dump()
    return {"work_duration": policy, "sandbox_snapshot": {"configuration": {"work_duration": copy.deepcopy(policy)}}}


def test_legacy_binding_and_envelope_remain_exact_without_duration_fields():
    value = legacy_binding()
    binding = invocation_binding(value)
    assert type(binding) is InvocationBinding and binding.model_dump() == value
    payload = {
        "version": 2,
        "request_id": "fbr1_" + "a" * 32,
        "intent_digest": "b" * 64,
        "checkpoint_digest": "c" * 64,
        "snapshot": "fictional legacy serialized history",
        "invocation": value,
    }
    assert (
        checkpoint_payload(payload).model_dump() == CheckpointPayloadV2.model_validate(payload).model_dump() == payload
    )
    assert selected_work_duration({}) is None
    assert binding_deadline(binding) == value["deadline_ms"]


def test_owned_unbounded_binding_has_no_fake_deadline_or_legacy_downgrade(monkeypatch):
    value = owned_binding()
    binding = invocation_binding(value)
    assert binding.model_dump() == value and binding_deadline(binding) is None
    monkeypatch.setattr("firefly_forge.checkpoint_work_duration.time.time", lambda: 10**12)
    assert binding_remaining(binding) is None
    with pytest.raises(ValueError):
        InvocationBinding.model_validate(value)
    with pytest.raises(ValueError):
        invocation_binding({**value, "deadline_ms": 2**53 - 1})
    validate_binding_duration(duration_run(), binding)
    with pytest.raises(ValueError):
        validate_binding_duration({}, binding)


@pytest.mark.parametrize("timeout", [1, 300, 10**9])
def test_explicit_cap_binds_exact_start_and_cannot_be_reset(timeout):
    value = owned_binding(timeout)
    binding = invocation_binding(value)
    assert binding_remaining(binding, now_ms=1000) == timeout
    with pytest.raises(ValueError, match="expired"):
        binding_remaining(binding, now_ms=value["work_deadline_ms"])
    with pytest.raises(ValueError):
        invocation_binding({**value, "work_deadline_ms": value["work_deadline_ms"] + 1})
    with pytest.raises(ValueError):
        validate_binding_duration(duration_run(timeout + 1), binding)


@pytest.mark.parametrize("change", ["missing_top", "missing_frozen", "different", "bool", "unknown"])
def test_new_selector_must_equal_its_frozen_policy(change):
    run = duration_run(12)
    if change == "missing_top":
        run.pop("work_duration")
    elif change == "missing_frozen":
        run["sandbox_snapshot"]["configuration"].clear()
    elif change == "different":
        run["work_duration"]["timeout_seconds"] = 13
    elif change == "bool":
        run["work_duration"]["timeout_seconds"] = True
    else:
        run["work_duration"]["extra"] = True
    with pytest.raises(ValueError):
        selected_work_duration(run)


async def test_new_checkpoint_storage_preserves_codec_and_current_ownership(tmp_path, monkeypatch):
    store, journal, manager, service, scope, descriptor, captured, body, response = await setup(tmp_path, monkeypatch)
    store.run.data.update(duration_run())
    run = await store.get_run(RUN)
    task = await journal.intent(RUN, "implement")
    calls = []

    async def guard():
        calls.append(True)

    async def context():
        return owned_binding()

    current = service.scope(run, task, capture=lambda wire: captured, capture_context=context, assert_ownership=guard)
    intent = await current.admit(descriptor, body)
    record = next(row.data for row in journal.records.rows if row.kind == "agent_checkpoint")
    assert record["payload"]["version"] == 3
    assert record["payload"]["snapshot"] == captured.payload.decode()
    assert checkpoint_payload(record["payload"]).invocation.model_dump() == owned_binding()
    await current.response(intent, response)
    assert calls and len([r for r in journal.records.rows if r.kind == "agent_consume"]) == 1


async def restore_guard_case(timeout=None):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from firefly_forge.agent_restores import AgentRestores
    from firefly_forge.agentic_checkpoint_invocation import invocation_cut
    from firefly_forge.journal import digest
    from firefly_forge.project_access import AccessDenied

    run = {**duration_run(timeout), "id": RUN, "agent": "bridge", "status": "developing"}
    bound = owned_binding(timeout)
    bound["run_cut"] = invocation_cut(run)
    request = {
        "completion_request_id": "fbr1_" + "a" * 32,
        "actor": {},
        "input": {
            "request_id": "d" * 32,
            "expected_checkpoint_digest": "a" * 64,
            "expected_intent_digest": "b" * 64,
            "source": {"kind": "http_response", "record_digest": "c" * 64},
        },
    }
    ticket = {
        "id": "d" * 32,
        "run_id": RUN,
        "kind": "agent_restore",
        "status": "executing",
        "generation": "e" * 32,
        "request": request,
        "request_digest": digest(request),
        "owner_pid": 123,
    }
    state = SimpleNamespace(
        run=run,
        current=copy.deepcopy(ticket),
        owned=True,
        authorized=True,
        calls=[],
        task=SimpleNamespace(status="running", attempt=1),
    )

    async def ownership():
        state.calls.append("ownership")
        if not state.owned:
            raise ValueError("Controlled owner lost")

    async def current(*args):
        return state.current

    async def get_run(*args):
        return state.run

    async def access(*args):
        state.calls.append("authority")
        if not state.authorized:
            raise AccessDenied("Controlled authority revoked")

    @asynccontextmanager
    async def unit():
        yield None

    async def checkpoint(*args):
        return {
            "digest": "a" * 64,
            "payload": {
                "version": 3,
                "request_id": request["completion_request_id"],
                "intent_digest": "b" * 64,
                "checkpoint_digest": "c" * 64,
                "snapshot": "controlled guard metadata",
                "invocation": bound,
            },
        }

    async def intent(*args):
        return {"digest": "b" * 64, "payload": {"task_key": "f" * 32, "task_attempt": 1}}

    async def task(*args):
        state.calls.append("task")
        return state.task

    service = object.__new__(AgentRestores)
    service.store = SimpleNamespace(assert_ownership=ownership, get_run=get_run, _unit=unit, _ownership_pid=123)
    service.requests = SimpleNamespace(get=current)
    service.access = access
    service.checkpoints = SimpleNamespace(_record=checkpoint)
    service.completions = SimpleNamespace(_record=intent)
    service.journal = SimpleNamespace(_effect_task=task)
    return service, ticket, state


async def test_unbounded_restore_guard_still_checks_native_owner_actor_and_task():
    service, ticket, state = await restore_guard_case()
    await service.guard(ticket)
    assert state.calls == ["ownership", "authority", "task"]


@pytest.mark.parametrize("drift", ["owner", "generation", "pid", "authority", "cancel", "task", "policy"])
async def test_unbounded_restore_refuses_every_existing_execution_fence(drift):
    from firefly_forge.project_access import AccessDenied

    service, ticket, state = await restore_guard_case()
    if drift == "owner":
        state.owned = False
    elif drift == "generation":
        state.current["generation"] = "0" * 32
    elif drift == "pid":
        state.current["owner_pid"] = 456
    elif drift == "authority":
        state.authorized = False
    elif drift == "cancel":
        state.run["cancel_request"] = {"status": "requested"}
    elif drift == "task":
        state.task.attempt = 2
    else:
        state.run["work_duration"]["timeout_seconds"] = 1
    with pytest.raises((ValueError, AccessDenied)):
        await service.guard(ticket)


async def test_chosen_work_cap_is_rechecked_by_active_restore_guard(monkeypatch):
    service, ticket, state = await restore_guard_case(1)
    monkeypatch.setattr("firefly_forge.checkpoint_work_duration.time.time", lambda: 10)
    with pytest.raises(ValueError, match="expired"):
        await service.guard(ticket)


@pytest.mark.parametrize("timeout", [None, 5000])
async def test_real_invocation_retains_its_work_policy_across_checkpoint_capture(tmp_path, monkeypatch, timeout):
    from types import SimpleNamespace

    from firefly_forge import agentic_checkpoint_invocation as module
    from firefly_forge.agentic_checkpoint_invocation import CheckpointInvocation
    from firefly_forge.checkpoint_work_duration import same_owned_duration

    run = {**duration_run(timeout), "id": RUN, "attempt": 1}
    calls = []

    async def guard():
        calls.append(True)

    async def source(*args):
        return {"path": "owned", "head": "a", "metadata": "b", "source": "c"}

    monkeypatch.setattr(module, "workspace_cut", source)
    monkeypatch.setattr(module, "runtime_contract", lambda: "b" * 64)
    invocation = CheckpointInvocation(SimpleNamespace(), SimpleNamespace(), run, {}, tmp_path, guard)
    invocation.service.scope = lambda *args, **kwargs: kwargs
    context = SimpleNamespace(
        allowed_tools=frozenset({"read_file"}),
        qa_references=(),
        work_duration=WorkDurationPolicyV2(timeout_seconds=timeout),
    )
    boundary = SimpleNamespace(capture=lambda wire: SimpleNamespace(digest="e" * 64))
    scope = invocation.attach(
        boundary, "Fictional owned work", "python", "react", context, {"FORGE_PROVIDER_TIMEOUT": "1"}
    )
    first = invocation.binding
    assert (invocation.remaining() is None) == (timeout is None)
    scope["capture"](b"controlled capture")
    captured = invocation_binding(await scope["capture_context"]())
    assert same_owned_duration(first, captured) and len(calls) == 2
    assert "deadline_ms" not in captured.model_dump()
    scope["capture"](b"next controlled capture")
    again = invocation_binding(await scope["capture_context"]())
    assert same_owned_duration(captured, again) and again.ordinal == captured.ordinal + 1
    changed = {
        **again.model_dump(),
        "work_started_at_ms": again.work_started_at_ms + 1,
        "work_deadline_ms": None if timeout is None else again.work_deadline_ms + 1,
    }
    assert same_owned_duration(captured, invocation_binding(changed)) is False


def test_owned_binding_cannot_remove_its_chosen_deadline_by_assignment():
    binding = invocation_binding(owned_binding(20))
    with pytest.raises(ValueError):
        binding.work_deadline_ms = None
