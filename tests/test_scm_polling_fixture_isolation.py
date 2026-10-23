# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host polling must restore repositories even when a consumer is first imported inside the fixture."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("consumer_name", ["scm_conversations", "scm_poll_functional"])
def test_poll_fixture_cold_import_restores_native_conversation_repository(tmp_path, consumer_name):
    program = """
import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from test_scm_polling import poll_case
from firefly_forge.identity_persistence import ForgeIdentityUserRepository

name = 'firefly_forge.' + sys.argv[2]
assert name not in sys.modules

async def check():
    with pytest.MonkeyPatch.context() as patch:
        create = poll_case.__wrapped__(Path(sys.argv[1]), patch)
        async with create():
            importlib.import_module(name)
    consumer = importlib.import_module(name)
    assert consumer.ForgeIdentityUserRepository is ForgeIdentityUserRepository, (
        'Host polling leaked its fictional user repository into native conversation admission'
    )

asyncio.run(check())
"""
    tests = Path(__file__).resolve().parent
    environment = {**os.environ, "PYTHONPATH": str(tests) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run(
        [sys.executable, "-B", "-c", program, str(tmp_path), consumer_name],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
