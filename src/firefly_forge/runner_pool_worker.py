# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Factory-authorized single submission with durable WAL and atomic workspace publication."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from pyfly.container import service

from firefly_forge.journal import Journal
from firefly_forge.process import ProcessResult
from firefly_forge.project_models import component_path
from firefly_forge.runner_controller_client import RunnerControllerClient
from firefly_forge.runner_factory_authority import RunnerFactoryAuthority
from firefly_forge.runner_factory_models import UNCAPPED_POOL_REASON, uncapped_pool_reason, validate_factory_pool
from firefly_forge.runner_git_snapshot import RunnerGitSnapshot
from firefly_forge.runner_pool_artifacts import ArtifactKind, RunnerPoolArtifacts, validate_artifact_receipt
from firefly_forge.runner_pool_bundle import frame_project_archives
from firefly_forge.runner_pool_models import RunnerProjectInvocation, pool_digest
from firefly_forge.runner_run_liveness import (
    RUN_LIVENESS_PROTOCOL,
    RunLivenessBindingV1,
    run_cut_digest,
    run_digest,
    validate_run_ack,
)
from firefly_forge.sandbox import collect_workspace, pack_workspace
from firefly_forge.sandbox_models import bounded_argv
from firefly_forge.secrets import contains_known_credential
from firefly_forge.store import Store


