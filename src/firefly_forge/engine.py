# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable software production lifecycle on isolated Git clones."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock, Timeout
from pyfly.container import service
from pyfly.kernel.lifecycle import CONSUMER_PHASE

from firefly_forge.configuration import redact_secrets
from firefly_forge.models import Command
from firefly_forge.policies import inject_policy_snapshot, policy_commands
from firefly_forge.process import checked, execute
from firefly_forge.scm import PullRequests, clone, push
from firefly_forge.secrets import redact_environment
from firefly_forge.skills import DEFAULT_SKILLS, inject_skills
from firefly_forge.store import TERMINAL, Store, now


def redact(value: str) -> str:
    return redact_environment(value)


def default_commands(workspace: Path) -> list[Command]:
    makefile = workspace / "Makefile"
    if makefile.is_file() and re.search(r"^check\s*:", makefile.read_text(), re.MULTILINE):
        return [Command(argv=["make", "check"], timeout=600)]
    package = workspace / "package.json"
    if package.is_file():
        scripts = json.loads(package.read_text()).get("scripts", {})
        if "check" in scripts:
            return [Command(argv=["npm", "run", "check"], timeout=600)]
        if "test" in scripts:
            return [Command(argv=["npm", "test"], timeout=600)]
    if (workspace / "pyproject.toml").is_file() and (workspace / "tests").is_dir():
        return [Command(argv=["uv", "run", "pytest"], timeout=600)]
    if (workspace / "artisan").is_file() and (workspace / "vendor").is_dir():
        return [Command(argv=["php", "artisan", "test"], timeout=600)]
    raise ValueError("No verification command found; supply commands or add an executable make check target")


def private_path(path: str) -> bool:
    parts = Path(path).parts
    return any(
        part in {".git", ".codex", ".claude", ".forge", "node_modules", ".venv", "vendor"} for part in parts
    ) or any(
        part in {".env", ".npmrc", ".pypirc", ".ssh", "credentials.json", "id_rsa", "id_ed25519"}
        or (part.startswith(".env.") and part not in {".env.example", ".env.sample", ".env.template"})
        or part.endswith((".pem", ".key"))
        for part in parts
    )


class ReconciliationRequired(RuntimeError):
    pass


