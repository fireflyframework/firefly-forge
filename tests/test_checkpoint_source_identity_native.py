# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared native Store/lease/source checks; controlled runner, no real npm or model."""

from types import SimpleNamespace

import pytest
from pyfly.data.transaction import current_unit_of_work
from test_checkpoint_source_identity_host import repository
from test_runner_factory_authority import seed

from firefly_forge.agentic_checkpoint_invocation import workspace_cut
from firefly_forge.journal import Journal
from firefly_forge.process import ProcessResult
from firefly_forge.sandbox import SandboxExecutor


async def test_native_completed_install_keeps_checkpoint_source_and_lock_provenance(store, tmp_path):
    run, journal = await seed(store)
    root = tmp_path / "source"
    root.mkdir()
    repository(root)
    before = await workspace_cut(store, root)
    calls = []

    class Backend:
        async def run(self, config, argv, cwd, timeout, output_limit):
            assert current_unit_of_work() is None
            calls.append(argv)
            package = root / "node_modules" / "typescript" / "bin"
            package.mkdir(parents=True)
            (package / "tsc").write_text("controlled installed executable")
            bins = root / "node_modules" / ".bin"
            bins.mkdir()
            (bins / "tsc").symlink_to("../typescript/bin/tsc")
            (root / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{}}\n')
            return ProcessResult(argv, 0, "controlled install complete")

    executor = SandboxExecutor(store, journal, SimpleNamespace(select=lambda config: Backend()))
    outcome = await executor.execute(run["id"], root, root, ["npm", "install"], 2, "verify")
    assert outcome.exit_code == 0 and calls == [["npm", "install"]]
    installed = await workspace_cut(store, root)
    assert installed["source"] != before["source"]
    (root / "node_modules/typescript/bin/tsc").write_text("changed ignored dependency bytes")
    assert await workspace_cut(store, root) == installed
    (root / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{"changed":{}}}\n')
    assert (await workspace_cut(store, root))["source"] != installed["source"]
    executions = await journal.sandbox_executions(run["id"])
    assert len(executions) == 1 and executions[0]["ordinal"] == 1
    assert all(executions[0]["effects"][kind]["status"] == "done" for kind in ("create", "start", "cleanup"))
    assert Journal.verify_chain(await journal.entries(run["id"]))


@pytest.mark.parametrize("change", ["credential_cut", "new_selected_file"])
async def test_native_source_final_wait_refuses_changed_cut_without_runner_dispatch(
    store, tmp_path, monkeypatch, change
):
    run, journal = await seed(store)
    root = tmp_path / "source"
    root.mkdir()
    repository(root)
    empty = root / "unused" / "nested"
    empty.mkdir(parents=True)
    registry = {"TOKEN": "fictional-native-original-token"}
    store.credential_registry = lambda: dict(registry)
    prepare = store.source_patch_scanner
    changed = []

    async def scanner(**kwargs):
        value = await prepare(**kwargs)
        verify = value._verify_current

        async def final_wait():
            await verify()
            if not changed:
                changed.append(change)
                if change == "credential_cut":
                    registry["TOKEN"] = "fictional-native-replaced-token"
                    await verify()
                else:
                    (empty / "late-source.txt").write_text("late source")

        value._verify_current = final_wait
        return value

    monkeypatch.setattr(store, "source_patch_scanner", scanner)
    with pytest.raises(ValueError, match="changed"):
        await workspace_cut(store, root)
    assert changed == [change]
    assert await journal.sandbox_executions(run["id"]) == []
    plan = await journal.plan(run["id"])
    task = next(item for item in plan["tasks"] if item["id"] == "verify")
    assert task["status"] == "running" and task["attempt"] == 1
    assert Journal.verify_chain(await journal.entries(run["id"]))
