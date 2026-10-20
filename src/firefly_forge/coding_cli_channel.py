# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit per-invocation official CLI MCP configuration, separate from operator MCP."""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from firefly_forge.coding_check_transport import TOKEN_ENV, CodingCheckServer


class CodingCLIChannel:
    def __init__(self, server: CodingCheckServer, instructions: str) -> None:
        if server.endpoint is None or not server.accepting or server.expires_at_ms <= int(time.time() * 1000):
            raise ValueError("The scoped coding channel must be ready before official CLI configuration")
        self.server, self.instructions = server, instructions
        self.private_file: Path | None = None
        self.private_identity: tuple[int, int] | None = None

    def codex_override(self) -> str:
        timeout = max(1, math.ceil((self.server.expires_at_ms - time.time() * 1000) / 1000))
        return (
            "mcp_servers={forge_coding={url="
            + json.dumps(self.server.endpoint)
            + ',bearer_token_env_var="'
            + TOKEN_ENV
            + '",enabled=true,required=true,enabled_tools=["run_configured_check"],'
            + 'startup_readiness="connection",startup_timeout_sec=5,tool_timeout_sec='
            + str(timeout)
            + "}}"
        )

    def claude_configuration(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="forge-coding-mcp-"))
        directory.chmod(0o700)
        path = directory / "channel.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(
                {
                    "mcpServers": {
                        "forge_coding": {
                            "type": "http",
                            "url": self.server.endpoint,
                            "headers": {"Authorization": "Bearer " + self.server.token},
                        }
                    }
                },
                stream,
            )
        metadata = path.lstat()
        self.private_file, self.private_identity = path, (metadata.st_dev, metadata.st_ino)
        return path

    def settled(self, outcome: Any) -> None:
        if self.private_file is None or outcome is None:
            return
        if not (outcome.leader_waited and outcome.streams_drained and outcome.group_absent):
            return
        path = self.private_file
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != self.private_identity:
            raise ValueError("The owned private coding channel file identity changed")
        path.unlink()
        path.parent.rmdir()
        self.private_file = None
