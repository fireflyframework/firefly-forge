# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Existing bounded host and Docker control transports; future pools stay disabled."""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from pyfly.container import service

from firefly_forge.process import ProcessResult
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.secrets import runtime_environment


async def _bounded_process(
    argv: list[str], cwd: Path, timeout: int, max_output_bytes: int, *, input_data: bytes | None = None
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        start_new_session=True,
        env=runtime_environment(),
    )
    output = bytearray()

    async def send_input() -> None:
        if input_data is not None:
            assert process.stdin is not None
            try:
                process.stdin.write(input_data)
                await process.stdin.drain()
            finally:
                process.stdin.close()

    async def consume() -> None:
        assert process.stdout is not None
        while data := await process.stdout.read(65536):
            if len(output) + len(data) > max_output_bytes:
                raise RuntimeError("Runner output exceeded the frozen output limit")
            output.extend(data)
        await process.wait()

    streams = [asyncio.create_task(send_input()), asyncio.create_task(consume())]
    try:
        await asyncio.wait_for(asyncio.gather(*streams), timeout)
    except BaseException:
        for stream in streams:
            stream.cancel()
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 2)
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        await asyncio.gather(*streams, return_exceptions=True)
        raise
    return process.returncode or 0, bytes(output)


async def bounded_process(
    argv: list[str], cwd: Path, timeout: int, max_output_bytes: int, *, input_data: bytes | None = None
) -> ProcessResult:
    code, output = await _bounded_process(argv, cwd, timeout, max_output_bytes, input_data=input_data)
    return ProcessResult(argv, code, output.decode("utf-8", errors="replace"))


def require_available_runner(config: SandboxInput) -> None:
    if config.pool_binding is not None:
        raise ValueError("Runner pool execution is not enabled; a future binding cannot select a legacy runner")


class RunnerBackend(Protocol):
    async def run(
        self,
        config: SandboxInput,
        argv: list[str],
        cwd: Path,
        timeout: int,
        max_output_bytes: int,
        *,
        input_data: bytes | None = None,
    ) -> ProcessResult: ...

    async def run_bytes(
        self,
        config: SandboxInput,
        argv: list[str],
        cwd: Path,
        timeout: int,
        max_output_bytes: int,
        *,
        input_data: bytes | None = None,
    ) -> tuple[int, bytes]: ...


class HostRunnerBackend:
    def _argv(self, config: SandboxInput, argv: list[str]) -> list[str]:
        require_available_runner(config)
        if config.mode != "host":
            raise ValueError("Host transport requires host mode")
        return argv

    async def run(
        self,
        config: SandboxInput,
        argv: list[str],
        cwd: Path,
        timeout: int,
        max_output_bytes: int,
        *,
        input_data: bytes | None = None,
    ) -> ProcessResult:
        return await bounded_process(self._argv(config, argv), cwd, timeout, max_output_bytes, input_data=input_data)

    async def run_bytes(
        self,
        config: SandboxInput,
        argv: list[str],
        cwd: Path,
        timeout: int,
        max_output_bytes: int,
        *,
        input_data: bytes | None = None,
    ) -> tuple[int, bytes]:
        return await _bounded_process(self._argv(config, argv), cwd, timeout, max_output_bytes, input_data=input_data)


class DockerRunnerBackend(HostRunnerBackend):
    # Arguments are Docker control operations assembled by the fenced sandbox facade.
    def _argv(self, config: SandboxInput, argv: list[str]) -> list[str]:
        require_available_runner(config)
        if config.mode != "docker" or not config.context:
            raise ValueError("Docker control transport requires Docker mode and an explicit context")
        return ["docker", "--context", config.context, *argv]


@service
class RunnerBackends:
    def __init__(self) -> None:
        self._host = HostRunnerBackend()
        self._docker = DockerRunnerBackend()

    def select(self, config: SandboxInput) -> RunnerBackend:
        require_available_runner(config)
        if config.mode == "host":
            return self._host
        if config.mode == "docker":
            return self._docker
        raise ValueError("Runner backend is not implemented")
