# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact selected physical source remains distinct from Git index descriptors."""

import pytest
from test_checkpoint_source_identity_host import SourceStore, git, repository


async def test_native_staging_changes_full_cut_but_not_complete_selected_physical_source(tmp_path):
    from firefly_forge.agentic_checkpoint_invocation import workspace_cut, workspace_source_capture

    root = repository(tmp_path)
    asset = root / "build" / "asset.txt"
    asset.parent.mkdir()
    asset.write_text("Tracked ignored source\n")
    git(root, "add", "-f", "build/asset.txt")
    (root / "new.py").write_text("VALUE = 2\n")
    store = SourceStore()
    before = await workspace_source_capture(store, root)
    assert before.cut == await workspace_cut(store, root)
    git(root, "add", "--all")
    after = await workspace_source_capture(store, root)
    assert before.cut["source"] != after.cut["source"]
    assert before.physical_digest == after.physical_digest
    assert before.index != after.index
    assert {k: v for k, v in before.cut.items() if k != "source"} == {
        k: v for k, v in after.cut.items() if k != "source"
    }
    asset.write_text("Changed tracked ignored source\n")
    assert (await workspace_source_capture(store, root)).physical_digest != after.physical_digest


@pytest.mark.parametrize("change", ["bytes", "mode", "deleted", "member"])
async def test_complete_physical_fingerprint_refuses_real_changes(tmp_path, change):
    from firefly_forge.agentic_checkpoint_invocation import workspace_source_capture

    root = repository(tmp_path)
    path = root / "package.json"
    before = await workspace_source_capture(SourceStore(), root)
    if change == "bytes":
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "mode":
        path.chmod(0o755)
    elif change == "deleted":
        path.unlink()
    else:
        (root / "new.py").write_text("NEW = True\n")
    assert (await workspace_source_capture(SourceStore(), root)).physical_digest != before.physical_digest


async def test_staging_preexisting_tombstone_is_explicitly_conservative(tmp_path):
    from firefly_forge.agentic_checkpoint_invocation import workspace_source_capture

    root = repository(tmp_path)
    (root / "package.json").unlink()
    before = await workspace_source_capture(SourceStore(), root)
    git(root, "add", "--all")
    after = await workspace_source_capture(SourceStore(), root)
    assert before.physical_digest != after.physical_digest


@pytest.fixture
async def snapshot_case(tmp_path):
    """Real Git/snapshot/capture; explicit in-memory ownership boundary for this unit."""
    from types import SimpleNamespace

    from firefly_forge.agentic_checkpoint_invocation import workspace_cut
    from firefly_forge.consumed_terminal_completions import ConsumedTerminalExecution
    from firefly_forge.engine import Engine
    from firefly_forge.git_workspace import metadata_digest
    from firefly_forge.journal import digest
    from firefly_forge.source_catalog import SourceCatalogBudget
    from firefly_forge.verification_source_preparation import prepare_verification_source

    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    root = workspaces / ("a" * 32)
    root.mkdir()
    repository(root)
    (root / "new.py").write_text("VALUE = 2\n")
    ignored = root / "build" / "asset.txt"
    ignored.parent.mkdir()
    ignored.write_text("Tracked ignored source\n")
    git(root, "add", "-f", "build/asset.txt")
    store = SourceStore()
    store.runtime_roles = None
    store.owned = True

    async def current():
        if not store.owned:
            raise ValueError("Controlled current owner changed")

    async def run(_):
        return {}

    store.assert_ownership, store.get_run = current, run
    engine = object.__new__(Engine)
    engine.store, engine.workspaces, engine.component_roots = store, workspaces, {}
    engine.enforce_ownership = True
    engine.git_metadata = {root.resolve(): metadata_digest(root)}
    execution = object.__new__(ConsumedTerminalExecution)
    execution.service = SimpleNamespace(store=store, engine=engine)
    execution.workspace, execution.run_id = root, root.name
    execution._snapshot_started, execution._snapshot = False, None
    execution.guard = execution.qualified = current
    initial = await workspace_cut(store, root)
    execution.binding = SimpleNamespace(workspace=initial)
    execution.witness = {"payload": {"candidate": {"proof": {"source_digest": digest(initial)}}}}
    catalog = tmp_path / "catalog"
    catalog.mkdir(mode=0o700)
    execution._catalog = await prepare_verification_source(
        root, catalog, budget=SourceCatalogBudget(), assert_current=current
    )
    case = SimpleNamespace(
        execution=execution, engine=engine, store=store, root=root, ignored=ignored, base=git(root, "rev-parse", "HEAD")
    )
    yield case
    if execution._snapshot is not None:
        execution._snapshot.source_fence.close()


