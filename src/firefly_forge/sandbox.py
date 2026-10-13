# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded command execution with persistent ownership and Docker effects."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import uuid
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from pyfly.container import service

from firefly_forge.configuration import redact_secrets
from firefly_forge.journal import Journal, digest
from firefly_forge.process import ProcessResult
from firefly_forge.sandbox_models import SandboxInput, bounded_argv, capture_sandbox, validate_sandbox_snapshot
from firefly_forge.secrets import contains_known_credential, redact_environment, runtime_environment
from firefly_forge.store import Store


class SandboxReconciliationRequired(RuntimeError):
    pass


async def bounded_process(
    argv: list[str], cwd: Path, timeout: int, max_output_bytes: int, *, input_data: bytes | None = None
) -> ProcessResult:
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
    return ProcessResult(argv, process.returncode or 0, output.decode("utf-8", errors="replace"))


@service
class SandboxExecutor:
    def __init__(self, store: Store, journal: Journal) -> None:
        self.store, self.journal = store, journal
        self.owner = hashlib.sha256((str(store.path.parent.resolve()) + store.schema).encode()).hexdigest()[:24]
        self.credential_registry: Callable[[], dict[str, str]] = dict
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
            f"type=bind,src={workspace},dst=/workspace",
            "--workdir",
            "/workspace" + ("/" + relative if relative != "." else ""),
        ]
        if git_root is not None:
            metadata = git_root / ".git"
            if metadata.is_symlink() or not metadata.is_dir() or "," in str(metadata):
                raise ValueError("Verification requires a real Git metadata directory")
            command += ["--mount", f"type=bind,src={metadata},dst=/workspace/.git,readonly"]
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
        observed = await self._inspect(config, payload["container_name"], labels)
        if observed is not None:
            result = await self._docker(config, ["container", "rm", "--force", payload["container_name"]])
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
            request = {
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
            payload = {
                "parent_attempt": attempt,
                "ordinal": ordinal,
                "container_name": "forge-sandbox-" + execution_id,
                "snapshot_digest": snapshot["digest"],
                "request_digest": digest(request),
                "request": request,
            }
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
                create_argv = self._create_argv(
                    config, workspace, directory, argv, payload["container_name"], labels, git_root=git_root
                )
                if _browser_input is not None:
                    create_argv.insert(1, "--interactive")
                result = await self._docker(
                    config,
                    create_argv,
                )
                if result.exit_code:
                    raise RuntimeError("Selected Docker runner could not create the configured container")
                await self.journal.sandbox_effect_receipt(
                    run_id, task_id, execution_id, effect="create", evidence={"container_id": result.output.strip()}
                )
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
