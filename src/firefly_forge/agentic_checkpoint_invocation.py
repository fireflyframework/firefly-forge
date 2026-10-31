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

from firefly_forge.agent_checkpoint_models import InvocationContext
from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary, BoundaryCheckpoint, OriginalCompletion
from firefly_forge.bridge_completions import BridgeCompletions, _cut, _outside_unit
from firefly_forge.checkpoint_source_identity import git_head_identity, source_identity
from firefly_forge.checkpoint_work_duration import (
    binding_remaining,
    invocation_binding,
    selected_work_duration,
    validate_binding_duration,
)
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
                name: version(name)
                for name in (
                    "pydantic-ai-slim",
                    "openai",
                    "pyfly",
                )
            },
            "prompts": prompt_catalog().digest,
            "adapter": {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in (
                    "agentic_runtime.py",
                    "agentic_tool_custody.py",
                    "agentic_tool_results.py",
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
                    "known_final_outcome_proof.py",
                    "final_outcome_models.py",
                    "final_outcome_continuations.py",
                    "final_outcome_acceptance.py",
                    "retained_verified_source.py",
                    "final_outcome_controllers.py",
                    "native_terminal_completion_models.py",
                    "native_terminal_completions.py",
                    "native_terminal_restore_proof.py",
                    "checkpoint_continuations.py",
                    "checkpoint_continuation_proof.py",
                    "checkpoint_continuation_models.py",
                    "checkpoint_continuation_controllers.py",
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
                    "original_selection_readmissions.py",
                    "original_selection_models.py",
                    "original_selection_proof.py",
                    "original_selection_runtime.py",
                    "original_selection_controllers.py",
                    "configuration.py",
                    "model_profiles.py",
                    "task_routing.py",
                    "verification_handoffs.py",
                    "scm_authority_models.py",
                    "git_deliveries.py",
                    "persistence.py",
                    "worker_evidence_query.py",
                    "run_evidence.py",
                    "run_evidence_plans.py",
                    "api.py",
                    "invalid_read_continuities.py",
                    "invalid_read_continuity.py",
                    "invalid_read_contract.py",
                    "invalid_read_epochs.py",
                    "invalid_read_continuity_controllers.py",
                    "invalid_read_continuity_models.py",
                    "invalid_read_lease.py",
                    "invalid_read_reconstruction.py",
                    "invalid_read_settlements.py",
                    "invalid_read_tickets.py",
                    "invalid_read_recoveries.py",
                    "provider_rejection_models.py",
                    "provider_rejection_recoveries.py",
                    "readonly_observation_models.py",
                    "readonly_observation_reconstruction.py",
                    "readonly_observation_recoveries.py",
                    "pending_checkpoint_reconstruction.py",
                    "invalid_read_recovery_events.py",
                    "invalid_read_recovery_models.py",
                    "invalid_read_dispatch_guard.py",
                    "controllers.py",
                    "knowledge.py",
                    "knowledge_first.py",
                    "learning_context.py",
                    "framework_knowledge.py",
                    "framework_knowledge_models.py",
                    "framework_knowledge_persistence.py",
                    "framework_knowledge_bootstrap.py",
                    "framework_knowledge_conformance.py",
                    "framework_knowledge_requirements.py",
                    "framework_knowledge_targets.py",
                    "framework_knowledge_upstream.py",
                    "framework_knowledge_worker.py",
                    "framework_knowledge_sync.py",
                    "framework_knowledge_generation.py",
                    "framework_knowledge_evidence.py",
                    "framework_knowledge_controllers.py",
                    "profiles.py",
                    "access_routes.py",
                    "migrations.py",
                    "store.py",
                    "process.py",
                    "assurance.py",
                    "runner_configuration.py",
                    "runner_pool_owner.py",
                    "runner_pool_launcher.py",
                    "runner_pool_dispatch.py",
                    "runner_pool_execution.py",
                    "runner_pool_artifacts.py",
                    "runner_controller_client.py",
                    "runner_monitor_models.py",
                    "work_duration_models.py",
                    "runner_monitor_transport.py",
                    "runner_monitor_guardian.py",
                    "runner_monitor_custody.py",
                    "runner_monitors.py",
                    "checkpoint_work_duration.py",
                    "models.py",
                    "runner_controller.py",
                    "runner_pools.py",
                    "runner_pool_persistence.py",
                    "runner_factory_authority.py",
                    "runner_run_liveness.py",
                    "runner_run_authority.py",
                    "runner_run_owner.py",
                    "mcp_registry.py",
                    "tool_operation_models.py",
                    "tool_operation_lease.py",
                    "tool_operations.py",
                    "model_operation_client.py",
                    "tool_persistence.py",
                    "project_access.py",
                    "runner_continuations.py",
                    "runner_continuation_authority.py",
                    "verification_dependencies.py",
                    "verification_scope.py",
                    "verification_scope_attestation.py",
                    "verification_result_reuse.py",
                    "verification_models.py",
                    "execution_environment.py",
                    "source_catalog.py",
                    "verification_source_preparation.py",
                    "secrets.py",
                    "private_identity.py",
                    "product_assistant.py",
                    "product_assistant_runtime.py",
                    "native_location_cli.py",
                    "native_location_files.py",
                    "native_location_maintenance.py",
                    "native_location_models.py",
                    "native_location_projection.py",
                    "native_location_proof.py",
                    "native_location_runtime.py",
                    "runtime_roles.py",
                    "runtime_role_persistence.py",
                    "agent_roles.py",
                    "policies.py",
                    "completed_final_epoch.py",
                    "personal_token_format.py",
                    "knowledge_platform.py",
                    "knowledge_ingestion.py",
                    "knowledge_queries.py",
                    "knowledge_native_sources.py",
                    "framework_knowledge_koinon.py",
                    "personal_token_controllers.py",
                    "personal_tokens.py",
                    "personal_token_models.py",
                    "personal_token_persistence.py",
                    "learning_projection.py",
                    "learning_native.py",
                    "learning_native_models.py",
                    "access_persistence.py",
                    "identity.py",
                    "identity_persistence.py",
                    "product_provenance.py",
                    "learning.py",
                    "learning_configuration.py",
                    "learning_models.py",
                    "learning_persistence.py",
                    "repair_proposals.py",
                    "communication.py",
                    "communication_diagnostics.py",
                    "collaboration_feedback.py",
                    "scm_conversation_models.py",
                    "collaboration.py",
                    "engine_activity.py",
                    "engine_activity_persistence.py",
                )
            },
        }
    )


