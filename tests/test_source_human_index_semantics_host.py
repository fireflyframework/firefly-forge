# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Only a proven sanitation ancestry permits semantically identical index stat refreshes."""

# ruff: noqa: F401, F811
import hashlib

import pytest
from test_source_edit_continuations_host import controlled_metadata
from test_source_human_readmission_host import answer_successor, answered_source, human_request
from test_workspace_dependency_reconciliation import git, legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge.source_edit_continuations import source_edit_receipt
from firefly_forge.source_edit_model_catalog import model_catalog_path
from firefly_forge.source_human_readmission import SourceHumanReadmissions
from firefly_forge.source_patch_admission import SourcePatch
from firefly_forge.store import Store


def refresh_stat_cache(root):
    # Change only a cached stat field in a disposable SHA-1 index, never its mode/name/blob/flags.
    path = root / ".git/index"
    old = path.read_bytes()
    assert old[:4] == b"DIRC" and int.from_bytes(old[4:8], "big") in (2, 3)
    body = bytearray(old[:-20])
    body[12:16] = ((int.from_bytes(body[12:16], "big") + 1) % 2**32).to_bytes(4, "big")
    path.write_bytes(bytes(body) + hashlib.sha1(body).digest())
    assert path.read_bytes() != old


async def test_two_stat_refreshes_follow_exact_sanitation_and_human_receipt_ancestry(
    reconciliation_service, tmp_path, monkeypatch
):
    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch, observed=True, sanitized=True)
    service, path = SourceHumanReadmissions(env.service), str(model_catalog_path("fixture-model"))
    original = parent
    for attempt in (3, 4):
        refresh_stat_cache(env.root)
        raw = (env.root / ".git/index").read_bytes()
        candidate = await service.candidate(env.root.name, parent["request"]["request_id"], path)
        receipt = await service.admit(env.root.name, parent["request"]["request_id"], human_request(candidate, path))
        assert (env.root / ".git/index").read_bytes() == raw
        assert receipt["candidate"]["human_readmission"]["index_semantics"] == candidate["index_semantics"]
        assert receipt["candidate"]["source_edit"]["remaining_commands"] == 7
        assert receipt["candidate"]["source_edit"]["command_limit"] == 35
        assert env.task.attempt == attempt
        assert (
            await source_edit_receipt(env.recoveries, await env.store.get_run(env.root.name), qualify=True) == receipt
        )
        if attempt == 3:
            await answer_successor(env, 3)
        parent = receipt
    assert original["completed_cut"]["index_digest"] != parent["completed_cut"]["index_digest"]


@pytest.mark.parametrize("fault", ["no_sanitation", "different_blob", "private_extra", "await_refresh"])
async def test_stat_refresh_never_authorizes_foreign_staged_content_or_an_await_change(
    reconciliation_service, tmp_path, monkeypatch, fault
):
    env, parent = await answered_source(
        reconciliation_service, tmp_path, monkeypatch, observed=True, sanitized=fault != "no_sanitation"
    )
    service, path = SourceHumanReadmissions(env.service), str(model_catalog_path("fixture-model"))
    refresh_stat_cache(env.root)
    before = len(env.journal.records.rows)
    if fault == "different_blob":
        foreign = tmp_path / "foreign-public-blob"
        foreign.write_text("DIFFERENT = True\n")
        blob = git(env.root, "hash-object", "-w", str(foreign))
        git(env.root, "update-index", "--cacheinfo", "100644," + blob + ",frontend/app.js")
    elif fault == "private_extra":
        (env.root / ".env").write_text("PUBLIC_FIXTURE=private-name\n")
        git(env.root, "add", "--force", "--", ".env")
    if fault == "await_refresh":
        candidate = await service.candidate(env.root.name, parent["request"]["request_id"], path)

        async def changed(self, patch):
            assert isinstance(patch, SourcePatch)
            refresh_stat_cache(env.root)

        monkeypatch.setattr(Store, "admit_source_patch", changed)
        with pytest.raises(ValueError, match="changed during credential admission"):
            await service.admit(env.root.name, parent["request"]["request_id"], human_request(candidate, path))
    else:
        with pytest.raises(ValueError):
            await service.candidate(env.root.name, parent["request"]["request_id"], path)
    assert len(env.journal.records.rows) == before
    assert env.task.attempt == 2 and env.task.status == "paused"