@pytest.mark.parametrize("change", ["bytes", "mode", "ignored", "head", "config", "owner", "index_after", "repeat"])
async def test_native_snapshot_transition_refuses_uncredited_mutations(snapshot_case, monkeypatch, change):
    c = snapshot_case
    native = c.engine._snapshot

    async def changed(*args, **kwargs):
        answer = await native(*args, **kwargs)
        if change == "bytes":
            (c.root / "new.py").write_text("VALUE = 9\n")
        elif change == "mode":
            (c.root / "new.py").chmod(0o755)
        elif change == "ignored":
            c.ignored.write_text("Changed tracked ignored bytes\n")
        elif change == "head":
            git(c.root, "commit", "-qm", "Unadmitted history")
        elif change == "config":
            git(c.root, "config", "controlled.drift", "true")
        elif change == "owner":
            c.store.owned = False
        return answer

    monkeypatch.setattr(c.engine, "_snapshot", changed)
    if change in {"index_after", "repeat"}:
        await c.execution.snapshot(c.engine, c.base, secrets={}, expected_head=None)
        if change == "repeat":
            with pytest.raises(ValueError, match="unique"):
                await c.execution.snapshot(c.engine, c.base, secrets={}, expected_head=None)
        else:
            git(c.root, "update-index", "--assume-unchanged", "new.py")
            with pytest.raises(ValueError, match="index"):
                await c.execution.guard_source()
    else:
        with pytest.raises(ValueError):
            await c.execution.snapshot(c.engine, c.base, secrets={}, expected_head=None)


async def test_external_index_drift_before_snapshot_is_not_admitted(snapshot_case):
    c = snapshot_case
    git(c.root, "add", "new.py")
    with pytest.raises(ValueError, match="source"):
        await c.execution.snapshot(c.engine, c.base, secrets={}, expected_head=None)
    assert c.execution._snapshot is None


async def test_complete_selected_witness_fence_covers_tracked_ignored_and_new_members(snapshot_case):
    c = snapshot_case
    await c.execution.snapshot(c.engine, c.base, secrets={}, expected_head=None)
    c.execution.snapshot_fence()
    c.ignored.write_text("Changed beyond the public verification catalog\n")
    with pytest.raises(ValueError, match="source|metadata"):
        c.execution.snapshot_fence()


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_snapshot_witness_handles_close_on_success_failure_and_cancel(snapshot_case, monkeypatch, outcome):
    import asyncio

    import firefly_forge.consumed_terminal_completions as recovery

    c = snapshot_case
    original_capture = recovery.workspace_source_capture
    captured = []

    async def capture(*args, **kwargs):
        result = await original_capture(*args, **kwargs)
        captured.append(result.fence)
        return result

    monkeypatch.setattr(recovery, "workspace_source_capture", capture)
    if outcome != "success":
        native = c.engine._snapshot

        async def interrupted(*args, **kwargs):
            await native(*args, **kwargs)
            if outcome == "cancel":
                raise asyncio.CancelledError
            c.ignored.write_text("Unadmitted selected payload\n")
            raise RuntimeError("Controlled snapshot interruption")

        monkeypatch.setattr(c.engine, "_snapshot", interrupted)
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
            await c.execution.snapshot(c.engine, c.base, secrets={}, expected_head=None)
    else:
        await c.execution.snapshot(c.engine, c.base, secrets={}, expected_head=None)
        assert all(stream.closed for stream in captured[0].streams)
        assert not all(stream.closed for stream in captured[-1].streams)
        c.execution.close_snapshot()
    assert captured and all(stream.closed for fence in captured for stream in fence.streams)


@pytest.mark.parametrize("change", ["tracked_ignored", "new_public", "ignored_parent_mode"])
async def test_post_snapshot_fence_covers_all_selected_payload_and_namespace(snapshot_case, change):
    c = snapshot_case
    await c.execution.snapshot(c.engine, c.base, secrets={}, expected_head=None)
    if change == "tracked_ignored":
        c.ignored.write_text("Changed after the source hash\n")
    elif change == "new_public":
        (c.root / "late.py").write_text("LATE = True\n")
    else:
        c.ignored.parent.chmod(0o700)
    with pytest.raises(ValueError, match="source|metadata"):
        c.execution.snapshot_fence()


def test_snapshot_delegates_are_in_runtime_and_admission_fingerprints(monkeypatch):
    from pathlib import Path

    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.source_budget_allowances import admission_contract_digest

    original = Path.read_bytes
    before_runtime, before_admission = runtime_contract(), admission_contract_digest()
    for target in (
        "checkpoint_source_identity.py",
        "agentic_checkpoint_invocation.py",
        "consumed_terminal_completions.py",
        "engine.py",
    ):
        with monkeypatch.context() as mutation:

            def changed(path, *, target=target):
                raw = original(path)
                return raw + b"\n# controlled delegate mutation\n" if path.name == target else raw

            mutation.setattr(Path, "read_bytes", changed)
            assert runtime_contract() != before_runtime
            assert admission_contract_digest() != before_admission
