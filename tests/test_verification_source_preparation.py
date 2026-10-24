# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Synthetic filesystem checks for asynchronous verification-source preparation."""

import asyncio
import contextvars
import hashlib
import importlib
import json
import os
import threading
import time
from pathlib import Path

import pytest


def implementation():
    path = Path(__file__).parents[1] / "src/firefly_forge/verification_source_preparation.py"
    assert path.is_file(), "The off-loop verification source preparation seam is missing"
    return importlib.import_module("firefly_forge.verification_source_preparation")


def roots(tmp_path):
    source, catalogs = tmp_path / "source", tmp_path / "catalogs"
    source.mkdir()
    catalogs.mkdir(mode=0o700)
    return source, catalogs


async def test_preparation_preserves_legacy_digest_and_runtime_callback_context(
    tmp_path,
):
    module = implementation()
    source, catalogs = roots(tmp_path)
    (source / "public.txt").write_text("public synthetic text\n")
    (source / ".env.example").write_text("EXAMPLE=public\n")
    (source / "omitted.key").write_text("ordinary excluded filename\n")
    (source / "é").mkdir()
    (source / "é" / "index.txt").write_text("世界\n")
    expected = {"é/": str((source / "é").stat().st_mode)}
    for name in ["public.txt", ".env.example", "é/index.txt"]:
        path = source / name
        expected[name] = hashlib.sha256(path.read_bytes()).hexdigest() + str(path.stat().st_mode)
    old = json.dumps(
        {"protocol": "forge-verification-source-v1", "files": expected},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    loop, thread = asyncio.get_running_loop(), threading.get_ident()
    actor = contextvars.ContextVar("synthetic_preparation_actor")
    token = actor.set("current-fixture-owner")
    observations = []

    async def current():
        assert asyncio.get_running_loop() is loop and threading.get_ident() == thread
        assert actor.get() == "current-fixture-owner"
        observations.append("current")

    try:
        result = await module.prepare_verification_source(
            source,
            catalogs,
            budget=module.SourceCatalogBudget(min_free_bytes=0),
            assert_current=current,
        )
    finally:
        actor.reset(token)
    assert result.source_digest == hashlib.sha256(old).hexdigest()
    assert result.reference.file_count == 3 and result.reference.entry_count == 4
    assert len(observations) >= 2
    result.fence()


async def test_streamed_hash_does_not_block_runtime_loop(tmp_path, monkeypatch):
    module = implementation()
    source, catalogs = roots(tmp_path)
    path = source / "large-public.txt"
    path.write_bytes(b"public line\n" * 4096)
    inode = path.stat().st_ino
    original = os.read
    reads = []
    loop_thread = threading.get_ident()

    def delayed_read(fd, width):
        if os.fstat(fd).st_ino == inode:
            reads.append(threading.get_ident())
            time.sleep(0.03)
        return original(fd, width)

    monkeypatch.setattr(os, "read", delayed_read)
    ticks = []

    async def heartbeat():
        for _ in range(8):
            await asyncio.sleep(0.005)
            ticks.append(time.monotonic())

    async def current():
        pass

    await asyncio.gather(
        module.prepare_verification_source(
            source,
            catalogs,
            budget=module.SourceCatalogBudget(min_free_bytes=0, chunk_bytes=1024),
            assert_current=current,
        ),
        heartbeat(),
    )
    assert len(ticks) == 8 and reads and all(t != loop_thread for t in reads)


@pytest.mark.parametrize("change", ["content", "mode", "new_nested_file"])
async def test_revalidation_rejects_changed_source_without_rehashing_contents(tmp_path, monkeypatch, change):
    module = implementation()
    source, catalogs = roots(tmp_path)
    (source / "nested").mkdir()
    path = source / "nested" / "file.txt"
    path.write_text("public synthetic text")

    async def current():
        pass

    result = await module.prepare_verification_source(
        source,
        catalogs,
        budget=module.SourceCatalogBudget(min_free_bytes=0),
        assert_current=current,
    )
    if change == "content":
        path.write_text("changed public text")
    elif change == "mode":
        path.chmod(0o600)
    else:
        (source / "nested" / "new.txt").touch()
    original = os.read

    def no_source_read(fd, width):
        assert os.fstat(fd).st_ino != path.stat().st_ino, "Revalidation reread source bytes"
        return original(fd, width)

    monkeypatch.setattr(os, "read", no_source_read)
    with pytest.raises(ValueError, match="changed"):
        await result.revalidate(assert_current=current)


async def test_final_fence_keeps_original_root_identity(tmp_path):
    module = implementation()
    source, catalogs = roots(tmp_path)
    (source / "file.txt").write_text("public synthetic text")

    async def current():
        pass

    result = await module.prepare_verification_source(
        source,
        catalogs,
        budget=module.SourceCatalogBudget(min_free_bytes=0),
        assert_current=current,
    )
    await result.revalidate(assert_current=current)
    source.chmod((source.stat().st_mode & 0o777) ^ 0o010)
    with pytest.raises(ValueError, match="changed"):
        result.fence()


async def test_preparation_refuses_caller_writer_transaction_before_filesystem_work(tmp_path, monkeypatch):
    module = implementation()
    source, catalogs = roots(tmp_path)
    monkeypatch.setattr(module, "current_unit_of_work", lambda: object())

    async def current():
        raise AssertionError("Must refuse before authority or filesystem work")

    with pytest.raises(ValueError, match="outside.*transaction"):
        await module.prepare_verification_source(
            source,
            catalogs,
            budget=module.SourceCatalogBudget(min_free_bytes=0),
            assert_current=current,
        )
    assert list(catalogs.iterdir()) == []


async def test_cancel_waits_for_offloop_cleanup_without_publishing_catalog(tmp_path, monkeypatch):
    module = implementation()
    source, catalogs = roots(tmp_path)
    path = source / "public.txt"
    path.write_bytes(b"public text\n" * 4096)
    original = os.read
    entered = threading.Event()

    def delayed_read(fd, width):
        if os.fstat(fd).st_ino == path.stat().st_ino:
            entered.set()
            time.sleep(0.02)
        return original(fd, width)

    monkeypatch.setattr(os, "read", delayed_read)

    async def current():
        pass

    task = asyncio.create_task(
        module.prepare_verification_source(
            source,
            catalogs,
            budget=module.SourceCatalogBudget(min_free_bytes=0, chunk_bytes=1024),
            assert_current=current,
        )
    )
    while not entered.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(catalogs.iterdir()) == []


async def test_revoked_current_callback_does_not_release_catalog(tmp_path):
    module = implementation()
    source, catalogs = roots(tmp_path)
    (source / "public.txt").write_text("public synthetic text")
    calls = 0

    async def current():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("Current owner changed")

    with pytest.raises(ValueError, match="owner changed"):
        await module.prepare_verification_source(
            source,
            catalogs,
            budget=module.SourceCatalogBudget(min_free_bytes=0),
            assert_current=current,
        )
    assert list(catalogs.iterdir()) == []


async def test_final_callback_change_is_checked_against_original_prepared_metadata(
    tmp_path,
):
    module = implementation()
    source, catalogs = roots(tmp_path)
    (source / "nested").mkdir()
    path = source / "nested" / "public.txt"
    path.write_text("public synthetic original")

    async def current():
        pass

    result = await module.prepare_verification_source(
        source,
        catalogs,
        budget=module.SourceCatalogBudget(min_free_bytes=0),
        assert_current=current,
    )
    calls = 0

    async def change_at_final_authority():
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text("public synthetic changed")

    with pytest.raises(ValueError, match="changed"):
        await result.revalidate(assert_current=change_at_final_authority)


async def test_existing_verification_source_accepts_prepared_digest_without_a_source_map(tmp_path, monkeypatch):
    module = implementation()
    # An overlay qualification adds the unchanged package directory; full-repository runs need no setting.
    import firefly_forge

    if base := os.environ.get("FORGE_CATALOG_REFERENCE_PACKAGE"):
        monkeypatch.setattr(firefly_forge, "__path__", [*firefly_forge.__path__, base])
    models = importlib.import_module("firefly_forge.verification_models")
    assurance = importlib.import_module("firefly_forge.assurance")
    source, catalogs = roots(tmp_path)
    (source / "é").mkdir()
    (source / "é" / "file.txt").write_text("public synthetic 世界\n")
    old = models.verification_source(source)

    async def current():
        pass

    prepared = await module.prepare_verification_source(
        source,
        catalogs,
        budget=module.SourceCatalogBudget(min_free_bytes=0),
        assert_current=current,
    )
    await prepared.revalidate(assert_current=current)

    def no_map(*args, **kwargs):
        raise AssertionError("The prepared caller reconstructed a full project map")

    monkeypatch.setattr(assurance, "_manifest", no_map)
    assert models.verification_source(source, prepared=prepared) == old == prepared.source_digest
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="another workspace"):
        models.verification_source(other, prepared=prepared)


async def test_cancel_during_authority_wait_drains_without_waiting_for_its_deadline(
    tmp_path,
):
    module = implementation()
    source, catalogs = roots(tmp_path)
    (source / "public.txt").write_text("public synthetic text")
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting_authority():
        entered.set()
        await release.wait()

    task = asyncio.create_task(
        module.prepare_verification_source(
            source,
            catalogs,
            budget=module.SourceCatalogBudget(min_free_bytes=0),
            assert_current=waiting_authority,
        )
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0.05)
    prompt = task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert prompt, "Cancellation waited for the authority callback/catalog deadline"
    assert list(catalogs.iterdir()) == []
