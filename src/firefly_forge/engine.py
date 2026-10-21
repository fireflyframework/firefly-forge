# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable software production lifecycle on isolated Git clones."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path
from typing import Any, cast

import httpx
from filelock import FileLock, Timeout
from pyfly.container import service
from pyfly.kernel.lifecycle import CONSUMER_PHASE

from firefly_forge.configuration import redact_secrets
from firefly_forge.diagnostic_recipes import (
    PythonProfileRequest,
    PythonTraceRequest,
    build_profile_argv,
    build_trace_argv,
    profile_available,
    profile_configuration,
    profile_observation,
    trace_observation,
)
from firefly_forge.git_workspace import git_environment, metadata_digest, trusted_git
from firefly_forge.models import Command
from firefly_forge.php_diagnostics import PHPProfileRequest, build_php_profile_argv, php_profile_observation
from firefly_forge.php_diagnostics import profile_configuration as php_profile_configuration
from firefly_forge.policies import inject_policy_snapshot, policy_commands
from firefly_forge.process import checked as process_checked
from firefly_forge.process import execute as process_execute
from firefly_forge.prompts import prompt_text
from firefly_forge.qa import BrowserJourney, EvidenceImage, QAReference
from firefly_forge.scm import PullRequests, clone, push, source_revision
from firefly_forge.secrets import contains_known_credential, redact_environment
from firefly_forge.skills import DEFAULT_SKILLS, inject_skills
from firefly_forge.store import TERMINAL, ExecutionOwnerBusy, Store, now

RETAINED_EXECUTION_FENCES: set[FileLock] = set()


async def checked(argv: list[str], cwd: Path, timeout: int = 300, *, env: dict[str, str] | None = None) -> str:
    return await process_checked(trusted_git(argv), cwd, timeout, env=git_environment(env) if argv[0] == "git" else env)


async def execute(argv: list[str], cwd: Path, timeout: int = 300, *, env: dict[str, str] | None = None) -> Any:
    return await process_execute(trusted_git(argv), cwd, timeout, env=git_environment(env) if argv[0] == "git" else env)


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
    if (workspace / "package.json").is_file():
        from firefly_forge.profiles import verification_commands

        return [Command(argv=argv, timeout=600) for argv in verification_commands(workspace, "none", "react")]
    raise ValueError("No verification command found; supply commands or add an executable make check target")


def private_path(path: str) -> bool:
    parts = Path(path).parts
    return any(
        part in {".git", ".codex", ".claude", ".forge", "node_modules", ".venv", "vendor"} for part in parts
    ) or credential_path(path)


def credential_path(path: str) -> bool:
    return any(
        part in {".env", ".npmrc", ".pypirc", ".ssh", "credentials.json", "id_rsa", "id_ed25519"}
        or (part.startswith(".env.") and part not in {".env.example", ".env.sample", ".env.template"})
        or part.endswith((".pem", ".key"))
        for part in Path(path).parts
    )


class ReconciliationRequired(RuntimeError):
    pass