@service
class Engine:
    phase = CONSUMER_PHASE

    def __init__(
        self,
        store: Store,
        workspaces: Path,
        provider_factory: Callable[[str], Any] | None = None,
        concurrency: int = 2,
        connections: Any = None,
        journal: Any = None,
    ) -> None:
        self.store = store
        self.connections = connections
        self.journal = journal
        self.knowledge: Any = None
        self.workspaces = workspaces
        self.workspaces.mkdir(parents=True, exist_ok=True)
        if provider_factory is None:
            from firefly_forge.providers import get_provider

            provider_factory = get_provider
        self.configured_provider = provider_factory.__module__ == "firefly_forge.providers"
        self.provider_factory = provider_factory
        self.concurrency = concurrency
        self.active: dict[str, asyncio.Task[None]] = {}
        self.active_projects: dict[str, str] = {}
        self.last_project: str | None = None
        self.poller: asyncio.Task[None] | None = None
        self.stopping = False
        self.enforce_ownership = False
        self.ownership_failure: str | None = None
        self.publication_lock = asyncio.Lock()
        self.owner_lock = FileLock(str(store.path.with_suffix(".lock")))

    async def start(self) -> None:
        if self.poller is not None and not self.poller.done():
            return
        try:
            self.owner_lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError("A factory server is already running against this data directory") from exc
        self.enforce_ownership = True
        try:
            await self.store.acquire_ownership()
        except BaseException:
            self.owner_lock.release()
            raise
        if self.journal:
            await self.journal.recover()
        (await self.store.recover())
        self.stopping = False
        self.poller = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        self.stopping = True
        if self.poller:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)
        tasks = list(self.active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.release_ownership()
        self.owner_lock.release()

    async def assert_ownership(self) -> None:
        if self.enforce_ownership:
            await self.store.assert_ownership()

    async def _poll(self) -> None:
        while True:
            try:
                await self.assert_ownership()
            except RuntimeError:
                self.ownership_failure = "Execution ownership was lost; restart after inspecting interrupted work"
                self.stopping = True
                for task in self.active.values():
                    task.cancel()
                await asyncio.gather(*self.active.values(), return_exceptions=True)
                await self.store.release_ownership()
                self.owner_lock.release()
                return
            self.active = {identifier: task for identifier, task in self.active.items() if not task.done()}
            if self.journal:
                for identifier in await self.journal.ready_retries():
                    if identifier not in self.active:
                        try:
                            await self.journal.resume(identifier, auto=True)
                        except ValueError:
                            continue
            await self.dispatch_queued()
            await asyncio.sleep(0.2)

    async def dispatch_queued(self) -> None:
        self.active = {identifier: task for identifier, task in self.active.items() if not task.done()}
        self.active_projects = {
            identifier: project for identifier, project in self.active_projects.items() if identifier in self.active
        }
        available = max(0, self.concurrency - len(self.active))
        if not available:
            return
        occupied = set(self.active_projects.values())
        eligible: dict[str, str] = {}
        for identifier in await self.store.queued():
            if identifier in self.active:
                continue
            run = await self.store.get_run(identifier)
            project = run["repository_id"]
            if project not in occupied:
                eligible.setdefault(project, identifier)
        projects = list(eligible)
        if self.last_project in projects:
            pivot = projects.index(self.last_project) + 1
            projects = projects[pivot:] + projects[:pivot]
        for project in projects[:available]:
            identifier = eligible[project]
            self.active_projects[identifier] = project
            self.active[identifier] = asyncio.create_task(self.execute_run(identifier))
            self.last_project = project

    async def cancel(self, identifier: str) -> None:
        current = await self.store.get_run(identifier)
        if current["status"] in TERMINAL:
            return
        (await self.store.finish(identifier, "cancelled"))
        task = self.active.get(identifier)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def execute_run(self, identifier: str) -> None:
        if not (await self.store.claim(identifier)):
            return
        workspace = self.workspaces / identifier
        settings: dict[str, str] = {}
        active_key: str | None = None

        def safe(message: str) -> str:
            return redact_secrets(redact(message), settings)

        def safe_data(value: Any) -> Any:
            if isinstance(value, str):
                return safe(value)
            if isinstance(value, dict):
                return {safe(str(key)): safe_data(item) for key, item in value.items()}
            if isinstance(value, list):
                return [safe_data(item) for item in value]
            return value

        async def emit(stage: str, message: str) -> None:
            (await self.store.event(identifier, stage, safe(message)))

        try:
            run = await self.store.get_run(identifier)
            if run.get("config_revision"):
                settings = self.store.configuration.load_snapshot(run["config_revision"])
            if self.knowledge:
                settings.update(self.knowledge.credentials(run))
            repository = await self.store.repository(run["repository_id"])
            git_env = None
            if self.connections and (
                repository.get("connection_provider") or self.connections.configured(repository["provider"])
            ):
                git_env = await self.connections.git_environment(repository)
                settings.update(git_env)
            branch = f"forge/{identifier[:12]}"
            prepare = await self._begin_task(identifier, "prepare", workspace, settings)
            active_key = prepare.get("idempotency_key")
            if run.get("resume_existing") or prepare["disposition"] == "completed":
                observed = await self._inspect_workspace(identifier, workspace, settings)
                base = observed["base_revision"]
                run = await self.store.get_run(identifier)
                await emit("preparing", "Continuing the retained authorized workspace")
            else:
                await emit("preparing", "Creating isolated repository workspace")
                base = await clone(repository, workspace, branch, env=git_env)
            exclusions = workspace / ".git" / "info" / "exclude"
            existing = exclusions.read_text() if exclusions.exists() else ""
            exclusions.write_text(
                existing + "\n.venv/\nnode_modules/\nvendor/\n__pycache__/\n*.pyc\n"
                "dist/\nbuild/\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n"
            )
            (await self.store.update(identifier, base_revision=base, branch=branch))
            await self._receipt(
                identifier,
                active_key,
                {"base_revision": base, "branch": branch, "origin": repository["url"]},
                checkpoint={"base_revision": base, "branch": branch},
            )
            active_key = None
            if run.get("operation") == "test":
                await self._test(identifier, workspace, base, run, safe, emit, settings)
                await self._remember_knowledge(identifier, emit)
                return
            if self.configured_provider:
                from firefly_forge.providers import get_provider

                provider = get_provider(run["agent"], settings if run.get("config_revision") else None)
            else:
                provider = self.provider_factory(run["agent"])
            source_brief = (
                inject_policy_snapshot(run["brief"], run["policy_snapshot"])
                if run.get("policy_snapshot")
                else inject_skills(run["brief"], run.get("skills", DEFAULT_SKILLS))
            )
            if run.get("operation") == "maintain":
                source_brief += (
                    "\n\nMaintenance operation: inspect the existing implementation, preserve public "
                    "behavior, fix the requested issue or dependencies, and add focused regression "
                    "tests. Do not replace the product with a new scaffold."
                )
            if run.get("resume_existing"):
                previous = [item for item in run.get("verification", []) if item["exit_code"] != 0][-5:]
                source_brief += (
                    "\n\nContinue the retained partial implementation. Inspect existing files and the durable plan "
                    "before editing; preserve completed work."
                )
                if previous:
                    source_brief += "\nPrevious failed checks:\n" + safe(json.dumps(previous))
            if self.knowledge and "knowledge_context" in run:
                source_brief += run["knowledge_context"]
            elif self.knowledge:
                retrieved = await self.knowledge.retrieve(run)
                await self.store.update(
                    identifier,
                    knowledge_citations=retrieved["citations"],
                    knowledge_warnings=retrieved["warnings"],
                    knowledge_context=retrieved["context"],
                    knowledge_context_digest=hashlib.sha256(retrieved["context"].encode()).hexdigest(),
                )
                for warning in retrieved["warnings"]:
                    await emit("knowledge", warning)
                if retrieved["citations"]:
                    await emit(
                        "knowledge", f"Retrieved {len(retrieved['citations'])} cited project/framework references"
                    )
                source_brief += retrieved["context"]
            brief = source_brief
            repairs = max(0, min(3, int(os.environ.get("FORGE_REPAIR_ATTEMPTS", "2"))))
            repair_failures: list[dict[str, Any]] = []
            for attempt in range(1, repairs + 2):
                (await self.store.update(identifier, status="developing", attempt=attempt))
                implementation = await self._begin_task(identifier, "implement", workspace, settings, repair_failures)
                active_key = implementation.get("idempotency_key")
                if implementation["disposition"] != "completed":
                    result = await provider.execute(workspace, brief, run["backend"], run["frontend"], emit)
                    usage = safe_data(getattr(result, "usage", {}))
                    (await self.store.update(identifier, summary=safe(result.summary), usage=usage))
                files, diff, tree = await self._snapshot(
                    workspace, base, secrets=settings, expected_head=run.get("checkpoint_revision")
                )
                await self._receipt(
                    identifier,
                    active_key,
                    {"source_tree": tree, "changed_files": files},
                    checkpoint={"base_revision": base, "source_tree": tree},
                )
                active_key = None
                (
                    await self.store.update(
                        identifier, changed_files=files, diff=safe(diff[:1_000_000]), status="validating"
                    )
                )
                commands = [Command.model_validate(command) for command in run["commands"]] or default_commands(
                    workspace
                )
                commands += policy_commands(run.get("policy_snapshot", {}), run["backend"])
                if len(commands) > 20:
                    raise ValueError("Selected policies exceed the 20 verification-command limit")
                verification = await self._begin_task(identifier, "verify", workspace, settings, repair_failures)
                active_key = verification.get("idempotency_key")
                retained = await self.store.get_run(identifier)
                final: list[dict[str, Any]] = (
                    retained.get("final_verification", []) if verification["disposition"] == "completed" else []
                )
                if verification["disposition"] == "completed":
                    saved_task = next(
                        item for item in (await self.journal.plan(identifier))["tasks"] if item["id"] == "verify"
                    )
                    final = final or saved_task.get("evidence", {}).get("checks", [])
                    saved_tree = retained.get("source_tree") or saved_task.get("evidence", {}).get("source_tree")
                    if not final or saved_tree != tree:
                        raise ReconciliationRequired("Retained verification does not match the resumed source tree")
                for command in [] if verification["disposition"] == "completed" else commands:
                    await emit("validating", f"Attempt {attempt}: {' '.join(command.argv)}")
                    check = await execute(command.argv, workspace, command.timeout)
                    evidence = {
                        "argv": command.argv,
                        "exit_code": check.exit_code,
                        "output": safe(check.output[-32000:]),
                        "created_at": now(),
                        "attempt": attempt,
                    }
                    final.append(evidence)
                    evidence_list = (await self.store.get_run(identifier))["verification"]
                    evidence_list.append(evidence)
                    (await self.store.update(identifier, verification=evidence_list))
                failures = [check for check in final if check["exit_code"] != 0]
                final_files, final_diff, final_tree = await self._snapshot(
                    workspace, base, secrets=settings, expected_head=run.get("checkpoint_revision")
                )
                if final_tree != tree:
                    failures.append(
                        {
                            "argv": ["source-snapshot"],
                            "exit_code": 1,
                            "output": "Verification modified the source tree; validate the final source snapshot",
                        }
                    )
                await self._receipt(
                    identifier, active_key, {"checks": final, "failed_checks": failures, "source_tree": final_tree}
                )
                active_key = None
                if not failures:
                    files = final_files
                    (
                        await self.store.update(
                            identifier,
                            final_verification=final,
                            source_tree=final_tree,
                            changed_files=files,
                            diff=safe(final_diff[:1_000_000]),
                        )
                    )
                    break
                if attempt > repairs:
                    raise ValueError(f"Verification failed after {attempt} implementation attempts")
                feedback = "\n\n".join(
                    f"{' '.join(check['argv'])}: exit {check['exit_code']}\n{check['output']}" for check in failures
                )
                await emit("repairing", "Verification failed; returning all failed checks to the coding worker")
                repair_failures = failures
                brief = (
                    source_brief
                    + "\n\nVerification failed. Repair the existing implementation without weakening "
                    + ("checks or replacing tests with no-ops. Run these exact checks successfully:\n" + feedback)
                )
            checkpoint = await self._begin_task(identifier, "checkpoint", workspace, settings)
            active_key = checkpoint.get("idempotency_key")
            if checkpoint["disposition"] != "completed":
                await self._checkpoint_commit(identifier, workspace)
                await self._artifact(identifier, workspace, files, final_diff)
                saved = await self.store.get_run(identifier)
                await self._receipt(
                    identifier,
                    active_key,
                    {
                        "source_tree": saved["source_tree"],
                        "checkpoint_revision": saved["checkpoint_revision"],
                        "artifacts": saved["artifacts"],
                    },
                    checkpoint={
                        "source_tree": saved["source_tree"],
                        "checkpoint_revision": saved["checkpoint_revision"],
                    },
                )
            active_key = None
            (await self.store.finish(identifier, "succeeded"))
            await self._remember_knowledge(identifier, emit)
            if run["publish"]:
                await self.publish(identifier)
        except asyncio.CancelledError:
            (await self.store.finish(identifier, "interrupted" if self.stopping else "cancelled"))
            raise
        except Exception as exc:
            from firefly_forge.providers import classify_provider_failure

            classification = classify_provider_failure(exc)
            if self.journal and active_key and classification:
                await self.journal.fail(
                    identifier, active_key, classification[0], safe(str(exc)), retry_after=classification[1]
                )
                await emit("paused", "Execution paused: " + classification[0])
            elif isinstance(exc, ReconciliationRequired):
                await self.store.update(identifier, status="needs_reconciliation", error=safe(str(exc)))
            else:
                (await self.store.finish(identifier, "failed", error=safe(str(exc))))
            if (await self.store.get_run(identifier))["status"] == "succeeded":
                (await self.store.update(identifier, publication_error=safe(str(exc))))
                await emit("publication", f"Publication failed: {exc}")

    async def _inspect_workspace(self, identifier: str, workspace: Path, secrets: dict[str, str]) -> dict[str, Any]:
        run = await self.store.get_run(identifier)
        repository = await self.store.repository(run["repository_id"])
        if workspace.is_symlink() or not (workspace / ".git").is_dir() or (workspace / ".git").is_symlink():
            raise ReconciliationRequired("Retained workspace is unavailable or its Git metadata is invalid")
        top = await checked(["git", "rev-parse", "--show-toplevel"], workspace)
        if Path(top).resolve() != workspace.resolve():
            raise ReconciliationRequired("Retained workspace Git root does not match the run")
        origin = await checked(["git", "remote", "get-url", "origin"], workspace)
        if origin != repository["url"]:
            raise ReconciliationRequired(
                "Retained workspace origin changed; reconcile its authorization before continuing"
            )
        head = await checked(["git", "rev-parse", "HEAD"], workspace)
        base = run.get("base_revision") or run.get("checkpoint", {}).get("base_revision") or head
        expected = run.get("checkpoint_revision") or base
        if head != expected:
            tree = await checked(["git", "rev-parse", "HEAD^{tree}"], workspace)
            subject = await checked(["git", "show", "-s", "--format=%s", "HEAD"], workspace)
            parent = await checked(["git", "rev-parse", "HEAD^"], workspace)
            if (
                tree != run.get("source_tree")
                or parent != base
                or subject != f"Checkpoint verified forge run {identifier}"
            ):
                raise ReconciliationRequired("Retained workspace HEAD changed outside the recorded checkpoint")
            await self.store.update(identifier, checkpoint_revision=head)
        files, _, tree = await self._snapshot(
            workspace, base, allow_unchanged=True, secrets=secrets, expected_head=head
        )
        return {"base_revision": base, "head": head, "source_tree": tree, "origin": origin, "changed_files": files}

    async def _begin_task(
        self,
        identifier: str,
        task_id: str,
        workspace: Path,
        secrets: dict[str, str],
        failed_checks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        await self.assert_ownership()
        if not self.journal:
            return {"disposition": "execute"}
        plan = await self.journal.ensure_plan(identifier)
        task = next(item for item in plan["tasks"] if item["id"] == task_id)
        attempt = task["attempt"] + int(bool(failed_checks) and task["status"] == "done")
        run = await self.store.get_run(identifier)
        payload = {
            "config_revision": run.get("config_revision"),
            "policy_digest": run.get("policy_snapshot", {}).get("digest"),
            "knowledge_digest": run.get("knowledge_context_digest"),
        }
        if failed_checks:
            payload.update(reason="verification_repair", failed_checks=failed_checks)
        intent = await self.journal.intent(identifier, task_id, attempt=attempt, payload=payload)
        if intent["disposition"] != "needs_reconciliation":
            return dict(intent)
        run = await self.store.get_run(identifier)
        if not run.get("resume_existing"):
            raise ReconciliationRequired(
                "An unfinished task needs explicit operator resume and workspace reconciliation"
            )
        observed = await self._inspect_workspace(identifier, workspace, secrets)
        completed = task_id == "prepare"
        await self.journal.reconcile(
            identifier, intent["idempotency_key"], evidence=observed, outcome="completed" if completed else "retry"
        )
        return await self._begin_task(identifier, task_id, workspace, secrets, failed_checks)

    async def _receipt(
        self, identifier: str, key: str | None, evidence: dict[str, Any], *, checkpoint: dict[str, Any] | None = None
    ) -> None:
        if self.journal and key:
            await self.journal.receipt(identifier, key, evidence, checkpoint=checkpoint)

    async def _checkpoint_commit(self, identifier: str, workspace: Path) -> None:
        run = await self.store.get_run(identifier)
        if run.get("checkpoint_revision"):
            return
        if run["changed_files"]:
            await checked(
                [
                    "git",
                    "-c",
                    "user.name=firefly forge",
                    "-c",
                    "user.email=forge@getfirefly.io",
                    "commit",
                    "-m",
                    f"Checkpoint verified forge run {identifier}",
                ],
                workspace,
            )
        revision = await checked(["git", "rev-parse", "HEAD"], workspace)
        tree = await checked(["git", "rev-parse", "HEAD^{tree}"], workspace)
        if tree != run["source_tree"]:
            raise ValueError("Checkpoint commit does not match the verified source tree")
        await self.store.update(identifier, checkpoint_revision=revision)

    async def _remember_knowledge(self, identifier: str, emit: Callable[..., Any]) -> None:
        if not self.knowledge:
            return
        run = await self.store.get_run(identifier)
        try:
            result = await self.knowledge.remember(run, automatic=True)
            if not result.get("skipped"):
                await emit("knowledge", "Verified project memory saved")
        except ValueError as exc:
            await self.store.update(identifier, knowledge_memory_error=str(exc))
            await emit("knowledge", str(exc))

    async def _test(
        self,
        identifier: str,
        workspace: Path,
        base: str,
        run: dict[str, Any],
        safe: Callable[[str], str],
        emit: Callable[..., Any],
        settings: dict[str, str],
    ) -> None:
        _, _, tree = await self._snapshot(
            workspace, base, allow_unchanged=True, secrets=settings, expected_head=run.get("checkpoint_revision")
        )
        (await self.store.update(identifier, status="validating", attempt=1))
        await emit("validating", "Testing existing source without a coding provider")
        commands = [Command.model_validate(command) for command in run["commands"]] or default_commands(workspace)
        commands += policy_commands(run.get("policy_snapshot", {}), run["backend"])
        if len(commands) > 20:
            raise ValueError("Selected policies exceed the 20 verification-command limit")
        verification = await self._begin_task(identifier, "verify", workspace, settings)
        evidence = []
        if verification["disposition"] == "completed":
            saved_task = next(item for item in (await self.journal.plan(identifier))["tasks"] if item["id"] == "verify")
            evidence = saved_task.get("evidence", {}).get("checks", [])
            if not evidence or saved_task.get("evidence", {}).get("source_tree") != tree:
                raise ReconciliationRequired("Completed test evidence does not match the retained source tree")
        for command in [] if verification["disposition"] == "completed" else commands:
            await emit("validating", " ".join(command.argv))
            check = await execute(command.argv, workspace, command.timeout)
            evidence.append(
                {
                    "argv": command.argv,
                    "exit_code": check.exit_code,
                    "output": safe(check.output[-32000:]),
                    "created_at": now(),
                    "attempt": 1,
                }
            )
            (await self.store.update(identifier, verification=evidence))
        _, _, after = await self._snapshot(
            workspace, base, allow_unchanged=True, secrets=settings, expected_head=run.get("checkpoint_revision")
        )
        await self._receipt(identifier, verification.get("idempotency_key"), {"checks": evidence, "source_tree": after})
        if after != tree:
            raise ValueError("Verification modified the source tree; create a fresh run for changed source")
        if any(check["exit_code"] != 0 for check in evidence):
            raise ValueError("Verification failed; test operations never invoke automatic coding repairs")
        (
            await self.store.update(
                identifier, final_verification=evidence, source_tree=tree, summary="Existing source passed verification"
            )
        )
        checkpoint = await self._begin_task(identifier, "checkpoint", workspace, settings)
        await self._checkpoint_commit(identifier, workspace)
        saved = await self.store.get_run(identifier)
        await self._receipt(
            identifier,
            checkpoint.get("idempotency_key"),
            {"source_tree": tree, "checkpoint_revision": saved["checkpoint_revision"]},
            checkpoint={"source_tree": tree, "checkpoint_revision": saved["checkpoint_revision"]},
        )
        (await self.store.finish(identifier, "succeeded"))

    async def _snapshot(
        self,
        workspace: Path,
        base: str,
        *,
        allow_unchanged: bool = False,
        secrets: dict[str, str] | None = None,
        expected_head: str | None = None,
    ) -> tuple[list[str], str, str]:
        await checked(["git", "add", "--all", "--", "."], workspace)
        inventory = await checked(["git", "diff", "--name-only", "-z", base, "--"], workspace)
        files = [path for path in inventory.split("\x00") if path]
        if not files and not allow_unchanged:
            raise ValueError("Worker produced no source changes")
        if any(private_path(path) for path in files):
            raise ValueError("Generated changes contain private credentials or worker metadata; inspect the workspace")
        if await checked(["git", "rev-parse", "HEAD"], workspace) != (expected_head or base):
            raise ValueError("Worker changed Git history; source must remain on the isolated baseline")
        patch = await execute(["git", "diff", "--binary", "--no-ext-diff", "--no-color", base, "--"], workspace)
        if patch.exit_code:
            raise ValueError("Cannot create source patch")
        diff = patch.output
        if redact_secrets(redact(diff), secrets or {}) != diff:
            raise ValueError("Generated source contains a configured credential; remove it before delivery")
        tree = await checked(["git", "write-tree"], workspace)
        return files, diff, tree

    async def validated_workspace(self, identifier: str) -> Path:
        await self.assert_ownership()
        run = await self.store.get_run(identifier)
        if (
            run["status"] != "succeeded"
            or not run.get("final_verification")
            or any(check["exit_code"] != 0 for check in run["final_verification"])
        ):
            raise ValueError("Only a successful run with passing verification can be deployed or published")
        if not run.get("base_revision") or not run.get("source_tree"):
            raise ValueError("Verified source baseline is unavailable")
        workspace = self.workspaces / identifier
        settings = self.store.configuration.load_snapshot(run["config_revision"]) if run.get("config_revision") else {}
        _, _, tree = await self._snapshot(
            workspace,
            run["base_revision"],
            allow_unchanged=run.get("operation") == "test",
            secrets=settings,
            expected_head=run.get("checkpoint_revision"),
        )
        if tree != run["source_tree"]:
            raise ValueError("Source changed since verification; create a fresh run before deployment or publication")
        return workspace

    async def _artifact(self, identifier: str, workspace: Path, files: list[str], patch: str) -> None:
        target = self.workspaces / f"{identifier}.zip"
        total = 0
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("forge-changes.patch", patch)
            for filename in files:
                path = workspace / filename
                if path.is_symlink() or not path.is_file() or private_path(filename):
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(workspace.resolve()):
                    raise ValueError("Artifact escaped the run workspace")
                total += path.stat().st_size
                if total > 50_000_000:
                    raise ValueError("Changed-file artifact exceeds 50 MB")
                archive.write(path, filename)
        (
            await self.store.update(
                identifier,
                artifacts=[
                    {
                        "name": "changes.zip",
                        "size": target.stat().st_size,
                        "url": f"/api/v1/runs/{identifier}/artifacts/changes.zip",
                    }
                ],
            )
        )

    async def publish(self, identifier: str) -> str:
        async with self.publication_lock:
            run = await self.store.get_run(identifier)
            if (
                run["status"] != "succeeded"
                or not run.get("final_verification")
                or any(check["exit_code"] != 0 for check in run["final_verification"])
            ):
                raise ValueError("Only a successful run with passing verification can be published")
            if run["pr_url"]:
                return str(run["pr_url"])
            if run.get("publication_status") == "publishing":
                raise ValueError(
                    "Publication is in progress or has an ambiguous outcome; inspect the remote before retrying"
                )
            repo = await self.store.repository(run["repository_id"])
            if run.get("operation") == "test":
                raise ValueError("Test operations verify existing source and have no implementation branch to publish")
            if repo["provider"] == "git":
                raise ValueError("Choose a source-control provider to create a pull request")
            authentication = None
            git_env = None
            if self.connections and (repo.get("connection_provider") or self.connections.configured(repo["provider"])):
                authentication = await self.connections.authentication(repo)
                git_env = await self.connections.git_environment(repo)
            if not authentication and not os.environ.get(f"FORGE_{repo['provider'].upper()}_TOKEN"):
                raise ValueError(f"Configure FORGE_{repo['provider'].upper()}_TOKEN before publishing")
            workspace = await self.validated_workspace(identifier)
            publication_key = None
            if self.journal:
                publication = await self.journal.intent(
                    identifier, "publish", payload={"branch": run["branch"], "source_tree": run["source_tree"]}
                )
                if publication["disposition"] != "execute":
                    raise ReconciliationRequired(
                        "Publication intent exists without a reconciled outcome; inspect the remote before retrying"
                    )
                publication_key = publication["idempotency_key"]
            (await self.store.update(identifier, publication_status="publishing"))
            if not run.get("checkpoint_revision"):
                await self._checkpoint_commit(identifier, workspace)
            revision = (await self.store.get_run(identifier))["checkpoint_revision"]
            await self.assert_ownership()
            await push(workspace, run["branch"], env=git_env, repository_url=repo["url"], revision=revision)
            body = (
                "Implemented by firefly forge.\n\n"
                + run["summary"]
                + "\n\nVerification:\n"
                + "\n".join(f"- {' '.join(check['argv'])}: passed" for check in run["final_verification"])
            )
            async with httpx.AsyncClient(follow_redirects=False) as client:
                await self.assert_ownership()
                url = await PullRequests(client).create(
                    repo, run["branch"], run["brief"][:150], body, authentication=authentication
                )
            (await self.store.update(identifier, pr_url=url, publication_status="published"))
            await self._receipt(
                identifier, publication_key, {"pr_url": url, "branch": run["branch"], "source_tree": run["source_tree"]}
            )
            (await self.store.event(identifier, "publication", f"Pull request created: {url}"))
            return url
