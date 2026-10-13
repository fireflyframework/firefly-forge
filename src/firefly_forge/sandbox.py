# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded command execution with persistent ownership and Docker effects."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import signal
import tarfile
import tempfile
import uuid
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any

from pyfly.container import service

from firefly_forge.configuration import redact_secrets
from firefly_forge.journal import Journal, digest
from firefly_forge.process import ProcessResult
from firefly_forge.sandbox_models import (
    SandboxInput,
    bounded_argv,
    capture_sandbox,
    pinned_image,
    validate_sandbox_snapshot,
)
from firefly_forge.secrets import contains_known_credential, redact_environment, runtime_environment
from firefly_forge.store import Store


class SandboxReconciliationRequired(RuntimeError):
    pass


def _private_path(parts: tuple[str, ...]) -> bool:
    return any(
        part in {".git", ".ssh", ".aws", ".azure", ".docker"} or part == ".env" or part.startswith(".env.")
        for part in parts
    )


def pack_workspace(
    workspace: Path, limit: int, *, metadata: bool = False, registry: dict[str, str] | None = None
) -> tuple[bytes, str]:
    output = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as stream:
        root = tarfile.TarInfo(".")
        root.type, root.mode, root.uid, root.gid = tarfile.DIRTYPE, 0o755, os.getuid(), os.getgid()
        stream.addfile(root)
        for path in sorted(workspace.rglob("*")):
            relative = path.relative_to(workspace)
            if not metadata and _private_path(relative.parts):
                continue
            if contains_known_credential(relative.as_posix(), registry):
                raise ValueError("Workspace archive cannot contain configured credentials")
            stat = path.lstat()
            if path.is_symlink() or not (path.is_file() or path.is_dir()) or (path.is_file() and stat.st_nlink != 1):
                raise ValueError("Archive workspace permits only regular files and directories without links")
            item = tarfile.TarInfo(relative.as_posix())
            item.uid, item.gid = os.getuid(), os.getgid()
            item.mode = 0o755 if path.is_dir() or stat.st_mode & 0o111 else 0o644
            if path.is_dir():
                item.type = tarfile.DIRTYPE
                stream.addfile(item)
            else:
                total += stat.st_size
                if total > limit:
                    raise ValueError("Workspace exceeds the archive byte limit")
                with path.open("rb") as source:
                    data = source.read(limit - total + stat.st_size + 1)
                if len(data) != stat.st_size:
                    raise ValueError("Workspace changed while creating the archive")
                if contains_known_credential(data.decode("utf-8", errors="ignore"), registry):
                    raise ValueError("Workspace archive cannot contain configured credentials")
                item.size = len(data)
                stream.addfile(item, io.BytesIO(data))
            if output.tell() > limit + 1_048_576:
                raise ValueError("Workspace exceeds the archive byte limit")
    data = output.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def unpack_workspace(data: bytes, destination: Path, limit: int, *, registry: dict[str, str] | None = None) -> None:
    if destination.is_symlink():
        raise ValueError("Workspace archive destination contains a link")
    destination = destination.resolve()
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
        for path, item in validated:
            target = destination.joinpath(*path.parts)
            if any(parent.is_symlink() for parent in [target, *target.parents]):
                raise ValueError("Workspace archive destination contains a link")
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = stream.extractfile(item)
                assert source is not None
                target.write_bytes(source.read())
                target.chmod(0o755 if item.mode & 0o111 else 0o644)


def collect_workspace(
    data: bytes, workspace: Path, limit: int, original_digest: str, *, registry: dict[str, str] | None = None
) -> None:
    with tempfile.TemporaryDirectory(prefix="forge-archive-") as temporary:
        staged = Path(temporary)
        unpack_workspace(data, staged, limit, registry=registry)
        current_digest = pack_workspace(workspace, limit, registry=registry)[1]
        if current_digest not in {original_digest, pack_workspace(staged, limit, registry=registry)[1]}:
            raise ValueError("Host workspace changed during archive execution; results require reconciliation")
        for path in sorted(workspace.rglob("*"), reverse=True):
            relative = path.relative_to(workspace)
            if _private_path(relative.parts) or (staged / relative).exists():
                continue
            if path.is_file():
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        shutil.copytree(staged, workspace, dirs_exist_ok=True)


