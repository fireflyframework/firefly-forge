# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual local Git and archive bytes exercise the streaming Engine boundary."""

import base64
import hashlib
import subprocess
import zipfile
from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from test_execution import seed_repository

from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.engine import Engine
from firefly_forge.git_diff_artifacts import read_diff_page
from firefly_forge.git_diff_models import GitDiffBudget
from firefly_forge.process import checked
from firefly_forge.source_snapshot import diff_artifact_root
from firefly_forge.store import Store

RUN = "7" * 32


@pytest.fixture
async def snapshot_case(tmp_path, monkeypatch):
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir(mode=0o700)
    workspace = await seed_repository(workspaces / RUN)
    base = await checked(["git", "rev-parse", "HEAD"], workspace)
    store = Store(tmp_path / "state" / "factory.db")

    async def execute(_statement):
        return SimpleNamespace(mappings=lambda: [])

    @asynccontextmanager
    async def unit(**_kwargs):
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(store, "_unit", unit)
    oauth = ConnectionOAuth(store, encryption_key=Fernet.generate_key().decode(), browser_origin="")
    state = {"run": {"id": RUN, "base_revision": base, "git_delivery_policy": {"enabled": False}}}

    async def owner():
        if state.get("stopped"):
            raise RuntimeError("Public fixture owner stopped")

    async def run(_identifier):
        assert _identifier == RUN
        return dict(state["run"])

    async def update(_identifier, **fields):
        assert _identifier == RUN
        state["run"].update(fields)

    monkeypatch.setattr(store, "get_run", run)
    monkeypatch.setattr(store, "update", update)
    engine = Engine(store, workspaces)
    monkeypatch.setattr(engine, "assert_ownership", owner)
    scanner = store.source_patch_scanner

    async def controlled(_store, **kwargs):
        return await scanner(**{**kwargs, "assert_current": owner})

    monkeypatch.setattr(store, "source_patch_scanner", MethodType(controlled, store))
    return SimpleNamespace(engine=engine, store=store, root=workspace, base=base, state=state, oauth=oauth)


def actual_diff_digest(workspace, base):
    process = subprocess.Popen(
        [
            "git",
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-color",
            "--no-renames",
            base,
            "--",
        ],
        cwd=workspace,
        stdout=subprocess.PIPE,
    )
    digest = hashlib.sha256()
    size = 0
    assert process.stdout is not None
    while chunk := process.stdout.read(65536):
        digest.update(chunk)
        size += len(chunk)
    assert process.wait(timeout=10) == 0
    return digest.hexdigest(), size


async def test_actual_engine_snapshot_retains_full_diff_beyond_old_capture_limit(snapshot_case):
    c = snapshot_case
    (c.root / "implementation.py").write_text("value = '" + "public-source" * 450000 + "'\n")
    files, source, tree = await c.engine._snapshot(c.root, c.base)
    wanted_sha, wanted_size = actual_diff_digest(c.root, c.base)
    assert files == ["implementation.py"] and len(tree) == 40
    assert source.artifact.byte_count == wanted_size and wanted_size > 4 * 1024 * 1024
    assert source.artifact.sha256 == wanted_sha
    assert source.preview_bytes == 65536 and source.preview_truncated is True
    assert len(source.preview.encode()) <= 65536
    root = diff_artifact_root(c.engine.workspaces, RUN)
    digest = hashlib.sha256()
    offset = 0
    while offset < wanted_size:
        page = read_diff_page(root, source.artifact, offset, 65536)
        data = base64.b64decode(page.content_base64, validate=True)
        digest.update(data)
        offset += len(data)
    assert digest.hexdigest() == wanted_sha and offset == wanted_size
    c.state["run"]["source_tree"] = tree
    await c.engine._artifact(RUN, c.root, files, source)
    with zipfile.ZipFile(c.engine.workspaces / (RUN + ".zip")) as archive:
        digest = hashlib.sha256()
        with archive.open("forge-changes.patch") as stream:
            while chunk := stream.read(65536):
                digest.update(chunk)
        assert digest.hexdigest() == wanted_sha


