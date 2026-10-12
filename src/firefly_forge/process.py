# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded subprocess execution; cancellation terminates the process group."""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessResult:
    argv: list[str]
    exit_code: int
    output: str


async def execute(argv: list[str], cwd: Path, timeout: int = 300) -> ProcessResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    chunks: list[bytes] = []
    size = 0

    async def consume() -> None:
        nonlocal size
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            size += len(chunk)
            if size > 2_000_000:
                raise RuntimeError("Command output exceeded 2 MB")
            chunks.append(chunk)
        await process.wait()

    try:
        await asyncio.wait_for(consume(), timeout)
    except BaseException:
        # A child can keep the pipes open after the original process has exited.
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 2)
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    return ProcessResult(argv, process.returncode or 0, b"".join(chunks).decode("utf-8", errors="replace"))


async def checked(argv: list[str], cwd: Path, timeout: int = 300) -> str:
    result = await execute(argv, cwd, timeout)
    if result.exit_code:
        raise RuntimeError(f"{argv[0]} failed ({result.exit_code}): {result.output[-4000:]}")
    return result.output.strip()
