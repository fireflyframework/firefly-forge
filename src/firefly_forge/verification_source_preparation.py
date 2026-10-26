# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Off-loop source preparation for short native verification admission fences."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import os
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from pyfly.data.transaction import current_unit_of_work

from firefly_forge.source_catalog import (
    SourceCatalogBudget,
    SourceCatalogRefV1,
    _directory,
    _read,
    _stamp,
    retain_source_catalog,
    revalidate_source_catalog,
)

Authority = Callable[[], Awaitable[None]]


def _outside_transaction() -> None:
    if current_unit_of_work() is not None:
        raise ValueError("Source catalog preparation must run outside a caller transaction")


def _identity(path: Path, *, private: bool = False) -> tuple[int, ...]:
    descriptor = _directory(path, private=private)
    try:
        value = tuple(_stamp(os.fstat(descriptor))[:5])
        if tuple(_stamp(path.lstat())[:5]) != value:
            raise ValueError("Source directory changed during its identity read")
        return value
    finally:
        os.close(descriptor)


def _manifest_identity(root: Path, reference: SourceCatalogRefV1) -> tuple[int, ...]:
    parent = _directory(root, private=True)
    folder = None
    try:
        folder = _directory(reference.artifact_id, parent, private=True)
        parent_identity = _stamp(os.fstat(parent))[:5]
        folder_identity = _stamp(os.fstat(folder))[:5]

        def assert_paths() -> None:
            if (
                _stamp(os.fstat(parent))[:5] != parent_identity
                or _stamp(os.stat(root, follow_symlinks=False))[:5] != parent_identity
                or _stamp(os.fstat(folder))[:5] != folder_identity
                or _stamp(os.stat(reference.artifact_id, dir_fd=parent, follow_symlinks=False))[:5] != folder_identity
            ):
                raise ValueError("Prepared verification catalog path changed during its identity read")

        assert_paths()
        before = _stamp(os.stat("manifest.json", dir_fd=folder, follow_symlinks=False))
        encoded = _read(folder, "manifest.json", 1048576)
        after = _stamp(os.stat("manifest.json", dir_fd=folder, follow_symlinks=False))
        assert_paths()
        # Post-publication ctime can change without payload changes on Documents filesystems.
        # The immutable descriptor read still checks full stamps, including ctime, during I/O.
        if before != after or hashlib.sha256(encoded).hexdigest() != reference.manifest_sha256:
            raise ValueError("Prepared verification catalog manifest changed during its identity read")
        return (*parent_identity[:2], *folder_identity[:2], *after[:-1])
    finally:
        if folder is not None:
            os.close(folder)
        os.close(parent)


async def _offloop[T](
    operation: Callable[[Authority, threading.Event], Awaitable[T]],
    assert_current: Authority,
) -> T:
    _outside_transaction()
    loop = asyncio.get_running_loop()
    authority_context = contextvars.copy_context()
    cancelled = threading.Event()
    cancel_worker: concurrent.futures.Future[Callable[[], None]] = concurrent.futures.Future()

    async def current() -> None:
        if cancelled.is_set():
            raise asyncio.CancelledError
        future: concurrent.futures.Future[None] = concurrent.futures.Future()

        def schedule() -> None:
            if future.cancelled():
                return

            async def check() -> None:
                _outside_transaction()
                await assert_current()
                _outside_transaction()

            task = loop.create_task(check())

            def finish(done: asyncio.Task[None]) -> None:
                if future.done():
                    if not done.cancelled():
                        done.exception()
                    return
                if done.cancelled():
                    future.cancel()
                elif (error := done.exception()) is not None:
                    future.set_exception(error)
                else:
                    future.set_result(None)

            task.add_done_callback(finish)
            future.add_done_callback(lambda done: loop.call_soon_threadsafe(task.cancel) if done.cancelled() else None)

        loop.call_soon_threadsafe(schedule, context=authority_context.copy())
        await asyncio.wrap_future(future)
        if cancelled.is_set():
            raise asyncio.CancelledError

    def worker() -> T:
        async def execute() -> T:
            worker_loop = asyncio.get_running_loop()
            worker_task = asyncio.current_task()
            assert worker_task is not None

            def cancel() -> None:
                if not worker_loop.is_closed():
                    worker_loop.call_soon_threadsafe(worker_task.cancel)

            cancel_worker.set_result(cancel)
            return await operation(current, cancelled)

        return contextvars.Context().run(lambda: asyncio.run(execute()))

    task = asyncio.create_task(asyncio.to_thread(worker))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        cancel_worker.add_done_callback(lambda ready: ready.result()())
        # The worker must close descriptors and remove its partial catalog before cancellation returns.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()
        raise
    _outside_transaction()
    return result


