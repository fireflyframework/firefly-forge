# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current native custody with concurrent ignored outputs; no model or command dispatch."""

import asyncio

import pytest
from test_checkpoint_source_identity_host import repository
from test_runner_factory_authority import seed

from firefly_forge.agentic_checkpoint_invocation import workspace_cut
from firefly_forge.journal import Journal


@pytest.mark.parametrize(
    "name",
    ["node_modules/.cache/result.json", "generated/cache/result.json", ".forge-sandbox-results/command/result.json"],
)
async def test_native_final_ignored_output_keeps_source_authority_and_spent_state(store, tmp_path, monkeypatch, name):
    run, journal = await seed(store)
    root = tmp_path / "source"
    root.mkdir()
    repository(root)
    (root / ".git/info/exclude").write_text(".forge-sandbox-results/\n")
    expected = await workspace_cut(store, root)
    prepare = store.source_patch_scanner
    ready, written = asyncio.Event(), asyncio.Event()

    async def scanner(**kwargs):
        value = await prepare(**kwargs)
        verify = value._verify_current

        async def final_wait():
            await verify()
            ready.set()
            await written.wait()
            await verify()

        value._verify_current = final_wait
        return value

    async def writer():
        await ready.wait()
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("controlled native output\n")
        written.set()

    monkeypatch.setattr(store, "source_patch_scanner", scanner)
    background = asyncio.create_task(writer())
    try:
        assert await workspace_cut(store, root) == expected
    finally:
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)
    assert written.is_set()
    assert await journal.sandbox_executions(run["id"]) == []
    plan = await journal.plan(run["id"])
    task = next(item for item in plan["tasks"] if item["id"] == "verify")
    assert task["status"] == "running" and task["attempt"] == 1
    assert Journal.verify_chain(await journal.entries(run["id"]))