async def test_snapshot_honors_explicit_project_budget_without_accepting_a_prefix(snapshot_case):
    c = snapshot_case
    (c.root / "implementation.py").write_text("public\n" * 10000)
    with pytest.raises(ValueError, match="budget|bound"):
        await c.engine._snapshot(c.root, c.base, budget=GitDiffBudget(max_bytes=1024))


async def test_all_changed_files_remain_in_archive_beyond_bounded_name_preview(snapshot_case):
    c = snapshot_case
    for ordinal in range(121):
        (c.root / f"public-{ordinal:03}.txt").write_text(f"public implementation {ordinal}\n")
    files, source, tree = await c.engine._snapshot(c.root, c.base)
    assert len(files) == 100 and source.artifact.file_count == 121
    c.state["run"]["source_tree"] = tree
    await c.engine._artifact(RUN, c.root, files, source)
    with zipfile.ZipFile(c.engine.workspaces / (RUN + ".zip")) as archive:
        assert len(archive.namelist()) == 122
        assert archive.read("public-120.txt") == b"public implementation 120\n"


@pytest.mark.parametrize("tail", ["zzz/.env", "zzz/outside.txt"])
async def test_hidden_private_or_component_escape_after_name_preview_is_refused(snapshot_case, tail):
    c = snapshot_case
    (c.root / "api").mkdir()
    for ordinal in range(105):
        (c.root / "api" / f"public-{ordinal:03}.txt").write_text("public source\n")
    outside = c.root / tail
    outside.parent.mkdir()
    outside.write_text("must not be delivered\n")
    if not tail.endswith(".env"):
        c.engine.component_roots[c.root.resolve()] = "api"
    with pytest.raises(ValueError, match="private|component"):
        await c.engine._snapshot(c.root, c.base)


@pytest.mark.parametrize("binary", [False, True])
async def test_plaintext_credential_cannot_hide_in_binary_diff_or_quoted_filename(snapshot_case, binary):
    c = snapshot_case
    token = "public-fixture-🐉-credential"
    if binary:
        (c.root / "image.png").write_bytes(b"\x89PNG\x00\xff" + token.encode() + b"\x00public")
    else:
        (c.root / (token + ".txt")).write_text("public implementation\n")
    with pytest.raises(ValueError, match="credential"):
        await c.engine._snapshot(c.root, c.base, secrets={"API_TOKEN": token})


async def test_binary_source_without_credentials_is_retained_byte_exact(snapshot_case):
    c = snapshot_case
    blob = b"\x89PNG\x00\xff" + bytes(range(256)) * 40
    (c.root / "image.png").write_bytes(blob)
    files, source, tree = await c.engine._snapshot(c.root, c.base)
    c.state["run"]["source_tree"] = tree
    await c.engine._artifact(RUN, c.root, files, source)
    with zipfile.ZipFile(c.engine.workspaces / (RUN + ".zip")) as archive:
        assert archive.read("image.png") == blob


@pytest.mark.parametrize("staged", [False, True])
async def test_source_or_index_change_before_final_admission_creates_no_tree(snapshot_case, monkeypatch, staged):
    import firefly_forge.engine as engine_module
    import firefly_forge.git_diff_artifacts as module

    c = snapshot_case
    (c.root / "implementation.py").write_text("public original implementation\n")
    original, command = module.retain_git_diff, engine_module.checked
    writes = []

    async def checked_command(argv, *args, **kwargs):
        if "write-tree" in argv:
            writes.append(argv)
        return await command(argv, *args, **kwargs)

    async def change(workspace, base, artifactroot, *, scan_chunk, **kwargs):
        async def callback(raw, final):
            if final:
                (c.root / "implementation.py").write_text("changed while admission awaited\n")
                if staged:
                    await command(["git", "add", "--", "implementation.py"], c.root)
            await scan_chunk(raw, final)

        return await original(workspace, base, artifactroot, scan_chunk=callback, **kwargs)

    monkeypatch.setattr(engine_module, "checked", checked_command)
    monkeypatch.setattr(module, "retain_git_diff", change)
    with pytest.raises((ValueError, RuntimeError)):
        await c.engine._snapshot(c.root, c.base)
    assert writes == []
    assert list(diff_artifact_root(c.engine.workspaces, RUN).iterdir()) == []