@dataclass(frozen=True)
class PreparedVerificationSource:
    workspace: Path
    catalog_root: Path
    reference: SourceCatalogRefV1
    _source_identity: tuple[int, ...]
    _manifest_identity: tuple[int, ...]

    @property
    def source_digest(self) -> str:
        return self.reference.source_digest

    def fence(self) -> None:
        """Bounded manifest fence; the caller must still hold exclusive workspace ownership."""
        if _identity(self.workspace) != self._source_identity:
            raise ValueError("Verification source root changed after preparation")
        if _manifest_identity(self.catalog_root, self.reference) != self._manifest_identity:
            raise ValueError("Prepared verification catalog changed after preparation")

    async def revalidate(self, *, assert_current: Authority) -> None:
        _outside_transaction()
        self.fence()

        async def operation(current: Authority, cancelled: threading.Event) -> None:
            await revalidate_source_catalog(
                self.workspace,
                self.catalog_root,
                self.reference,
                assert_current=current,
                cancel_requested=cancelled.is_set,
            )

        await _offloop(operation, assert_current)
        self.fence()


async def prepare_verification_source(
    workspace: Path,
    catalog_root: Path,
    *,
    budget: SourceCatalogBudget,
    assert_current: Authority,
) -> PreparedVerificationSource:
    """Prepare exact legacy identity without blocking runtime renewal or carrying a writer unit."""
    _outside_transaction()
    before = _identity(workspace)

    async def operation(current: Authority, cancelled: threading.Event) -> SourceCatalogRefV1:
        return await retain_source_catalog(
            workspace,
            catalog_root,
            budget=budget,
            assert_current=current,
            cancel_requested=cancelled.is_set,
        )

    reference = await _offloop(operation, assert_current)
    prepared = PreparedVerificationSource(
        workspace,
        catalog_root,
        reference,
        before,
        _manifest_identity(catalog_root, reference),
    )
    prepared.fence()
    return prepared


_CURRENT_VERIFICATION_SOURCE: contextvars.ContextVar[PreparedVerificationSource | None] = contextvars.ContextVar(
    "forge_prepared_verification_source", default=None
)


def current_verification_source(workspace: Path) -> PreparedVerificationSource | None:
    prepared = _CURRENT_VERIFICATION_SOURCE.get()
    if prepared is not None and prepared.workspace != workspace:
        raise ValueError("Prepared verification source belongs to another workspace")
    return prepared


@contextmanager
def verification_source_scope(prepared: PreparedVerificationSource) -> Iterator[None]:
    _outside_transaction()
    prepared.fence()
    token = _CURRENT_VERIFICATION_SOURCE.set(prepared)
    try:
        yield
    finally:
        _CURRENT_VERIFICATION_SOURCE.reset(token)


def verification_catalog_root(data_path: Path, run_id: str) -> Path:
    from uuid import UUID

    if UUID(run_id).hex != run_id:
        raise ValueError("A canonical run identity is required for its source catalog")
    root = data_path.parent / "verification-source-catalogs"
    for path in (root, root / run_id):
        with suppress(FileExistsError):
            path.mkdir(mode=0o700)
        descriptor = _directory(path, private=True)
        os.close(descriptor)
    return root / run_id
