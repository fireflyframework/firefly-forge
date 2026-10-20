# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real local subprocesses and strict scoped check inputs; no model or database."""

import json
import os
import sys

import pytest
from pydantic import ValidationError

from firefly_forge.providers import run_process


@pytest.mark.asyncio
async def test_coding_runtime_token_is_redacted_before_stream_emit_and_retained_result(tmp_path):
    token = "fixture-coding-runtime-token-72f39db8"
    events = []

    async def emit(kind, value):
        events.append((kind, value))

    program = (
        "import json,os,sys;secret=os.environ['FORGE_CODING_RUNTIME_TOKEN'];"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':secret}}));"
        "print(secret,file=sys.stderr)"
    )
    command = [sys.executable, "-B", "-s", "-c", program]
    result = await run_process(
        command,
        tmp_path,
        emit,
        timeout=5,
        env={"PATH": "/usr/bin:/bin", "FORGE_CODING_RUNTIME_TOKEN": token},
    )
    assert result.returncode == 0
    assert token not in result.stdout + result.stderr + json.dumps(events)
    assert result.process_outcome.model_dump() == {
        "exit_code": 0,
        "leader_waited": True,
        "streams_drained": True,
        "group_absent": True,
    }
    assert token not in json.dumps(command)
    assert "FORGE_CODING_RUNTIME_TOKEN" not in os.environ


def test_configured_check_request_cannot_choose_argv_or_runtime_authority():
    from firefly_forge.coding_check_models import ConfiguredCheckRequest

    accepted = ConfiguredCheckRequest(command_index=0, request_id="a" * 32)
    assert accepted.model_dump() == {"command_index": 0, "request_id": "a" * 32}
    assert ConfiguredCheckRequest(command_index=19, request_id="b" * 32).command_index == 19
    for untrusted in (
        {"argv": ["python", "-c", "print('outside')"]},
        {"cwd": "/tmp"},
        {"env": {"TOKEN": "fixture"}},
        {"timeout": 3600},
        {"run_id": "b" * 32},
        {"provider": "agentic"},
        {"command_index": -1},
        {"command_index": 20},
        {"command_index": True},
    ):
        with pytest.raises(ValidationError):
            ConfiguredCheckRequest.model_validate({"command_index": 0, "request_id": "a" * 32, **untrusted})


def test_configured_check_resolves_only_an_allowed_index_in_the_actual_frozen_list():
    from firefly_forge.coding_check_models import ConfiguredCheckRequest, configured_command

    commands = [{"argv": ["python", "check.py", str(index)], "timeout": 30} for index in range(20)]
    request = ConfiguredCheckRequest(command_index=19, request_id="c" * 32)
    command = configured_command(commands, [19], request)
    assert command.model_dump() == {"argv": ["python", "check.py", "19"], "timeout": 30}
    with pytest.raises(ValueError, match="configured scope"):
        configured_command(commands[:3], [19], request)
    with pytest.raises(ValueError, match="configured scope"):
        configured_command(commands, [0], request)
    command.argv.append("caller-mutated")
    assert commands[19]["argv"] == ["python", "check.py", "19"]


def test_coding_check_grant_requires_distinct_configured_indices_and_explicit_future_request():
    from firefly_forge.coding_check_models import CodingCheckGrantInput

    value = dict(
        request_id="a" * 32,
        recovery_request_id="b" * 32,
        candidate_digest="c" * 64,
        expected_revision=0,
        command_indices=[0, 19],
        call_limit=2,
        valid_seconds=300,
        reason="Review the explicitly selected configured checks.",
    )
    assert CodingCheckGrantInput.model_validate(value).command_indices == [0, 19]
    for change in (
        {"command_indices": []},
        {"command_indices": [0, 0]},
        {"command_indices": [20]},
        {"command_indices": [True]},
        {"recovery_request_id": None},
        {"call_limit": 21},
        {"reason": " "},
        {"argv": ["other"]},
    ):
        with pytest.raises(ValidationError):
            CodingCheckGrantInput.model_validate(value | change)


def test_configured_check_output_zero_budget_is_empty_and_unicode_result_stays_byte_bounded():
    from firefly_forge.coding_check_runtime import bounded_check_output, bounded_check_result

    assert bounded_check_output("never include the whole output", 0) == ""
    assert bounded_check_output("x😀é", 2) == "é"
    assert bounded_check_output("😀", 3) == ""
    for maximum in (1024, 2048, 2050, 4096):
        result = bounded_check_result({"exit_code": 0, "source_before": "a" * 64}, "😀é" * 20000, maximum)
        assert len(json.dumps(result).encode()) <= maximum
        assert "\ufffd" not in result["output"]
    with pytest.raises(ValueError):
        bounded_check_result({"metadata": "a" * 2000}, "outside", 1024)