@service
class RunnerPoolWorker:
    def __init__(
        self,
        store: Store,
        journal: Journal,
        authority: RunnerFactoryAuthority,
        client: RunnerControllerClient,
        git_snapshot: RunnerGitSnapshot,
        workspaces: Path,
        directory: Path,
    ) -> None:
        self.store, self.journal, self.authority, self.client = store, journal, authority, client
        self.git_snapshot, self.workspaces = git_snapshot, workspaces.resolve(strict=True)
        if workspaces.is_symlink() or not workspaces.is_absolute():
            raise ValueError("Factory workspace authority requires an explicit real root")
        self.artifacts = RunnerPoolArtifacts(directory)
        self.artifacts.credential_registry = lambda: {
            **store.credential_registry(),
            "FORGE_RUNNER_CONTROLLER_TOKEN": client.transport.token.get_secret_value(),
        }
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.waiting: dict[str, dict[str, Any]] = {}
        self.run_clock: Callable[[], float] = time.monotonic

    async def probe(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        policy = validate_factory_pool(snapshot)
        if reason := uncapped_pool_reason(policy):
            return {"available": False, "mode": "pool", "status": "not_implemented", "reason": reason}
        policy.require_transport(self.client.transport)
        profile = await self.client.profile()
        if policy.profile.run_liveness is not None and RUN_LIVENESS_PROTOCOL not in profile.get(
            "run_liveness_protocols", []
        ):
            raise RuntimeError("Frozen run ownership protocol is unsupported by this controller")
        if (
            profile.get("profile") != policy.profile.model_dump(mode="json")
            or profile.get("revision") != policy.profile.revision
            or profile.get("status") != "ready"
            or "forge-project-bundle-v1" not in profile.get("workspace_protocols", [])
        ):
            raise RuntimeError("Frozen project controller profile is inactive or unsupported")
        return {"available": True, "mode": "pool", "reason": "Approved command controller verified"}

    def _workspace(self, run: dict[str, Any], workspace: Path, cwd: Path) -> str:
        root = self.workspaces / run["id"]
        relative = component_path((run.get("component_snapshot") or {}).get("path", "."))
        expected = root / relative
        if (
            root.is_symlink()
            or workspace.is_symlink()
            or cwd.is_symlink()
            or not workspace.is_absolute()
            or workspace.resolve(strict=True) != expected.resolve(strict=True)
            or not expected.resolve(strict=True).is_relative_to(root.resolve(strict=True))
            or not root.resolve(strict=True).is_relative_to(self.workspaces)
            or not cwd.resolve(strict=True).is_relative_to(expected.resolve(strict=True))
        ):
            raise ValueError("Command source differs from the retained run/component workspace")
        return cwd.resolve(strict=True).relative_to(expected.resolve(strict=True)).as_posix()

    async def _current(self, run_id: str, task_id: str, original: dict[str, Any]) -> dict[str, Any]:
        current = await self.authority.authorize(run_id, task_id)
        if (
            current["attempt"] != original["attempt"]
            or current["fence"] != original["fence"]
            or current["run"].get("sandbox_snapshot") != original["run"].get("sandbox_snapshot")
            or current["run"].get("component_snapshot") != original["run"].get("component_snapshot")
            or any(
                current["run"].get(key) != original["run"].get(key)
                for key in ("repository_id", "project_id", "execution_actor", "agent")
            )
        ):
            raise RuntimeError("Frozen Factory stage or exclusive owner changed; reconciliation required")
        return current

    def _result(self, argv: list[str], exit_code: int, output: str) -> ProcessResult:
        if contains_known_credential(output, self.artifacts.credential_registry()):
            raise RuntimeError("Retained command output contains configured credentials; reconciliation required")
        return ProcessResult(argv, exit_code, output)

    async def continuation_scope(self, request: dict[str, Any]) -> tuple[RunnerProjectInvocation, dict[str, Any]]:
        active = self.waiting.get(request["execution_id"])
        if active is None:
            raise ValueError("Original Factory command is no longer actively waiting")
        scope, invocation = active["scope"], active["invocation"]
        if (
            request["run_id"] != invocation.run_id.hex
            or request["task_id"] != active["task_id"]
            or request["attempt"] != scope["attempt"]
            or request["fence"] != scope["fence"].model_dump(mode="json")
            or request["invocation_digest"] != pool_digest(invocation.model_dump(mode="json"))
            or request["wal_digest"] != pool_digest(active["payload"])
        ):
            raise ValueError("Continuation differs from the original waiting command")
        await self._current(request["run_id"], request["task_id"], scope)
        policy = validate_factory_pool(scope["run"]["sandbox_snapshot"])
        self._workspace(scope["run"], active["workspace"], active["cwd"])
        source, _ = pack_workspace(
            active["workspace"], policy.workspace_max_bytes, registry=self.artifacts.credential_registry()
        )
        if invocation.bundle.source_archive_digest != hashlib.sha256(source).hexdigest():
            raise ValueError("The original command source changed; reconciliation required")
        self.artifacts.read(
            policy.profile,
            invocation,
            "source",
            {"kind": "source", "sha256": invocation.archive_digest, "bytes": invocation.source_bytes},
        )
        await self.probe(scope["run"]["sandbox_snapshot"])
        await self._current(request["run_id"], request["task_id"], scope)
        return invocation, scope

    async def continue_invocation(
        self, request_id: str, request_digest: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.project_access import ProjectAccess

        invocation, scope = await self.continuation_scope(request)
        await self.store.admit_credentials(invocation.model_dump(mode="json"))
        await ProjectAccess(self.store).authorize_actor(
            request["actor"], scope["run"].get("project_id"), "release" if scope["run"].get("publish") else "execute"
        )
        await self._current(request["run_id"], request["task_id"], scope)
        result = await self.client.continue_invocation(invocation, scope["fence"], request_id, request_digest)
        if (
            result.get("execution_id") != invocation.execution_id
            or result.get("request_id") != request_id
            or result.get("request_digest") != request_digest
            or result.get("disposition") not in {"accepted", "refused"}
        ):
            raise RuntimeError("Controller continuation returned an unbound outcome")
        return result

    async def recover(self, row: dict[str, Any]) -> dict[str, int]:
        """Observe/cancel a retained identity after restart, without submission or source import."""
        run_id, task_id, execution_id = row["run_id"], row["task_id"], row["execution_id"]
        run = await self.store.get_run(run_id)
        policy = validate_factory_pool(run.get("sandbox_snapshot") or {})
        payload = row["request"]
        invocation = RunnerProjectInvocation.model_validate(payload["request"])
        policy.profile.validate_invocation(invocation)
        if (
            invocation.run_id.hex != run_id
            or execution_id != invocation.execution_id
            or payload["snapshot_digest"] != run["sandbox_snapshot"]["digest"]
        ):
            raise ValueError("Recovery request differs from the retained Factory invocation")
        await self.probe(run["sandbox_snapshot"])
        fence = await self.authority.exclusive_fence()
        created = row["effects"].get("create", {})
        if created.get("status") != "done":
            await self.journal.sandbox_effect_reconcile(
                run_id,
                task_id,
                execution_id,
                effect="create",
                evidence={"scope": "factory_request_prepared"},
                outcome="completed",
            )
        start = row["effects"].get("start", {})
        evidence: dict[str, Any] = {"scope": "factory_preparation_only", "workspace_import": "not_attempted"}
        if start:
            observed = await self.client.observe(invocation)
            if observed.get("request") != invocation.model_dump(mode="json"):
                raise RuntimeError("Controller recovery changed the immutable invocation")
            if observed["status"] != "finished":
                observed = await self.client.cancel(invocation, fence)
            if await self.authority.exclusive_fence() != fence:
                raise RuntimeError("Factory recovery owner changed; reconciliation required")
            receipt = observed.get("receipt") or {}
            if observed.get("request") != invocation.model_dump(mode="json") or (
                observed["status"] != "cancelled"
                and (observed["status"] != "finished" or receipt.get("drained") is not True)
            ):
                raise RuntimeError("Controller recovery has no observed drain; reconciliation required")
            evidence.update(
                scope="controller_observed_recovery", controller_status=observed["status"], controller_receipt=receipt
            )
            if start.get("status") != "done":
                await self.journal.sandbox_effect_reconcile(
                    run_id, task_id, execution_id, effect="start", evidence=evidence, outcome="interrupted"
                )
        await self.store.assert_ownership()
        await self.journal.sandbox_effect_intent(run_id, task_id, execution_id, effect="cleanup", payload=payload)
        await self.journal.sandbox_effect_receipt(run_id, task_id, execution_id, effect="cleanup", evidence=evidence)
        return {
            "removed": 1,
            "interrupted": int(bool(start) and start.get("status") != "done"),
            "recovered_receipts": 0,
        }

    async def execute(
        self,
        run_id: str,
        workspace: Path,
        cwd: Path,
        argv: list[str],
        timeout: int | None,
        task_id: str,
        *,
        ordinal: int | None = None,
    ) -> ProcessResult:
        from firefly_forge.sandbox_command_slots import assert_command_boundary

        assert_command_boundary()
        bounded_argv(argv)
        if timeout is not None and (type(timeout) is not int or timeout < 1):
            raise ValueError("Command timeout must be a positive integer")
        if contains_known_credential(argv, self.artifacts.credential_registry()):
            raise ValueError("Pool command cannot contain configured credentials")
        async with self.locks[run_id]:
            scope = await self.authority.authorize(run_id, task_id)
            run, attempt = scope["run"], scope["attempt"]
            snapshot = run.get("sandbox_snapshot")
            policy = validate_factory_pool(snapshot or {})
            from firefly_forge.work_duration_models import WorkDurationPolicyV2, effective_work_timeout

            if policy.work_duration is None:
                if timeout is None:
                    raise ValueError("A legacy pool requires its finite command timeout")
                work_timeout: int | None = min(timeout, policy.timeout)
            else:
                if policy.profile.work_duration is None:
                    raise ValueError("The administrator pool has not admitted the new work-duration policy")
                work_timeout = effective_work_timeout(
                    timeout, policy.work_duration.timeout_seconds, policy.profile.work_duration.timeout_seconds
                )
                if work_timeout is None and policy.profile.run_liveness != RUN_LIVENESS_PROTOCOL:
                    raise ValueError(UNCAPPED_POOL_REASON)
            if ordinal is not None and (type(ordinal) is not int or not 1 <= ordinal <= policy.command_limit):
                raise ValueError("Frozen command budget is exhausted")
            relative = self._workspace(run, workspace, cwd)
            command = {
                "argv": argv,
                "cwd": relative,
                "timeout": work_timeout,
                "workspace": str(workspace),
                "snapshot_digest": snapshot["digest"],
            }

            def retained_command(number: int, identity: str, previous: dict[str, Any] | None) -> dict[str, Any]:
                assert previous is not None
                retained = previous["request"]
                if retained["command"] != command:
                    raise ValueError("Frozen Factory invocation command changed")
                return dict(retained)

            if ordinal is not None:
                replay = await self.journal.sandbox_command_replay(
                    run_id, task_id, attempt, snapshot, retained_command, ordinal=ordinal
                )
                if replay["disposition"] != "absent":
                    previous = replay["execution"]
                    evidence = previous["effects"].get("start", {}).get("evidence", {})
                    if previous["status"] == "removed" and evidence.get("workspace_import") == "complete":
                        await self._current(run_id, task_id, scope)
                        return self._result(argv, evidence["exit_code"], evidence["output"])
                    raise RuntimeError("Existing Factory invocation requires reconciliation; it cannot be resubmitted")
            await self.probe(snapshot)
            await self._current(run_id, task_id, scope)
            source, _ = pack_workspace(
                workspace, policy.workspace_max_bytes, registry=self.artifacts.credential_registry()
            )
            metadata = await self.git_snapshot.build(source, self.artifacts.directory, policy.workspace_max_bytes)
            archive, bundle = frame_project_archives(source, metadata, policy.workspace_max_bytes)

            def prepare(number: int, identity: str, previous: dict[str, Any] | None) -> dict[str, Any]:
                if previous is not None:
                    retained = previous["request"]
                    if retained["command"] != command:
                        raise ValueError("Frozen Factory invocation command changed")
                    return dict(retained)
                invocation = RunnerProjectInvocation(
                    execution_id=identity,
                    profile_id=policy.profile.profile_id,
                    profile_revision=policy.profile.revision,
                    project_id=run.get("project_id"),
                    repository_id=run["repository_id"],
                    run_id=UUID(run_id),
                    image=policy.image,
                    argv=tuple(argv),
                    cwd=relative,
                    source_digest=bundle.source_digest,
                    archive_digest=bundle.archive_digest,
                    source_bytes=len(archive),
                    bundle=bundle,
                    cpus=policy.cpus,
                    memory_mb=policy.memory_mb,
                    pids_limit=policy.pids_limit,
                    timeout=work_timeout,
                    work_duration=None
                    if policy.work_duration is None
                    else WorkDurationPolicyV2(timeout_seconds=work_timeout),
                    run_liveness=RunLivenessBindingV1.model_validate(
                        {
                            "task_id": task_id,
                            "attempt": attempt,
                            "snapshot_digest": snapshot["digest"],
                            "actor_digest": run_digest(run.get("execution_actor")),
                            "run_cut_digest": run_cut_digest(run),
                            "factory_fence_digest": run_digest(scope["fence"].model_dump(mode="json")),
                            "worker_epoch": uuid4().hex,
                        }
                    )
                    if policy.profile.run_liveness is not None
                    else None,
                    max_output_bytes=policy.max_output_bytes,
                )
                payload = {
                    "parent_attempt": attempt,
                    "factory_fence": scope["fence"].model_dump(mode="json"),
                    "ordinal": number,
                    "container_name": "pool-" + identity,
                    "snapshot_digest": snapshot["digest"],
                    "command": command,
                    "request_digest": pool_digest(invocation.model_dump(mode="json")),
                    "request": invocation.model_dump(mode="json"),
                }
                return payload

            await self._current(run_id, task_id, scope)
            current_source, _ = pack_workspace(
                workspace, policy.workspace_max_bytes, registry=self.artifacts.credential_registry()
            )
            if hashlib.sha256(current_source).hexdigest() != bundle.source_archive_digest:
                raise ValueError("Command source changed during pool preparation")
            created = await self.journal.sandbox_command_intent(
                run_id, task_id, attempt, snapshot, prepare, ordinal=ordinal
            )
            execution_id = created["execution"]["execution_id"]
            payload = created["execution"]["request"]
            invocation = RunnerProjectInvocation.model_validate(payload["request"])
            if not created["fresh"]:
                previous = created["execution"]
                evidence = previous["effects"].get("start", {}).get("evidence", {})
                if previous["status"] == "removed" and evidence.get("workspace_import") == "complete":
                    await self._current(run_id, task_id, scope)
                    return self._result(argv, evidence["exit_code"], evidence["output"])
                raise RuntimeError("Existing Factory invocation requires reconciliation; it cannot be resubmitted")
            self.artifacts.retain(policy.profile, invocation, "source", archive)
            await self.journal.sandbox_effect_receipt(
                run_id,
                task_id,
                execution_id,
                effect="create",
                evidence={"scope": "factory_request_prepared", "bundle": bundle.model_dump(mode="json")},
            )
            await self._current(run_id, task_id, scope)
            started = await self.journal.sandbox_effect_intent(
                run_id, task_id, execution_id, effect="start", payload=payload
            )
            if started["disposition"] != "execute":
                raise RuntimeError("Factory submission already exists; reconciliation required")
            await self._current(run_id, task_id, scope)
            self.waiting[execution_id] = {
                "scope": scope,
                "invocation": invocation,
                "task_id": task_id,
                "payload": payload,
                "workspace": workspace,
                "cwd": cwd,
            }
            try:
                submitted = await self.client.submit(invocation, scope["fence"], archive)
                run_ack: dict[str, Any] | None = None
                renew_at = 0.0
                if invocation.run_liveness is not None:
                    run_ack = validate_run_ack(
                        submitted.get("run_lease"),
                        pool_digest(invocation.model_dump(mode="json")),
                        invocation.run_liveness,
                        sequence=0,
                    )
                    renew_at = self.run_clock() + policy.profile.lease_seconds / 3
                async with asyncio.timeout(
                    None if invocation.timeout is None else policy.profile.admission_seconds + invocation.timeout + 120
                ):
                    while True:
                        await self._current(run_id, task_id, scope)
                        try:
                            observed = await self.client.observe(invocation)
                        except (httpx.TransportError, TimeoutError):
                            # Read-only observation may span a controller restart; submission is never retried.
                            await asyncio.sleep(0.1)
                            continue
                        if observed.get("request") != invocation.model_dump(mode="json"):
                            raise RuntimeError("Controller changed the immutable invocation")
                        if (
                            observed["status"] == "finished"
                            and (observed.get("receipt") or {}).get("outcome") == "completed"
                        ):
                            break
                        if observed["status"] not in {
                            "queued",
                            "leased",
                            "creating",
                            "created",
                            "starting",
                            "running",
                            "draining",
                        }:
                            raise RuntimeError("Controller invocation requires reconciliation")
                        if run_ack is not None and self.run_clock() >= renew_at:
                            await self._current(run_id, task_id, scope)
                            acknowledged = await self.client.renew_run(invocation, scope["fence"], run_ack)
                            assert invocation.run_liveness is not None
                            run_ack = validate_run_ack(
                                acknowledged,
                                pool_digest(invocation.model_dump(mode="json")),
                                invocation.run_liveness,
                                sequence=run_ack["sequence"] + 1,
                                controller_epoch=run_ack["controller_epoch"],
                            )
                            await self._current(run_id, task_id, scope)
                            renew_at = self.run_clock() + policy.profile.lease_seconds / 3
                        await asyncio.sleep(0.1)
                receipt = observed.get("receipt") or {}
                if receipt.get("drained") is not True or type(receipt.get("exit_code")) is not int:
                    raise RuntimeError("Controller completion lacks an observed drain or exit receipt")
                policy.profile.validate_invocation(invocation)
                limits = {
                    "output": min(
                        invocation.max_output_bytes, policy.max_output_bytes, policy.profile.max_output_bytes
                    ),
                    "result": min(policy.workspace_max_bytes, policy.profile.max_source_bytes) + 1_048_576,
                }
                admitted = {
                    kind: validate_artifact_receipt(kind, receipt[kind], limit) for kind, limit in limits.items()
                }
                files: dict[ArtifactKind, bytes] = {}
                for kind in ("output", "result"):
                    await self._current(run_id, task_id, scope)
                    data = await self.client.artifact(invocation, kind, admitted[kind])
                    await self._current(run_id, task_id, scope)
                    if self.artifacts.retain(policy.profile, invocation, kind, data) != admitted[kind]:
                        raise RuntimeError("Returned artifact differs from the immutable receipt")
                    files[kind] = data
                output, returned = files["output"], files["result"]
            except asyncio.CancelledError:
                with suppress(Exception):
                    fence = await self.authority.exclusive_fence()
                    await self.client.cancel(invocation, fence)
                raise
            except Exception:
                if policy.work_duration is not None:
                    with suppress(Exception):
                        fence = await self.authority.exclusive_fence()
                        await self.client.cancel(invocation, fence)
                raise RuntimeError(
                    "Factory submission or observation requires reconciliation; no automatic replay"
                ) from None
            finally:
                self.waiting.pop(execution_id, None)
            evidence = {
                "exit_code": receipt["exit_code"],
                "output": output.decode("utf-8", errors="replace"),
                "output_receipt": receipt["output"],
                "result": receipt["result"],
                "workspace_import": "requires_reconciliation",
                "controller_drained": True,
            }
            try:
                await self._current(run_id, task_id, scope)
                evidence.update(
                    collect_workspace(
                        returned,
                        workspace,
                        policy.workspace_max_bytes,
                        bundle.source_archive_digest,
                        registry=self.artifacts.credential_registry(),
                    )
                )
                evidence["workspace_import"] = "complete"
            except Exception:
                await self.store.assert_ownership()
                await self.journal.sandbox_effect_receipt(
                    run_id, task_id, execution_id, effect="start", evidence=evidence
                )
                raise RuntimeError(
                    "Factory workspace publication requires reconciliation; retained result cannot replay"
                ) from None
            await self._current(run_id, task_id, scope)
            await self.journal.sandbox_effect_receipt(run_id, task_id, execution_id, effect="start", evidence=evidence)
            await self.journal.sandbox_effect_intent(run_id, task_id, execution_id, effect="cleanup", payload=payload)
            await self.journal.sandbox_effect_receipt(
                run_id,
                task_id,
                execution_id,
                effect="cleanup",
                evidence={"scope": "controller_observed_drain", "drained": True},
            )
            return self._result(argv, receipt["exit_code"], evidence["output"])
