# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded, reviewed removal of new generated Vitest entries from the native index only."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.git_workspace import metadata_digest
from firefly_forge.journal import digest
from firefly_forge.workspace_reconciliation import _git, _regular, _sync
from firefly_forge.workspace_tools import WorkspaceTools, _descriptor_path

Data = dict[str, Any]
SANITATION_INTENT = "index_sanitation_intent"
SANITATION_RECEIPT = "index_sanitation_receipt"
MAX_ARTIFACTS = 16
MAX_ARTIFACT_BYTES = 131072
MAX_TOTAL_ARTIFACT_BYTES = 1048576


class IndexSanitationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    original_intent_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    admission_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_catalog_path: str = Field(min_length=1, max_length=4096)
    valid_for_seconds: int = Field(ge=1, le=1800)
    acknowledge_index_only_generated_artifacts: Literal[True]
    acknowledge_preserve_files_source_and_budgets: Literal[True]
    acknowledge_original_scm_outcome_unknown: Literal[True]
    acknowledge_no_queue_or_provider_dispatch: Literal[True]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Index sanitation version is invalid")
        return value

    @field_validator(
        "acknowledge_index_only_generated_artifacts",
        "acknowledge_preserve_files_source_and_budgets",
        "acknowledge_original_scm_outcome_unknown",
        "acknowledge_no_queue_or_provider_dispatch",
        mode="before",
    )
    @classmethod
    def exact_acknowledgement(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicit native index-only sanitation acknowledgements are required")
        return value


def _relative(name: str) -> Path:
    path = Path(name)
    if (
        type(name) is not str
        or not 1 <= len(name) <= 1000
        or path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or any(c in name for c in "\x00\n\r")
        or any(len(p.encode()) > 255 for p in path.parts)
    ):
        raise ValueError("Indexed source requires a bounded confined relative path")
    return path


def _known_cache(name: str) -> bool:
    from firefly_forge.engine import private_path

    if re.fullmatch(r"(?:[^/]+/)*\.cache/vite/vitest/[a-f0-9]{40}/results\.json", name) is None:
        return False
    _relative(name)
    if private_path(name):
        raise ValueError("Private indexed entries cannot be sanitized as generated artifacts")
    return True


def _baseline_names(workspace: Path, base: str) -> set[str]:
    if re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", base) is None:
        raise ValueError("Index sanitation requires the exact original base")
    names = set()
    for record in _git(workspace, "ls-tree", "-r", "-z", "--full-tree", base).split(b"\0"):
        if record:
            name = record.split(b"\t", 1)[1].decode()
            _relative(name)
            names.add(name)
    return names


def _identity(info: os.stat_result) -> dict[str, int]:
    return {
        name: getattr(info, "st_" + name) for name in ("dev", "ino", "mode", "nlink", "size", "mtime_ns", "ctime_ns")
    }


def _indexed_file(tools: WorkspaceTools, name: str, mode: str, object_id: str, *, maximum: int) -> Data:
    parts = tools._parts(name)
    parent = tools._parent_fd(parts, create=False)
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
                raise ValueError("Indexed artifacts require bounded unaliased regular files")
            tools._check_descriptor(stream.fileno())
            if _descriptor_path(stream.fileno()).relative_to(tools.root).as_posix() != name:
                raise ValueError("Indexed artifact spelling differs from its actual opened path")
            blob = hashlib.new("sha1" if len(object_id) == 40 else "sha256")
            blob.update(f"blob {before.st_size}\0".encode())
            content = hashlib.sha256()
            total = 0
            while data := stream.read(1024 * 1024):
                total += len(data)
                if total > maximum:
                    raise ValueError("Indexed artifact exceeds its finite read bound")
                blob.update(data)
                content.update(data)
            tools._check_descriptor(stream.fileno())
            if _descriptor_path(stream.fileno()).relative_to(tools.root).as_posix() != name:
                raise ValueError("Indexed artifact spelling changed during its read")
            after = os.fstat(stream.fileno())
        named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
    finally:
        os.close(parent)
    if (
        _identity(before) != _identity(after)
        or _identity(named) != _identity(before)
        or blob.hexdigest() != object_id
        or mode != ("100755" if before.st_mode & 0o111 else "100644")
    ):
        raise ValueError("Observed native index staged blob or mode differs from protected source")
    return {
        "path": name,
        "mode": mode,
        "object_id": object_id,
        "file_sha256": content.hexdigest(),
        "identity": _identity(before),
    }


def inspect_public_index(workspace: Path, base: str, *, generated: bool = False, component: str = ".") -> Data:
    from firefly_forge.assurance import _manifest
    from firefly_forge.engine import private_path
    from firefly_forge.verification_models import verification_source

    scope = Path(component)
    if scope.is_absolute() or ".." in scope.parts:
        raise ValueError("Index sanitation component is invalid")
    before = _regular(workspace / ".git/index", 1048576)
    metadata = metadata_digest(workspace)
    source = verification_source(workspace)
    manifest = _manifest(workspace, original=True, extra_ignored=frozenset({".cache"}))
    files = {n for n in manifest if not n.endswith("/")}
    baseline = _baseline_names(workspace, base) if generated else set()
    folded_base = {n.casefold() for n in baseline}
    public, artifacts, seen = {}, [], set()
    total_public, total_artifacts = 0, 0
    tools = WorkspaceTools(workspace)
    for record in _git(workspace, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        entry, raw = record.split(b"\t", 1)
        mode, object_id, stage = entry.decode().split(" ")
        name = raw.decode()
        relative = _relative(name)
        if (
            stage != "0"
            or mode not in {"100644", "100755"}
            or name in seen
            or private_path(name)
            or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", object_id) is None
        ):
            raise ValueError("Observed native index contains a foreign or private source entry")
        seen.add(name)
        if name in files:
            item = _indexed_file(tools, name, mode, object_id, maximum=100000000)
            total_public += item["identity"]["size"]
            if total_public > 100000000:
                raise ValueError("Observed source exceeds its existing finite bound")
            public[name] = item
        elif (
            generated
            and _known_cache(name)
            and name.casefold() not in folded_base
            and relative.is_relative_to(scope)
            and mode == "100644"
        ):
            item = _indexed_file(tools, name, mode, object_id, maximum=MAX_ARTIFACT_BYTES)
            total_artifacts += item["identity"]["size"]
            artifacts.append(item)
            if len(artifacts) > MAX_ARTIFACTS or total_artifacts > MAX_TOTAL_ARTIFACT_BYTES:
                raise ValueError("Generated index artifacts exceed their finite bound")
        else:
            raise ValueError("Observed native index contains a foreign or private source entry")
    if set(public) != files:
        raise ValueError("Observed native index does not cover the exact protected public source")
    if (
        _regular(workspace / ".git/index", 1048576) != before
        or metadata_digest(workspace) != metadata
        or verification_source(workspace) != source
        or _git(workspace, "rev-parse", "HEAD").strip().decode() != base
    ):
        raise ValueError("Native index, source, metadata or history changed during inspection")
    return {
        "version": 1,
        "base_revision": base,
        "component": component,
        "index_before": hashlib.sha256(before).hexdigest(),
        "metadata_before": metadata,
        "source_before": source,
        "public_entries": public,
        "artifacts": sorted(artifacts, key=lambda item: item["path"]),
    }


def sanitation_plan(workspace: Path, base: str, *, component: str) -> Data:
    plan = inspect_public_index(workspace, base, generated=True, component=component)
    if not plan["artifacts"]:
        raise ValueError("No supported new generated index artifact requires sanitation")
    return {**plan, "kind": "vite_vitest_index_sanitation_v1"}


def assert_retained_artifacts(workspace: Path, plan: Data) -> None:
    tools = WorkspaceTools(workspace)
    for item in plan["artifacts"]:
        if _indexed_file(tools, item["path"], item["mode"], item["object_id"], maximum=MAX_ARTIFACT_BYTES) != item:
            raise ValueError("Native sanitation changed a retained physical generated file")


def apply_sanitation(workspace: Path, plan: Data) -> Data:
    """Its native caller must commit an exact durable intent before this index-only effect."""
    if sanitation_plan(workspace, plan["base_revision"], component=plan["component"]) != plan:
        raise ValueError("The reviewed native index sanitation plan changed")
    names = [item["path"] for item in plan["artifacts"]]
    _git(workspace, "update-index", "--force-remove", "--", *names)
    _sync(workspace / ".git/index")
    _sync(workspace / ".git")
    after = inspect_public_index(workspace, plan["base_revision"])
    if (
        after["public_entries"] != plan["public_entries"]
        or after["metadata_before"] != plan["metadata_before"]
        or after["source_before"] != plan["source_before"]
    ):
        raise ValueError("Native sanitation changed protected public source or Git metadata")
    assert_retained_artifacts(workspace, plan)
    return {
        "version": 1,
        "index_before": plan["index_before"],
        "index_after": after["index_before"],
        "metadata_digest": plan["metadata_before"],
        "source_digest": plan["source_before"],
        "public_index_digest": digest(plan["public_entries"]),
        "artifacts": plan["artifacts"],
    }


def staging_exclusions(workspace: Path, base: str) -> list[str]:
    """Exclude only new untracked known results; never remove already staged or baseline assets."""
    baseline = {name.casefold() for name in _baseline_names(workspace, base)}
    for raw in _git(workspace, "ls-files", "-z").split(b"\0"):
        if raw:
            name = raw.decode()
            if _known_cache(name) and name.casefold() not in baseline:
                raise ValueError("A new generated cache index entry requires explicit native sanitation")
    excluded = []
    total = 0
    tools = WorkspaceTools(workspace)
    for raw in _git(workspace, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if not raw:
            continue
        name = raw.decode()
        if _known_cache(name) and name.casefold() not in baseline:
            parts = tools._parts(name)
            parent = tools._parent_fd(parts, create=False)
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_ARTIFACT_BYTES:
                        raise ValueError("New generated cache requires a bounded unaliased regular file")
                    total += info.st_size
                    if total > MAX_TOTAL_ARTIFACT_BYTES:
                        raise ValueError("New generated cache staging exceeds its aggregate bound")
                    tools._check_descriptor(fd)
                    if _descriptor_path(fd).relative_to(tools.root).as_posix() != name:
                        raise ValueError("Generated cache spelling differs from its opened path")
                finally:
                    os.close(fd)
            finally:
                os.close(parent)
            excluded.append(":(exclude,literal)" + name)
            if len(excluded) > MAX_ARTIFACTS:
                raise ValueError("New generated cache staging exclusions exceed their finite bound")
    return sorted(excluded)
