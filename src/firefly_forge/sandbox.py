# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded command execution with persistent ownership and Docker effects."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
import uuid
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from pyfly.container import service

from firefly_forge.configuration import redact_secrets
from firefly_forge.journal import Journal, digest
from firefly_forge.process import ProcessResult
from firefly_forge.runner_backends import RunnerBackends
from firefly_forge.runner_backends import bounded_process as bounded_process
from firefly_forge.sandbox_models import (
    SandboxInput,
    bounded_argv,
    capture_sandbox,
    pinned_image,
    validate_sandbox_snapshot,
)
from firefly_forge.secrets import contains_known_credential, redact_environment
from firefly_forge.store import Store
from firefly_forge.workspace_tools import _private_name

if TYPE_CHECKING:
    from firefly_forge.runner_pool_worker import RunnerPoolWorker


class SandboxReconciliationRequired(RuntimeError):
    pass


class SandboxPublicationReconciliationRequired(SandboxReconciliationRequired):
    def __init__(self, backup_path: Path) -> None:
        super().__init__("Source publication rollback is uncertain; the original tree was retained for reconciliation")
        self.backup_path = backup_path


def _private_path(parts: tuple[str, ...]) -> bool:
    return any(
        _private_name(part)
        or part in {".git", ".ssh", ".aws", ".azure", ".docker", ".forge-sandbox-results"}
        or part == ".env"
        or part.startswith(".env.")
        for part in parts
    )


def _directory_fd(name: str | Path, *, parent: int | None = None) -> int:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        raise ValueError("Workspace contains a link or changed directory") from exc


def _regular_data(parent: int, name: str, limit: int, *, private: bool = False) -> tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except OSError as exc:
        raise ValueError("Workspace contains a link or changed file") from exc
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or (not private and before.st_nlink != 1):
            raise ValueError("Archive workspace permits only regular files without links")
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
        if len(data) > limit:
            raise ValueError("Workspace exceeds the archive byte limit")
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("Workspace changed while reading a regular file")
        return data, after


def _parent_fd(root: int, parts: tuple[str, ...], *, create: bool = False) -> int:
    directory = os.dup(root)
    try:
        for part in parts[:-1]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o755, dir_fd=directory)
            child = _directory_fd(part, parent=directory)
            os.close(directory)
            directory = child
        return directory
    except BaseException:
        os.close(directory)
        raise


def pack_workspace(
    workspace: Path,
    limit: int,
    *,
    metadata: bool = False,
    registry: dict[str, str] | None = None,
    _root_fd: int | None = None,
) -> tuple[bytes, str]:
    output = io.BytesIO()
    total = 0
    root_fd = os.dup(_root_fd) if _root_fd is not None else _directory_fd(workspace)
    try:
        with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as stream:
            root = tarfile.TarInfo(".")
            root.type, root.mode, root.uid, root.gid = tarfile.DIRTYPE, 0o755, os.getuid(), os.getgid()
            stream.addfile(root)

            def visit(directory: int, parts: tuple[str, ...]) -> None:
                nonlocal total
                for name in sorted(os.listdir(directory)):
                    path = (*parts, name)
                    if not metadata and _private_path(path):
                        continue
                    relative = "/".join(path)
                    if contains_known_credential(relative, registry):
                        raise ValueError("Workspace archive cannot contain configured credentials")
                    observed = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    item = tarfile.TarInfo(relative)
                    item.uid, item.gid = os.getuid(), os.getgid()
                    item.mode = 0o755 if stat.S_ISDIR(observed.st_mode) or observed.st_mode & 0o111 else 0o644
                    if stat.S_ISDIR(observed.st_mode):
                        child = _directory_fd(name, parent=directory)
                        try:
                            item.type = tarfile.DIRTYPE
                            stream.addfile(item)
                            visit(child, path)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(observed.st_mode):
                        data, actual = _regular_data(directory, name, limit - total)
                        if (observed.st_dev, observed.st_ino) != (actual.st_dev, actual.st_ino):
                            raise ValueError("Workspace changed before reading a regular file")
                        total += len(data)
                        if contains_known_credential(data.decode("utf-8", errors="ignore"), registry):
                            raise ValueError("Workspace archive cannot contain configured credentials")
                        item.size = len(data)
                        stream.addfile(item, io.BytesIO(data))
                    else:
                        raise ValueError("Archive workspace permits only regular files and directories without links")
                    if output.tell() > limit + 1_048_576:
                        raise ValueError("Workspace exceeds the archive byte limit")

            visit(root_fd, ())
    finally:
        os.close(root_fd)
    data = output.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def unpack_workspace(
    data: bytes, destination: Path, limit: int, *, registry: dict[str, str] | None = None, _root_fd: int | None = None
) -> None:
    if destination.is_symlink():
        raise ValueError("Workspace archive destination contains a link")
    if len(data) > limit + 1_048_576:
        raise ValueError("Workspace exceeds the archive byte limit")
    if contains_known_credential(data.decode("utf-8", errors="ignore"), registry):
        raise ValueError("Workspace archive cannot contain configured credentials")
    validated: list[tuple[PurePosixPath, tarfile.TarInfo]] = []
    total = 0
    seen: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as stream:
        for item in stream:
            path = PurePosixPath(item.name)
            if path.is_absolute() or ".." in path.parts or "\\" in item.name or "\x00" in item.name:
                raise ValueError("Workspace archive path is not confined")
            if not (item.isfile() or item.isdir()) or getattr(item, "sparse", None):
                raise ValueError("Workspace archive permits only regular files and directories")
            if _private_path(path.parts) or path.as_posix() == ".":
                continue
            if path.as_posix() in seen:
                raise ValueError("Workspace archive contains duplicate paths")
            seen.add(path.as_posix())
            total += item.size
            if total > limit:
                raise ValueError("Workspace exceeds the archive byte limit")
            validated.append((path, item))
        file_paths = {path for path, item in validated if item.isfile()}
        if any(parent in file_paths for path, _ in validated for parent in path.parents):
            raise ValueError("Workspace archive path conflicts with a file")
        for path, _ in validated:
            target = destination.joinpath(*path.parts)
            if any(parent.is_symlink() for parent in [target, *target.parents]):
                raise ValueError("Workspace archive destination contains a link")
        destination.mkdir(parents=True, exist_ok=True)
        root = os.dup(_root_fd) if _root_fd is not None else _directory_fd(destination)
        try:
            for path, item in validated:
                directory = _parent_fd(root, path.parts, create=True)
                try:
                    if item.isdir():
                        with suppress(FileExistsError):
                            os.mkdir(path.name, 0o755, dir_fd=directory)
                        child = _directory_fd(path.name, parent=directory)
                        os.close(child)
                    else:
                        descriptor = os.open(
                            path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
                        )
                        with os.fdopen(descriptor, "wb") as output:
                            source = stream.extractfile(item)
                            assert source is not None
                            output.write(source.read())
                            output.flush()
                            os.fchmod(output.fileno(), 0o755 if item.mode & 0o111 else 0o644)
                            os.fsync(output.fileno())
                finally:
                    os.close(directory)
            os.fsync(root)
        finally:
            os.close(root)