async def _bounded_process(
    argv: list[str], cwd: Path, timeout: int, max_output_bytes: int, *, input_data: bytes | None = None
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        start_new_session=True,
        env=runtime_environment(),
    )
    output = bytearray()

    async def consume() -> None:
        if input_data is not None:
            assert process.stdin is not None
            process.stdin.write(input_data)
            await process.stdin.drain()
            process.stdin.close()
        assert process.stdout is not None
        while data := await process.stdout.read(65536):
            if len(output) + len(data) > max_output_bytes:
                raise RuntimeError("Runner output exceeded the frozen output limit")
            output.extend(data)
        await process.wait()

    try:
        await asyncio.wait_for(consume(), timeout)
    except BaseException:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 2)
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    return process.returncode or 0, bytes(output)


async def bounded_process(
    argv: list[str], cwd: Path, timeout: int, max_output_bytes: int, *, input_data: bytes | None = None
) -> ProcessResult:
    code, output = await _bounded_process(argv, cwd, timeout, max_output_bytes, input_data=input_data)
    return ProcessResult(argv, code, output.decode("utf-8", errors="replace"))


@service
class SandboxExecutor:
    def __init__(self, store: Store, journal: Journal) -> None:
        self.store, self.journal = store, journal
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
        assert config.context is not None
        return await bounded_process(
            ["docker", "--context", config.context, *argv],
            self.store.path.parent,
            timeout,
            limit,
            input_data=input_data,
        )

    async def probe(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        config = validate_sandbox_snapshot(snapshot)
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

    async def _archive_collect(self, config: SandboxInput, name: str, workspace: Path, original_digest: str) -> None:
        code, data = await _bounded_process(
            ["docker", "--context", str(config.context), "cp", name + ":/workspace/.", "-"],
            self.store.path.parent,
            30,
            config.workspace_max_bytes + 1_048_576,
        )
        if code:
            raise SandboxReconciliationRequired("Completed runner workspace archive could not be recovered")
        collect_workspace(
            data, workspace, config.workspace_max_bytes, original_digest, registry=self.credential_registry()
        )

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
            config = validate_sandbox_snapshot(snapshot)
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
                "configuration": config.model_dump(mode="json"),
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
                    result = await bounded_process(
                        argv, directory, min(timeout, config.timeout), config.max_output_bytes
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
                    if config.workspace_transport == "archive":
                        await self._archive_collect(
                            config, payload["container_name"], workspace, request["workspace_digest"]
                        )
                    await self.journal.sandbox_effect_reconcile(
                        run_id,
                        task_id,
                        execution_id,
                        effect="start",
                        evidence={"exit_code": result.exit_code, "output": result.output, "observed_state": "exited"},
                        outcome="completed",
                    )
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
                    if config.workspace_transport == "archive":
                        await self._archive_collect(
                            config, payload["container_name"], workspace, request["workspace_digest"]
                        )
                    await self.journal.sandbox_effect_receipt(
                        run_id,
                        task_id,
                        execution_id,
                        effect="start",
                        evidence={"exit_code": result.exit_code, "output": result.output},
                    )
                else:
                    evidence = started["execution"]["effects"]["start"]["evidence"]
                    result = ProcessResult(argv, evidence["exit_code"], evidence["output"])
            finally:
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
            if started.get("status") == "intent":
                if observed and observed["State"]["Status"] == "exited":
                    output_limit = row["request"]["request"].get("output_limit", config.max_output_bytes)
                    logs = await self._docker(config, ["logs", row["container_name"]], limit=output_limit)
                    if logs.exit_code:
                        raise SandboxReconciliationRequired("Exited owned container logs could not be recovered")
                    private_rejected = bool(payload["request"].get("browser")) and contains_known_credential(
                        logs.output, self.credential_registry()
                    )
                    if config.workspace_transport == "archive" and not private_rejected:
                        await self._archive_collect(
                            config,
                            row["container_name"],
                            Path(payload["request"]["workspace"]),
                            payload["request"]["workspace_digest"],
                        )
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
                        },
                        outcome="interrupted" if private_rejected else "completed",
                    )
                    counts["interrupted" if private_rejected else "recovered_receipts"] += 1
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
                await self._cleanup(config, run_id, task_id, execution_id, payload, labels)
                counts["removed"] += 1
            else:
                await self._cleanup_host(run_id, task_id, execution_id, payload)
        return counts
