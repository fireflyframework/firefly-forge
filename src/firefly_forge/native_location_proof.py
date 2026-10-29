# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Selected relocation inputs are measured; unavailable history is never defaulted."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from firefly_forge.agentic_checkpoint_invocation import workspace_cut
from firefly_forge.journal import digest
from firefly_forge.native_location_files import _identity, _root_identity, read_storage_identity
from firefly_forge.native_location_models import NativeLocationFilesV1, NativeLocationPlanV1
from firefly_forge.source_catalog import SourceCatalogBudget, _json, _stamp
from firefly_forge.store import Store
from firefly_forge.verification_dependencies import (
    PreparedMetadataCut,
    prepare_dependency_capture,
    prepare_public_source_cut,
)

Data = dict[str, Any]
_GLOBAL_REQUIRED = frozenset(
    {
        "runtime-storage.identity",
        "configuration/providers.json",
        "configuration/source-budget-policy.json",
    }
)


def required_native_files(runs: list[Data]) -> set[str]:
    required = set(_GLOBAL_REQUIRED)
    for run in runs:
        for field in ("config_revision", "review_config_revision"):
            revision = run.get(field)
            if revision is not None:
                if type(revision) is not str or not re.fullmatch(r"[a-f0-9]{32}", revision):
                    raise ValueError("Selected native run configuration reference is invalid")
                required.add(f"configuration/revisions/{revision}.json")
        for field in ("model_selection", "review_model_selection"):
            selection = run.get(field)
            if selection:
                revision = selection.get("config_revision")
                if type(revision) is not str or not re.fullmatch(r"[a-f0-9]{32}", revision):
                    raise ValueError("Selected immutable model configuration reference is invalid")
                required.add(f"configuration/revisions/{revision}.json")
                required.add(f"configuration/model-profile-revisions/{revision}.json")
    return required


@dataclass(frozen=True)
class _RequiredFile:
    path: Path
    stamp: tuple[int, ...]
    sha256: str

    def fence(self) -> None:
        if self.path.resolve() != self.path or _identity(self.path) != self.stamp:
            raise ValueError("A required native relocation input changed or became unavailable")


async def _required_file(path: Path, budget: SourceCatalogBudget) -> _RequiredFile:
    before = path.lstat()
    if (
        getattr(before, "st_flags", 0) & 0x40000000
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_mode & 0o077
        or before.st_nlink != 1
        or path.resolve() != path
        or (budget.max_source_bytes is not None and before.st_size > budget.max_source_bytes)
    ):
        raise ValueError("Required native relocation inputs must be locally available private ordinary files")
    value = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    deadline = time.monotonic() + budget.timeout_seconds
    with os.fdopen(descriptor, "rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError("Required native input changed before reading")
        while raw := stream.read(budget.chunk_bytes):
            value.update(raw)
            if time.monotonic() >= deadline:
                raise TimeoutError("The selected native input capture budget expired")
            await asyncio.sleep(0)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError("Required native input changed while reading")
    captured = _RequiredFile(path, (*_stamp(before), getattr(before, "st_flags", 0)), value.hexdigest())
    captured.fence()
    return captured


@dataclass(frozen=True)
class PreparedNativeLocationFiles:
    plan: NativeLocationPlanV1
    digest: str
    _files: tuple[_RequiredFile, ...]
    _cuts: tuple[PreparedMetadataCut, ...]

    async def assert_current(self) -> None:
        for value in self._files:
            value.fence()
        for cut in self._cuts:
            await cut.assert_current()
        for value in self._files:
            value.fence()


async def prepare_location_files(
    store: Store,
    plan: NativeLocationPlanV1,
    manifest: NativeLocationFilesV1,
    runs: dict[str, Data],
    expected_workspaces: dict[str, dict[str, str]],
    scratch: Path,
    budget: SourceCatalogBudget,
) -> PreparedNativeLocationFiles:
    target = Path(plan.target_storage)
    identity = read_storage_identity(target)
    if set(runs) != {value.run_id for value in plan.workspaces} or set(expected_workspaces) != set(runs):
        raise ValueError("Relocation requires exact selected native run and checkpoint source bindings")
    declared = {value.path for value in manifest.required_files}
    if not required_native_files(list(runs.values())).issubset(declared):
        raise ValueError("Selected immutable native configuration bytes remain unavailable")
    files = []
    for value in manifest.required_files:
        captured = await _required_file(target / value.path, budget)
        if captured.sha256 != value.sha256:
            raise ValueError("Required relocated native bytes differ from their reviewed retained identity")
        files.append(captured)
    cuts: list[PreparedMetadataCut] = []
    workspaces = {}
    runtimes = {}

    async def current() -> None:
        identity.fence()
        for value in files:
            value.fence()

    for selected in plan.workspaces:
        root = Path(selected.target)
        expected = expected_workspaces[selected.run_id]
        if expected.get("path") != selected.source:
            raise ValueError("Relocated workspace differs from its original logical checkpoint namespace")
        public = await prepare_public_source_cut(root, scratch, budget)
        git = await prepare_dependency_capture([root / ".git"], scratch, budget, current, portable_root=root)
        actual = await workspace_cut(store, root, assert_current=current)
        if _json(actual) != _json({**expected, "path": selected.target}):
            raise ValueError("Relocated workspace source, HEAD or private Git metadata changed")
        await public.assert_current()
        await git.assert_current()
        cuts.extend((public, git))
        workspaces[selected.run_id] = actual
    for selected_runtime in plan.verification_runtimes:
        root = Path(selected_runtime.target)
        captured_runtime = await prepare_dependency_capture([root], scratch, budget, current, portable_root=root)
        if captured_runtime.digest != selected_runtime.target_content_digest:
            raise ValueError("Selected verification runtime differs from its explicit reviewed baseline")
        cuts.append(captured_runtime)
        runtimes[selected_runtime.run_id + ":" + selected_runtime.source] = captured_runtime.digest
    proof = PreparedNativeLocationFiles(
        plan,
        digest(
            {
                "manifest": manifest.model_dump(mode="json"),
                "workspaces": workspaces,
                "runtimes": runtimes,
                "storage_root": _root_identity(target),
                "required_native_files": [{"path": str(value.path), "stamp": value.stamp} for value in files],
                "metadata_cuts": [value.metadata_cut for value in cuts],
            }
        ),
        tuple(files),
        tuple(cuts),
    )
    await proof.assert_current()
    return proof
