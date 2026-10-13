# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit deployment and rollback of independently verified source snapshots."""

from __future__ import annotations

import asyncio
import stat
import zipfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pyfly.container import service
from pyfly.kernel.lifecycle import CONSUMER_PHASE

from firefly_forge.configuration import redact_secrets
from firefly_forge.deployment_credentials import DeploymentCredentials
from firefly_forge.engine import Engine, private_path
from firefly_forge.journal import Journal
from firefly_forge.models import Command
from firefly_forge.process import checked, execute
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.secrets import redact_environment
from firefly_forge.store import Store, now


class DeploymentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    repository_id: str = Field(min_length=1, max_length=100)
    commands: list[Command] = Field(min_length=1, max_length=20)
    verification: list[Command] = Field(min_length=1, max_length=20)
    rollback_commands: list[Command] = Field(default_factory=list, max_length=20)
    rollback_verification: list[Command] = Field(default_factory=list, max_length=20)
    recipe_files: dict[str, str] = Field(default_factory=dict, max_length=50)
    credential_profile: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")

    @field_validator("recipe_files")
    @classmethod
    def bounded_recipe(cls, value: dict[str, str]) -> dict[str, str]:
        if sum(len(content.encode()) for content in value.values()) > 1_000_000:
            raise ValueError("Deployment recipe exceeds 1 MB")
        for name in value:
            path = Path(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or "\\" in name
                or any(part.startswith(".") for part in path.parts)
                or any(ord(char) < 32 for char in name)
            ):
                raise ValueError("Use ordinary relative paths inside the managed deployment recipe")
        return value

    @model_validator(mode="after")
    def complete_rollback(self) -> DeploymentTarget:
        if bool(self.rollback_commands) != bool(self.rollback_verification):
            raise ValueError("Configure both rollback commands and rollback verification")
        return self


class DeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")


