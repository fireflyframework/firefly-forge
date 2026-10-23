# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded no-model configuration exchange with the official Codex app server."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Native metadata contains duplicate keys")
        result[key] = value
    return result


class ConfigurationRPC:
    """Keep stdin open until initialization and both metadata replies settle.

    Raw configuration stays in memory. Errors expose only fixed predicates,
    never response bodies, configuration values or native error messages.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.results: dict[int, dict[str, Any]] = {}
        self._queue: asyncio.Queue[int] = asyncio.Queue(maxsize=64)
        self._bytes = 0
        self._packets = 0

    def receive_stdout(self, raw: bytes) -> None:
        self._bytes += len(raw)
        self._packets += 1
        if self._bytes > 1_000_000 or self._packets > 64:
            raise ValueError("Native metadata exceeds its response bounds")
        try:
            row = json.loads(raw, object_pairs_hook=_unique_keys)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Native metadata response is invalid") from None
        if not isinstance(row, dict):
            raise ValueError("Native metadata response must be an object")
        if "id" not in row:
            if not isinstance(row.get("method"), str) or "result" in row or "error" in row:
                raise ValueError("Native metadata notification is invalid")
            return
        identity = row["id"]
        if (
            type(identity) is not int
            or identity not in (1, 2, 3)
            or identity in self.results
            or "error" in row
            or not isinstance(row.get("result"), dict)
        ):
            raise ValueError("Native metadata reply is missing, repeated or unsuccessful")
        self.results[identity] = row["result"]
        self._queue.put_nowait(identity)

    async def exchange(self, writer: asyncio.StreamWriter) -> None:
        async def send(packet: dict[str, Any]) -> None:
            writer.write((json.dumps(packet) + "\n").encode())
            await writer.drain()

        await send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "forge_source_policy", "version": "2"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        if await self._queue.get() != 1:
            raise ValueError("Native metadata initialization did not precede its replies")
        await send({"method": "initialized", "params": {}})
        await send({"id": 2, "method": "config/read", "params": {"includeLayers": False, "cwd": str(self.workspace)}})
        await send({"id": 3, "method": "configRequirements/read", "params": {}})
        if {await self._queue.get(), await self._queue.get()} != {2, 3}:
            raise ValueError("Native metadata replies are incomplete")
        writer.close()
