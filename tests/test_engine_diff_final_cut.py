# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Causal peer control: current credentials change during the final owner wait."""

import pytest
from test_engine_diff_artifacts import snapshot_case as snapshot_case

from firefly_forge.source_snapshot import diff_artifact_root
from firefly_forge.store import Store


async def test_credential_cut_changed_after_scanner_finalization_must_not_publish(snapshot_case, monkeypatch):
    c = snapshot_case
    token = "late-public-fixture-provider-secret"
    registry = {}
    streams = []
    changed = []
    c.store.credential_registry = lambda: registry
    (c.root / "implementation.py").write_text("value = " + repr(token) + "\n")

    async def owner():
        if streams and streams[0]._closed and not changed:
            registry["API_TOKEN"] = token
            changed.append(True)

    async def prepare(**kwargs):
        stream = await Store.source_patch_scanner(c.store, **kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setattr(c.engine, "assert_ownership", owner)
    monkeypatch.setattr(c.store, "source_patch_scanner", prepare)
    with pytest.raises(ValueError, match="changed|credential"):
        await c.engine._snapshot(c.root, c.base)
    assert changed
    assert list(diff_artifact_root(c.engine.workspaces, c.root.name).iterdir()) == []
