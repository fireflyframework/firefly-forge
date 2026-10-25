# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The real command slot/WAL validator never dispatches a prior command again."""

import copy
from types import SimpleNamespace

import pytest
from test_sandbox_command_ordinals_host import command

from firefly_forge.git_partial_source_checkpoints import GitPartialSourceCheckpoints
from firefly_forge.sandbox import SandboxReconciliationRequired

pytest_plugins = ["test_sandbox_command_ordinals_host"]


async def effect_case(case):
    await command(case, ["known-command"])

    async def empty(_):
        return []

    service = GitPartialSourceCheckpoints(
        SimpleNamespace(store=case.store, journal=case.journal), tool_records=SimpleNamespace(for_run=empty)
    )
    run = await case.store.get_run(case.run_id)
    entries = [copy.deepcopy(r.data) for r in case.store.records.rows]
    return service, run, entries, max(e["sequence"] for e in entries) + 1


async def test_completed_command_is_attested_without_dispatch_or_counter_reset(command_case):
    c = command_case
    service, run, entries, frontier = await effect_case(c)
    observed = await service._effects(run, entries, frontier)
    assert await service._effects(run, entries, frontier) == observed
    assert c.calls == [["known-command"]]
    assert [r.ordinal for r in c.store.sandboxes.rows] == [1]


@pytest.mark.parametrize("change", ["unknown", "interrupted", "orphan", "late"])
async def test_unknown_or_later_command_custody_does_not_create_source_authority(command_case, change):
    c = command_case
    service, run, entries, frontier = await effect_case(c)
    row = c.store.sandboxes.rows[0]
    if change == "unknown":
        row.data["effects"]["start"]["status"] = "intent"
    elif change == "interrupted":
        row.data["effects"]["start"]["evidence"]["outcome"] = "interrupted"
    elif change == "orphan":
        c.store.sandboxes.rows.clear()
    else:
        frontier = entries[-1]["sequence"]
    with pytest.raises((ValueError, SandboxReconciliationRequired)):
        await service._effects(run, entries, frontier)
    assert c.calls == [["known-command"]]
