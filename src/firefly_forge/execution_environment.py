# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Execution-owned Python caches keep verification tied to current source."""

from collections.abc import Iterator
from contextlib import contextmanager
from tempfile import TemporaryDirectory


def container_python_environment() -> dict[str, str]:
    return {"PYTHONPYCACHEPREFIX": "/tmp/forge-python-cache", "PYTHONDONTWRITEBYTECODE": "1"}


@contextmanager
def execution_environment(environment: dict[str, str]) -> Iterator[dict[str, str]]:
    with TemporaryDirectory(prefix="forge-python-cache-") as directory:
        # Disabling writes alone still permits old timestamp/size caches to be read.
        yield {**environment, "PYTHONPYCACHEPREFIX": directory + "/bytecode", "PYTHONDONTWRITEBYTECODE": "1"}