def invocation_cut(run: dict[str, Any]) -> str:
    value = {
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
    if run.get("framework_knowledge_request"):
        value["framework_knowledge"] = {
            name: run.get(name)
            for name in ("framework_knowledge_request", "framework_knowledge_pin", "framework_runtime_attestation")
        }
    duration = selected_work_duration(run)
    if duration is not None:
        value["work_duration"] = duration.model_dump(mode="json")
    return digest(value)


async def workspace_cut(
    store: Any, workspace: Path, *, assert_current: Callable[[], Awaitable[None]] | None = None
) -> dict[str, str]:
    _outside_unit()
    if assert_current is not None:
        await assert_current()
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("The retained owned workspace is unavailable")
    metadata = metadata_digest(workspace)
    head = await checked(trusted_git(["git", "rev-parse", "HEAD"]), workspace, env=git_environment())
    index = git_index_identity(workspace)
    head_identity = git_head_identity(workspace)
    scanner = (
        await store.source_patch_scanner()
        if assert_current is None
        else await store.source_patch_scanner(assert_current=assert_current)
    )

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
    if assert_current is not None:
        await assert_current()
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
        self.binding: InvocationContext | None = None
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
        duration = selected_work_duration(self.run)
        if getattr(context, "work_duration", None) != duration:
            raise ValueError("Checkpoint runtime context differs from its frozen work duration")
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
            validate_binding_duration(self.run, binding)
            self.binding = binding
            from firefly_forge.invalid_read_tickets import InvalidReadExecution

            if isinstance(self.restored, InvalidReadExecution):
                self.ordinal, self.predecessor = 0, None
                self.original = None
            else:
                self.ordinal, self.predecessor = binding.ordinal, self.restored.checkpoint.digest
                self.original = OriginalCompletion(self.restored.checkpoint, self.restored.body, self.restored.consume)
        else:
            started = int(time.time() * 1000)
            fields = dict(
                invocation_id=uuid.uuid4().hex,
                ordinal=1,
                predecessor=None,
                brief=brief,
                backend=backend,
                frontend=frontend,
                round=int(self.run.get("attempt", 1)),
                grants=sorted(grants),
                contract=runtime_contract(),
                run_cut=invocation_cut(self.run),
                workspace={"path": "", "head": "", "metadata": "", "source": ""},
            )
            self.binding = invocation_binding(
                {
                    **fields,
                    "protocol": "forge-owned-checkpoint-v3",
                    "work_duration": duration.model_dump(mode="json"),
                    "work_started_at_ms": started,
                    "work_deadline_ms": None
                    if duration.timeout_seconds is None
                    else started + duration.timeout_seconds * 1000,
                }
                if duration is not None
                else {
                    **fields,
                    "deadline_ms": int((time.time() + float(settings.get("FORGE_PROVIDER_TIMEOUT", "1800"))) * 1000),
                }
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
            self.binding = invocation_binding(data)
            return data

        return self.service.scope(
            self.run, self.task, capture=capture, capture_context=capture_context, assert_ownership=self.guard
        )

    def remaining(self) -> float | None:
        assert self.binding is not None
        return binding_remaining(self.binding)

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
