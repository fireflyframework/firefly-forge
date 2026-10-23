# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Patch inspection preserves the exact staged index across stat-cache refreshes."""
# ruff: noqa: F811

import os

import pytest
from test_workspace_dependency_reconciliation import legacy_workspace  # noqa: F401
from test_workspace_reconciliation_journal import environment_journal  # noqa: F401
from test_workspace_reconciliation_service import implementation, reconciliation_service, request  # noqa: F401

from firefly_forge import workspace_reconciliations as module


@pytest.mark.parametrize("path", ["README.md", "frontend/app.js"])
async def test_reconciliation_patch_read_never_refreshes_staged_index(
    reconciliation_service, tmp_path, monkeypatch, path
):
    env = implementation(reconciliation_service)
    candidate = await env.service.candidate(env.root.name)
    original = module._git
    observed = []

    def inspect(workspace, *args):
        if args[0] not in {"diff", "diff-index"}:
            return original(workspace, *args)
        source = workspace / path
        before_source = source.read_bytes()
        stat = source.stat()
        # Reproduce Git's stale stat cache without changing the authorized source.
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns - 5_000_000_000))
        index = workspace / ".git/index"
        before_index = index.read_bytes()
        patch = original(workspace, *args)
        assert index.read_bytes() == before_index, "Read-only patch extraction refreshed the staged index"
        assert source.read_bytes() == before_source
        assert b"frontend/app.js" in patch
        assert b"export const value = 1" in patch
        observed.append(True)
        return patch

    monkeypatch.setattr(module, "_git", inspect)
    receipt = await env.service.admit(env.root.name, request(candidate))
    assert observed == [True]
    assert receipt["completed_cut"]["index_digest"] == receipt["effect"]["index_after"]
    assert env.store.run.status == "queued" and env.task.attempt == 2
