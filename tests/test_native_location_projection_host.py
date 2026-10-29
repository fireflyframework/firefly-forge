# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Physical path projection never rewrites the frozen logical command or history."""

import copy

import pytest
from test_native_location_ledger_host import events
from test_native_location_ledger_host import ledger_case as _ledger_case

from firefly_forge.journal import digest
from firefly_forge.native_location_projection import project_native_argv, workspace_matches


@pytest.fixture
def projection(tmp_path):
    case = _ledger_case.__wrapped__(tmp_path)
    root, _, _, candidate, request = case
    plan = candidate["plan"]
    run_id = "a" * 32
    plan["workspaces"] = [
        {
            "run_id": run_id,
            "source": "/retained/storage/workspaces/" + run_id,
            "target": str(root / "workspaces" / run_id),
        }
    ]
    plan["verification_runtimes"] = [
        {
            "run_id": run_id,
            "source": "/retained/python",
            "target": "/new/python",
            "mode": "verification_runtime_baseline",
            "source_content_digest": None,
            "target_content_digest": "e" * 64,
            "approved_baseline_sha256": "f" * 64,
        }
    ]
    candidate["digest"] = digest({k: v for k, v in candidate.items() if k != "digest"})
    request["candidate_digest"] = candidate["digest"]
    history = events(case)
    return {"version": 1, "receipt_digest": history[-1]["digest"], "events": history}


def test_exact_path_tokens_only_leave_frozen_original_unmodified(projection):
    logical = ["/retained/python/bin/python", "-m", "pytest", "backend/tests", "--value=/retained/python"]
    before = copy.deepcopy(logical)
    assert project_native_argv(projection, "a" * 32, logical) == ["/new/python/bin/python", *logical[1:]]
    assert logical == before
    with pytest.raises(ValueError, match="selected"):
        project_native_argv(projection, "b" * 32, logical)


def test_workspace_projection_preserves_every_nonpath_checkpoint_field(projection):
    expected = {"path": "/retained/storage/workspaces/" + "a" * 32, "head": "h", "metadata": "m", "source": "s"}
    actual = {**expected, "path": projection["events"][-1]["candidate"]["plan"]["workspaces"][0]["target"]}
    assert workspace_matches(projection, "a" * 32, actual, expected)
    assert not workspace_matches(projection, "a" * 32, {**actual, "metadata": "changed"}, expected)
    bad = copy.deepcopy(projection)
    bad["events"][-1]["sequence"] = 2.0
    with pytest.raises(ValueError):
        project_native_argv(bad, "a" * 32, ["/retained/python/bin/python"])


async def test_source_guard_accepts_only_current_native_projection(monkeypatch, projection):
    from types import SimpleNamespace

    import firefly_forge.native_location_projection as module

    expected = {"path": "/retained/storage/workspaces/" + "a" * 32, "head": "h", "metadata": "m", "source": "s"}
    actual = {**expected, "path": projection["events"][-1]["candidate"]["plan"]["workspaces"][0]["target"]}
    store = SimpleNamespace(native_location_state=object())

    async def current(value):
        return module.NativeLocationProjection(store, projection)

    monkeypatch.setattr(module, "current_native_location", current)
    assert await module.source_workspace_matches(store, "a" * 32, actual, expected)
    store.native_location_state = None
    assert not await module.source_workspace_matches(store, "a" * 32, actual, expected)
    assert await module.source_workspace_matches(store, "a" * 32, expected, expected)


async def test_runner_projection_keeps_logical_receipt_and_refuses_coding_rebinding(monkeypatch, projection):
    from types import SimpleNamespace

    import firefly_forge.native_location_projection as module

    store = SimpleNamespace(native_location_state=object())
    current = module.NativeLocationProjection(store, projection)

    async def location(value):
        return current

    monkeypatch.setattr(module, "current_native_location", location)
    physical, evidence, guard = await module.runner_location(
        store, "a" * 32, "verify", ["/retained/python/bin/python", "-m", "pytest"]
    )
    assert physical[0] == "/new/python/bin/python"
    assert evidence["logical_argv"][0] == "/retained/python/bin/python"
    assert evidence["physical_argv"] == physical
    assert evidence["receipt_digest"] == projection["receipt_digest"]
    assert guard is not None
    with pytest.raises(ValueError, match="verification"):
        await module.runner_location(store, "a" * 32, "implement", ["/retained/python/bin/python"])


def test_later_checkpoint_paths_must_be_exact_members_of_completed_chain(projection, tmp_path):
    from firefly_forge.native_location_files import read_storage_identity
    from firefly_forge.native_location_runtime import location_event

    first = projection["events"][-1]
    candidate = copy.deepcopy(first["candidate"])
    plan = candidate["plan"]
    previous_root = plan["target_storage"]
    plan["source_storage"], plan["target_storage"] = previous_root, "/second/native-storage"
    selected = plan["workspaces"][0]
    intermediate = selected["target"]
    selected["source"] = intermediate
    selected["target"] = "/second/native-storage/workspaces/" + "a" * 32
    plan["verification_runtimes"] = []
    candidate["previous_receipt_digest"] = first["digest"]
    candidate["digest"] = digest({k: v for k, v in candidate.items() if k != "digest"})
    request = {**first["request"], "request_id": "2" * 32, "candidate_digest": candidate["digest"]}
    identity = read_storage_identity(__import__("pathlib").Path(previous_root))
    intent = location_event("native_location_intent", 3, request, candidate, first["digest"], None, identity)
    receipt = location_event(
        "native_location_receipt", 4, request, candidate, intent["digest"], intent["digest"], identity
    )
    current = {"version": 1, "receipt_digest": receipt["digest"], "events": [*projection["events"], intent, receipt]}
    actual = {"path": selected["target"], "head": "h", "metadata": "m", "source": "s"}
    for expected_path in (
        projection["events"][0]["candidate"]["plan"]["workspaces"][0]["source"],
        intermediate,
        actual["path"],
    ):
        assert workspace_matches(current, "a" * 32, actual, {**actual, "path": expected_path})
    assert not workspace_matches(current, "a" * 32, actual, {**actual, "path": "/unknown/workspace"})
    assert not workspace_matches(current, "a" * 32, {**actual, "source": "changed"}, actual)


async def test_handoff_requires_exact_live_native_record_and_current_physical_workspace(monkeypatch, projection):
    from types import SimpleNamespace

    import firefly_forge.native_location_projection as module

    store = SimpleNamespace(native_location_state=object())
    value = module.NativeLocationProjection(store, projection)

    async def current(_):
        return value

    monkeypatch.setattr(module, "current_native_location", current)
    physical = projection["events"][-1]["candidate"]["plan"]["workspaces"][0]["target"]
    intent = {"workspace": physical, "native_location": projection}
    await module.assert_handoff_location(store, "a" * 32, intent)
    for bad in (
        {"workspace": physical},
        {**intent, "workspace": "/unknown/workspace"},
        {**intent, "native_location": {**projection, "receipt_digest": "0" * 64}},
    ):
        with pytest.raises(ValueError):
            await module.assert_handoff_location(store, "a" * 32, bad)
    store.native_location_state = None
    with pytest.raises(ValueError, match="authority"):
        await module.assert_handoff_location(store, "a" * 32, intent)
