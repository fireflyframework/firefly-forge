# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Observer failures cannot retain consumed-source snapshots or credentials."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from test_engine_activity_host import cleanup_engine

from firefly_forge.consumed_terminal_completions import ConsumedTerminalExecution


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize(
    "observer_failure", [RuntimeError("observer failed"), asyncio.CancelledError("observer cancelled")]
)
async def test_consumed_source_and_credentials_close_when_stage_observation_fails(
    tmp_path, monkeypatch, cancel, observer_failure
):
    engine, state, run = cleanup_engine(tmp_path, monkeypatch, observer_failure)

    async def update(_identifier, **fields):
        run.update(fields)

    engine.store.update = AsyncMock(side_effect=update)
    restoration = ConsumedTerminalExecution.__new__(ConsumedTerminalExecution)
    restoration.guard_source = AsyncMock()
    restoration.close_snapshot = Mock()
    if cancel:
        entered = asyncio.Event()

        async def repository(*_args):
            entered.set()
            await asyncio.Event().wait()

        engine.store.repository.side_effect = repository
        task = asyncio.create_task(engine.execute_run(run["id"], restoration=restoration))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel("operator requested")
        with pytest.raises(asyncio.CancelledError, match="operator requested"):
            await task
        assert run["status"] == "cancelled"
    else:
        await engine.execute_run(run["id"], restoration=restoration)
        assert run["status"] == "needs_reconciliation"
        assert run["error"] == "business failure"
    assert state == {"entered": True, "closed": True}
    restoration.close_snapshot.assert_called_once_with()