@service
class DeploymentManager:
    phase = CONSUMER_PHASE + 1

    def __init__(
        self,
        store: Store,
        engine: Engine,
        journal: Journal | None = None,
        credentials: DeploymentCredentials | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.journal = journal or Journal(store)
        self.private = store.configuration
        self.credentials = credentials or DeploymentCredentials(self.private)
        self.secret_registries: list[Any] = []
        self.engine.credential_registry = self._redaction_settings
        self.path = self.private.directory / "deployment-targets.json"
        self.workspaces = store.path.parent / "deployments"
        self.workspaces.mkdir(parents=True, exist_ok=True)
        self.active: dict[str, asyncio.Task[None]] = {}
        self.poller: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.store.runtime_roles and not self.store.runtime_roles.executes_jobs:
            return
        if self.poller is not None and not self.poller.done():
            return
        await self._assert_ownership()
        await self.store.recover_deployments()
        self.poller = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self.poller:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)
        for task in self.active.values():
            task.cancel()
        await asyncio.gather(*self.active.values(), return_exceptions=True)
        self.active.clear()

    def targets(self) -> list[dict[str, Any]]:
        return [{"id": identifier, **value} for identifier, value in self.private._read(self.path).items()]

    def _redaction_settings(self, run: dict[str, Any] | None = None) -> dict[str, str]:
        settings = {
            f"{provider.upper()}_{key}": value
            for provider in ("codex", "claude", "bridge", "agentic")
            for key, value in self.private.resolve(provider).items()
        }
        if run and run.get("config_revision"):
            settings.update(
                {"RUN_" + key: value for key, value in self.private.load_snapshot(run["config_revision"]).items()}
            )
        for component in (self.engine.connections, self.engine.knowledge, *self.secret_registries):
            registry = getattr(component, "redaction_settings", None)
            if callable(registry):
                settings.update(registry())
        settings.update(self.credentials.redaction_settings())
        return settings

    async def configure_target(self, identifier: str, request: DeploymentTarget) -> dict[str, Any]:
        DeploymentRequest(target_id=identifier)
        await self.store.repository(request.repository_id)
        redaction = self._redaction_settings()
        for command in (
            request.commands + request.verification + request.rollback_commands + request.rollback_verification
        ):
            text = " ".join(command.argv)
            if redact_environment(text) != text or redact_secrets(text, redaction) != text:
                raise ValueError(
                    "Deployment commands must not contain credentials; use the server credential environment"
                )
        for content in request.recipe_files.values():
            if redact_environment(content) != content or redact_secrets(content, redaction) != content:
                raise ValueError("Deployment recipe files must not contain credentials")
        with self.private.lock:
            targets = self.private._read(self.path)
            targets[identifier] = request.model_dump()
            self.private._write(self.path, targets)
        return {"id": identifier, **request.model_dump()}

    async def create(self, run_id: str, target_id: str) -> dict[str, Any]:
        DeploymentRequest(target_id=target_id)
        target = self.private._read(self.path).get(target_id)
        if not target:
            raise ValueError("Configure a deployment target before deploying")
        parsed = DeploymentTarget.model_validate(target)
        run = await self.store.get_run(run_id)
        if parsed.repository_id != run["repository_id"]:
            raise ValueError("The deployment target belongs to another project")
        actor = self._release_actor(run)
        await ProjectAccess(self.store).authorize_actor(actor, run.get("project_id"), "release")
        if self.store.runtime_roles and self.store.runtime_roles.role == "api":
            if (
                run["status"] != "succeeded"
                or not run.get("source_tree")
                or not run.get("final_verification")
                or any(check["exit_code"] != 0 for check in run["final_verification"])
            ):
                raise ValueError("Deployment requires a verified source snapshot")
        else:
            await self.engine.validated_workspace(run_id)
        result = await self.store.create_deployment(
            {
                "run_id": run_id,
                "target_id": target_id,
                "operation": "deploy",
                "project_id": run.get("project_id"),
                "execution_actor": actor,
                "source_tree": run["source_tree"],
                "base_revision": run["base_revision"],
                "target_snapshot": parsed.model_dump(),
                "credential_revision": self.credentials.snapshot(parsed.credential_profile),
                "evidence": [],
            }
        )
        await self.store.event(run_id, "deployment", f"Deployment queued for {parsed.name}: {result['id']}")
        return result

    async def rollback(self, identifier: str) -> dict[str, Any]:
        previous = await self.store.get_deployment(identifier)
        if previous["operation"] != "deploy" or previous["status"] not in {"succeeded", "failed", "interrupted"}:
            raise ValueError("Rollback requires a completed deployment that has no active worker")
        target = DeploymentTarget.model_validate(previous["target_snapshot"])
        if not target.rollback_commands:
            raise ValueError("This deployment has no configured rollback commands")
        run = await self.store.get_run(previous["run_id"])
        actor = self._release_actor(run)
        project_id = previous.get("project_id", run.get("project_id"))
        await ProjectAccess(self.store).authorize_actor(actor, project_id, "release")
        result = await self.store.create_deployment(
            {
                "run_id": previous["run_id"],
                "target_id": previous["target_id"],
                "operation": "rollback",
                "project_id": project_id,
                "execution_actor": actor,
                "parent_id": previous["id"],
                "source_tree": previous["source_tree"],
                "base_revision": previous["base_revision"],
                "target_snapshot": target.model_dump(),
                "credential_revision": previous.get("credential_revision"),
                "evidence": [],
            }
        )
        await self.store.event(previous["run_id"], "deployment", f"Rollback explicitly requested: {result['id']}")
        return result

    async def _poll(self) -> None:
        try:
            while True:
                await self._assert_ownership()
                self.active = {identifier: task for identifier, task in self.active.items() if not task.done()}
                for job in await self.store.queued_deployments():
                    if len(self.active) >= 2:
                        break
                    if job["status"] == "queued" and job["id"] not in self.active:
                        self.active[job["id"]] = asyncio.create_task(self.execute_deployment(job["id"]))
                await asyncio.sleep(0.2)
        finally:
            for task in self.active.values():
                task.cancel()
            await asyncio.gather(*self.active.values(), return_exceptions=True)

    async def _assert_ownership(self) -> None:
        if self.store.runtime_roles:
            self.store.runtime_roles.require_execution()
        if self.engine.poller is not None:
            await self.store.assert_ownership()

    @staticmethod
    def _release_actor(run: dict[str, Any]) -> dict[str, Any] | None:
        principal = CURRENT_PRINCIPAL.get()
        return ProjectAccess.actor_snapshot(principal) if principal else run.get("execution_actor")

    async def _authorize_job(self, job: dict[str, Any]) -> None:
        run = await self.store.get_run(job["run_id"])
        actor = job["execution_actor"] if "execution_actor" in job else run.get("execution_actor")
        await ProjectAccess(self.store).authorize_actor(actor, job.get("project_id", run.get("project_id")), "release")

    async def _source(self, job: dict[str, Any], directory: Path) -> None:
        await self._authorize_job(job)
        workspace = await self.engine.validated_workspace(job["run_id"])
        run = await self.store.get_run(job["run_id"])
        if run["source_tree"] != job["source_tree"]:
            raise ValueError("The queued deployment no longer matches its verified source")
        archive = self.workspaces / (job["id"] + ".zip")
        await checked(
            ["git", "archive", "--format=zip", f"--output={archive.resolve()}", job["source_tree"]], workspace
        )
        directory.mkdir()
        with zipfile.ZipFile(archive) as source:
            total = 0
            for info in source.infolist():
                filename = Path(info.filename)
                if filename.is_absolute() or ".." in filename.parts or private_path(info.filename):
                    raise ValueError("Verified source contains a private or unsafe deployment path")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("Deployment source must not contain symbolic links")
                total += info.file_size
                if total > 500_000_000:
                    raise ValueError("Deployment source exceeds 500 MB")
                source.extract(info, directory)

    async def execute_deployment(self, identifier: str) -> None:
        await self._assert_ownership()
        if not await self.store.claim_deployment(identifier):
            return
        job = await self.store.get_deployment(identifier)
        run = await self.store.get_run(job["run_id"])
        settings = self._redaction_settings(run)

        def safe(value: str) -> str:
            return redact_secrets(redact_environment(value), settings)

        active_key: str | None = None
        try:
            target = DeploymentTarget.model_validate(job["target_snapshot"])
            directory = self.workspaces / identifier
            await self._source(job, directory)
            if target.recipe_files:
                managed = directory / ".forge-deployment"
                if managed.exists() or managed.is_symlink():
                    raise ValueError("Verified source already contains the reserved deployment recipe directory")
                managed.mkdir(mode=0o700)
                for name, content in target.recipe_files.items():
                    path = managed / name
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path.write_text(content)
                    path.chmod(0o600)
            credential_environment = self.credentials.load(job.get("credential_revision"))
            home = self.private.directory / "deployment-homes" / identifier
            home.mkdir(parents=True, mode=0o700)
            credential_environment = {
                "HOME": str(home.resolve()),
                "XDG_CONFIG_HOME": str(home.resolve() / ".config"),
                "AWS_CONFIG_FILE": str(home.resolve() / "aws-config"),
                "AWS_SHARED_CREDENTIALS_FILE": str(home.resolve() / "aws-credentials"),
                "AWS_EC2_METADATA_DISABLED": "true",
                "GCE_METADATA_HOST": "127.0.0.1:9",
                **credential_environment,
            }
            known_hosts = credential_environment.pop("SSH_KNOWN_HOSTS_FILE", None)
            if known_hosts:
                ssh = home / ".ssh"
                ssh.mkdir(mode=0o700)
                destination = ssh / "known_hosts"
                destination.write_bytes(Path(known_hosts).read_bytes())
                destination.chmod(0o600)
            rollback = job["operation"] == "rollback"
            steps = [
                ("rollback" if rollback else "deploy", command)
                for command in (target.rollback_commands if rollback else target.commands)
            ]
            steps += [
                ("verify", command) for command in (target.rollback_verification if rollback else target.verification)
            ]
            evidence: list[dict[str, Any]] = []
            for index, (stage, command) in enumerate(steps):
                await self._authorize_job(job)
                task_id = f"{job['operation']}-{identifier}-{index}"
                intent = await self.journal.intent(
                    job["run_id"],
                    task_id,
                    payload={
                        "deployment_id": identifier,
                        "target_id": job["target_id"],
                        "source_tree": job["source_tree"],
                        "stage": stage,
                        "actor_id": (job.get("execution_actor") or {}).get("user_id"),
                        "argv": [safe(arg) for arg in command.argv],
                    },
                )
                if intent["disposition"] != "execute":
                    raise ValueError("Deployment effect requires reconciliation; it will not be replayed")
                active_key = intent["idempotency_key"]
                await self.store.update_deployment(identifier, active_stage=stage, active_task=task_id)
                await self.store.event(
                    job["run_id"], "deployment", safe(f"{target.name}: {stage} {' '.join(command.argv)}")
                )
                await self._assert_ownership()
                try:
                    await self._authorize_job(job)
                except AccessDenied:
                    await self.journal.fail(
                        job["run_id"],
                        active_key,
                        "authentication",
                        "Release authorization was revoked before execution",
                    )
                    active_key = None
                    raise
                result = await execute(command.argv, directory, command.timeout, env=credential_environment)
                evidence.append(
                    {
                        "stage": stage,
                        "argv": [safe(arg) for arg in command.argv],
                        "exit_code": result.exit_code,
                        "output": safe(result.output[-32000:]),
                        "created_at": now(),
                    }
                )
                await self.store.update_deployment(identifier, evidence=evidence)
                if result.exit_code:
                    await self.journal.fail(
                        job["run_id"], active_key, "verification", f"{stage} failed with exit code {result.exit_code}"
                    )
                    active_key = None
                    raise ValueError(f"{stage.title()} command failed with exit code {result.exit_code}")
                await self.journal.receipt(job["run_id"], active_key, evidence[-1])
                active_key = None
            await self.store.update_deployment(identifier, status="succeeded", active_stage="completed")
            await self.store.event(job["run_id"], "deployment", f"{job['operation'].title()} verified: {identifier}")
        except asyncio.CancelledError:
            await self.store.update_deployment(
                identifier, status="interrupted", error="Worker stopped; inspect the target before another deployment"
            )
            raise
        except Exception as exc:
            # An unreceipted external effect remains ambiguous after a transport or persistence failure.
            await self.store.update_deployment(
                identifier, status="interrupted" if active_key else "failed", error=safe(str(exc))
            )
            await self.store.event(job["run_id"], "deployment", safe(f"{job['operation'].title()} failed: {exc}"))