class WorkerAttention(RuntimeError):
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
        if not 1 <= concurrency <= 64:
            raise ValueError("Execution concurrency must be between 1 and 64")
        self.store = store
        self.connections = connections
        self.journal = journal
        self.knowledge: Any = None
        self.assurance: Any = None
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
        self.execution_state = "stopped"
        self.consumer_tasks: Callable[[], list[asyncio.Task[None]]] = list
        self.shutdown_tasks: set[asyncio.Task[None]] = set()
        self.publication_lock = asyncio.Lock()
        self.owner_lock = FileLock(str(store.path.with_suffix(".lock")))
        self.credential_registry: Callable[[], dict[str, str]] = dict
        self.component_roots: dict[Path, str] = {}
        self.sandbox: Any = None
        self.browser: Any = None
        self.search: Any = None
        self.tool_broker: Any = None
        self.verification_handoffs: Any = None
        self.provider_recoveries: Any = None
        self.coding_checks: Any = None
        self.external_mcp: Any = None
        self.learning: Any = None
        self.knowledge_first: Any = None
        self.git_metadata: dict[Path, str] = {}

    async def start(self) -> None:
        if self.store.runtime_roles and not self.store.runtime_roles.executes_jobs:
            return
        if self.store.runtime_roles:
            self.store.runtime_roles.require_execution()
        if self.poller is not None and not self.poller.done():
            return
        if any(not task.done() for task in self.shutdown_tasks | set(self.consumer_tasks())):
            raise RuntimeError("Execution consumers must finish draining before restarting this owner")
        standby = bool(self.store.runtime_roles and self.store.runtime_roles.role == "worker")
        self.stopping = False
        self.enforce_ownership = True
        self.execution_state = "standby" if standby else "stopped"
        try:
            while not self.stopping:
                try:
                    self.owner_lock.acquire(timeout=0)
                except Timeout as exc:
                    if not standby:
                        raise RuntimeError("A factory server is already running against this data directory") from exc
                    await asyncio.sleep(0.5)
                    continue
                try:
                    await self.store.acquire_ownership()
                except ExecutionOwnerBusy:
                    self.owner_lock.release()
                    if not standby:
                        raise
                    await asyncio.sleep(0.5)
                    continue
                except BaseException:
                    self.owner_lock.release()
                    raise
                if self.stopping:
                    try:
                        await self.store.release_ownership()
                    finally:
                        self.owner_lock.release()
                        self.execution_state = "stopped"
                    return
                self.execution_state = "recovering"
                self.store.execution_stopping = False
                if self.journal:
                    await self.assert_ownership()
                    await self.journal.recover()
                if self.sandbox:
                    await self.assert_ownership()
                    await self.sandbox.recover()
                await self.assert_ownership()
                await self.store.recover()
                await self.assert_ownership()
                self.poller = asyncio.create_task(self._poll())
                self.execution_state = "ready"
                return
        except BaseException:
            self.execution_state = "stopped"
            if self.enforce_ownership:
                try:
                    await self.store.release_ownership()
                finally:
                    self.owner_lock.release()
            raise

    def prepare_shutdown(self) -> None:
        self.stopping = True
        self.store.execution_stopping = True
        self.execution_state = "stopped"
        self.shutdown_tasks.update(self.consumer_tasks())
        self.shutdown_tasks.update(self.active.values())
        if self.poller:
            self.shutdown_tasks.add(self.poller)
        if self.owner_lock.is_locked:
            RETAINED_EXECUTION_FENCES.add(self.owner_lock)

    async def stop(self) -> None:
        self.prepare_shutdown()
        if self.poller:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)
        tasks = list(self.active.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        self.shutdown_tasks.update(self.consumer_tasks())
        if any(not task.done() for task in self.shutdown_tasks):
            self.ownership_failure = "Execution consumers have not drained; storage ownership remains fenced"
            self.execution_state = "degraded"
            return
        if self.enforce_ownership:
            try:
                await self.store.release_ownership()
            finally:
                self.owner_lock.release()
                RETAINED_EXECUTION_FENCES.discard(self.owner_lock)

    async def assert_ownership(self) -> None:
        if self.store.runtime_roles:
            self.store.runtime_roles.require_execution()
        if self.enforce_ownership:
            await self.store.assert_ownership()

    async def resolve_campaign_source(self, repository: dict[str, Any]) -> str:
        if not self.store.runtime_roles or self.store.runtime_roles.executes_jobs:
            await self.assert_ownership()
        environment = None
        if self.connections and (
            repository.get("connection_provider") or self.connections.configured(repository["provider"])
        ):
            environment = await self.connections.git_environment(repository)
        return await source_revision(repository, env=environment)

    async def _poll(self) -> None:
        while True:
            try:
                await self.assert_ownership()
            except RuntimeError:
                self.ownership_failure = "Execution ownership was lost; restart after inspecting interrupted work"
                self.execution_state = "degraded"
                self.stopping = True
                self.store.execution_stopping = True
                for task in self.active.values():
                    task.cancel()
                # Another consumer can trigger native shutdown during this drain.
                # Cancelling the poller must not cancel effect cleanup a second time.
                await asyncio.shield(asyncio.gather(*self.active.values(), return_exceptions=True))
                # Native shutdown drains every higher-phase effect consumer before Engine.stop releases storage.
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
        await self.assert_ownership()
        self.active = {identifier: task for identifier, task in self.active.items() if not task.done()}
        self.active_projects = {
            identifier: project for identifier, project in self.active_projects.items() if identifier in self.active
        }
        available = max(0, self.concurrency - len(self.active))
        if not available:
            return
        occupied = set(self.active_projects.values())
        eligible: dict[str, tuple[str, bool]] = {}
        verification = await self.verification_handoffs.ready() if self.verification_handoffs else []
        for identifier in [*verification, *await self.store.queued()]:
            if identifier in self.active:
                continue
            run = await self.store.get_run(identifier)
            project = run["repository_id"]
            if project not in occupied:
                eligible.setdefault(project, (identifier, identifier in verification))
        projects = list(eligible)
        if self.last_project in projects:
            pivot = projects.index(self.last_project) + 1
            projects = projects[pivot:] + projects[:pivot]
        for project in projects[:available]:
            identifier, handoff = eligible[project]
            self.active_projects[identifier] = project
            action = self.verification_handoffs.execute(identifier) if handoff else self.execute_run(identifier)
            self.active[identifier] = asyncio.create_task(action)
            self.last_project = project

    async def cancel(self, identifier: str) -> None:
        if self.store.runtime_roles and self.store.runtime_roles.role == "api":
            await self.store.execution_requests.request_cancel(identifier)
            return
        await self.cancel_effect(identifier)

    async def cancel_effect(self, identifier: str) -> dict[str, Any]:
        await self.assert_ownership()
        current = await self.store.get_run(identifier)
        if current["status"] in TERMINAL:
            return {"no_op": True, "terminal_status": current["status"]}
        task = self.active.get(identifier)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.store.finish(identifier, "cancelled")
        return {"status": "cancelled", "worker_drained": True}

    async def restore_capacity(self, identifier: str) -> bool:
        run = await self.store.get_run(identifier)
        return (
            not self.stopping
            and identifier not in self.active
            and len([task for task in self.active.values() if not task.done()]) < self.concurrency
            and run["repository_id"] not in self.active_projects.values()
        )

    async def reserve_agent_restore(self, identifier: str) -> bool:
        run = await self.store.get_run(identifier)
        if not await self.restore_capacity(identifier):
            return False
        task = asyncio.current_task()
        assert task is not None
        self.active[identifier] = task
        self.active_projects[identifier] = run["repository_id"]
        return True

    def release_agent_restore(self, identifier: str) -> None:
        if self.active.get(identifier) is asyncio.current_task():
            self.active.pop(identifier, None)
            self.active_projects.pop(identifier, None)

    async def continue_agent_checkpoint(self, restoration: Any) -> None:
        identifier = restoration.run_id
        if self.active.get(identifier) is not asyncio.current_task():
            raise ValueError("The checkpoint worker does not own its reserved execution workspace")
        await restoration.guard_source()
        await self.execute_run(identifier, restoration=restoration)

    async def execute_run(self, identifier: str, *, restoration: Any = None) -> None:
        await self.assert_ownership()
        if restoration is None:
            if not (await self.store.claim(identifier)):
                return
        else:
            await restoration.guard_source()
        workspace = self.workspaces / identifier
        settings: dict[str, str] = {}
        active_key: str | None = None
        credential_scopes = ExitStack()

        def safe(message: str) -> str:
            return redact_secrets(redact_secrets(redact(message), self.credential_registry()), settings)

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

        async def retain_unknown(message: str, observation: dict[str, Any]) -> None:
            if self.journal and active_key:
                await self.journal.fail(
                    identifier,
                    active_key,
                    "provider_outcome_unknown",
                    safe(message),
                    observation=safe_data(observation),
                )
            else:
                current_pause = (await self.store.get_run(identifier)).get("pause", {})
                await self.store.update(
                    identifier,
                    status="needs_reconciliation",
                    error=safe(message),
                    pause={
                        **current_pause,
                        "kind": "provider_outcome_unknown",
                        "automatic": False,
                        "retry_at": None,
                        "provider_observation": safe_data(observation),
                    },
                )
            await emit("needs_reconciliation", "Bridge outcome requires reconciliation; no automatic resubmission")

        try:
            run = await self.store.get_run(identifier)
            from firefly_forge.project_access import ProjectAccess

            await ProjectAccess(self.store).authorize_execution(run)
            recovery = await self.provider_recoveries.begin(identifier) if self.provider_recoveries else None
            if recovery is not None and restoration is not None:
                raise ReconciliationRequired("Provider recovery cannot replace a checkpoint restoration")
            if self.store.runtime_roles and self.store.runners:
                from firefly_forge.agent_roles import stage_allowed_tools

                frozen_tools = (
                    stage_allowed_tools(
                        run.get("role_snapshot", {}), "implement", "agentic" if run["agent"] == "api" else run["agent"]
                    )
                    or set()
                )
                worker_tools = await self.store.runners.capabilities(run, run["agent"])
                available = {tool["id"] for tool in worker_tools if tool["availability"] == "available"}
                if not frozen_tools <= available:
                    raise ValueError("The worker cannot attest every frozen tool grant; execution is blocked")
                if (
                    run.get("sandbox_snapshot", {}).get("configuration", {}).get("mode") in {"docker", "pool"}
                    and not (await self.sandbox.probe(run["sandbox_snapshot"]))["available"]
                ):
                    raise ValueError("The frozen Docker runner is unavailable on the execution worker")
                await self.store.event(
                    identifier, "runner_attestation", "Execution worker attested frozen runner and tool grants"
                )
            if run.get("component_snapshot"):
                self.component_roots[workspace.resolve()] = run["component_snapshot"]["path"]
            if run.get("model_selection"):
                if self.store.task_router is None or run["agent"] != run["model_selection"]["provider"]:
                    raise ValueError("The frozen implementation model profile is unavailable or inconsistent")
                settings = self.store.task_router.profiles.validate_snapshot(run["model_selection"])
                if run["config_revision"] != run["model_selection"]["config_revision"]:
                    raise ValueError("The implementation configuration differs from its frozen model selection")
            elif run.get("config_revision"):
                settings = self.store.configuration.load_snapshot(run["config_revision"])
            if "repair_budget" not in run:
                budget = max(0, min(3, int(os.environ.get("FORGE_REPAIR_ATTEMPTS", "2"))))
                await self.store.update(identifier, repair_budget=budget)
                run = await self.store.get_run(identifier)
            if run.get("review_config_revision"):
                review_settings = self.store.configuration.load_snapshot(run["review_config_revision"])
                settings.update({"REVIEW_" + key: value for key, value in review_settings.items()})
            if self.knowledge:
                settings.update(self.knowledge.credentials(run))
            repository = await self.store.repository(run["repository_id"])
            if self.connections:
                self.connections.assert_snapshot(repository, run.get("scm_snapshot"))
            git_env = None
            if self.connections and (
                repository.get("connection_provider") or self.connections.configured(repository["provider"])
            ):
                git_env = await self.connections.git_environment(repository)
                settings.update(git_env)
            if restoration is not None:
                base = run["base_revision"]
                self.git_metadata[workspace.resolve()] = run["git_metadata_digest"]
                active_key = restoration.intent["payload"]["task_key"]
            else:
                branch = f"forge/{identifier[:12]}"
                prepare = await self._begin_task(identifier, "prepare", workspace, settings)
                active_key = prepare.get("idempotency_key")
                if run.get("resume_existing") or prepare["disposition"] == "completed":
                    if recovery is not None:
                        observed = await self.provider_recoveries.prepare_workspace(
                            identifier, lambda: self._inspect_workspace(identifier, workspace, settings)
                        )
                    else:
                        observed = await self._inspect_workspace(identifier, workspace, settings)
                    base = observed["base_revision"]
                    run = await self.store.get_run(identifier)
                    await emit("preparing", "Continuing the retained authorized workspace")
                else:
                    await emit("preparing", "Creating isolated repository workspace")
                    base = await clone(
                        repository,
                        workspace,
                        branch,
                        env=git_env,
                        revision=run.get("campaign_context", {}).get("source_base"),
                    )
                if not run.get("git_metadata_digest"):
                    exclusions = workspace / ".git" / "info" / "exclude"
                    existing = exclusions.read_text() if exclusions.exists() else ""
                    exclusions.write_text(
                        existing + "\n.venv/\nnode_modules/\nvendor/\n__pycache__/\n*.pyc\n"
                        "dist/\nbuild/\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n"
                    )
                    baseline_metadata = metadata_digest(workspace)
                    self.git_metadata[workspace.resolve()] = baseline_metadata
                    await self.store.update(identifier, git_metadata_digest=baseline_metadata)
                (await self.store.update(identifier, base_revision=base, branch=branch))
                tracked = await checked(["git", "ls-files", "-z"], workspace)
                if any(credential_path(path) for path in tracked.split("\x00") if path):
                    raise ValueError(
                        "Repository contains tracked private credential files; remove them before execution"
                    )
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
            if run["agent"] in {"agentic", "bridge", "api"} and hasattr(provider, "configure_runtime"):
                from firefly_forge.agent_roles import stage_allowed_tools
                from firefly_forge.qa import JourneyEvidence

                scope = self.component_directory(run, workspace, create=True)
                grants = (
                    stage_allowed_tools(
                        run["role_snapshot"], "implement", "agentic" if run["agent"] == "api" else run["agent"]
                    )
                    if run.get("role_snapshot")
                    else set()
                )

                async def command_executor(argv: list[str], timeout: int) -> Any:
                    return await self._project_command(identifier, run, workspace, argv, timeout, "implement")

                async def python_profile(request: PythonProfileRequest) -> dict[str, Any]:
                    return await self._profile_python(identifier, run, workspace, request)

                async def python_trace(request: PythonTraceRequest) -> dict[str, Any]:
                    return await self._trace_python(identifier, run, workspace, request)

                async def php_profile(request: PHPProfileRequest) -> dict[str, Any]:
                    return await self._profile_php(identifier, run, workspace, request)

                async def browser_runner(path: str, width: int, height: int, theme: str) -> dict[str, Any]:
                    return cast(
                        dict[str, Any],
                        await self.browser.check(
                            identifier, scope, task_id="implement", path=path, width=width, height=height, theme=theme
                        ),
                    )

                async def browser_journey(plan: BrowserJourney) -> JourneyEvidence:
                    return cast(
                        JourneyEvidence, await self.browser.journey(identifier, scope, plan, task_id="implement")
                    )

                async def reference_resolver(plan: BrowserJourney) -> tuple[QAReference, EvidenceImage] | None:
                    return await self._resolve_qa_reference(run, scope, plan)

                async def internet_search(
                    query: str, count: int, refresh: bool = False, refresh_request_id: str | None = None
                ) -> dict[str, Any]:
                    if self.tool_broker is None:
                        raise ValueError("A durable tool broker is required for agent internet search")
                    source = await self._inspect_workspace(identifier, workspace, settings)
                    if self.knowledge_first and (run.get("learning_snapshot") or {}).get("settings", {}).get("enabled"):
                        return cast(
                            dict[str, Any],
                            await self.knowledge_first.search(
                                identifier,
                                "implement",
                                query,
                                count,
                                source=source,
                                workspace=workspace,
                                refresh=refresh,
                                refresh_request_id=refresh_request_id,
                            ),
                        )
                    if refresh or refresh_request_id is not None:
                        raise ValueError("This run has no captured knowledge-first refresh permission")

                    async def request() -> dict[str, Any]:
                        return cast(
                            dict[str, Any], await self.search.search(query, count, snapshot=run["search_snapshot"])
                        )

                    return cast(
                        dict[str, Any],
                        await self.tool_broker.invoke(
                            identifier,
                            "implement",
                            "internet_search",
                            uuid.uuid4().hex,
                            {"query": query, "max_results": count},
                            request,
                            source=source,
                            configuration=run["search_snapshot"],
                        ),
                    )

                async def knowledge_lookup(query: str, refresh: bool, refresh_request_id: str | None) -> dict[str, Any]:
                    source = await self._inspect_workspace(identifier, workspace, settings)
                    return cast(
                        dict[str, Any],
                        await self.knowledge_first.search(
                            identifier,
                            "implement",
                            query,
                            5,
                            source=source,
                            workspace=workspace,
                            allow_external=False,
                            refresh=refresh,
                            refresh_request_id=refresh_request_id,
                        ),
                    )

                async def external_mcp(connector: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    if self.tool_broker is None or self.external_mcp is None:
                        raise ValueError("A durable registered tool broker is required for external MCP")
                    from firefly_forge.agent_roles import roles_for_stage

                    roles = [item["id"] for item in roles_for_stage(run["role_snapshot"], "implement")]
                    source = await self._inspect_workspace(identifier, workspace, settings)

                    async def request() -> dict[str, Any]:
                        return cast(
                            dict[str, Any],
                            await self.external_mcp.call(
                                run["mcp_snapshot"],
                                connector,
                                tool,
                                arguments,
                                roles=roles,
                                stage="implement",
                                run_id=identifier,
                            ),
                        )

                    return cast(
                        dict[str, Any],
                        await self.tool_broker.invoke(
                            identifier,
                            "implement",
                            f"external_mcp:{connector}:{tool}",
                            uuid.uuid4().hex,
                            {"connector": connector, "tool": tool, "arguments": arguments},
                            request,
                            source=source,
                            configuration=run["mcp_snapshot"],
                        ),
                    )

                async def read_run_evidence(query: dict[str, Any]) -> dict[str, Any]:
                    from firefly_forge.run_evidence import EvidenceQuery, RunEvidence

                    return await RunEvidence(self.store).for_worker(
                        identifier, EvidenceQuery.model_validate(query), task_id="implement"
                    )

                provider.configure_runtime(
                    scope_root=scope,
                    read_run_evidence=read_run_evidence if "read_run_evidence" in (grants or ()) else None,
                    repository_root=workspace,
                    allowed_tools=frozenset(grants or ()),
                    knowledge_lookup=knowledge_lookup
                    if self.knowledge_first and "knowledge_lookup" in (grants or ())
                    else None,
                    internet_search_refresh=internet_search
                    if self.knowledge_first and "internet_search" in (grants or ())
                    else None,
                    external_mcp=external_mcp
                    if self.external_mcp
                    and (run.get("mcp_snapshot") or {}).get("grants")
                    and "external_mcp" in (grants or ())
                    else None,
                    command_executor=command_executor if self.sandbox and "execute_command" in (grants or ()) else None,
                    python_profile=python_profile
                    if self.sandbox
                    and {"execute_command", "profile_python"}.issubset(grants or ())
                    and profile_available(run.get("sandbox_snapshot"))
                    else None,
                    python_trace=python_trace
                    if self.sandbox
                    and {"execute_command", "trace_python"}.issubset(grants or ())
                    and profile_available(run.get("sandbox_snapshot"))
                    else None,
                    php_profile=php_profile
                    if self.sandbox
                    and {"execute_command", "profile_php"}.issubset(grants or ())
                    and profile_available(run.get("sandbox_snapshot"))
                    else None,
                    browser_runner=browser_runner if self.browser and "browser_check" in (grants or ()) else None,
                    browser_journey=browser_journey if self.browser and "browser_journey" in (grants or ()) else None,
                    reference_resolver=reference_resolver
                    if self.browser and run.get("design_snapshot") and "browser_journey" in (grants or ())
                    else None,
                    qa_references=tuple(self._qa_reference_descriptors(run)),
                    image_capability=await provider.image_input_capability()
                    if "browser_journey" in (grants or ()) and hasattr(provider, "image_input_capability")
                    else {"status": "unverified"},
                    credential_registry=self.credential_registry,
                    credential_admission=self.store.admit_credentials,
                    internet_search=internet_search
                    if self.search and run.get("search_snapshot") and "internet_search" in (grants or ())
                    else None,
                )
            if restoration is not None:
                source_brief = restoration.binding.brief
            else:
                source_brief = (
                    inject_policy_snapshot(run["brief"], run["policy_snapshot"])
                    if run.get("policy_snapshot")
                    else inject_skills(run["brief"], run.get("skills", DEFAULT_SKILLS))
                )
                if run.get("role_snapshot"):
                    from firefly_forge.agent_roles import (
                        NativeAllowanceWorkerFeedback,
                        WorkerFeedback,
                        inject_role_snapshot,
                    )

                    source_brief = inject_role_snapshot(source_brief, run["role_snapshot"])
                    feedback_schema = WorkerFeedback.model_json_schema()
                    if recovery is not None and recovery["attempt"] == 5:
                        link = await self.provider_recoveries._allowance(run)
                        if not link:
                            raise ValueError("Final feedback schema requires a sealed native allowance")
                        feedback_schema = NativeAllowanceWorkerFeedback.model_json_schema()
                        for name, value in link.items():
                            feedback_schema["properties"][name]["const"] = value
                    source_brief += prompt_text("implementation/worker-feedback", schema=json.dumps(feedback_schema))
                if (run.get("mcp_snapshot") or {}).get("grants"):
                    source_brief += prompt_text(
                        "implementation/external-tools",
                        schemas=json.dumps(
                            [
                                {key: item[key] for key in ("connector", "tools", "scope", "roles", "stages")}
                                for item in run["mcp_snapshot"]["grants"]
                            ]
                        ),
                    )
                if run.get("worker_response") and run["worker_response"].get("source") != "factory_verification":
                    source_brief += prompt_text(
                        "implementation/operator-response", response=safe(json.dumps(run["worker_response"]))
                    )
                from firefly_forge.verification_models import automatic_verification_grant

                verification_grant = automatic_verification_grant(run.get("tool_policy_snapshot"))
                if (
                    verification_grant is not None
                    or run.get("worker_response", {}).get("source") == "factory_verification"
                ):
                    source_brief += prompt_text(
                        "implementation/automatic-verification",
                        policy=json.dumps(
                            verification_grant.model_dump()
                            if verification_grant
                            else {
                                "legacy_adoption": True,
                                "max_continuations": run.get("verification_handoff", {}).get("max_continuations", 1),
                            }
                        ),
                        response=safe(json.dumps(run.get("worker_response", {})))
                        if run.get("worker_response", {}).get("source") == "factory_verification"
                        else "",
                    )
                if run.get("component_snapshot"):
                    component = run["component_snapshot"]
                    source_brief += prompt_text(
                        "implementation/component",
                        component_id=component["id"],
                        path=component["path"],
                        responsibility=component["responsibility"],
                    )
                if run.get("campaign_context"):
                    source_brief += prompt_text(
                        "implementation/campaign", context=safe(json.dumps(run["campaign_context"], ensure_ascii=False))
                    )
                if run.get("product_definition"):
                    from firefly_forge.collaboration import customer_instruction

                    source_brief += customer_instruction(run["product_definition"])
                if run.get("branding_snapshot"):
                    branding = run["branding_snapshot"]
                    source_brief += prompt_text(
                        "implementation/brand", files=json.dumps(branding["files"], ensure_ascii=False)
                    )
                if run.get("design_snapshot"):
                    if run["design_snapshot"]["method"] == "process_map" or run["frontend"] == "none":
                        source_brief += prompt_text("implementation/design-api")
                    else:
                        source_brief += prompt_text("implementation/design-frontend")
                if run.get("operation") == "maintain":
                    source_brief += prompt_text("implementation/maintenance")
                if run.get("resume_existing"):
                    previous = [item for item in run.get("verification", []) if item["exit_code"] != 0][-5:]
                    source_brief += prompt_text("implementation/resume")
                    if previous:
                        source_brief += prompt_text("implementation/previous-checks", checks=safe(json.dumps(previous)))
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
            if run.get("repair_halt"):
                raise ValueError(run["repair_halt"])
            repairs = int(run["repair_budget"])
            continuation_allowance = (
                await self.journal.verification_continuation_allowance(identifier)
                if self.journal and hasattr(self.journal, "verification_continuation_allowance")
                else 0
            )
            human_allowance = (
                await self.journal.human_clarification_continuation_allowance(identifier)
                if self.journal and hasattr(self.journal, "human_clarification_continuation_allowance")
                else 0
            )
            implementation_limit = min(4, repairs + 1 + continuation_allowance + human_allowance)
            completed_rounds = int(run.get("implementation_rounds", 0))
            repair_failures: list[dict[str, Any]] = []
            start_round = max(1, completed_rounds)
            if self.journal:
                tasks = (await self.journal.plan(identifier))["tasks"]
                implementation_task = next(item for item in tasks if item["id"] == "implement")
                verification_task = next(item for item in tasks if item["id"] == "verify")
                verification_evidence = verification_task.get("evidence", {})
                repair_failures = verification_evidence.get("failed_checks", [])
                verified_round = int(
                    verification_evidence.get(
                        "implementation_round",
                        max((check.get("attempt", 0) for check in verification_evidence.get("checks", [])), default=0),
                    )
                )
                if implementation_task["status"] != "done" or (
                    verification_task["status"] == "done" and repair_failures and verified_round >= completed_rounds
                ):
                    start_round = completed_rounds + 1
            elif run.get("implementation_pending"):
                start_round = completed_rounds + 1
            if restoration is not None:
                start_round = restoration.binding.round
                repair_failures = []
            if recovery is not None:
                start_round = implementation_limit = recovery["attempt"]
                if not 1 <= start_round <= 4 and (
                    start_round != 5
                    or not await self.provider_recoveries._allowance(await self.store.get_run(identifier))
                ):
                    raise ReconciliationRequired("The reserved provider dispatch exceeds its finite bound")
                repair_failures = []
            if start_round > implementation_limit:
                raise ValueError("The frozen implementation attempt budget is exhausted")
            implementation_failures = repair_failures if start_round > completed_rounds else []
            for attempt in range(start_round, implementation_limit + 1):
                restored_receipt = restoration
                if implementation_failures and self.journal:
                    await self._repair_proposal(identifier, workspace, settings, attempt)
                await self.store.admit_credentials(brief)
                (await self.store.update(identifier, status="developing", attempt=attempt))
                implementation = (
                    recovery
                    if recovery is not None
                    else (
                        {"idempotency_key": restoration.intent["payload"]["task_key"], "disposition": "execute"}
                        if restoration is not None
                        else await self._begin_task(
                            identifier, "implement", workspace, settings, implementation_failures
                        )
                    )
                )
                active_key = implementation.get("idempotency_key")
                if implementation["disposition"] != "completed":
                    if completed_rounds >= implementation_limit:
                        raise ValueError("The frozen implementation attempt budget is exhausted")
                    await self.store.update(identifier, implementation_pending=True)
                    if restoration is None:
                        await self._materialize_design(identifier, workspace)
                    if contains_known_credential(brief, {**settings, **self.credential_registry()}):
                        raise ValueError(
                            "The composed worker context contains a configured credential; execution blocked"
                        )
                    current = await self.store.get_run(identifier)
                    if current.get("cancel_request", {}).get("status") == "requested":
                        raise asyncio.CancelledError()
                    await ProjectAccess(self.store).authorize_execution(current)
                    if self.tool_broker and await self.tool_broker.unresolved(identifier):
                        raise ReconciliationRequired(
                            "A prior tool invocation needs reconciliation before implementation"
                        )
                    from firefly_forge.bridge_completion_scope import bridge_completion_scope
                    from firefly_forge.bridge_completions import BridgeCompletions

                    completion_scope = (
                        BridgeCompletions(self.store, self.journal).scope(
                            current, implementation, assert_ownership=self.assert_ownership
                        )
                        if self.journal
                        else None
                    )
                    from firefly_forge.agentic_checkpoint_invocation import (
                        CheckpointInvocation,
                        checkpoint_invocation,
                        invocation_cut,
                    )

                    frozen_cut = invocation_cut(current)
                    restored_execution = restoration

                    async def checkpoint_guard(
                        frozen_cut: str = frozen_cut, restored_execution: Any = restored_execution
                    ) -> None:
                        await self.assert_ownership()
                        current_run = await self.store.get_run(identifier)
                        await ProjectAccess(self.store).authorize_execution(current_run)
                        if current_run.get("cancel_request", {}).get("status") == "requested":
                            raise asyncio.CancelledError()
                        if invocation_cut(current_run) != frozen_cut:
                            raise ReconciliationRequired("Checkpoint frozen execution context changed")
                        if restored_execution is not None:
                            await restored_execution.guard()

                    native_invocation = (
                        CheckpointInvocation(
                            self.store,
                            self.journal,
                            current,
                            implementation,
                            workspace,
                            checkpoint_guard,
                            restored=restoration,
                        )
                        if settings.get("FORGE_AGENTIC_CHECKPOINTS") == "required" and self.journal
                        else None
                    )
                    if (
                        self.journal
                        and restoration is None
                        and recovery is None
                        and hasattr(self.journal, "verification_provider_admission")
                    ):
                        try:
                            await self.journal.verification_provider_admission(identifier)
                        except ValueError as exc:
                            current_handoff = (await self.store.get_run(identifier)).get("verification_handoff", {})
                            if current_handoff.get("id"):
                                await self.journal.verification_hold(identifier, current_handoff["id"])
                            raise ReconciliationRequired(str(exc)) from exc
                    from firefly_forge.providers import assert_cli_runtime_compatible, cli_dispatch_scope

                    official_dispatch = self.provider_recoveries if run["agent"] in {"codex", "claude"} else None
                    dispatch_provider, adopted_executable = provider, None
                    coding_grant = None
                    checks = getattr(self, "coding_checks", None)
                    dispatch_pending = {"value": False}
                    if official_dispatch:
                        if self.configured_provider:
                            assert_cli_runtime_compatible()
                        if recovery is not None:
                            if (current.get("provider_recovery") or {}).get("coding_check_grant_id"):
                                try:
                                    if checks is None:
                                        raise ValueError("The scoped configured-check authority is unavailable")
                                    coding_grant = await checks.assert_reserved(identifier, active_key)
                                except ValueError as exc:
                                    raise ReconciliationRequired(str(exc)) from exc
                            selected = official_dispatch.execution_settings(current, settings, active_key)
                            adoption = (current.get("provider_recovery") or {}).get("executable_adoption")
                            if adoption:
                                if not self.configured_provider:
                                    raise ValueError(
                                        "CLI executable adoption requires the configured official provider"
                                    )
                                from firefly_forge.providers import get_provider

                                dispatch_provider = get_provider(run["agent"], selected)
                                adopted_executable = adoption["path"]
                            if coding_grant:
                                dispatch_pending["value"] = True
                            else:
                                await official_dispatch.dispatch(identifier, active_key)
                        else:
                            await official_dispatch.normal_dispatch(identifier, active_key)

                    async def launch_guard(
                        dispatch: Any = official_dispatch,
                        task_key: str | None = active_key,
                        grant: Any = coding_grant,
                        checks_service: Any = checks,
                        pending: Any = dispatch_pending,
                    ) -> None:
                        await self.assert_ownership()
                        if grant:
                            await self.store.assert_ownership()
                            await checks_service.assert_reserved(identifier, task_key)
                            if pending["value"]:
                                await dispatch.dispatch(identifier, task_key)
                                pending["value"] = False
                            await checks_service.assert_reserved(identifier, task_key)
                        if dispatch:
                            await dispatch.assert_launch(identifier, task_key)

                    channel_stack = AsyncExitStack()
                    try:
                        if coding_grant:
                            from firefly_forge.coding_check_runtime import ConfiguredCheckInvocation
                            from firefly_forge.coding_check_transport import CodingCheckServer
                            from firefly_forge.secrets import ephemeral_credentials

                            if not hasattr(dispatch_provider, "configure_coding_checks"):
                                raise ReconciliationRequired(
                                    "The configured official CLI scoped channel is unavailable"
                                )
                            if checks is None or not isinstance(active_key, str):
                                raise ReconciliationRequired(
                                    "The scoped coding invocation has no native task authority"
                                )
                            runtime = ConfiguredCheckInvocation(checks, identifier, active_key)
                            channel = CodingCheckServer(
                                runtime.run,
                                expires_at_ms=coding_grant["expires_at_ms"],
                                bind_port=coding_grant["candidate"]["channel"]["bind_port"],
                                invocation_timeout=float(settings.get("FORGE_PROVIDER_TIMEOUT", "1800")),
                            )
                            # Retain redaction until source admission, final evidence and all error handling finish.
                            credential_scopes.enter_context(ephemeral_credentials(channel.environment))
                            await channel_stack.enter_async_context(channel.running())
                            selected_checks = [
                                command
                                for command in coding_grant["candidate"]["commands"]
                                if command["command_index"] in coding_grant["request"]["command_indices"]
                            ]
                            dispatch_provider.configure_coding_checks(
                                channel,
                                prompt_text(
                                    "implementation/configured-checks",
                                    checks=json.dumps(selected_checks),
                                    call_limit=str(coding_grant["call_limit"]),
                                    final_check_count=str(coding_grant["candidate"]["independent_final_check_count"]),
                                ),
                            )
                        with (
                            bridge_completion_scope(completion_scope),
                            checkpoint_invocation(native_invocation),
                            cli_dispatch_scope(
                                launch_guard if official_dispatch else None, executable=adopted_executable
                            ),
                        ):
                            result = await dispatch_provider.execute(
                                workspace,
                                brief
                                if restoration is not None
                                else brief + "\nCurrent implementation attempt: " + str(attempt) + ".",
                                run["backend"],
                                run["frontend"],
                                emit,
                            )
                    except BaseException as exc:
                        if official_dispatch:
                            if dispatch_pending["value"]:
                                raise ReconciliationRequired(
                                    "The scoped coding invocation was held before consuming its reserved dispatch"
                                ) from exc
                            known = await official_dispatch.record_process(
                                identifier, active_key, getattr(exc, "process_outcome", None)
                            )
                            if not known and not isinstance(exc, asyncio.CancelledError):
                                raise ReconciliationRequired(
                                    "The native provider outcome requires reconciliation"
                                ) from exc
                        raise
                    finally:
                        await channel_stack.aclose()
                    if official_dispatch:
                        from firefly_forge.provider_recovery_models import ProviderAttemptClosed

                        try:
                            await official_dispatch.assert_result(identifier, active_key)
                        except ProviderAttemptClosed:
                            raise
                        except ValueError as exc:
                            raise ReconciliationRequired(str(exc)) from exc
                    if self.tool_broker and await self.tool_broker.unresolved(identifier):
                        raise ReconciliationRequired("Tool execution has an unresolved outcome; delivery is blocked")
                    if restoration is not None:
                        suffix = "\nCurrent implementation attempt: " + str(attempt) + "."
                        if not brief.endswith(suffix):
                            raise ReconciliationRequired("Retained implementation invocation framing differs")
                        brief = brief[: -len(suffix)]
                        restoration = None
                    completed_rounds += 1
                    await self.store.update(
                        identifier, implementation_rounds=completed_rounds, implementation_pending=False
                    )
                    usage = safe_data(getattr(result, "usage", {}))
                    (await self.store.update(identifier, summary=safe(result.summary), usage=usage))
                    await self._worker_feedback(identifier, workspace, result, attempt, safe_data)
                qa_report = (await self.store.get_run(identifier)).get("usage", {}).get("qa_report")
                if qa_report is not None:
                    from firefly_forge.qa import QAReport

                    qa = QAReport.model_validate(qa_report)
                    if any(finding.severity in {"high", "critical"} for finding in qa.findings):
                        raise ValueError("Visual QA requires changes; inspect the retained findings and image evidence")
                files, diff, tree = await self._snapshot(
                    workspace, base, secrets=settings, expected_head=run.get("checkpoint_revision")
                )
                evidence = {"source_tree": tree, "changed_files": files, "implementation_round": attempt}
                source_checkpoint = {"base_revision": base, "source_tree": tree}
                if restored_receipt is not None:
                    await restored_receipt.receipt(self, active_key, evidence, source_checkpoint)
                else:
                    await self._receipt(identifier, active_key, evidence, checkpoint=source_checkpoint)
                if self.provider_recoveries and recovery is not None:
                    await self.provider_recoveries.complete(identifier, active_key)
                    recovery = None
                active_key = None
                (
                    await self.store.update(
                        identifier, changed_files=files, diff=safe(diff[:1_000_000]), status="validating"
                    )
                )
                commands = [Command.model_validate(command) for command in run["commands"]] or default_commands(
                    self.component_directory(run, workspace)
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
                for check_index, command in enumerate(
                    [] if verification["disposition"] == "completed" else commands, 1
                ):
                    await emit("validating", f"Attempt {attempt}: {' '.join(command.argv)}")
                    check = await self._project_command(
                        identifier, run, workspace, command.argv, command.timeout, "verify", ordinal=check_index
                    )
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
                repair_context = None
                if failures and final_tree == tree and self.journal:
                    from firefly_forge.repair_proposals import verification_context

                    repair_context = await verification_context(
                        run,
                        {
                            "base_revision": base,
                            "head": await checked(["git", "rev-parse", "HEAD"], workspace),
                            "source_tree": tree,
                        },
                        workspace,
                    )
                halt = None
                async with self.store._unit():
                    if failures:
                        from firefly_forge.assurance import progress_record

                        retained = await self.store.get_run(identifier)
                        try:
                            history = progress_record(retained.get("repair_history", []), attempt, final_tree, failures)
                        except ValueError as exc:
                            halt = str(exc)
                            history = retained.get("repair_history", [])
                        await self.store.update(identifier, repair_history=history, repair_halt=halt)
                    await self._receipt(
                        identifier,
                        active_key,
                        {
                            "checks": final,
                            "failed_checks": failures,
                            "source_tree": final_tree,
                            "implementation_round": attempt,
                            "repair_context": repair_context,
                        },
                    )
                active_key = None
                if (
                    failures
                    and self.learning
                    and (run.get("learning_snapshot") or {}).get("settings", {}).get("enabled")
                ):
                    await self.learning.capture_failure(identifier)
                if halt:
                    raise ValueError(halt)
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
                if attempt > repairs + human_allowance:
                    raise ValueError(f"Verification failed after {attempt} implementation attempts")
                feedback = "\n\n".join(
                    f"{' '.join(check['argv'])}: exit {check['exit_code']}\n{check['output']}" for check in failures
                )
                await emit("repairing", "Verification failed; returning all failed checks to the coding worker")
                repair_failures = implementation_failures = failures
                brief = prompt_text("implementation/repair", brief=source_brief, checks=feedback)
            await self._independent_review(identifier, workspace, emit, settings)
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
            if self.learning and (run.get("learning_snapshot") or {}).get("settings", {}).get("enabled"):
                await self.learning.capture_outcome(identifier)
            await self._remember_knowledge(identifier, emit)
            if run["publish"]:
                await self.publish(identifier)
        except asyncio.CancelledError as exc:
            from firefly_forge.providers import ProviderSubmissionCancelled

            if isinstance(exc, ProviderSubmissionCancelled) and not getattr(exc, "native_retained", False):
                await retain_unknown("Bridge submission cancelled before its outcome was known", {})
            (await self.store.finish(identifier, "interrupted" if self.stopping else "cancelled"))
            raise
        except Exception as exc:
            from firefly_forge.assurance import ReviewPending
            from firefly_forge.provider_recovery_models import ProviderAttemptClosed
            from firefly_forge.providers import (
                ProviderAdmissionRefused,
                ProviderLimitError,
                ProviderOutcomeUnknown,
                classify_provider_failure,
            )

            if isinstance(exc, ProviderAttemptClosed):
                return
            classification = classify_provider_failure(exc)
            if restoration is not None:
                await self.store.update(identifier, status="needs_reconciliation", error=safe(str(exc)))
                return
            if isinstance(exc, ProviderOutcomeUnknown):
                await retain_unknown(str(exc), exc.observation)
            elif self.journal and active_key and classification:
                await self.journal.fail(
                    identifier,
                    active_key,
                    classification[0],
                    safe(str(exc)),
                    retry_after=classification[1],
                    **(
                        {"observation": safe_data(exc.observation)}
                        if isinstance(exc, (ProviderLimitError, ProviderAdmissionRefused)) and exc.observation
                        else {}
                    ),
                )
                await emit("paused", "Execution paused: " + classification[0])
            elif isinstance(exc, WorkerAttention):
                await emit("paused", "Waiting for the recorded worker question to be answered")
            elif isinstance(exc, ReviewPending):
                if (await self.store.get_run(identifier))["status"] != "paused":
                    await self.store.update(identifier, status="needs_reconciliation", error=safe(str(exc)))
            elif isinstance(exc, ReconciliationRequired) or self._sandbox_reconciliation(exc):
                await self.store.update(identifier, status="needs_reconciliation", error=safe(str(exc)))
            else:
                (await self.store.finish(identifier, "failed", error=safe(str(exc))))
            if (await self.store.get_run(identifier))["status"] == "succeeded":
                (await self.store.update(identifier, publication_error=safe(str(exc))))
                await emit("publication", f"Publication failed: {exc}")
        finally:
            credential_scopes.close()

    async def _worker_feedback(
        self, identifier: str, workspace: Path, result: Any, attempt: int, safe_data: Callable[[Any], Any]
    ) -> None:
        from firefly_forge.agent_roles import worker_feedback_model

        payload = getattr(result, "worker_feedback", None) or getattr(result, "usage", {}).get("worker_feedback")
        if payload is None and result.summary.strip().startswith("{"):
            try:
                report = json.loads(result.summary)
            except json.JSONDecodeError:
                report = None
            if isinstance(report, dict):
                payload = report.get("feedback")
        if payload is None:
            return
        feedback = worker_feedback_model(payload).model_validate(safe_data(payload))
        if feedback.role_id != "implementer" or feedback.task_id != "implement" or feedback.attempt != attempt:
            raise ValueError("Worker feedback must identify its actual implementation role, stage and attempt")
        run = await self.store.get_run(identifier)
        root = self.component_directory(run, workspace)
        for path in feedback.evidence_paths:
            evidence = workspace / path
            if (
                evidence.is_symlink()
                or not evidence.is_file()
                or not evidence.resolve().is_relative_to(root)
                or private_path(path)
            ):
                raise ValueError("Worker feedback evidence must exist inside the selected public component")
        if not self.journal:
            raise ValueError("Worker feedback requires the durable execution journal")
        await self.journal.worker_feedback(identifier, feedback)
        if feedback.request:
            raise WorkerAttention(feedback.request.id)
        if feedback.disposition == "failed":
            raise ValueError("The implementation worker reported failure: " + feedback.summary)

    async def _inspect_workspace(self, identifier: str, workspace: Path, secrets: dict[str, str]) -> dict[str, Any]:
        run = await self.store.get_run(identifier)
        repository = await self.store.repository(run["repository_id"])
        if not run.get("git_metadata_digest"):
            raise ReconciliationRequired("Retained workspace has no trusted Git metadata baseline")
        self.git_metadata[workspace.resolve()] = run["git_metadata_digest"]
        self._assert_git_metadata(workspace)
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

    async def _repair_proposal(
        self, identifier: str, workspace: Path, secrets: dict[str, str], next_attempt: int
    ) -> None:
        from firefly_forge.project_access import ProjectAccess
        from firefly_forge.repair_proposals import repair_proposal, verification_context

        await self.assert_ownership()
        if self.tool_broker and await self.tool_broker.unresolved(identifier):
            raise ReconciliationRequired("A prior tool invocation needs reconciliation before implementation")
        current = await self.store.get_run(identifier)
        if current.get("cancel_request", {}).get("status") == "requested":
            raise asyncio.CancelledError()
        await ProjectAccess(self.store).authorize_execution(current)
        source = await self._inspect_workspace(identifier, workspace, secrets)
        observed = await verification_context(current, source, workspace)
        assert self.journal is not None
        try:
            decision = await repair_proposal(
                self.journal, identifier, observed, next_attempt, source_tree=source["source_tree"]
            )
        except ValueError as exc:
            raise ReconciliationRequired(str(exc)) from exc
        if not decision["allowed"]:
            raise ValueError(decision["summary"])

    async def _begin_task(
        self,
        identifier: str,
        task_id: str,
        workspace: Path,
        secrets: dict[str, str],
        failed_checks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        await self.assert_ownership()
        from firefly_forge.project_access import ProjectAccess

        current = await self.store.get_run(identifier)
        if current.get("cancel_request", {}).get("status") == "requested":
            raise asyncio.CancelledError()
        await ProjectAccess(self.store).authorize_execution(current)
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

    @staticmethod
    def component_directory(run: dict[str, Any], workspace: Path, *, create: bool = False) -> Path:
        from firefly_forge.project_models import component_path

        relative = component_path(
            run.get("component_snapshot", {}).get("path", ".") if run.get("component_snapshot") else "."
        )
        directory = (workspace / relative).resolve()
        if not directory.is_relative_to(workspace.resolve()):
            raise ValueError("Project component directory escapes the authorized workspace")
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise ValueError("The selected project component directory does not exist")
        return directory

    @staticmethod
    def _qa_reference_descriptors(run: dict[str, Any]) -> list[dict[str, Any]]:
        from firefly_forge.product_design import MAX_BUNDLE_BYTES, VARIANTS

        bundle = run.get("design_snapshot")
        if not bundle or bundle.get("method") != "wireframe" or run.get("frontend") == "none":
            return []
        signed = {key: value for key, value in bundle.items() if key != "digest"}
        encoded = json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        if len(encoded) > MAX_BUNDLE_BYTES or hashlib.sha256(encoded).hexdigest() != bundle.get("digest"):
            raise ValueError("Frozen QA design reference failed its content digest or size bound")
        review = ((run.get("product_definition") or {}).get("definition") or {}).get("design_review")
        if not review or review.get("reviewed") is not True or review.get("digest") != bundle["digest"]:
            return []
        from firefly_forge.product_fonts import manrope_style

        if bundle.get("font_family") != "Manrope" or bundle.get("font_css") != manrope_style():
            return []
        references = []
        for screen in bundle["screens"]:
            if screen["id"] not in review["screen_ids"]:
                continue
            for field, (viewport, theme) in VARIANTS.items():
                svg = screen[field]
                size = re.search(r'\bviewBox="0 0 (\d+) (\d+)"', svg)
                if size is None:
                    raise ValueError("Frozen reference has no supported viewport")
                references.append(
                    {
                        "artifact_id": screen["id"] + "-" + viewport + "-" + theme,
                        "journey_id": screen["id"],
                        "state_id": screen["id"] + "-default",
                        "width": int(size[1]),
                        "height": int(size[2]),
                        "theme": theme,
                        "zoom": 100,
                        "source_sha256": hashlib.sha256(svg.encode()).hexdigest(),
                        "variant": field,
                    }
                )
        return references

    async def _resolve_qa_reference(
        self, run: dict[str, Any], scope: Path, plan: BrowserJourney
    ) -> tuple[QAReference, EvidenceImage] | None:
        reference = next(
            (item for item in self._qa_reference_descriptors(run) if item["artifact_id"] == plan.reference_id), None
        )
        if reference is None or (
            reference["journey_id"],
            reference["width"],
            reference["height"],
            reference["theme"],
            reference["zoom"],
        ) != (plan.journey_id, plan.width, plan.height, plan.theme, plan.zoom):
            return None
        bundle = run["design_snapshot"]
        screen = next(item for item in bundle["screens"] if item["id"] == reference["journey_id"])
        svg = screen[reference["variant"]]
        data = await self.browser.rasterize_reference(
            run["id"], scope, svg, plan.width, plan.height, theme=plan.theme, zoom=plan.zoom, task_id="implement"
        )
        raster_sha = hashlib.sha256(data).hexdigest()
        approved = QAReference(
            artifact_id=reference["artifact_id"],
            sha256=raster_sha,
            state_id=reference["state_id"],
            width=plan.width,
            height=plan.height,
            theme=plan.theme,
            zoom=plan.zoom,
        )
        evidence = EvidenceImage(
            data,
            {
                **{key: value for key, value in reference.items() if key != "variant"},
                "evidence_id": "approved-" + raster_sha,
                "screenshot_sha256": raster_sha,
                "design_digest": bundle["digest"],
                "kind": "approved_reference",
            },
        )
        return approved, evidence

    async def _materialize_design(self, identifier: str, workspace: Path) -> None:
        run = await self.store.get_run(identifier)
        bundle = run.get("design_snapshot")
        if not bundle or run.get("design_materialized_digest") == bundle["digest"]:
            return
        from firefly_forge.agentic_runtime import WorkspaceTools
        from firefly_forge.product_design import MAX_BUNDLE_BYTES, MAX_DESIGN_FILE_BYTES, design_files

        signed = {key: value for key, value in bundle.items() if key != "digest"}
        digest = hashlib.sha256(
            json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        if digest != bundle["digest"] or len(json.dumps(bundle, ensure_ascii=True).encode()) > MAX_BUNDLE_BYTES:
            raise ValueError("Frozen design reference failed its content digest or size bound")
        files = design_files(bundle)
        if len(files) > 210 or sum(len(content.encode()) for content in files.values()) > MAX_DESIGN_FILE_BYTES:
            raise ValueError("Materialized design references exceed the supported bounds")
        await self.assert_ownership()
        tools = WorkspaceTools(self.component_directory(run, workspace, create=True))
        for path, content in files.items():
            await tools.write_file(path, content)
        prefix = run.get("component_snapshot", {}).get("path", ".") if run.get("component_snapshot") else "."
        await self.store.update(
            identifier,
            design_materialized_digest=bundle["digest"],
            design_materialized_paths=[path if prefix == "." else prefix + "/" + path for path in files],
        )

    async def _independent_review(
        self, identifier: str, workspace: Path, emit: Callable[..., Any], settings: dict[str, str]
    ) -> None:
        run = await self.store.get_run(identifier)
        if run.get("assurance", {}).get("review_agent"):
            if not self.assurance:
                raise ValueError("Independent review service is unavailable")
            result = await self.assurance.review(run, workspace, emit, self.assert_ownership)
            if result["status"] != "approved":
                raise ValueError("Independent review requires changes; delivery is blocked")
        else:
            await self.store.update(identifier, review={"status": "not_requested", "source_tree": run["source_tree"]})
        await self._assert_delivery_tree(identifier, workspace, settings)

    async def _assert_delivery_tree(
        self, identifier: str, workspace: Path, settings: dict[str, str] | None = None
    ) -> None:
        run = await self.store.get_run(identifier)
        if run.get("component_snapshot"):
            self.component_roots[workspace.resolve()] = run["component_snapshot"]["path"]
        _, _, tree = await self._snapshot(
            workspace,
            run["base_revision"],
            allow_unchanged=run.get("operation") == "test",
            secrets=settings or {},
            expected_head=run.get("checkpoint_revision"),
        )
        if tree != run["source_tree"]:
            raise ValueError("Source changed after verification or independent review")
        if run.get("assurance", {}).get("review_agent") and (
            run.get("review", {}).get("status") != "approved" or run["review"].get("source_tree") != tree
        ):
            raise ValueError("Delivery requires approved independent review of this source tree")

    async def _checkpoint_commit(self, identifier: str, workspace: Path) -> None:
        await self._assert_delivery_tree(identifier, workspace)
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

    @staticmethod
    def _sandbox_reconciliation(exc: Exception) -> bool:
        from firefly_forge.sandbox import SandboxReconciliationRequired
        from firefly_forge.tool_broker import ToolReconciliationRequired

        return isinstance(exc, (SandboxReconciliationRequired, ToolReconciliationRequired))

    async def _project_command(
        self,
        identifier: str,
        run: dict[str, Any],
        workspace: Path,
        argv: list[str],
        timeout: int,
        stage: str,
        *,
        ordinal: int | None = None,
        _diagnostic_snapshot: str | None = None,
        _receipt_sink: Callable[[dict[str, Any]], None] | None = None,
        _invocation_guard: Callable[[], Any] | None = None,
    ) -> Any:
        from firefly_forge.project_access import ProjectAccess

        await self.assert_ownership()
        if (await self.store.get_run(identifier)).get("cancel_request", {}).get("status") == "requested":
            raise asyncio.CancelledError()
        await ProjectAccess(self.store).authorize_execution(run)
        directory = self.component_directory(run, workspace)
        diagnostic = (
            {"_diagnostic_snapshot": _diagnostic_snapshot, "_receipt_sink": _receipt_sink}
            if _diagnostic_snapshot is not None
            else {}
        )
        if self.sandbox is not None:
            return await self.sandbox.execute(
                identifier,
                directory,
                directory,
                argv,
                timeout,
                stage,
                ordinal=ordinal,
                git_root=workspace if stage == "verify" else None,
                **({"_invocation_guard": _invocation_guard} if _invocation_guard else {}),
                **diagnostic,
            )
        if _diagnostic_snapshot is not None:
            raise ValueError("Isolated diagnostics cannot execute without their runner")
        if run.get("sandbox_snapshot", {}).get("mode") in {"docker", "pool"}:
            raise ValueError("The selected Docker runner is unavailable; execution cannot fall back to the host")
        return await execute(argv, directory, timeout)

    async def _profile_python(
        self, identifier: str, run: dict[str, Any], workspace: Path, request: PythonProfileRequest
    ) -> dict[str, Any]:
        from firefly_forge.agent_roles import stage_allowed_tools
        from firefly_forge.project_access import ProjectAccess
        from firefly_forge.workspace_tools import WorkspaceTools

        request = PythonProfileRequest.model_validate(request.model_dump(mode="json"))
        grants = stage_allowed_tools(run.get("role_snapshot") or {}, "implement", "agentic")
        if self.sandbox is None or not {"profile_python", "execute_command"}.issubset(grants or ()):
            raise ValueError("Python profiling requires explicit profiling and command grants and an isolated runner")
        config = profile_configuration(run.get("sandbox_snapshot"))
        await self.assert_ownership()
        await ProjectAccess(self.store).authorize_execution(run)
        scope = self.component_directory(run, workspace)
        source = await WorkspaceTools(scope, repository_root=workspace).read_file(request.script)
        source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
        binding: dict[str, Any] = {}
        result = await self._project_command(
            identifier,
            run,
            workspace,
            build_profile_argv(request, source_sha256),
            request.timeout,
            "implement",
            _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
            _receipt_sink=binding.update,
        )
        return profile_observation(request, source_sha256, str(config.image), result, binding)

    async def _profile_php(
        self, identifier: str, run: dict[str, Any], workspace: Path, request: PHPProfileRequest
    ) -> dict[str, Any]:
        from firefly_forge.agent_roles import stage_allowed_tools
        from firefly_forge.project_access import AccessDenied, ProjectAccess
        from firefly_forge.workspace_tools import WorkspaceTools

        request = PHPProfileRequest.model_validate(request.model_dump(mode="json"))
        grants = stage_allowed_tools(run.get("role_snapshot") or {}, "implement", "agentic")
        if self.sandbox is None or not {"profile_php", "execute_command"}.issubset(grants or ()):
            raise ValueError("PHP profiling requires explicit profiling and command grants and an isolated runner")
        config = php_profile_configuration(run.get("sandbox_snapshot"))
        await self.assert_ownership()
        await ProjectAccess(self.store).authorize_execution(run)
        scope = self.component_directory(run, workspace)
        source = await WorkspaceTools(scope, repository_root=workspace).read_file(request.script)
        source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
        binding: dict[str, Any] = {}
        result = await self._project_command(
            identifier,
            run,
            workspace,
            build_php_profile_argv(request, source_sha256),
            request.timeout,
            "implement",
            _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
            _receipt_sink=binding.update,
        )
        observation = php_profile_observation(request, source_sha256, str(config.image), result, binding)
        retention: dict[str, Any] = {"status": "not_retained"}
        if observation["receipt"] is not None:
            try:
                entry = await self.journal.php_diagnostic(identifier, request, observation)
                retention = {"status": "recorded", "sequence": entry["sequence"], "digest": entry["digest"]}
            except AccessDenied:
                raise
            except Exception:
                # The command already completed; failed observation retention never authorizes replay.
                retention = {"status": "unknown"}
        response = {**observation, "retention": retention}
        unavailable = False
        try:
            await self.store.admit_credentials(response)
        except AccessDenied:
            raise
        except Exception:
            unavailable = True
        await self.assert_ownership()
        current_run = await self.store.get_run(identifier)
        if (current_run.get("cancel_request") or {}).get("status") == "requested":
            raise asyncio.CancelledError()
        await ProjectAccess(self.store).authorize_execution(current_run)
        try:
            self.store.reject_credentials(response)
        except ValueError:
            unavailable = True
        if unavailable:
            return {
                "recipe": request.recipe,
                "observation_only": True,
                "best_effort": True,
                "exit_code": result.exit_code,
                "diagnostic_unavailable": True,
                "retention": {"status": retention["status"]},
            }
        return response

    async def _trace_python(
        self, identifier: str, run: dict[str, Any], workspace: Path, request: PythonTraceRequest
    ) -> dict[str, Any]:
        from firefly_forge.agent_roles import stage_allowed_tools
        from firefly_forge.project_access import ProjectAccess
        from firefly_forge.workspace_tools import WorkspaceTools

        request = PythonTraceRequest.model_validate(request.model_dump(mode="json"))
        grants = stage_allowed_tools(run.get("role_snapshot") or {}, "implement", "agentic")
        if self.sandbox is None or not {"trace_python", "execute_command"}.issubset(grants or ()):
            raise ValueError("Python tracing requires explicit tracing and command grants and an isolated runner")
        config = profile_configuration(run.get("sandbox_snapshot"))
        await self.assert_ownership()
        await ProjectAccess(self.store).authorize_execution(run)
        scope = self.component_directory(run, workspace)
        source = await WorkspaceTools(scope, repository_root=workspace).read_file(request.script)
        source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
        binding: dict[str, Any] = {}
        result = await self._project_command(
            identifier,
            run,
            workspace,
            build_trace_argv(request, source_sha256),
            request.timeout,
            "implement",
            _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
            _receipt_sink=binding.update,
        )
        return trace_observation(request, source_sha256, str(config.image), result, binding)

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
        commands = [Command.model_validate(command) for command in run["commands"]] or default_commands(
            self.component_directory(run, workspace)
        )
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
        for check_index, command in enumerate([] if verification["disposition"] == "completed" else commands, 1):
            await emit("validating", " ".join(command.argv))
            check = await self._project_command(
                identifier, run, workspace, command.argv, command.timeout, "verify", ordinal=check_index
            )
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
        await self._independent_review(identifier, workspace, emit, settings)
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
        self._assert_git_metadata(workspace)
        await checked(["git", "fsck", "--strict", "--no-reflogs"], workspace, 60)
        await checked(["git", "add", "--all", "--", "."], workspace)
        inventory = await checked(["git", "diff", "--name-only", "-z", base, "--"], workspace)
        files = [path for path in inventory.split("\x00") if path]
        root = self.component_roots.get(workspace.resolve(), ".")
        if root != "." and any(not path.startswith(root + "/") for path in files):
            raise ValueError("Source changes leave the selected project component; delivery is blocked")
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

    def _assert_git_metadata(self, workspace: Path) -> None:
        baseline = self.git_metadata.get(workspace.resolve())
        if baseline is not None and metadata_digest(workspace) != baseline:
            raise ReconciliationRequired("Git metadata changed after workspace preparation; inspect before continuing")

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
        if run.get("component_snapshot"):
            self.component_roots[workspace.resolve()] = run["component_snapshot"]["path"]
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
        await self._assert_delivery_tree(identifier, workspace, settings)
        return workspace

    async def _artifact(self, identifier: str, workspace: Path, files: list[str], patch: str) -> None:
        target = self.workspaces / f"{identifier}.zip"
        run = await self.store.get_run(identifier)
        tracked = await checked(["git", "ls-tree", "-r", "--name-only", "-z", run["source_tree"]], workspace)
        selected = [name for name in files if name in tracked.split("\x00") and not private_path(name)]
        total = 0
        with tempfile.TemporaryDirectory(dir=self.workspaces) as temporary:
            source_archive = Path(temporary) / "source.zip"
            if selected:
                await checked(
                    [
                        "git",
                        "archive",
                        "--format=zip",
                        f"--output={source_archive}",
                        run["source_tree"],
                        "--",
                        *selected,
                    ],
                    workspace,
                )
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("forge-changes.patch", patch)
                if selected:
                    with zipfile.ZipFile(source_archive) as source:
                        for entry in source.infolist():
                            if entry.is_dir() or entry.filename not in selected:
                                continue
                            if stat.S_ISLNK(entry.external_attr >> 16):
                                raise ValueError("Changed-file artifacts cannot contain symbolic links")
                            total += entry.file_size
                            if total > 50_000_000:
                                raise ValueError("Changed-file artifact exceeds 50 MB")
                            archive.writestr(entry.filename, source.read(entry))
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

    async def publish(self, identifier: str, *, actor: dict[str, Any] | None = None) -> str:
        if self.store.runtime_roles and self.store.runtime_roles.role == "api":
            await self.store.execution_requests.request_publish(identifier)
            return ""
        await self.assert_ownership()
        async with self.publication_lock:
            run = await self.store.get_run(identifier)
            from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess

            principal = CURRENT_PRINCIPAL.get()
            releaser = (
                actor
                if actor is not None
                else (ProjectAccess.actor_snapshot(principal) if principal else run.get("execution_actor"))
            )
            access = ProjectAccess(self.store)
            await access.authorize_actor(releaser, run.get("project_id"), "release")
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
            if self.connections:
                self.connections.assert_snapshot(repo, run.get("scm_snapshot"))
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
                    identifier,
                    "publish",
                    payload={"branch": run["branch"], "source_tree": run["source_tree"], "actor": releaser},
                )
                if publication["disposition"] != "execute":
                    raise ReconciliationRequired(
                        "Publication intent exists without a reconciled outcome; inspect the remote before retrying"
                    )
                publication_key = publication["idempotency_key"]
            (await self.store.update(identifier, publication_status="publishing", publication_actor=releaser))
            if not run.get("checkpoint_revision"):
                await self._checkpoint_commit(identifier, workspace)
            revision = (await self.store.get_run(identifier))["checkpoint_revision"]
            await self.assert_ownership()
            await access.authorize_actor(releaser, run.get("project_id"), "release")
            await push(workspace, run["branch"], env=git_env, repository_url=repo["url"], revision=revision)
            body = (
                "Implemented by firefly forge.\n\n"
                + run["summary"]
                + "\n\nVerification:\n"
                + "\n".join(f"- {' '.join(check['argv'])}: passed" for check in run["final_verification"])
            )
            async with httpx.AsyncClient(follow_redirects=False) as client:
                await self.assert_ownership()
                await access.authorize_actor(releaser, run.get("project_id"), "release")
                url = await PullRequests(client).create(
                    repo, run["branch"], run["brief"][:150], body, authentication=authentication
                )
            (await self.store.update(identifier, pr_url=url, publication_status="published"))
            await self._receipt(
                identifier, publication_key, {"pr_url": url, "branch": run["branch"], "source_tree": run["source_tree"]}
            )
            (await self.store.event(identifier, "publication", f"Pull request created: {url}"))
            return url
