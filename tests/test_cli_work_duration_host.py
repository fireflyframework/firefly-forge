# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""New CLI work does not acquire implicit command or observer wall-clock caps."""

import pytest
from click.testing import CliRunner

from firefly_forge import cli


@pytest.mark.parametrize("wait", [False, True])
def test_cli_wait_survives_more_than_an_hour_and_new_checks_are_explicitly_uncapped(monkeypatch, wait):
    bodies, reads = [], []
    clock = {"seconds": 0}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create_run(self, request):
            bodies.append(request.model_dump())
            return {"id": "a" * 32, "status": "implementing"}

        def get_run(self, run):
            reads.append(run)
            return {"id": run, "status": "implementing" if len(reads) == 1 else "succeeded"}

    def sleep(seconds):
        assert seconds == 0.5
        clock["seconds"] += 7201

    monkeypatch.setattr(cli, "client", Client)
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["seconds"])
    monkeypatch.setattr(cli.time, "sleep", sleep)
    result = CliRunner().invoke(
        cli.main,
        [
            "run",
            "--repository",
            "fixture",
            "--brief",
            "Check the approved source",
            "--check",
            "pytest -q",
            *(["--wait"] if wait else []),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(reads) == (2 if wait else 0)
    assert clock["seconds"] > 3600 if wait else clock["seconds"] == 0
    assert bodies[0]["commands"] == [{"argv": ["pytest", "-q"], "timeout": None}]
