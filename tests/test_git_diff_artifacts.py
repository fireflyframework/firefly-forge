# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Elastic private diff artifacts use actual local Git, never a provider or shared database."""

import base64
import hashlib
import subprocess
import tracemalloc

import pytest


def git(root, *args):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=root, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().decode()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=main")
    git(root, "config", "user.name", "Public fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "product.py").write_text("value = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Public baseline")
    base = git(root, "rev-parse", "HEAD")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    return root, base, artifacts


async def test_elastic_diff_roundtrip_exceeds_old_four_megabytes_with_bounded_pages(source, record_property):
    from firefly_forge.git_diff_artifacts import (
        read_diff_file_page,
        read_diff_index_page,
        read_diff_page,
        retain_git_diff,
    )
    from firefly_forge.git_diff_models import GitDiffBudget, GitDiffRefV1

    root, base, artifacts = source
    for index in range(24):
        (root / f"component-{index}.py").write_text("# public source\n" + ("value = 123456789\n" * 44000))
    git(root, "add", ".")
    seen = []

    async def scan(chunk, final):
        seen.append((len(chunk), final))

    tracemalloc.start()
    try:
        ref = await retain_git_diff(root, base, artifacts, budget=GitDiffBudget(min_free_bytes=0), scan_chunk=scan)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    record_property("diff_bytes", ref.byte_count)
    record_property("traced_retention_peak_bytes", peak)
    record_property(
        "storage_file_max_bytes", max(path.stat().st_size for path in (artifacts / ref.artifact_id).iterdir())
    )
    assert isinstance(ref, GitDiffRefV1) and ref.byte_count > 16 * 1024 * 1024 and ref.file_count == 24
    assert all(size <= 65536 for size, final in seen) and seen[-1] == (0, True) and sum(final for _, final in seen) == 1
    assert "/" not in ref.artifact_id and str(artifacts) not in ref.model_dump_json()
    whole = hashlib.sha256()
    offset = 0
    while True:
        page = read_diff_page(artifacts, ref, offset, 65536)
        raw = base64.b64decode(page.content_base64)
        assert page.offset == offset and len(raw) == page.byte_count <= 65536
        whole.update(raw)
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert whole.hexdigest() == ref.sha256
    assert max(path.stat().st_size for path in (artifacts / ref.artifact_id).iterdir()) <= 65536
    files = read_diff_index_page(artifacts, ref, 0).files
    assert len(files) == 24 and all(700000 < f.byte_count < 1000000 for f in files)
    view = read_diff_file_page(artifacts, ref, files[2].file_index, 0, 100)
    assert base64.b64decode(view.content_base64).startswith(b"diff --git ")
    reloaded = GitDiffRefV1.model_validate_json(ref.model_dump_json())
    assert read_diff_page(artifacts, reloaded, 0, 100) == read_diff_page(artifacts, ref, 0, 100)


async def allow(chunk, final):
    pass


async def retained(source, **kwargs):
    from firefly_forge.git_diff_artifacts import retain_git_diff
    from firefly_forge.git_diff_models import GitDiffBudget

    root, base, artifacts = source
    (root / "unicode space-é.py").write_text('value = "世界🌍"\n' * 5000)
    git(root, "add", ".")
    return await retain_git_diff(
        root,
        base,
        artifacts,
        budget=GitDiffBudget(min_free_bytes=0, chunk_bytes=1024, index_page_files=1, **kwargs),
        scan_chunk=allow,
    )


async def test_utf8_file_pages_restart_and_multiple_merkle_levels(source):
    from firefly_forge.git_diff_artifacts import read_diff_file_page, read_diff_index_page

    ref = await retained(source)
    artifacts = source[2]
    record = read_diff_index_page(artifacts, ref, 0).files[0]
    assert record.path == "unicode space-é.py"
    offset = 0
    whole = hashlib.sha256()
    count = 0
    while True:
        page = read_diff_file_page(artifacts, ref, 0, offset, 127)
        raw = base64.b64decode(page.content_base64)
        raw.decode("utf-8")
        assert page.utf8_complete and len(raw) <= 127
        whole.update(raw)
        count += len(raw)
        if page.next_offset is None:
            break
        assert page.next_offset > offset
        offset = page.next_offset
    assert count == record.byte_count and whole.hexdigest() == record.sha256


@pytest.mark.parametrize(
    "target",
    [
        "chunk-0000000000000000.bin",
        "chunks-0000-0000000000000000.bin",
        "files-0000000000000000.json",
        "files-meta-0000000000000000.bin",
        "manifest.json",
    ],
)
async def test_tampered_payload_or_index_is_not_served(source, target):
    from firefly_forge.git_diff_artifacts import read_diff_index_page, read_diff_page

    ref = await retained(source)
    folder = source[2] / ref.artifact_id
    path = folder / target
    path.chmod(0o600)
    raw = path.read_bytes()
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    path.chmod(0o400)
    with pytest.raises(ValueError):
        if target.startswith("files"):
            read_diff_index_page(source[2], ref, 0)
        else:
            read_diff_page(source[2], ref, 0, 100)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
async def test_link_replacement_never_serves_foreign_payload(source, kind):
    from firefly_forge.git_diff_artifacts import read_diff_page

    ref = await retained(source)
    path = source[2] / ref.artifact_id / "chunk-0000000000000000.bin"
    foreign = source[2] / "foreign"
    foreign.write_bytes(path.read_bytes())
    foreign.chmod(0o400)
    path.unlink()
    if kind == "symlink":
        path.symlink_to(foreign)
    else:
        import os

        os.link(foreign, path)
    with pytest.raises((ValueError, OSError)):
        read_diff_page(source[2], ref, 0, 100)
    assert foreign.exists()


@pytest.mark.parametrize("failure", ["max_bytes", "floor", "scan", "final", "timeout", "cancel"])
async def test_failure_never_publishes_and_cleans_owned_partial(source, failure):
    import asyncio

    from firefly_forge.git_diff_artifacts import retain_git_diff
    from firefly_forge.git_diff_models import GitDiffBudget

    root, base, artifacts = source
    (root / "new.py").write_text("public = 1\n" * 10000)
    git(root, "add", ".")
    seen = []

    async def scan(chunk, final):
        seen.append((len(chunk), final))
        if failure == "scan" or (failure == "final" and final):
            raise ValueError("Admission refused")
        if failure == "timeout":
            await asyncio.sleep(2)
        if failure == "cancel":
            raise asyncio.CancelledError

    budget = GitDiffBudget(
        min_free_bytes=2**63 if failure == "floor" else 0,
        max_bytes=100 if failure == "max_bytes" else None,
        timeout_seconds=1,
    )
    with pytest.raises((ValueError, TimeoutError, asyncio.CancelledError)):
        await retain_git_diff(root, base, artifacts, budget=budget, scan_chunk=scan)
    assert list(artifacts.iterdir()) == []
    assert sum(final for _, final in seen) <= 1


async def test_empty_diff_and_strict_page_bounds(source):
    from firefly_forge.git_diff_artifacts import (
        read_diff_file_page,
        read_diff_index_page,
        read_diff_page,
        retain_git_diff,
    )
    from firefly_forge.git_diff_models import GitDiffBudget

    root, base, artifacts = source
    ref = await retain_git_diff(root, base, artifacts, budget=GitDiffBudget(min_free_bytes=0), scan_chunk=allow)
    assert (ref.byte_count, ref.file_count, ref.index_page_count) == (0, 0, 0)
    assert read_diff_page(artifacts, ref, 0, 1).next_offset is None
    for args in [(True, 1), (0, True), (-1, 1), (1, 1), (0, 1048577)]:
        with pytest.raises(ValueError):
            read_diff_page(artifacts, ref, *args)
    with pytest.raises(ValueError):
        read_diff_index_page(artifacts, ref, 0)
    with pytest.raises(ValueError):
        read_diff_file_page(artifacts, ref, 0, 0, 10)


async def test_many_file_pages_cross_metadata_boundary_and_exact_header_boundaries(source):
    from firefly_forge.git_diff_artifacts import read_diff_file_page, read_diff_index_page, retain_git_diff
    from firefly_forge.git_diff_models import GitDiffBudget

    root, base, artifacts = source
    for index in range(380):
        (root / f"unit-{index:04d}.py").write_text("x = 1\n" * (1 + index % 7))
    git(root, "add", ".")
    ref = await retain_git_diff(
        root,
        base,
        artifacts,
        budget=GitDiffBudget(min_free_bytes=0, chunk_bytes=1024, index_page_files=1),
        scan_chunk=allow,
    )
    assert ref.file_count == ref.index_page_count == 380
    previous = 0
    for index in range(380):
        page = read_diff_index_page(artifacts, ref, index)
        assert len(page.files) == 1 and page.files[0].file_index == index
        file = page.files[0]
        assert file.offset == previous
        previous += file.byte_count
        raw = base64.b64decode(read_diff_file_page(artifacts, ref, index, 0, 1048576).content_base64)
        assert hashlib.sha256(raw).hexdigest() == file.sha256
        assert raw.startswith(f"diff --git a/{file.path} b/{file.path}\n".encode())
        assert page.next_page == (index + 1 if index < 379 else None)
    assert previous == ref.byte_count


async def test_final_callback_has_no_published_reference_and_git_failure_has_no_final(source):
    from firefly_forge.git_diff_artifacts import retain_git_diff
    from firefly_forge.git_diff_models import GitDiffBudget

    root, base, artifacts = source
    (root / "added.py").write_text("value = 2\n")
    git(root, "add", ".")
    calls = []

    async def scan(chunk, final):
        calls.append(final)
        assert all(path.name.startswith(".") for path in artifacts.iterdir())

    ref = await retain_git_diff(root, base, artifacts, budget=GitDiffBudget(min_free_bytes=0), scan_chunk=scan)
    assert calls == [False, True] and (artifacts / ref.artifact_id).is_dir()
    calls.clear()
    with pytest.raises(ValueError, match="Git could not"):
        await retain_git_diff(root, "0" * 40, artifacts, budget=GitDiffBudget(min_free_bytes=0), scan_chunk=scan)
    assert calls == []
    assert len(list(artifacts.iterdir())) == 1


@pytest.mark.parametrize("boundary", range(12))
def test_file_marker_split_preserves_exact_section_hashes(tmp_path, boundary):
    import json
    import os

    from firefly_forge.git_diff_artifacts import _Files
    from firefly_forge.git_diff_models import GitDiffBudget

    folder = tmp_path / "parser"
    folder.mkdir(mode=0o700)
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    first = b"diff --git a/a.py b/a.py\n" + b"+" * (1024 - boundary - 24) + b"\n"
    second = b"diff --git a/b.py b/b.py\n+second\n"
    raw = first + second
    try:
        files = _Files(fd, GitDiffBudget(min_free_bytes=0, chunk_bytes=1024), lambda: None)
        for offset in range(0, len(raw), 1024):
            files.feed(raw[offset : offset + 1024])
        files.feed(b"", True)
    finally:
        os.close(fd)
    records = json.loads((folder / "files-0000000000000000.json").read_bytes())
    assert [(value["path"], value["offset"], value["byte_count"], value["sha256"]) for value in records] == [
        ("a.py", 0, len(first), hashlib.sha256(first).hexdigest()),
        ("b.py", len(first), len(second), hashlib.sha256(second).hexdigest()),
    ]
