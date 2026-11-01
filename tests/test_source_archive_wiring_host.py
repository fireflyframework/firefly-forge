# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real native registration and the Engine snapshot publication boundary."""

import base64
import hashlib
from types import SimpleNamespace

from firefly_forge.engine import Engine
from firefly_forge.git_diff_models import GitDiffPageV1, GitDiffRefV1
from firefly_forge.source_archive_service import SourceArchiveManager
from firefly_forge.source_explorer import SourceExplorer, SourceExplorerController


def test_native_application_registers_source_readers_and_publication_lifecycle_without_startup(tmp_path):
    from firefly_forge.api import create_runtime

    runtime = create_runtime(tmp_path, "controlled-source-archive-bootstrap-token", "api")
    for component in (SourceArchiveManager, SourceExplorer, SourceExplorerController):
        assert runtime.context.container.contains_type(component)


async def test_engine_snapshot_awaits_publication_and_propagates_failure_before_return(tmp_path, monkeypatch):
    import firefly_forge.engine as subject
    import firefly_forge.git_diff_artifacts as artifacts
    import firefly_forge.source_index_sanitation as sanitation
    import firefly_forge.source_snapshot as snapshots

    run = {"id": "a" * 32}
    calls = []

    async def current():
        return None

    class Scanner:
        async def __call__(self, *args):
            return None

        async def revalidate(self):
            return None

    async def scanner(**kwargs):
        return Scanner()

    async def get(_):
        return run

    async def checked(argv, *args):
        if argv[1:] == ["write-tree"]:
            return "b" * 40
        if argv[1:] == ["rev-parse", "HEAD"]:
            return "c" * 40
        return ""

    async def scan(*args, **kwargs):
        return ["app.py"], 1

    raw = b"+recorded\n"
    artifact = GitDiffRefV1(
        artifact_id="d" * 32,
        manifest_sha256="e" * 64,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        file_count=1,
        index_page_count=1,
    )

    async def retain(*args, scan_chunk, **kwargs):
        await scan_chunk(raw, True)
        return artifact

    def read(*args):
        return GitDiffPageV1(
            artifact_id=artifact.artifact_id,
            manifest_sha256=artifact.manifest_sha256,
            offset=0,
            byte_count=len(raw),
            total_bytes=len(raw),
            next_offset=None,
            content_base64=base64.b64encode(raw).decode(),
            content_sha256=artifact.sha256,
            starts_mid_line=False,
            ends_mid_line=False,
            utf8_complete=True,
        )

    async def publish(*args, **kwargs):
        calls.append(args)
        await kwargs["assert_current"]()
        raise ValueError("controlled archive publication refusal")

    engine = object.__new__(Engine)
    engine.store = SimpleNamespace(get_run=get, source_patch_scanner=scanner)
    engine.workspaces = tmp_path
    engine.component_roots = {}
    engine.assert_ownership = current
    engine._assert_git_metadata = lambda _: None
    engine.source_archives = SimpleNamespace(publish=publish)
    monkeypatch.setattr(subject, "checked", checked)
    monkeypatch.setattr(sanitation, "staging_exclusions", lambda *args: [])
    monkeypatch.setattr(snapshots, "git_index_identity", lambda _: "stable-index")
    monkeypatch.setattr(snapshots, "scan_changed_source", scan)
    monkeypatch.setattr(artifacts, "retain_git_diff", retain)
    monkeypatch.setattr(artifacts, "read_diff_page", read)
    import pytest

    with pytest.raises(ValueError, match="archive publication refusal"):
        await engine._snapshot(tmp_path / run["id"], "c" * 40)
    assert len(calls) == 1 and calls[0] == (run, tmp_path / run["id"], "c" * 40, "b" * 40)
