# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Conversation capability reads and deliberate recovery preserve the native contract."""

import contextlib

import pytest
from click.testing import CliRunner
from test_client_task_profiles import connection

from firefly_forge import cli
from firefly_forge.mcp_server import create_server

REPOSITORY, CONVERSATION, TURN = "1" * 32, "2" * 32, "3" * 32


def recovery():
    return {"request_id": "a" * 32, "expected_epoch": 2, "legacy_confirmed_stopped": False}


def test_sdk_capabilities_and_recovery_preserve_scoped_contract_without_turn_dispatch():
    seen = []
    with connection(seen) as client:
        client.assistant_capabilities(REPOSITORY, CONVERSATION)
        client.recover_assistant_turn(REPOSITORY, CONVERSATION, TURN, recovery())
        with pytest.raises(ValueError):
            client.recover_assistant_turn(REPOSITORY, CONVERSATION, TURN, {**recovery(), "expected_epoch": -1})
    route = f"/api/v1/repositories/{REPOSITORY}/assistant/conversations/{CONVERSATION}"
    assert seen == [("GET", route + "/capabilities", None), ("POST", route + f"/turns/{TURN}/recover", recovery())]


def test_cli_recovery_requires_explicit_epoch_and_never_assumes_legacy_stopped(monkeypatch):
    seen = []
    with connection(seen) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        runner = CliRunner()
        assert runner.invoke(cli.main, ["assistant", "capabilities", REPOSITORY, CONVERSATION]).exit_code == 0
        arguments = ["assistant", "recover", REPOSITORY, CONVERSATION, TURN, "--request-id", "a" * 32]
        assert runner.invoke(cli.main, arguments).exit_code != 0 and len(seen) == 1
        result = runner.invoke(cli.main, arguments + ["--expected-epoch", "2"])
        assert result.exit_code == 0, result.output
        assert seen[-1][2] == recovery()


async def test_mcp_capability_and_recovery_tools_preserve_explicit_request(monkeypatch):
    seen = []
    with connection(seen) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        server = create_server()
        await server.call_tool(
            "get_product_conversation_capabilities", {"repository_id": REPOSITORY, "conversation_id": CONVERSATION}
        )
        await server.call_tool(
            "recover_product_design_turn",
            {"repository_id": REPOSITORY, "conversation_id": CONVERSATION, "turn_id": TURN, **recovery()},
        )
    assert len(seen) == 2 and seen[0][0] == "GET"
    assert seen[1][1].endswith(f"/turns/{TURN}/recover") and seen[1][2] == recovery()