def _exchange_trees(parent: int, first: str, second: str) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameatx_np" if sys.platform == "darwin" else "renameat2", None)
    if function is None:
        raise OSError(errno.ENOTSUP, "Atomic directory exchange is unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(parent, os.fsencode(first), parent, os.fsencode(second), 2) != 0:
        failure = ctypes.get_errno()
        raise OSError(failure, "Atomic directory exchange failed")


def _clear_staging(directory: int) -> None:
    for name in os.listdir(directory):
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = _directory_fd(name, parent=directory)
            try:
                _clear_staging(child)
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                identity = os.fstat(child)
                if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                    raise ValueError("Owned staging directory changed during cleanup")
                os.rmdir(name, dir_fd=directory)
            finally:
                os.close(child)
        else:
            os.unlink(name, dir_fd=directory)


def _private_copy(source: int, destination: int | None, limit: int) -> str:
    records: list[tuple[Any, ...]] = []
    total = 0

    def has_private(directory: int, parts: tuple[str, ...]) -> bool:
        for name in os.listdir(directory):
            path = (*parts, name)
            if _private_path(path):
                return True
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = _directory_fd(name, parent=directory)
                try:
                    if has_private(child, path):
                        return True
                finally:
                    os.close(child)
        return False

    def visit(directory: int, target: int | None, parts: tuple[str, ...], preserving: bool = False) -> None:
        nonlocal total
        for name in sorted(os.listdir(directory)):
            path = (*parts, name)
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            keep = preserving or _private_path(path)
            if stat.S_ISDIR(info.st_mode):
                child = _directory_fd(name, parent=directory)
                target_child = None
                try:
                    if not keep and not has_private(child, path):
                        continue
                    if target is not None:
                        with suppress(FileExistsError):
                            os.mkdir(name, 0o700, dir_fd=target)
                        target_child = _directory_fd(name, parent=target)
                    visit(child, target_child, path, keep)
                    if keep:
                        records.append((path, info.st_mode, info.st_uid, info.st_gid, info.st_mtime_ns))
                        if target_child is not None:
                            os.fchown(target_child, info.st_uid, info.st_gid)
                            os.fchmod(target_child, stat.S_IMODE(info.st_mode))
                            os.utime(target_child, ns=(info.st_atime_ns, info.st_mtime_ns))
                            os.fsync(target_child)
                finally:
                    os.close(child)
                    if target_child is not None:
                        os.close(target_child)
                continue
            if not keep:
                continue
            if stat.S_ISLNK(info.st_mode):
                value = os.readlink(name, dir_fd=directory)
                records.append((path, info.st_mode, value))
                if target is not None:
                    os.symlink(value, name, dir_fd=target)
            elif stat.S_ISREG(info.st_mode):
                data, actual = _regular_data(directory, name, limit - total, private=True)
                if (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
                    raise ValueError("Private workspace metadata changed")
                total += len(data)
                records.append(
                    (
                        path,
                        actual.st_mode,
                        actual.st_uid,
                        actual.st_gid,
                        actual.st_mtime_ns,
                        hashlib.sha256(data).hexdigest(),
                    )
                )
                if target is not None:
                    descriptor = os.open(
                        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target
                    )
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(data)
                        output.flush()
                        os.fchown(output.fileno(), actual.st_uid, actual.st_gid)
                        os.fchmod(output.fileno(), stat.S_IMODE(actual.st_mode))
                        os.utime(output.fileno(), ns=(actual.st_atime_ns, actual.st_mtime_ns))
                        os.fsync(output.fileno())
            else:
                raise ValueError("Private workspace metadata contains a special file")

    visit(source, destination, ())
    return hashlib.sha256(repr(records).encode()).hexdigest()


def collect_workspace(
    data: bytes, workspace: Path, limit: int, original_digest: str, *, registry: dict[str, str] | None = None
) -> dict[str, Any]:
    parent = _directory_fd(workspace.parent)
    try:
        original = _directory_fd(workspace.name, parent=parent)
    except BaseException:
        os.close(parent)
        raise
    name = ".forge-archive-" + uuid.uuid4().hex
    staged_fd = None
    published = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        staged = workspace.parent / name
        staged_fd = _directory_fd(name, parent=parent)
        unpack_workspace(data, staged, limit, registry=registry, _root_fd=staged_fd)
        result_digest = pack_workspace(staged, limit, registry=registry, _root_fd=staged_fd)[1]
        current_digest = pack_workspace(workspace, limit, registry=registry, _root_fd=original)[1]
        if current_digest == result_digest:
            return {}
        if current_digest != original_digest:
            raise ValueError("Host workspace changed during archive execution; results require reconciliation")
        private_digest = _private_copy(original, staged_fd, limit)
        original_mode = os.fstat(original)
        os.fchown(staged_fd, original_mode.st_uid, original_mode.st_gid)
        os.fchmod(staged_fd, stat.S_IMODE(original_mode.st_mode))
        os.fsync(staged_fd)
        observed = os.stat(workspace.name, dir_fd=parent, follow_symlinks=False)
        actual = os.fstat(original)
        if (observed.st_dev, observed.st_ino) != (actual.st_dev, actual.st_ino):
            raise ValueError("Host workspace root changed before publication")
        if pack_workspace(workspace, limit, registry=registry, _root_fd=original)[1] != original_digest:
            raise ValueError("Host workspace changed before publication")
        _exchange_trees(parent, workspace.name, name)
        published = True
        try:
            backup_entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            active_entry = os.stat(workspace.name, dir_fd=parent, follow_symlinks=False)
            stage_identity = os.fstat(staged_fd)
            if (
                active_entry.st_dev,
                active_entry.st_ino,
            ) != (stage_identity.st_dev, stage_identity.st_ino):
                raise SandboxPublicationReconciliationRequired(workspace.parent / name)
            if (backup_entry.st_dev, backup_entry.st_ino) != (actual.st_dev, actual.st_ino):
                raise ValueError("Host workspace root changed during publication")
            if (
                pack_workspace(workspace, limit, registry=registry, _root_fd=original)[1] != original_digest
                or _private_copy(original, None, limit) != private_digest
            ):
                raise ValueError("Host workspace changed during publication")
            os.fsync(parent)
            return {"workspace_backup_path": str(staged), "workspace_backup_sha256": original_digest}
        except SandboxPublicationReconciliationRequired:
            raise
        except BaseException as failure:
            try:
                _exchange_trees(parent, workspace.name, name)
            except BaseException:
                raise SandboxPublicationReconciliationRequired(workspace.parent / name) from failure
            published = False
            os.fsync(parent)
            raise
    finally:
        try:
            if staged_fd is not None and not published:
                _clear_staging(staged_fd)
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                identity = os.fstat(staged_fd)
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    os.rmdir(name, dir_fd=parent)
        finally:
            os.close(original)
            if staged_fd is not None:
                os.close(staged_fd)
            os.close(parent)


@service
class SandboxExecutor:
    def __init__(self, store: Store, journal: Journal, backends: RunnerBackends | None = None) -> None:
        self.store, self.journal = store, journal
        self.backends = backends if backends is not None else RunnerBackends()
        self.pool_worker: RunnerPoolWorker | None = None
        self.owner = hashlib.sha256((str(store.path.parent.resolve()) + store.schema).encode()).hexdigest()[:24]
        self.credential_registry: Callable[[], dict[str, str]] = dict
        transfer_image = os.environ.get("FORGE_SANDBOX_TRANSFER_IMAGE")
        self.transfer_image = pinned_image(transfer_image) if transfer_image else None
        self._capacity = asyncio.Semaphore(2)
        self._run_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._ordinals: dict[tuple[str, str, int], int] = defaultdict(int)

    def _safe_result(self, result: ProcessResult, *, browser: bool = False) -> ProcessResult:
        if browser and contains_known_credential(result.output, self.credential_registry()):
            raise ValueError("Browser evidence containing configured credentials cannot be admitted")
        return ProcessResult(
            result.argv, result.exit_code, redact_secrets(redact_environment(result.output), self.credential_registry())
        )

    async def _docker(
        self,
        config: SandboxInput,
        argv: list[str],
        timeout: int = 30,
        limit: int = 500_000,
        *,
        input_data: bytes | None = None,
    ) -> ProcessResult:
        if config.mode != "docker":
            raise ValueError("Docker control requires Docker mode")
        return await self.backends.select(config).run(
            config, argv, self.store.path.parent, timeout, limit, input_data=input_data
        )

    async def probe(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if snapshot.get("mode") == "pool":
            if self.pool_worker is None:
                return {"available": False, "mode": "pool", "reason": "Factory pool worker is not configured"}
            return await self.pool_worker.probe(snapshot)
        config = validate_sandbox_snapshot(snapshot)
        if config.pool_binding is not None:
            return {
                "available": False,
                "mode": config.mode,
                "status": "not_implemented",
                "reason": "Runner pool execution is not enabled",
            }
        if config.mode == "host":
            return {"available": True, "mode": "host", "reason": snapshot["reason"]}
        try:
            result = await self._docker(config, ["image", "inspect", str(config.image), "--format", "{{.Id}}"])
            return {
                "available": result.exit_code == 0,
                "mode": "docker",
                "reason": "Pinned image is cached" if result.exit_code == 0 else "Pinned image is not cached",
            }
        except (OSError, RuntimeError, TimeoutError):
            return {"available": False, "mode": "docker", "reason": "Selected Docker context is unavailable"}

    async def _archive_prepare(
        self, config: SandboxInput, workspace: Path, git_root: Path | None, name: str, labels: dict[str, str]
    ) -> dict[str, str]:
        helper = name + "-transfer"
        if not self.transfer_image:
            raise ValueError("Archive execution requires an administrator-configured pinned transfer image")
        args = [
            "create",
            "--pull=never",
            "--name",
            helper,
            "--read-only",
            "--network=none",
            "--cap-drop=ALL",
            "--cap-add=CHOWN",
            "--user=0:0",
            "--cpus",
            str(config.cpus),
            "--memory",
            f"{config.memory_mb}m",
            "--memory-swap",
            f"{config.memory_mb}m",
            "--pids-limit",
            str(config.pids_limit),
            "--security-opt=no-new-privileges:true",
            "--mount",
            "type=volume,dst=/workspace,volume-nocopy",
            "--mount",
            "type=volume,dst=/forge-git,volume-nocopy",
        ]
        for key, value in labels.items():
            args += ["--label", f"{key}={value}"]
        script = (
            "import os\n"
            "for root in ('/workspace','/forge-git'):\n"
            f" os.chown(root,{os.getuid()},{os.getgid()},follow_symlinks=False)\n"
            " for base,dirs,files in os.walk(root,followlinks=False):\n"
            "  for name in dirs+files:\n"
            f"   os.chown(os.path.join(base,name),{os.getuid()},{os.getgid()},follow_symlinks=False)\n"
        )
        args += ["--entrypoint", "/app/.venv/bin/python", self.transfer_image, "-I", "-S", "-c", script]
        result = await self._docker(config, args)
        if result.exit_code:
            raise RuntimeError("Selected Docker runner could not create the archive transfer container")
        archive, _ = pack_workspace(workspace, config.workspace_max_bytes, registry=self.credential_registry())
        copied = await self._docker(config, ["cp", "--archive", "-", helper + ":/workspace"], input_data=archive)
        if copied.exit_code:
            raise RuntimeError("Selected Docker runner could not receive the workspace archive")
        if git_root is not None:
            metadata = git_root / ".git"
            if metadata.is_symlink() or not metadata.is_dir():
                raise ValueError("Verification requires a real Git metadata directory")
            archive, _ = pack_workspace(
                metadata, config.workspace_max_bytes, metadata=True, registry=self.credential_registry()
            )
            copied = await self._docker(config, ["cp", "--archive", "-", helper + ":/forge-git"], input_data=archive)
            if copied.exit_code:
                raise RuntimeError("Selected Docker runner could not receive the Git metadata archive")
        initialized = await self._docker(config, ["start", "--attach", helper])
        if initialized.exit_code:
            raise RuntimeError("Selected Docker runner could not initialize archive ownership")
        observed = await self._inspect(config, helper, labels)
        assert observed is not None
        if observed["State"]["Status"] != "exited" or observed["State"]["ExitCode"] != 0:
            raise SandboxReconciliationRequired("Archive ownership initialization did not complete")
        volumes = {mount["Destination"]: mount["Name"] for mount in observed["Mounts"]}
        if set(volumes) != {"/workspace", "/forge-git"}:
            raise SandboxReconciliationRequired("Archive transfer mount roots differ from the owned policy")
        if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in volumes.values()):
            raise SandboxReconciliationRequired("Archive transfer volumes are not daemon-owned anonymous volumes")
        return volumes

    def _retain_archive(self, name: str, data: bytes) -> dict[str, Any]:
        root = _directory_fd(self.store.path.parent)
        folder = ".forge-sandbox-results"
        filename = name + ".tar"
        directory = None
        try:
            with suppress(FileExistsError):
                os.mkdir(folder, 0o700, dir_fd=root)
            directory = _directory_fd(folder, parent=root)
            if os.fstat(directory).st_uid != os.getuid() or stat.S_IMODE(os.fstat(directory).st_mode) != 0o700:
                raise ValueError("Retained runner archive directory must be private and owner-controlled")
            try:
                descriptor = os.open(
                    filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
                )
            except FileExistsError:
                previous, _ = _regular_data(directory, filename, len(data), private=True)
                if previous != data:
                    raise SandboxReconciliationRequired(
                        "Retained runner archive differs from the exited command"
                    ) from None
            else:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.fsync(directory)
            return {
                "archive_path": str(self.store.path.parent / folder / filename),
                "archive_sha256": hashlib.sha256(data).hexdigest(),
                "archive_bytes": len(data),
            }
        finally:
            if directory is not None:
                os.close(directory)
            os.close(root)

    async def _archive_collect(
        self, config: SandboxInput, name: str, workspace: Path, original_digest: str
    ) -> tuple[dict[str, Any], BaseException | None]:
        evidence: dict[str, Any] = {"workspace_import": "requires_reconciliation", "archive_container_retained": True}
        try:
            code, data = await self.backends.select(config).run_bytes(
                config,
                ["cp", name + ":/workspace/.", "-"],
                self.store.path.parent,
                30,
                config.workspace_max_bytes + 1_048_576,
            )
            if code:
                raise SandboxReconciliationRequired("Completed runner workspace archive could not be recovered")
            if contains_known_credential(data.decode("utf-8", errors="ignore"), self.credential_registry()):
                evidence["archive_container_retained"] = False
                raise ValueError("Workspace archive cannot contain configured credentials")
            evidence.update(self._retain_archive(name, data))
            evidence["archive_container_retained"] = False
            await self.store.assert_ownership()
            evidence.update(
                collect_workspace(
                    data, workspace, config.workspace_max_bytes, original_digest, registry=self.credential_registry()
                )
            )
            evidence["workspace_import"] = "complete"
            return evidence, None
        except BaseException as failure:
            evidence["import_failure"] = self._safe_result(ProcessResult([], 0, str(failure))).output
            if isinstance(failure, SandboxPublicationReconciliationRequired):
                evidence["workspace_backup_path"] = str(failure.backup_path)
            return evidence, failure

    def _create_argv(
        self,
        config: SandboxInput,
        workspace: Path,
        cwd: Path,
        argv: list[str],
        name: str,
        labels: dict[str, str],
        *,
        git_root: Path | None,
        volumes: dict[str, str] | None = None,
    ) -> list[str]:
        if any("," in str(path) for path in (workspace, cwd)):
            raise ValueError("Docker workspace paths cannot contain mount delimiters")
        if os.getuid() == 0:
            raise ValueError("Docker runner requires a non-root service account")
        relative = cwd.relative_to(workspace).as_posix()
        command = [
            "create",
            "--pull=never",
            "--name",
            name,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--cpus",
            str(config.cpus),
            "--memory",
            f"{config.memory_mb}m",
            "--memory-swap",
            f"{config.memory_mb}m",
            "--pids-limit",
            str(config.pids_limit),
            "--network",
            "none" if config.network == "none" else "bridge",
            "--init",
            "--log-driver=local",
            "--log-opt=max-size=" + ("10m" if config.browser is not None else "2m"),
            "--log-opt=max-file=2",
            "--ipc=private",
            "--shm-size=128m",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=134217728,mode=1777",
            "--env",
            "HOME=/tmp/forge-home",
            "--env",
            "XDG_CACHE_HOME=/tmp/forge-cache",
            "--mount",
            f"type=volume,src={volumes['/workspace']},dst=/workspace,volume-nocopy"
            if volumes
            else f"type=bind,src={workspace},dst=/workspace",
            "--workdir",
            "/workspace" + ("/" + relative if relative != "." else ""),
        ]
        if git_root is not None:
            metadata = git_root / ".git"
            if metadata.is_symlink() or not metadata.is_dir() or "," in str(metadata):
                raise ValueError("Verification requires a real Git metadata directory")
            command += [
                "--mount",
                f"type=volume,src={volumes['/forge-git']},dst=/workspace/.git,readonly,volume-nocopy"
                if volumes
                else f"type=bind,src={metadata},dst=/workspace/.git,readonly",
            ]
        elif (workspace / ".git").exists():
            # Hide repository metadata from implementation commands while mounting source only.
            command += ["--tmpfs", "/workspace/.git:ro,nosuid,nodev,size=1024,mode=000"]
        for key, value in labels.items():
            command += ["--label", f"{key}={value}"]
        return [*command, "--entrypoint", argv[0], str(config.image), *argv[1:]]

    async def _inspect(self, config: SandboxInput, name: str, labels: dict[str, str]) -> dict[str, Any] | None:
        result = await self._docker(config, ["container", "inspect", name])
        if result.exit_code:
            if "no such container" in result.output.lower() or "no such object" in result.output.lower():
                return None
            raise SandboxReconciliationRequired("Docker inspection failed; absence was not established")
        data: dict[str, Any] = json.loads(result.output)[0]
        actual = data.get("Config", {}).get("Labels", {})
        if any(actual.get(key) != value for key, value in labels.items()):
            raise SandboxReconciliationRequired("Container ownership labels do not match the durable invocation")
        return data

    async def _cleanup(
        self,
        config: SandboxInput,
        run_id: str,
        task_id: str,
        execution_id: str,
        payload: dict[str, Any],
        labels: dict[str, str],
    ) -> None:
        intent = await self.journal.sandbox_effect_intent(
            run_id, task_id, execution_id, effect="cleanup", payload=payload
        )
        if intent["disposition"] == "completed":
            return
        names = [payload["container_name"]]
        if config.workspace_transport == "archive":
            names.append(payload["container_name"] + "-transfer")
        for name in names:
            observed = await self._inspect(config, name, labels)
            if observed is not None:
                result = await self._docker(config, ["container", "rm", "--force", "--volumes", name])
                if result.exit_code:
                    raise SandboxReconciliationRequired("Owned container cleanup could not be confirmed")
        if intent["disposition"] == "needs_reconciliation":
            await self.journal.sandbox_effect_reconcile(
                run_id,
                task_id,
                execution_id,
                effect="cleanup",
                evidence={"container_absent": True},
                outcome="completed",
            )
        else:
            await self.journal.sandbox_effect_receipt(
                run_id, task_id, execution_id, effect="cleanup", evidence={"container_absent": True}
            )

    async def _cleanup_host(self, run_id: str, task_id: str, execution_id: str, payload: dict[str, Any]) -> None:
        intent = await self.journal.sandbox_effect_intent(
            run_id, task_id, execution_id, effect="cleanup", payload=payload
        )
        if intent["disposition"] == "completed":
            return
        if intent["disposition"] == "needs_reconciliation":
            await self.journal.sandbox_effect_reconcile(
                run_id,
                task_id,
                execution_id,
                effect="cleanup",
                evidence={"mode": "host", "container": False},
                outcome="completed",
            )
        else:
            await self.journal.sandbox_effect_receipt(
                run_id, task_id, execution_id, effect="cleanup", evidence={"mode": "host", "container": False}
            )

    async def execute(
        self,
        run_id: str,
        workspace_root: Path,
        cwd: Path,
        argv: list[str],
        timeout: int,
        task_id: str,
        *,
        ordinal: int | None = None,
        git_root: Path | None = None,
        _browser: bool = False,
        _browser_input: bytes | None = None,
    ) -> ProcessResult:
        if task_id not in {"implement", "verify"}:
            raise ValueError("Runner commands must belong to an implementation or verification stage")
        if not argv or len(argv) > 64 or type(timeout) is not int or timeout < 1:
            raise ValueError("Runner command arguments or timeout are invalid")
        bounded_argv(argv)
        if contains_known_credential(argv, self.credential_registry()):
            raise ValueError("Runner arguments cannot contain configured credentials")
        if _browser_input is not None:
            if not _browser or len(_browser_input) > 2_100_000:
                raise ValueError("Browser input requires the isolated profile and at most 2.1 MB")
            if contains_known_credential(_browser_input.decode("utf-8"), self.credential_registry()):
                raise ValueError("Browser input cannot contain configured credentials")
        if workspace_root.is_symlink() or cwd.is_symlink():
            raise ValueError("Runner directories cannot be symlinks")
        workspace, directory = workspace_root.resolve(strict=True), cwd.resolve(strict=True)
        if not directory.is_relative_to(workspace) or not workspace.is_dir() or not directory.is_dir():
            raise ValueError("Runner working directory must be confined to the component workspace")
        async with self._run_locks[run_id], self._capacity:
            await self.store.assert_ownership()
            run = await self.store.get_run(run_id)
            snapshot = run.get("sandbox_snapshot") or capture_sandbox(None)
            if snapshot.get("mode") == "pool":
                if self.pool_worker is None:
                    raise RuntimeError("Factory pool worker is not configured; execution cannot fall back")
                if _browser or _browser_input is not None:
                    raise ValueError("Pool browser execution requires a separately qualified browser profile")
                stage = next(item for item in (await self.journal.plan(run_id))["tasks"] if item["id"] == task_id)
                if ordinal is None:
                    slot = (run_id, task_id, stage["attempt"])
                    self._ordinals[slot] += 1
                    ordinal = self._ordinals[slot]
                return await self.pool_worker.execute(
                    run_id, workspace, directory, argv, timeout, task_id, ordinal=ordinal
                )
            config = validate_sandbox_snapshot(snapshot)
            backend = self.backends.select(config)
            if _browser:
                if config.mode != "docker" or config.browser is None:
                    raise ValueError("Browser execution requires an explicit pinned browser image")
                config = config.model_copy(update={"image": config.browser.image, "network": "none"})
            output_limit = 8_300_000 if _browser else config.max_output_bytes
            planned = next(item for item in (await self.journal.plan(run_id))["tasks"] if item["id"] == task_id)
            attempt = planned["attempt"]
            slot = (run_id, task_id, attempt)
            if ordinal is None:
                self._ordinals[slot] += 1
                ordinal = self._ordinals[slot]
            if type(ordinal) is not int or not 1 <= ordinal <= config.command_limit:
                raise ValueError("Frozen command budget is exhausted")
            execution_id = uuid.uuid5(uuid.NAMESPACE_URL, f"forge:sandbox:{run_id}:{task_id}:{attempt}:{ordinal}").hex
            records = await self.journal.sandbox_executions(run_id)
            existing = next((record for record in records if record["execution_id"] == execution_id), None)
            if existing is None and len(records) >= config.command_limit:
                raise ValueError("Frozen total command budget is exhausted; resume and handoff do not reset it")
            if _browser and existing is None:
                assert config.browser is not None
                browser_calls = sum(bool(row["request"]["request"].get("browser", False)) for row in records)
                if browser_calls >= config.browser.max_calls:
                    raise ValueError("Frozen browser call budget is exhausted; resume and handoff do not reset it")
            labels = {
                "org.firefly.forge.owner": self.owner,
                "org.firefly.forge.execution": execution_id,
                "org.firefly.forge.run": run_id,
                "org.firefly.forge.snapshot": snapshot["digest"],
            }
            request: dict[str, Any] = {
                "browser": _browser,
                "output_limit": output_limit,
                "input_sha256": hashlib.sha256(_browser_input).hexdigest() if _browser_input is not None else None,
                "argv": argv,
                "cwd": directory.relative_to(workspace).as_posix(),
                "workspace": str(workspace),
                "configuration": capture_sandbox(config)["configuration"],
                "timeout": min(timeout, config.timeout),
                "git_root": str(git_root) if git_root else None,
            }
            if config.workspace_transport == "archive":
                if not self.transfer_image:
                    raise ValueError("Archive execution requires an administrator-configured pinned transfer image")
                request["transfer_image"] = self.transfer_image
                request["workspace_digest"] = (
                    existing["request"]["request"]["workspace_digest"]
                    if existing
                    else pack_workspace(workspace, config.workspace_max_bytes, registry=self.credential_registry())[1]
                )
            payload = {
                "parent_attempt": attempt,
                "ordinal": ordinal,
                "container_name": "forge-sandbox-" + execution_id,
                "snapshot_digest": snapshot["digest"],
                "request_digest": digest(request),
                "request": request,
            }
            if existing and SandboxInput.model_validate(existing["request"]["request"]["configuration"]) == config:
                request["configuration"] = existing["request"]["request"]["configuration"]
                payload["request_digest"] = digest(request)
            if existing is not None:
                if existing["request"] != payload:
                    raise SandboxReconciliationRequired("Durable runner invocation differs from the requested command")
                finished = existing["effects"].get("start", {})
                if finished.get("status") == "done":
                    evidence = finished["evidence"]
                    if evidence.get("workspace_import") == "requires_reconciliation":
                        raise SandboxReconciliationRequired(
                            "Retained runner result requires source publication reconciliation"
                        )
                    if "exit_code" not in evidence:
                        raise SandboxReconciliationRequired(
                            "Interrupted runner command requires explicit stage recovery"
                        )
                    if config.mode == "docker":
                        await self._cleanup(config, run_id, task_id, execution_id, payload, labels)
                    else:
                        await self._cleanup_host(run_id, task_id, execution_id, payload)
                    return ProcessResult(argv, evidence["exit_code"], evidence["output"])
                if any(
                    effect.get("evidence", {}).get("outcome") == "interrupted"
                    for effect in existing["effects"].values()
                ):
                    raise SandboxReconciliationRequired(
                        "Interrupted runner invocation requires explicit stage recovery"
                    )
            created = await self.journal.sandbox_effect_intent(
                run_id, task_id, execution_id, effect="create", payload=payload
            )
            if config.mode == "host":
                if created["disposition"] == "needs_reconciliation":
                    raise SandboxReconciliationRequired("Unknown host command outcome cannot be replayed")
                if created["disposition"] == "execute":
                    await self.journal.sandbox_effect_receipt(
                        run_id, task_id, execution_id, effect="create", evidence={"mode": "host"}
                    )
                started = await self.journal.sandbox_effect_intent(
                    run_id, task_id, execution_id, effect="start", payload=payload
                )
                if started["disposition"] != "execute":
                    raise SandboxReconciliationRequired("Unknown host command outcome cannot be replayed")
                await self.store.assert_ownership()
                try:
                    result = await backend.run(
                        config, argv, directory, min(timeout, config.timeout), config.max_output_bytes
                    )
                    result = self._safe_result(result)
                    await self.journal.sandbox_effect_receipt(
                        run_id,
                        task_id,
                        execution_id,
                        effect="start",
                        evidence={"exit_code": result.exit_code, "output": result.output},
                    )
                    return result
                finally:
                    await asyncio.shield(self._cleanup_host(run_id, task_id, execution_id, payload))
            observed = None
            if created["disposition"] == "needs_reconciliation":
                observed = await self._inspect(config, payload["container_name"], labels)
                if observed is None:
                    raise SandboxReconciliationRequired("Unknown Docker creation outcome cannot be replayed")
                await self.journal.sandbox_effect_reconcile(
                    run_id,
                    task_id,
                    execution_id,
                    effect="create",
                    evidence={"container_id": observed["Id"], "observed_state": observed["State"]["Status"]},
                    outcome="completed",
                )
            elif created["disposition"] == "execute":
                await self.store.assert_ownership()
                try:
                    volumes = (
                        await self._archive_prepare(config, workspace, git_root, payload["container_name"], labels)
                        if config.workspace_transport == "archive"
                        else None
                    )
                    create_argv = self._create_argv(
                        config,
                        workspace,
                        directory,
                        argv,
                        payload["container_name"],
                        labels,
                        git_root=git_root,
                        volumes=volumes,
                    )
                    if _browser_input is not None:
                        create_argv.insert(1, "--interactive")
                    result = await self._docker(config, create_argv)
                    if result.exit_code:
                        raise RuntimeError("Selected Docker runner could not create the configured container")
                    await self.journal.sandbox_effect_receipt(
                        run_id,
                        task_id,
                        execution_id,
                        effect="create",
                        evidence={
                            "container_id": result.output.strip(),
                            **(
                                {
                                    "transfer_container_name": payload["container_name"] + "-transfer",
                                    "transfer_image": self.transfer_image,
                                    "transfer_initialization_exit_code": 0,
                                    "workspace_digest": request["workspace_digest"],
                                }
                                if volumes
                                else {}
                            ),
                        },
                    )
                except BaseException:
                    await asyncio.shield(self._cleanup(config, run_id, task_id, execution_id, payload, labels))
                    raise
            started = await self.journal.sandbox_effect_intent(
                run_id, task_id, execution_id, effect="start", payload=payload
            )
            retain_container = False
            try:
                if started["disposition"] == "needs_reconciliation":
                    observed = observed or await self._inspect(config, payload["container_name"], labels)
                    if observed is None or observed["State"]["Status"] != "exited":
                        await self.journal.sandbox_effect_reconcile(
                            run_id,
                            task_id,
                            execution_id,
                            effect="start",
                            evidence={"observed_state": observed["State"]["Status"] if observed else "absent"},
                            outcome="interrupted",
                        )
                        raise SandboxReconciliationRequired(
                            "Unknown running Docker command was interrupted; explicit stage recovery is required"
                        )
                    logs = await self._docker(config, ["logs", payload["container_name"]], limit=output_limit)
                    result = self._safe_result(
                        ProcessResult(argv, observed["State"]["ExitCode"], logs.output), browser=_browser
                    )
                    archive_evidence: dict[str, Any] = {}
                    import_failure = None
                    if config.workspace_transport == "archive":
                        archive_evidence, import_failure = await self._archive_collect(
                            config, payload["container_name"], workspace, request["workspace_digest"]
                        )
                        retain_container = archive_evidence.get("archive_container_retained", False)
                    await self.journal.sandbox_effect_reconcile(
                        run_id,
                        task_id,
                        execution_id,
                        effect="start",
                        evidence={
                            "exit_code": result.exit_code,
                            "output": result.output,
                            "observed_state": "exited",
                            **archive_evidence,
                        },
                        outcome="completed",
                    )
                    if import_failure is not None:
                        raise import_failure
                elif started["disposition"] == "execute":
                    await self.store.assert_ownership()
                    start_argv = ["start", "--attach", payload["container_name"]]
                    if _browser_input is not None:
                        start_argv.insert(1, "--interactive")
                        result = await self._docker(
                            config,
                            start_argv,
                            min(timeout, config.timeout),
                            output_limit,
                            input_data=_browser_input,
                        )
                    else:
                        result = await self._docker(config, start_argv, min(timeout, config.timeout), output_limit)
                    state = await self._inspect(config, payload["container_name"], labels)
                    if state is None or state["State"]["Status"] != "exited":
                        raise SandboxReconciliationRequired("Docker command completion could not be confirmed")
                    result = ProcessResult(argv, state["State"]["ExitCode"], result.output)
                    result = self._safe_result(result, browser=_browser)
                    archive_evidence = {}
                    import_failure = None
                    if config.workspace_transport == "archive":
                        archive_evidence, import_failure = await self._archive_collect(
                            config, payload["container_name"], workspace, request["workspace_digest"]
                        )
                        retain_container = archive_evidence.get("archive_container_retained", False)
                    await self.journal.sandbox_effect_receipt(
                        run_id,
                        task_id,
                        execution_id,
                        effect="start",
                        evidence={"exit_code": result.exit_code, "output": result.output, **archive_evidence},
                    )
                    if import_failure is not None:
                        raise import_failure
                else:
                    evidence = started["execution"]["effects"]["start"]["evidence"]
                    result = ProcessResult(argv, evidence["exit_code"], evidence["output"])
            finally:
                if not retain_container:
                    await asyncio.shield(self._cleanup(config, run_id, task_id, execution_id, payload, labels))
            return ProcessResult(argv, result.exit_code, result.output)

    async def recover(self) -> dict[str, int]:
        """Inspect and clean persistent owned executions after the engine acquires its lease."""
        await self.store.assert_ownership()
        async with self.store._unit():
            rows = [self.journal._sandbox_data(row) for row in await self.journal.sandboxes.unfinished()]
        counts = {"inspected": 0, "removed": 0, "interrupted": 0, "recovered_receipts": 0}
        for row in rows:
            await self.store.assert_ownership()
            counts["inspected"] += 1
            run_id, task_id, execution_id = row["run_id"], row["task_id"], row["execution_id"]
            payload = row["request"]
            if payload["request"].get("protocol") == "forge-project-invocation-v1":
                if self.pool_worker is None:
                    raise RuntimeError("Retained pool execution requires its configured Factory worker for recovery")
                recovered = await self.pool_worker.recover(row)
                for key, value in recovered.items():
                    counts[key] += value
                continue
            config = SandboxInput.model_validate(payload["request"]["configuration"])
            labels = {
                "org.firefly.forge.owner": self.owner,
                "org.firefly.forge.execution": execution_id,
                "org.firefly.forge.run": run_id,
                "org.firefly.forge.snapshot": row["snapshot_digest"],
            }
            observed = await self._inspect(config, row["container_name"], labels) if config.mode == "docker" else None
            created = row["effects"].get("create", {})
            if created.get("status") == "intent":
                await self.journal.sandbox_effect_reconcile(
                    run_id,
                    task_id,
                    execution_id,
                    effect="create",
                    evidence={"observed_state": observed["State"]["Status"] if observed else "absent"},
                    outcome="completed" if observed else "interrupted",
                )
            started = row["effects"].get("start", {})
            if started.get("evidence", {}).get("workspace_import") == "requires_reconciliation":
                counts["interrupted"] += 1
                continue
            retain_container = False
            if started.get("status") == "intent":
                if observed and observed["State"]["Status"] == "exited":
                    output_limit = row["request"]["request"].get("output_limit", config.max_output_bytes)
                    logs = await self._docker(config, ["logs", row["container_name"]], limit=output_limit)
                    if logs.exit_code:
                        raise SandboxReconciliationRequired("Exited owned container logs could not be recovered")
                    private_rejected = bool(payload["request"].get("browser")) and contains_known_credential(
                        logs.output, self.credential_registry()
                    )
                    archive_evidence: dict[str, Any] = {}
                    import_failure = None
                    if config.workspace_transport == "archive" and not private_rejected:
                        archive_evidence, import_failure = await self._archive_collect(
                            config,
                            row["container_name"],
                            Path(payload["request"]["workspace"]),
                            payload["request"]["workspace_digest"],
                        )
                        retain_container = archive_evidence.get("archive_container_retained", False)
                    await self.journal.sandbox_effect_reconcile(
                        run_id,
                        task_id,
                        execution_id,
                        effect="start",
                        evidence={"private_evidence_rejected": True}
                        if private_rejected
                        else {
                            "exit_code": observed["State"]["ExitCode"],
                            "output": self._safe_result(ProcessResult([], 0, logs.output)).output,
                            "observed_state": "exited",
                            **archive_evidence,
                        },
                        outcome="interrupted" if private_rejected or import_failure else "completed",
                    )
                    counts["interrupted" if private_rejected or import_failure else "recovered_receipts"] += 1
                else:
                    await self.journal.sandbox_effect_reconcile(
                        run_id,
                        task_id,
                        execution_id,
                        effect="start",
                        evidence={"observed_state": observed["State"]["Status"] if observed else "absent"},
                        outcome="interrupted",
                    )
                    counts["interrupted"] += 1
            if config.mode == "docker":
                if not retain_container:
                    await self._cleanup(config, run_id, task_id, execution_id, payload, labels)
                    counts["removed"] += 1
            else:
                await self._cleanup_host(run_id, task_id, execution_id, payload)
        return counts
