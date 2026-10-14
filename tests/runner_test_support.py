# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native installed prerequisites for recorded runner transport fixtures."""

import shutil
from pathlib import Path

import pytest


def installed_docker_binary() -> Path:
    executable = shutil.which("docker")
    if executable is None:
        pytest.fail("Install the Docker CLI to run runner controller tests")
    return Path(executable).resolve(strict=True)
