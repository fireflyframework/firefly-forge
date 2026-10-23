# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Restrict source readers without changing ordinary configured-check channels."""

import time
import tomllib

from firefly_forge.coding_check_transport import CodingCheckServer
from firefly_forge.coding_cli_channel import CodingCLIChannel


def test_default_coding_channel_keeps_ordinary_mcp_exposure():
    async def read(_request):
        raise AssertionError("Only configuration is inspected")

    server = CodingCheckServer(None, source_callback=read, expires_at_ms=int(time.time() * 1000) + 30000)
    server.endpoint = "http://127.0.0.1:19433/mcp"
    config = tomllib.loads(CodingCLIChannel(server, "Public fixture").codex_override())
    assert "omit_tools_from" not in config["mcp_servers"]["forge_coding"]
