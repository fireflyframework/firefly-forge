# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit implementation invocation shared by the real Agentic and SDK transport."""

from __future__ import annotations

import copy
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.metadata import version
from pathlib import Path
from typing import Any

from firefly_forge.agent_checkpoint_models import InvocationBinding
from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary, BoundaryCheckpoint, OriginalCompletion
from firefly_forge.bridge_completions import BridgeCompletions, _cut, _outside_unit
from firefly_forge.checkpoint_source_identity import git_head_identity, source_identity
from firefly_forge.git_workspace import git_environment, metadata_digest, trusted_git
from firefly_forge.journal import digest
from firefly_forge.process import checked
from firefly_forge.prompts import prompt_catalog
from firefly_forge.source_snapshot import git_index_identity

FILE_TOOLS = frozenset({"list_files", "read_file", "write_file", "search_text", "edit_lines", "git_diff"})
STATIC_TOOLS = FILE_TOOLS | {"execute_command", "knowledge_lookup", "read_run_evidence"}
_current: ContextVar[CheckpointInvocation | None] = ContextVar("agentic_checkpoint_invocation", default=None)


def current_checkpoint_invocation() -> CheckpointInvocation | None:
    return _current.get()


@contextmanager
def checkpoint_invocation(value: CheckpointInvocation | None) -> Iterator[None]:
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)


def runtime_contract() -> str:
    _outside_unit()
    root = Path(__file__).parent
    return digest(
        {
            "versions": {
                name: version(name) for name in ("fireflyframework-agentic", "pydantic-ai-slim", "openai", "pyfly")
            },
            "prompts": prompt_catalog().digest,
            "adapter": {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in (
                    "agentic_runtime.py",
                    "agent_restores.py",
                    "agent_checkpoints.py",
                    "agentic_checkpoint_boundary.py",
                    "agentic_checkpoint_invocation.py",
                    "checkpoint_source_identity.py",
                    "source_snapshot.py",
                    "source_diff_admission.py",
                    "git_workspace.py",
                    "workspace_tools.py",
                    "providers.py",
                    "sandbox.py",
                    "sandbox_command_slots.py",
                    "journal.py",
                    "runner_pool_worker.py",
                    "sandbox_models.py",
                    "sandbox_persistence.py",
                    "runner_backends.py",
                    "runner_factory_models.py",
                    "runner_pool_models.py",
                    "engine.py",
                    "known_worker_completion_models.py",
                    "known_worker_completion_proof.py",
                    "known_worker_completions.py",
                    "native_terminal_completion_models.py",
                    "native_terminal_completions.py",
                    "native_terminal_restore_proof.py",
                    "agent_checkpoint_models.py",
                    "bridge_completion_models.py",
                    "bridge_completions.py",
                    "execution_requests.py",
                    "execution_request_persistence.py",
                    "unknown_command_acknowledgements.py",
                    "unknown_command_models.py",
                    "unknown_command_proof.py",
                    "unknown_command_controllers.py",
                    "checkpoint_mode_adoptions.py",
                    "checkpoint_mode_models.py",
                    "checkpoint_mode_proof.py",
                    "checkpoint_mode_runtime.py",
                    "checkpoint_mode_custody.py",
                    "checkpoint_mode_handoff_custody.py",
                    "checkpoint_mode_controllers.py",
                    "scm_authority_models.py",
                    "git_deliveries.py",
                )
            },
        }
    )


def invocation_cut(run: dict[str, Any]) -> str:
    return digest(
        {
            "bridge": _cut(run, "config_revision"),
            "frozen": {
                name: run.get(name)
                for name in (
                    "agent",
                    "backend",
                    "frontend",
                    "role_snapshot",
                    "policy_snapshot",
                    "model_selection",
                    "knowledge_context_digest",
                    "campaign_context",
                    "design_snapshot",
                    "branding_snapshot",
                    "product_definition",
                )
            },
        }
    )


async def workspace_cut(store: Any, workspace: Path) -> dict[str, str]:
    _outside_unit()
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("The retained owned workspace is unavailable")
    metadata = metadata_digest(workspace)
    head = await checked(trusted_git(["git", "rev-parse", "HEAD"]), workspace, env=git_environment())
    index = git_index_identity(workspace)
    head_identity = git_head_identity(workspace)
    scanner = await store.source_patch_scanner()

    async def finalize() -> None:
        current_head = await checked(trusted_git(["git", "rev-parse", "HEAD"]), workspace, env=git_environment())
        await scanner.revalidate()
        if (
            current_head != head
            or metadata_digest(workspace) != metadata
            or git_index_identity(workspace) != index
            or git_head_identity(workspace) != head_identity
        ):
            raise ValueError("Workspace Git metadata changed during inspection")

    source = await source_identity(workspace, scanner, finalize=finalize)
    return {"path": str(workspace.resolve()), "head": head, "metadata": metadata, "source": source}


