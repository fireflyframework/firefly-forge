# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Execution-owned Python caches keep verification tied to current source."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory

_BUILD_SCRATCH: ContextVar[Path | None] = ContextVar("forge_build_scratch", default=None)


@contextmanager
def build_execution_environment(scratch: Path) -> Iterator[None]:
    token = _BUILD_SCRATCH.set(scratch)
    try:
        yield
    finally:
        _BUILD_SCRATCH.reset(token)


def container_python_environment() -> dict[str, str]:
    return {"PYTHONPYCACHEPREFIX": "/tmp/forge-python-cache", "PYTHONDONTWRITEBYTECODE": "1"}


@contextmanager
def execution_environment(environment: dict[str, str]) -> Iterator[dict[str, str]]:
    scratch = _BUILD_SCRATCH.get()
    if scratch is not None:
        environment = {
            **environment,
            "HOME": str(scratch / "home"),
            "TMPDIR": str(scratch / "tmp"),
            "MYPY_CACHE_DIR": str(scratch / "mypy-cache"),
            "RUFF_CACHE_DIR": str(scratch / "ruff-cache"),
            "npm_config_cache": str(scratch / "npm-cache"),
        }
    with TemporaryDirectory(prefix="forge-python-cache-", dir=scratch / "tmp" if scratch else None) as directory:
        # Disabling writes alone still permits old timestamp/size caches to be read.
        yield {**environment, "PYTHONPYCACHEPREFIX": directory + "/bytecode", "PYTHONDONTWRITEBYTECODE": "1"}
