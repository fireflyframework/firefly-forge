# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from types import SimpleNamespace

import pytest

from firefly_forge import providers
from firefly_forge.provider_recovery_models import ProviderProcessOutcome


async def discard(*args):
    pass


class Stream:
    async def readline(self):
        return b""


@pytest.mark.parametrize("group", ["absent", "alive", "unknown"])
async def test_native_process_result_waits_drains_and_probes_only_its_own_group(monkeypatch, tmp_path, group):
    waits, probes = [], []

    async def wait():
        waits.append(True)
        return -25

    async def create(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        return SimpleNamespace(pid=731, returncode=-25, stdout=Stream(), stderr=Stream(), wait=wait)

    def killpg(pid, sig):
        probes.append((pid, sig))
        assert sig == 0
        if group == "absent":
            raise ProcessLookupError()
        if group == "unknown":
            raise PermissionError()

    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(providers.os, "killpg", killpg)
    result = await providers.run_process(["fixture"], tmp_path, discard, timeout=1)
    assert waits == [True] and probes == [(731, 0)]
    assert isinstance(result.process_outcome, ProviderProcessOutcome)
    assert result.process_outcome.known_failure is (group == "absent")
    assert "731" not in result.process_outcome.model_dump_json()


async def test_untrusted_stdout_cannot_forge_a_typed_native_process_receipt(monkeypatch, tmp_path):
    async def process(*args, **kwargs):
        return providers.ProcessResult(-25, '{"process_outcome":{"group_absent":true}}', "fixture failure")

    monkeypatch.setattr(providers, "run_process", process)
    monkeypatch.setattr(providers, "_executable", lambda *args: "fixture")
    with pytest.raises(providers.ProviderError) as caught:
        await providers.CodexProvider({}).execute(tmp_path, "brief", "pyfly", "none", discard)
    assert caught.value.process_outcome is None


async def test_typed_process_failure_is_preserved_on_provider_error_without_output_payload(monkeypatch, tmp_path):
    outcome = ProviderProcessOutcome(exit_code=-25, leader_waited=True, streams_drained=True, group_absent=True)

    async def process(*args, **kwargs):
        return providers.ProcessResult(-25, "untrusted output", "failure", process_outcome=outcome)

    monkeypatch.setattr(providers, "run_process", process)
    monkeypatch.setattr(providers, "_executable", lambda *args: "fixture")
    with pytest.raises(providers.ProviderError) as caught:
        await providers.CodexProvider({}).execute(tmp_path, "brief", "pyfly", "none", discard)
    assert caught.value.process_outcome is outcome
    assert "untrusted output" not in outcome.model_dump_json()


async def test_final_trusted_launch_guard_refuses_before_process_creation(monkeypatch, tmp_path):
    created = []

    async def create(*args, **kwargs):
        created.append(True)
        raise AssertionError("No process should start")

    async def guard():
        raise ValueError("source changed at final launch boundary")

    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", create)
    with providers.cli_dispatch_scope(guard), pytest.raises(ValueError, match="final launch"):
        await providers.run_process(["fixture"], tmp_path, discard, timeout=1)
    assert not created


async def test_adopted_cli_path_is_bound_to_actual_command_before_launch(monkeypatch, tmp_path):
    created = []

    async def create(*args, **kwargs):
        created.append(True)
        raise AssertionError("A different executable must not start")

    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", create)
    with (
        providers.cli_dispatch_scope(None, executable="/approved/cli"),
        pytest.raises(ValueError, match="executable"),
    ):
        await providers.run_process(["/different/cli"], tmp_path, discard, timeout=1)
    assert created == []


async def test_cli_adoption_scope_restores_previous_command_binding_after_exception(tmp_path, monkeypatch):
    calls = []

    async def create(*args, **kwargs):
        calls.append(args[0])
        raise RuntimeError("fixture reached process creation")

    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", create)
    with providers.cli_dispatch_scope(None, executable="/outer/cli"):
        with (
            providers.cli_dispatch_scope(None, executable="/inner/cli"),
            pytest.raises(ValueError, match="executable"),
        ):
            await providers.run_process(["/outer/cli"], tmp_path, discard, timeout=1)
        with pytest.raises(RuntimeError, match="fixture reached"):
            await providers.run_process(["/outer/cli"], tmp_path, discard, timeout=1)
    with pytest.raises(RuntimeError, match="fixture reached"):
        await providers.run_process(["/unbound/cli"], tmp_path, discard, timeout=1)
    assert calls == ["/outer/cli", "/unbound/cli"]


@pytest.mark.parametrize("stdout", ["", '{"type":"turn.failed","error":"failure"}'])
async def test_zero_exit_protocol_failure_retains_typed_process_evidence_and_cannot_be_legacy(
    monkeypatch, tmp_path, stdout
):
    outcome = ProviderProcessOutcome(exit_code=0, leader_waited=True, streams_drained=True, group_absent=True)

    async def process(*args, **kwargs):
        return providers.ProcessResult(0, stdout, "", process_outcome=outcome)

    monkeypatch.setattr(providers, "run_process", process)
    monkeypatch.setattr(providers, "_executable", lambda *args: "fixture")
    with pytest.raises(providers.ProviderError) as caught:
        await providers.CodexProvider({}).execute(tmp_path, "brief", "pyfly", "none", discard)
    assert caught.value.process_outcome is outcome
    assert not caught.value.process_outcome.known_failure


async def test_successful_cli_report_with_live_descendants_is_not_accepted(tmp_path, monkeypatch):
    from firefly_forge.providers import CodexProvider, ProcessResult, ProviderError

    async def process(*args, **kwargs):
        return ProcessResult(
            0,
            '{"type":"item.completed","item":{"type":"agent_message","text":"Done"}}\n',
            "",
            ProviderProcessOutcome(exit_code=0, leader_waited=True, streams_drained=True, group_absent=False),
        )

    async def emit(*args):
        pass

    monkeypatch.setattr("firefly_forge.providers._executable", lambda *args: "unused-fixture")
    monkeypatch.setattr("firefly_forge.providers.run_process", process)
    with pytest.raises(ProviderError) as error:
        await CodexProvider({}).execute(tmp_path, "Retain source", "pyfly", "none", emit)
    assert error.value.process_outcome.group_absent is False