class CheckpointInvocation:
    def __init__(
        self,
        store: Any,
        journal: Any,
        run: dict[str, Any],
        task: dict[str, Any],
        workspace: Path,
        guard: Callable[[], Awaitable[None]],
        *,
        restored: Any = None,
    ) -> None:
        self.store, self.journal = store, journal
        self.run, self.task = copy.deepcopy(run), copy.deepcopy(task)
        self.workspace, self.guard, self.restored = workspace, guard, restored
        self.service = AgentCheckpoints(BridgeCompletions(store, journal))
        self.binding: InvocationBinding | None = None
        self.boundary: AgenticBoundary | None = None
        self.original: OriginalCompletion | None = None
        self.ordinal = 0
        self.predecessor: str | None = None
        self._captured: BoundaryCheckpoint | None = None
        self._original_delivered = False

    def attach(
        self, boundary: AgenticBoundary, brief: str, backend: str, frontend: str, context: Any, settings: Any
    ) -> Any:
        grants = context.allowed_tools
        if grants is None or not grants or not set(grants) <= STATIC_TOOLS:
            raise ValueError("Required checkpoints support only explicit static text-tool grants without QA state")
        from firefly_forge.engine import Engine

        try:
            references_match = digest({"references": context.qa_references}) == digest(
                {"references": Engine._qa_reference_descriptors(self.run)}
            )
        except (TypeError, ValueError):
            references_match = False
        if not references_match:
            raise ValueError("Required checkpoints need the exact frozen textual QA reference descriptors")
        self.boundary = boundary
        if self.restored is not None:
            binding = self.restored.binding
            if (binding.brief, binding.backend, binding.frontend, binding.grants, binding.contract) != (
                brief,
                backend,
                frontend,
                sorted(grants),
                runtime_contract(),
            ):
                raise ValueError("Restored invocation instructions, grants or runtime contract changed")
            self.binding = binding
            self.ordinal, self.predecessor = binding.ordinal, self.restored.checkpoint.digest
            self.original = OriginalCompletion(self.restored.checkpoint, self.restored.body, self.restored.consume)
        else:
            self.binding = InvocationBinding(
                invocation_id=uuid.uuid4().hex,
                ordinal=1,
                predecessor=None,
                brief=brief,
                backend=backend,
                frontend=frontend,
                round=int(self.run.get("attempt", 1)),
                deadline_ms=int((time.time() + float(settings.get("FORGE_PROVIDER_TIMEOUT", "1800"))) * 1000),
                grants=sorted(grants),
                contract=runtime_contract(),
                run_cut=invocation_cut(self.run),
                workspace={"path": "", "head": "", "metadata": "", "source": ""},
            )

        def capture(wire: bytes) -> BoundaryCheckpoint:
            self._captured = boundary.capture(wire)
            return self._captured

        async def capture_context() -> dict[str, Any]:
            assert self.binding is not None and self._captured is not None
            await self.guard()
            source = await workspace_cut(self.store, self.workspace)
            await self.guard()
            self.ordinal += 1
            data = self.binding.model_dump(mode="json")
            data.update(ordinal=self.ordinal, predecessor=self.predecessor, workspace=source)
            self.predecessor = self._captured.digest
            self.binding = InvocationBinding.model_validate(data)
            return data

        return self.service.scope(
            self.run, self.task, capture=capture, capture_context=capture_context, assert_ownership=self.guard
        )

    def remaining(self) -> float:
        assert self.binding is not None
        remaining = self.binding.deadline_ms / 1000 - time.time()
        if remaining <= 0:
            raise ValueError("Original checkpoint invocation deadline expired")
        return remaining

    async def replay_first(self, wire: bytes) -> bytes | None:
        if self.original is None or self._original_delivered:
            return None
        if self.original.claimed:
            raise ValueError("Original completion consumption was already attempted")
        assert self.boundary is not None and self.restored is not None
        await self.restored.guard_source()
        self.remaining()
        self.boundary.capture(wire)
        body = await self.original.expose(wire)
        self._original_delivered = True
        return body
