# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable software production lifecycle on isolated Git clones."""

from __future__ import annotations

import asyncio
import json
import os
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock, Timeout

from firefly_forge.models import Command
from firefly_forge.process import checked, execute
from firefly_forge.scm import PullRequests, clone
from firefly_forge.store import TERMINAL, Store, now


def redact(value: str) -> str:
    for name, secret in os.environ.items():
        if len(secret) >= 8 and any(part in name.upper() for part in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")):
            value = value.replace(secret, "[redacted]")
    return re.sub(r"(?i)(authorization: ?bearer )[\w.\-]+", r"\1[redacted]", value)


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


class Engine:
    def __init__(
        self, store: Store, workspaces: Path, provider_factory: Callable[[str], Any] | None = None, concurrency: int = 2
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.workspaces.mkdir(parents=True, exist_ok=True)
        if provider_factory is None:
            from firefly_forge.providers import get_provider

            provider_factory = get_provider
        self.provider_factory = provider_factory
        self.concurrency = concurrency
        self.active: dict[str, asyncio.Task[None]] = {}
        self.poller: asyncio.Task[None] | None = None
        self.stopping = False
        self.publication_lock = asyncio.Lock()
        self.owner_lock = FileLock(str(store.path.with_suffix(".lock")))

    async def start(self) -> None:
        if self.poller is not None and not self.poller.done():
            return
        try:
            self.owner_lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError("A factory server is already running against this data directory") from exc
        self.store.recover()
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
        self.owner_lock.release()

    async def _poll(self) -> None:
        while True:
            self.active = {identifier: task for identifier, task in self.active.items() if not task.done()}
            for identifier in self.store.queued()[: max(0, self.concurrency - len(self.active))]:
                if identifier not in self.active:
                    self.active[identifier] = asyncio.create_task(self.run(identifier))
            await asyncio.sleep(0.2)

    async def cancel(self, identifier: str) -> None:
        current = self.store.get_run(identifier)
        if current["status"] in TERMINAL:
            return
        self.store.finish(identifier, "cancelled")
        task = self.active.get(identifier)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(self, identifier: str) -> None:
        if not self.store.claim(identifier):
            return
        workspace = self.workspaces / identifier

        async def emit(stage: str, message: str) -> None:
            self.store.event(identifier, stage, redact(message))

        try:
            run = self.store.get_run(identifier)
            repository = self.store.repository(run["repository_id"])
            branch = f"forge/{identifier[:12]}"
            await emit("preparing", "Creating isolated repository workspace")
            base = await clone(repository, workspace, branch)
            exclusions = workspace / ".git" / "info" / "exclude"
            existing = exclusions.read_text() if exclusions.exists() else ""
            exclusions.write_text(
                existing + "\n.venv/\nnode_modules/\nvendor/\n__pycache__/\n*.pyc\n"
                "dist/\nbuild/\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n"
            )
            self.store.update(identifier, base_revision=base, branch=branch)
            provider = self.provider_factory(run["agent"])
            brief = run["brief"]
            repairs = max(0, min(3, int(os.environ.get("FORGE_REPAIR_ATTEMPTS", "2"))))
            for attempt in range(1, repairs + 2):
                self.store.update(identifier, status="developing", attempt=attempt)
                result = await provider.execute(workspace, brief, run["backend"], run["frontend"], emit)
                self.store.update(identifier, summary=redact(result.summary), usage=getattr(result, "usage", {}))
                files, diff, tree = await self._snapshot(workspace, base)
                self.store.update(identifier, changed_files=files, diff=redact(diff[:1_000_000]), status="validating")
                commands = [Command.model_validate(command) for command in run["commands"]] or default_commands(
                    workspace
                )
                final: list[dict[str, Any]] = []
                for command in commands:
                    await emit("validating", f"Attempt {attempt}: {' '.join(command.argv)}")
                    check = await execute(command.argv, workspace, command.timeout)
                    evidence = {
                        "argv": command.argv,
                        "exit_code": check.exit_code,
                        "output": redact(check.output[-32000:]),
                        "created_at": now(),
                        "attempt": attempt,
                    }
                    final.append(evidence)
                    evidence_list = self.store.get_run(identifier)["verification"]
                    evidence_list.append(evidence)
                    self.store.update(identifier, verification=evidence_list)
                failures = [check for check in final if check["exit_code"] != 0]
                final_files, final_diff, final_tree = await self._snapshot(workspace, base)
                if final_tree != tree:
                    failures.append(
                        {
                            "argv": ["source-snapshot"],
                            "exit_code": 1,
                            "output": "Verification modified the source tree; validate the final source snapshot",
                        }
                    )
                if not failures:
                    files = final_files
                    self.store.update(
                        identifier,
                        final_verification=final,
                        source_tree=final_tree,
                        changed_files=files,
                        diff=redact(final_diff[:1_000_000]),
                    )
                    break
                if attempt > repairs:
                    raise ValueError(f"Verification failed after {attempt} implementation attempts")
                feedback = "\n\n".join(
                    f"{' '.join(check['argv'])}: exit {check['exit_code']}\n{check['output']}" for check in failures
                )
                await emit("repairing", "Verification failed; returning all failed checks to the coding worker")
                brief = (
                    run["brief"]
                    + "\n\nVerification failed. Repair the existing implementation without weakening "
                    + ("checks or replacing tests with no-ops. Run these exact checks successfully:\n" + feedback)
                )
            self._artifact(identifier, workspace, files, final_diff)
            self.store.finish(identifier, "succeeded")
            if run["publish"]:
                await self.publish(identifier)
        except asyncio.CancelledError:
            self.store.finish(identifier, "interrupted" if self.stopping else "cancelled")
            raise
        except Exception as exc:
            self.store.finish(identifier, "failed", error=redact(str(exc)))
            if self.store.get_run(identifier)["status"] == "succeeded":
                self.store.update(identifier, publication_error=redact(str(exc)))
                await emit("publication", f"Publication failed: {exc}")

    async def _snapshot(self, workspace: Path, base: str) -> tuple[list[str], str, str]:
        await checked(["git", "add", "--all", "--", "."], workspace)
        inventory = await checked(["git", "diff", "--name-only", "-z", base, "--"], workspace)
        files = [path for path in inventory.split("\x00") if path]
        if not files:
            raise ValueError("Worker produced no source changes")
        if any(private_path(path) for path in files):
            raise ValueError("Generated changes contain private credentials or worker metadata; inspect the workspace")
        if await checked(["git", "rev-parse", "HEAD"], workspace) != base:
            raise ValueError("Worker changed Git history; source must remain on the isolated baseline")
        patch = await execute(["git", "diff", "--binary", "--no-ext-diff", "--no-color", base, "--"], workspace)
        if patch.exit_code:
            raise ValueError("Cannot create source patch")
        diff = patch.output
        if redact(diff) != diff:
            raise ValueError("Generated source contains a configured credential; remove it before delivery")
        tree = await checked(["git", "write-tree"], workspace)
        return files, diff, tree

    def _artifact(self, identifier: str, workspace: Path, files: list[str], patch: str) -> None:
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
        self.store.update(
            identifier,
            artifacts=[
                {
                    "name": "changes.zip",
                    "size": target.stat().st_size,
                    "url": f"/api/v1/runs/{identifier}/artifacts/changes.zip",
                }
            ],
        )

    async def publish(self, identifier: str) -> str:
        async with self.publication_lock:
            run = self.store.get_run(identifier)
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
            repo = self.store.repository(run["repository_id"])
            if repo["provider"] == "git":
                raise ValueError("Choose a source-control provider to create a pull request")
            if not os.environ.get(f"FORGE_{repo['provider'].upper()}_TOKEN"):
                raise ValueError(f"Configure FORGE_{repo['provider'].upper()}_TOKEN before publishing")
            workspace = self.workspaces / identifier
            _, _, tree = await self._snapshot(workspace, run["base_revision"])
            if tree != run.get("source_tree"):
                raise ValueError("Source changed since verification; create a fresh run before publishing")
            self.store.update(identifier, publication_status="publishing")
            await checked(
                [
                    "git",
                    "-c",
                    "user.name=Firefly Forge",
                    "-c",
                    "user.email=forge@getfirefly.io",
                    "commit",
                    "-m",
                    f"Implement verified product: {run['brief'][:100]}",
                ],
                workspace,
            )
            await checked(["git", "push", "origin", f"HEAD:refs/heads/{run['branch']}"], workspace)
            body = (
                "Implemented by Firefly Forge.\n\n"
                + run["summary"]
                + "\n\nVerification:\n"
                + "\n".join(f"- {' '.join(check['argv'])}: passed" for check in run["final_verification"])
            )
            async with httpx.AsyncClient(follow_redirects=False) as client:
                url = await PullRequests(client).create(repo, run["branch"], run["brief"][:150], body)
            self.store.update(identifier, pr_url=url, publication_status="published")
            self.store.event(identifier, "publication", f"Pull request created: {url}")
            return url
