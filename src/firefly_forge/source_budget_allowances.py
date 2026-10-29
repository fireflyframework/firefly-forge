# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit finite source-reader and activation allowances; original history is immutable."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pyfly.container import service

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.human_clarifications import _Link
from firefly_forge.journal import digest
from firefly_forge.source_human_readmission import SourceHumanReadmissions
from firefly_forge.store import Store

Data = dict[str, Any]
PROTOCOL: Literal["source-budget-continuation-v1"] = "source-budget-continuation-v1"


class SourceBudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    enabled: bool = False
    max_read_file_lifetime_limit: int = Field(default=10, ge=1, le=30)
    max_activation_ceiling: int = Field(default=4, ge=4, le=32)
    max_additional_activations: int = Field(default=0, ge=0, le=3)


class SourceBudgetProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    basis: Literal["known_completed_output"] | None = Field(default=None, exclude_if=lambda value: value is None)
    read_file_lifetime_limit: int = Field(ge=11, le=30)
    activation_ceiling: int = Field(ge=5, le=32)
    additional_activations: int = Field(ge=1, le=1)

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Source budget allowance requires exact version one")
        return value


class SourceBudgetAllowanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    admission_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_catalog_path: str = Field(min_length=1, max_length=4096)
    valid_for_seconds: int = Field(ge=1, le=1800)
    proposal: SourceBudgetProposal
    acknowledge_lifetime_history_preserved: Literal[True]
    acknowledge_original_commands_and_final_reserve: Literal[True]
    acknowledge_one_new_canonical_activation: Literal[True]
    acknowledge_no_replay: Literal[True]

    acknowledge_output_not_success: Literal[True] | None = Field(default=None, exclude_if=lambda value: value is None)
    acknowledge_no_exit_attestation: Literal[True] | None = Field(default=None, exclude_if=lambda value: value is None)
    acknowledge_current_source_changes: Literal[True] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    reviewed_source_digest: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None
    )
    reviewed_patch_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def explicit_output_review(self) -> SourceBudgetAllowanceInput:
        values = (
            self.acknowledge_output_not_success,
            self.acknowledge_no_exit_attestation,
            self.acknowledge_current_source_changes,
            self.reviewed_source_digest,
            self.reviewed_patch_sha256,
        )
        if self.proposal.basis == "known_completed_output":
            if any(value is not True for value in values[:3]) or any(value is None for value in values[3:]):
                raise ValueError("Known output admission requires explicit source and output review")
        elif any(value is not None for value in values):
            raise ValueError("Known output acknowledgements require their explicit admission basis")
        return self

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        return SourceBudgetProposal.exact_version(value)

    @field_validator(
        "acknowledge_lifetime_history_preserved",
        "acknowledge_original_commands_and_final_reserve",
        "acknowledge_one_new_canonical_activation",
        "acknowledge_no_replay",
        "acknowledge_output_not_success",
        "acknowledge_no_exit_attestation",
        "acknowledge_current_source_changes",
        mode="before",
    )
    @classmethod
    def explicit_consent(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicit source-budget allowance acknowledgements are required")
        return value


def checked_parameters(
    proposal: SourceBudgetProposal, policy: SourceBudgetPolicy, *, previous_attempt: int, used_allowances: int
) -> Data:
    proposal = SourceBudgetProposal.model_validate(proposal.model_dump())
    policy = SourceBudgetPolicy.model_validate(policy.model_dump())
    if not policy.enabled:
        raise ValueError("Source-budget allowances are disabled")
    if (
        type(previous_attempt) is not int
        or not 4 <= previous_attempt < 32
        or type(used_allowances) is not int
        or used_allowances < 0
        or proposal.activation_ceiling != previous_attempt + proposal.additional_activations
        or proposal.activation_ceiling > policy.max_activation_ceiling
        or proposal.read_file_lifetime_limit > policy.max_read_file_lifetime_limit
        or used_allowances + proposal.additional_activations > policy.max_additional_activations
    ):
        raise ValueError("The exact next activation exceeds its configured finite allowance")
    return proposal.model_dump(exclude={"version", "basis"})


def policy_snapshot(private: ConfigurationStore) -> Data:
    with private.lock:
        record = private._read(private.directory / "source-budget-policy.json")
    value = {
        "version": 1,
        "revision": record.get("revision"),
        "settings": SourceBudgetPolicy.model_validate(record.get("settings", {})).model_dump(),
    }
    return {**value, "digest": digest(value)}


def write_policy(private: ConfigurationStore, body: SourceBudgetPolicy) -> Data:
    body = SourceBudgetPolicy.model_validate(body.model_dump())
    with private.lock:
        private._write(
            private.directory / "source-budget-policy.json",
            {"revision": uuid.uuid4().hex, "settings": body.model_dump()},
        )
    return policy_snapshot(private)


@service
class SourceBudgetPolicies:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def _actor(self) -> Any:
        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

        actor = CURRENT_PRINCIPAL.get()
        if actor is None or not await ProjectAccess(self.store).current_administrator(actor):
            raise AccessDenied("A current native factory administrator must review source-budget policy")
        return actor

    async def get(self) -> Data:
        from firefly_forge.source_edit_continuations import _assert_source_administrator
        from firefly_forge.workspace_reconciliations import WorkspaceReconciliations

        actor = await self._actor()
        async with self.store._unit():
            await _assert_source_administrator(self.store, WorkspaceReconciliations._actor(actor))
            return policy_snapshot(self.store.configuration)

    async def configure(self, body: SourceBudgetPolicy) -> Data:
        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
        from firefly_forge.source_edit_continuations import _assert_source_administrator
        from firefly_forge.workspace_reconciliations import WorkspaceReconciliations

        body = SourceBudgetPolicy.model_validate(body.model_dump())
        actor = await self._actor()
        async with self.store._unit():
            await _assert_source_administrator(self.store, WorkspaceReconciliations._actor(actor))
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The source-budget policy administrator changed")
            return write_policy(self.store.configuration, body)


def admission_contract_digest() -> str:
    root = Path(__file__).parent
    return digest(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "source_budget_allowances.py",
                "source_budget_output.py",
                "source_tool_budget.py",
                "source_human_readmission.py",
                "source_result_readmission.py",
                "store.py",
                "native_question_owned_unit.py",
                "provider_recoveries.py",
                "workspace_reconciliations.py",
                "human_clarifications.py",
                "journal.py",
                "source_edit_continuations.py",
                "tool_broker.py",
                "engine.py",
                "source_edit_qualification.py",
                "source_snapshot.py",
                "source_diff_admission.py",
                "git_diff_artifacts.py",
                "git_diff_models.py",
                "connection_oauth.py",
                "oauth_credential_fingerprints.py",
                "secrets.py",
                "git_workspace.py",
                "sandbox.py",
                "source_index_sanitation.py",
                "workspace_reconciliation.py",
                "checkpoint_test_models.py",
                "checkpoint_test_sources.py",
                "source_catalog.py",
                "verification_source_preparation.py",
                "verification_models.py",
                "verification_handoffs.py",
                "sandbox_command_slots.py",
                "agentic_checkpoint_boundary.py",
                "checkpoint_source_identity.py",
                "agentic_runtime.py",
                "agentic_checkpoint_invocation.py",
                "agent_checkpoint_models.py",
                "agent_checkpoints.py",
                "bridge_completions.py",
                "bridge_completion_models.py",
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
                "agent_restores.py",
                "execution_requests.py",
                "execution_request_persistence.py",
                "project_snapshot.py",
                "project_models.py",
                "projects.py",
                "project_persistence.py",
                "models.py",
                "agent_roles.py",
                "project_access.py",
                "credential_text_stream.py",
                "unknown_command_acknowledgements.py",
                "unknown_command_models.py",
                "unknown_command_proof.py",
                "unknown_command_controllers.py",
                "scm_authority_models.py",
                "git_deliveries.py",
                "checkpoint_mode_adoptions.py",
                "checkpoint_mode_controllers.py",
                "checkpoint_mode_custody.py",
                "checkpoint_mode_handoff_custody.py",
                "checkpoint_mode_models.py",
                "checkpoint_mode_proof.py",
                "checkpoint_mode_runtime.py",
                "original_selection_controllers.py",
                "original_selection_models.py",
                "original_selection_proof.py",
                "original_selection_readmissions.py",
                "original_selection_runtime.py",
                "configuration.py",
                "model_profiles.py",
                "task_routing.py",
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
                "framework_knowledge.py",
                "framework_knowledge_bootstrap.py",
                "framework_knowledge_conformance.py",
                "framework_knowledge_controllers.py",
                "framework_knowledge_evidence.py",
                "framework_knowledge_generation.py",
                "framework_knowledge_models.py",
                "framework_knowledge_persistence.py",
                "framework_knowledge_requirements.py",
                "framework_knowledge_sync.py",
                "framework_knowledge_targets.py",
                "framework_knowledge_upstream.py",
                "framework_knowledge_worker.py",
                "knowledge.py",
                "knowledge_first.py",
                "learning_context.py",
                "profiles.py",
                "access_routes.py",
                "migrations.py",
                "agentic_tool_custody.py",
                "agentic_tool_results.py",
                "checkpoint_continuations.py",
                "checkpoint_continuation_proof.py",
                "checkpoint_continuation_models.py",
                "checkpoint_continuation_controllers.py",
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
                "runner_controller.py",
                "runner_pools.py",
                "runner_pool_persistence.py",
                "runner_factory_authority.py",
                "runner_pool_models.py",
                "runner_factory_models.py",
                "runner_run_liveness.py",
                "runner_run_authority.py",
                "runner_run_owner.py",
                "mcp_registry.py",
                "tool_operation_models.py",
                "tool_operation_lease.py",
                "tool_operations.py",
                "model_operation_client.py",
                "tool_persistence.py",
                "runner_continuations.py",
                "runner_continuation_authority.py",
            )
        }
    )


class SourceBudgetLink(_Link):
    protocol: Literal["source-budget-continuation-v1"]
    from_attempt: int = Field(ge=4, le=31)
    to_attempt: int = Field(ge=5, le=32)


class SourceBudgetHistoryHeader(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    correlation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["succeeded", "rejected", "abandoned"]
    tool: str = Field(pattern=r"^[a-z][a-z0-9_./-]{0,159}$")


def _paused_provider_context(run: Data, task: Data, entries: list[Data]) -> tuple[Data, Data, Data]:
    from uuid import NAMESPACE_URL, uuid5

    from firefly_forge.agent_roles import worker_feedback_model

    recovery = run.get("provider_recovery") or {}
    attempt = task.get("attempt")
    if type(attempt) is not int:
        raise ValueError("A paused source invocation requires its canonical task attempt")
    key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{attempt}").hex
    marker = run.get("source_edit_continuation") or {}
    parents = [
        e
        for e in entries
        if e["kind"] == "workspace_reconciliation_receipt"
        and e["idempotency_key"] == marker.get("request_id")
        and e["digest"] == marker.get("receipt_digest")
    ]
    dispatches = [e for e in entries if e["kind"] == "provider_dispatch_intent"]
    dispatch = [e for e in dispatches if e["idempotency_key"] == key + ":provider"]
    feedbacks = [
        e
        for e in entries
        if e["kind"] == "worker_feedback"
        and (e["payload"].get("request") or {}).get("id") == (run.get("worker_request") or {}).get("id")
    ]
    if (
        run.get("status") != "paused"
        or task.get("status") != "paused"
        or run.get("active_task") != "implement"
        or task.get("idempotency_key") != key
        or task.get("known_worker_response") is not True
        or recovery.get("status") != "consumed"
        or recovery.get("task_key") != key
        or recovery.get("attempt") != attempt
        or recovery.get("request_id") != marker.get("request_id")
        or task.get("provider_recovery_id") != recovery.get("request_id")
        or len(parents) != 1
        or len(dispatch) != 1
        or len(feedbacks) != 1
        or dispatches[-1]["digest"] != dispatch[0]["digest"]
        or recovery.get("actor") != parents[0]["payload"].get("actor")
        or parents[0]["payload"]["candidate"]["previous_task"]["attempt"] != attempt
        or dispatch[0]["payload"].get("task_key") != key
        or dispatch[0]["payload"].get("attempt") != attempt
        or dispatch[0]["payload"].get("recovery_id") != recovery.get("request_id")
        or not parents[0]["sequence"] < dispatch[0]["sequence"] < feedbacks[0]["sequence"]
        or not any(
            e["kind"] == "intent" and e["task_id"] == "implement" and e["idempotency_key"] == key for e in entries
        )
        or any(e["kind"] == "receipt" and e["idempotency_key"] == key for e in entries)
        or any(e["kind"] == "provider_failure_observation" and e["payload"].get("task_key") == key for e in entries)
        or run.get("cancel_request", {}).get("status") == "requested"
        or run.get("bridge_completion_hold")
    ):
        raise ValueError("The paused source invocation lacks its exact consumed dispatch and native question")
    feedback = feedbacks[0]
    payload = feedback["payload"]
    try:
        report = json.loads(run["summary"])
        raw = report["feedback"]
        observed = worker_feedback_model(raw).model_validate(raw).model_dump(mode="json")
    except (KeyError, TypeError, ValueError):
        raise ValueError("The paused native turn has no exact typed worker feedback") from None
    if (
        observed != payload
        or run.get("worker_feedback") != payload
        or task.get("worker_feedback") != payload
        or payload.get("role_id") != "implementer"
        or payload.get("task_id") != "implement"
        or payload.get("attempt") != attempt
        or payload.get("request") is None
    ):
        raise ValueError("The paused native turn differs from its accepted worker question")
    return parents[0], dispatch[0], feedback


def _validate_paused_provider_observation(run: Data, task: Data, entries: list[Data], review: Data) -> None:
    from datetime import datetime

    from firefly_forge.activity_models import validate_descriptor

    parent, dispatch, feedback = _paused_provider_context(run, task, entries)
    proof = review.get("paused_provider_observation")
    fields = {
        "version",
        "protocol",
        "run_id",
        "task_key",
        "attempt",
        "activation_receipt_digest",
        "dispatch_digest",
        "feedback_digest",
        "history_digest",
        "recovery_digest",
        "task_digest",
        "output",
        "digest",
    }
    if not isinstance(proof, dict) or set(proof) != fields:
        raise ValueError("A consumed source reservation requires its native paused-turn observation")
    output = proof["output"]
    required = {
        "version",
        "protocol",
        "dispatch_digest",
        "events",
        "summary_sha256",
        "usage_digest",
        "implementation_success",
        "process_exit_attested",
    }
    if (
        type(proof["version"]) is not int
        or proof["version"] != 1
        or proof["protocol"] != "native_paused_source_invocation_v1"
        or proof["run_id"] != run["id"]
        or proof["task_key"] != task["idempotency_key"]
        or proof["attempt"] != task["attempt"]
        or proof["activation_receipt_digest"] != parent["digest"]
        or proof["dispatch_digest"] != dispatch["digest"]
        or proof["feedback_digest"] != feedback["digest"]
        or proof["history_digest"] != review["history"]["digest"]
        or proof["recovery_digest"] != digest(run["provider_recovery"])
        or proof["task_digest"] != digest(task)
        or proof["digest"] != digest({k: v for k, v in proof.items() if k != "digest"})
        or not isinstance(output, dict)
        or set(output) not in (required, required | {"diagnostics"})
        or type(output.get("version")) is not int
        or output["version"] != 1
        or output.get("protocol") != "native_completed_turn_v1"
        or output.get("dispatch_digest") != dispatch["digest"]
        or output.get("implementation_success") is not False
        or output.get("process_exit_attested") is not False
        or output.get("summary_sha256") != hashlib.sha256(run["summary"].encode()).hexdigest()
        or output.get("usage_digest") != digest(cast(Data, run.get("usage")))
        or not isinstance(output.get("events"), list)
        or not 1 <= len(output["events"]) <= 256
    ):
        raise ValueError("The native paused-turn observation lost its exact task, output or history binding")
    events = output["events"]
    if any(not isinstance(event, dict) or not isinstance(event.get("created_at"), str) for event in events):
        raise ValueError("The native paused-turn event descriptor is invalid")
    if not isinstance(output.get("diagnostics", []), list) or len(output.get("diagnostics", [])) > 1:
        raise ValueError("The native paused-turn diagnostics exceed their exact known shape")
    if len({event.get("event_id") for event in events}) != len(events):
        raise ValueError("The native paused-turn observation has duplicate archived events")
    total = 0
    for event in events:
        descriptor = validate_descriptor({k: v for k, v in event.items() if k != "created_at"})
        if (
            descriptor["run_id"] != run["id"]
            or descriptor["stage"] != "stdout"
            or descriptor["status"] != "available"
            or not datetime.fromisoformat(dispatch["created_at"])
            < datetime.fromisoformat(event["created_at"])
            <= datetime.fromisoformat(feedback["created_at"])
        ):
            raise ValueError("The native paused-turn archive is outside its dispatch/question interval")
        total += descriptor["sanitized_message_bytes"]
    if total > 4 * 1024 * 1024:
        raise ValueError("The native paused-turn archive exceeds its aggregate bound")
    for notice in output.get("diagnostics", []):
        if (
            not isinstance(notice, dict)
            or set(notice) != {"kind", "feature", "event_id", "message_sha256", "frame_sha256"}
            or notice["kind"] != "codex_unstable_feature_notice_v1"
            or notice["feature"] != "skip_host_skill_discovery"
            or not any(
                e["event_id"] == notice["event_id"] and e["sanitized_message_sha256"] == notice["frame_sha256"]
                for e in events
            )
            or not isinstance(notice["message_sha256"], str)
            or len(notice["message_sha256"]) != 64
        ):
            raise ValueError("The native paused-turn diagnostic projection changed")


async def _observe_paused_provider(service: Any, run: Data, task: Data, entries: list[Data], history: Data) -> Data:
    from firefly_forge.source_result_readmission import SourceCompletedOutputReadmissions

    r = service.recoveries
    await r._settled(run, idle=True)
    parent, dispatch, feedback = _paused_provider_context(run, task, entries)
    output = await SourceCompletedOutputReadmissions(service)._observation(run, dispatch)
    proof = {
        "version": 1,
        "protocol": "native_paused_source_invocation_v1",
        "run_id": run["id"],
        "task_key": task["idempotency_key"],
        "attempt": task["attempt"],
        "activation_receipt_digest": parent["digest"],
        "dispatch_digest": dispatch["digest"],
        "feedback_digest": feedback["digest"],
        "history_digest": history["digest"],
        "recovery_digest": digest(run["provider_recovery"]),
        "task_digest": digest(task),
        "output": output,
    }
    proof["digest"] = digest(proof)
    _validate_paused_provider_observation(
        run, task, entries, {"paused_provider_observation": proof, "history": history}
    )
    await r._settled(await r.store.get_run(run["id"]), idle=True)
    return proof


def budget_continuation_link(run: Data, task: Data, entries: list[Data], actor: Data, review: Data) -> Data:
    from firefly_forge.human_clarifications import _Actor, _pair, awaiting_human_resume

    if (
        not awaiting_human_resume(run)
        or run.get("status") != "paused"
        or task.get("status") != "paused"
        or task.get("attempt") != review["previous_attempt"]
        or run.get("cancel_request", {}).get("status") == "requested"
        or run.get("provider_recovery", {}).get("status") not in {None, "completed", "consumed"}
    ):
        raise ValueError("A source allowance requires its exact answered, settled current implementation")
    if run.get("provider_recovery", {}).get("status") == "consumed":
        _validate_paused_provider_observation(run, task, entries, review)
    question = run["worker_request"]
    responses = [
        e for e in entries if e["kind"] == "worker_response" and e["payload"].get("request_id") == question["id"]
    ]
    feedback = [
        e
        for e in entries
        if e["kind"] == "worker_feedback" and (e["payload"].get("request") or {}).get("id") == question["id"]
    ]
    if len(responses) != 1 or len(feedback) != 1:
        raise ValueError("The exact immutable source question and authenticated answer are required")
    link = SourceBudgetLink(
        protocol=PROTOCOL,
        version=1,
        request_id=question["id"],
        feedback_digest=feedback[0]["digest"],
        response_digest=responses[0]["digest"],
        from_attempt=task["attempt"],
        to_attempt=review["parameters"]["activation_ceiling"],
        resuming_actor=_Actor.model_validate(actor),
    )
    if link.to_attempt != link.from_attempt + 1:
        raise ValueError("A source budget allowance permits one canonical next activation only")
    report, response = _pair(entries, link)
    assert report.request is not None
    expected = {
        **report.request.model_dump(),
        "status": "answered",
        "role_id": "implementer",
        "task_id": "implement",
        "attempt": report.attempt,
    }
    if question != expected or run.get("worker_response") != response:
        raise ValueError("The current source answer differs from its immutable records")
    if any(
        e["kind"] == "resume"
        and e["payload"].get("source_budget_continuation", {}).get("response_digest") == link.response_digest
        for e in entries
    ):
        raise ValueError("The source answer already admitted its exact budget continuation")
    return link.model_dump()


def history_snapshot(rows: list[Data]) -> Data:
    if len(rows) > 10000 or any(r.get("status") in {"pending", "unknown"} for r in rows):
        raise ValueError("Unresolved tool history cannot grant a source budget allowance")
    headers = [
        SourceBudgetHistoryHeader.model_validate(
            {
                "correlation_id": r["correlation_id"],
                "request_digest": r["request_digest"],
                "status": r["status"],
                "tool": r["admission"]["tool"],
            }
        ).model_dump()
        for r in rows
    ]
    if len({r["correlation_id"] for r in headers}) != len(headers):
        raise ValueError("The lifetime source tool history is ambiguous")
    headers.sort(key=lambda r: r["correlation_id"])
    return {"rows": headers, "digest": digest({"rows": headers})}


async def budget_review(service: Any, run: Data, proposal: SourceBudgetProposal) -> Data:
    r = service.recoveries
    entries = await r._entries(run["id"])
    grants = [
        e
        for e in entries
        if e["kind"] == "workspace_reconciliation_receipt"
        and e["payload"].get("candidate", {}).get("source_budget_readmission")
    ]
    task = r._task_view(await r.journal._task(run["id"], "implement"))
    policy = policy_snapshot(r.store.configuration)
    parameters = checked_parameters(
        proposal,
        SourceBudgetPolicy.model_validate(policy["settings"]),
        previous_attempt=task["attempt"],
        used_allowances=len(grants),
    )
    broker = r.engine.tool_broker
    if broker is None:
        raise ValueError("Source budget review requires the original durable tool broker")
    history = history_snapshot(await broker.invocations(run["id"]))
    if len(history["rows"]) >= min(30, run["tool_policy_snapshot"]["settings"]["calls_limit"]):
        raise ValueError("The original shared tool-call ceiling has no remaining capability")
    if sum(x["tool"] == "read_file" for x in history["rows"]) >= parameters["read_file_lifetime_limit"]:
        raise ValueError("The requested read_file ceiling has no additional remaining capability")
    await r.store.assert_ownership()
    return {
        "protocol": PROTOCOL,
        "policy": policy,
        "proposal": proposal.model_dump(),
        "parameters": parameters,
        "previous_attempt": task["attempt"],
        "prior_allowance_count": len(grants),
        "history": history,
        "paused_provider_observation": await _observe_paused_provider(service, run, task, entries, history)
        if run.get("provider_recovery", {}).get("status") == "consumed"
        else None,
    }


def qualified_scope_transition(
    r: Any, run: Data, old: Data, scope: Data, previous_path: str, review: Data
) -> tuple[Data, Data]:
    from firefly_forge.source_edit_filesystem_policy import build_named_permissions, source_private_roots
    from firefly_forge.source_edit_model_catalog import model_catalog_path
    from firefly_forge.source_edit_observed_reconciliation import _configuration_digest
    from firefly_forge.source_edit_qualification import (
        _profiles,
        historical_profile_seal,
        profile_seal,
        qualified_profile,
        source_budget_limits,
    )

    profile = qualified_profile(
        scope["executable"], r.store.configuration.load_snapshot(run["config_revision"])["FORGE_CODEX_MODEL"]
    )
    limits = source_budget_limits(profile)
    if any(review["parameters"][k] > limits[k] for k in ("read_file_lifetime_limit", "activation_ceiling")):
        raise ValueError("The source allowance exceeds its newly qualified native profile")
    legacy = (
        profile
        if profile_seal(profile) == old["qualification"]
        else next(
            (
                p
                for p in _profiles()
                if p.version < profile.version and historical_profile_seal(p) == old["qualification"]
            ),
            None,
        )
    )
    if legacy is None:
        raise ValueError("The source parent has no exact package-owned historical profile provenance")
    for name in (
        "executable_sha256",
        "executable_size",
        "system",
        "machine",
        "model",
        "model_catalog_sha256",
        "native_provider",
        "policy_digest",
        "runtime_versions",
        "catalog",
        "configuration_features",
        "configuration_tools",
    ):
        if getattr(legacy, name) != getattr(profile, name):
            raise ValueError("The budget profile transition cannot widen native provider or isolation controls")
    omit = {"qualification", "configuration_proof", "budget_allowance"}
    if {k: v for k, v in old.items() if k not in omit} != {k: v for k, v in scope.items() if k not in omit}:
        raise ValueError("A source budget allowance cannot change original commands, paths, tools or source scope")
    before, after = old["configuration_proof"], scope["configuration_proof"]
    if before["profile"] != old["qualification"] or after["profile"] != scope["qualification"]:
        raise ValueError("The budget transition lost its exact before and after native profiles")
    if {k: v for k, v in before.items() if k not in {"profile", "configuration_digest"}} != {
        k: v for k, v in after.items() if k not in {"profile", "configuration_digest"}
    }:
        raise ValueError("The source budget profile changed native permissions or managed controls")
    path = Path(previous_path)
    suffix = Path("resources/native/codex-0.163.0/source-only-model.json")
    if not path.is_absolute() or ".." in path.parts or path.parts[-len(suffix.parts) :] != suffix.parts:
        raise ValueError("The exact prior package-owned catalog path is required")
    permissions = build_named_permissions(
        r.engine.workspaces / run["id"], source_private_roots(r.store.configuration.directory)
    )
    if before["configuration_digest"] != _configuration_digest(legacy, permissions, previous_path) or after[
        "configuration_digest"
    ] != _configuration_digest(profile, permissions, str(model_catalog_path(profile.model))):
        raise ValueError("The reviewed native configuration transition is not exact")
    allowance = {
        "protocol": PROTOCOL,
        **review["parameters"],
        "policy_digest": review["policy"]["digest"],
        "original_calls_limit": scope["calls_limit"],
        "original_per_tool_limit": scope["per_tool_limit"],
    }
    return {**scope, "budget_allowance": allowance}, {
        "previous_profile": old["qualification"],
        "current_profile": scope["qualification"],
        "previous_catalog_path": previous_path,
        "current_catalog_path": str(model_catalog_path(profile.model)),
        "native_controls_unchanged": True,
    }


class SourceBudgetReadmissions(SourceHumanReadmissions):
    _input_model = SourceBudgetAllowanceInput
    _kind = "source_budget_readmission"
    _resume_argument = "source_budget_readmission"
    _link_effect = "source_budget_continuation"

    def __init__(self, reconciliations: Any, proposal: SourceBudgetProposal | None = None) -> None:
        super().__init__(reconciliations)
        self.proposal = SourceBudgetProposal.model_validate(proposal.model_dump()) if proposal is not None else None

    @asynccontextmanager
    async def _admission_lock(self, run_id: str, actor: Data) -> AsyncIterator[Any]:
        async with self.r.store.source_budget_admission_unit(), self.r._locked(run_id, actor) as row:
            yield row

    def _assert_review_current(self, review: Data) -> None:
        if policy_snapshot(self.r.store.configuration)["digest"] != review["budget_review"]["policy"]["digest"]:
            raise ValueError("The source budget allowance policy changed during admission")

    async def _assert_admission_current(self, run_id: str, actor: Data, review: Data) -> None:
        from firefly_forge.source_edit_continuations import _assert_source_administrator

        await _assert_source_administrator(self.r.store, actor)
        current = await self.r.store.get_run(run_id)
        await self.r._settled(current, idle=True)
        await self.r.store.assert_ownership()
        self._assert_review_current(review)

    async def _review(self, run: Data, parent_request: str, actor: Data, previous_path: str, **kwargs: Any) -> Data:
        if self.proposal is None:
            raise ValueError("An explicit finite source budget proposal is required")
        budget = await budget_review(self.service, run, self.proposal)
        return await super()._review(run, parent_request, actor, previous_path, budget=budget)

    async def candidate(
        self, run_id: str, parent_request: str, previous_catalog_path: str, proposal: SourceBudgetProposal | None = None
    ) -> Data:
        selected = proposal or self.proposal
        if selected is not None and selected.basis == "known_completed_output":
            from firefly_forge.source_budget_output import SourceBudgetOutputReadmissions

            return await SourceBudgetOutputReadmissions(self.service, selected).candidate(
                run_id, parent_request, previous_catalog_path
            )
        bound = SourceBudgetReadmissions(self.service, selected)
        return await SourceHumanReadmissions.candidate(bound, run_id, parent_request, previous_catalog_path)

    async def admit(self, run_id: str, parent_request: str, body: Any) -> Data:
        body = SourceBudgetAllowanceInput.model_validate(body.model_dump())
        if body.proposal.basis == "known_completed_output":
            from firefly_forge.source_budget_output import SourceBudgetOutputReadmissions

            return await SourceBudgetOutputReadmissions(self.service, body.proposal).admit(run_id, parent_request, body)
        return await SourceHumanReadmissions.admit(
            SourceBudgetReadmissions(self.service, body.proposal), run_id, parent_request, body
        )

    @staticmethod
    def _queued_task(review: Data) -> Data:
        from uuid import NAMESPACE_URL, uuid5

        key = uuid5(NAMESPACE_URL, f"forge:{review['run_id']}:implement:{review['link']['to_attempt']}").hex
        return {
            **copy.deepcopy(review["previous_task"]),
            "status": "queued",
            "attempt": review["link"]["to_attempt"],
            "idempotency_key": key,
        }

    @staticmethod
    def _existing(entries: list[Data], parent_request: str, request_id: str, actor: Data) -> Data | None:
        from firefly_forge.project_access import AccessDenied

        intent = next(
            (
                e
                for e in entries
                if e["kind"] == "workspace_reconciliation_intent" and e["idempotency_key"] == request_id
            ),
            None,
        )
        if intent is None:
            return None
        candidate = intent["payload"].get("candidate", {})
        proof = candidate.get("source_budget_readmission", {})
        if (
            intent["payload"].get("actor") != actor
            or proof.get("parent_marker", {}).get("request_id") != parent_request
        ):
            raise AccessDenied("The source budget allowance identity belongs to another actor or parent")
        event = next(
            (
                e
                for e in entries
                if e["kind"] == "workspace_reconciliation_receipt" and e["idempotency_key"] == request_id
            ),
            None,
        )
        if event is None:
            raise ValueError("The source budget admission outcome is unknown; its request cannot replay")
        validate_budget_parent(entries, event, actor)
        return cast(Data, copy.deepcopy(event["payload"]))


def validate_budget_parent(entries: list[Data], event: Data, actor: Data) -> Data:
    from datetime import datetime

    from firefly_forge.human_clarifications import _pair
    from firefly_forge.source_human_readmission import _ancestral_unknown, _validated_parent

    if event["payload"].get("candidate", {}).get("source_budget_readmission", {}).get("basis") is not None:
        from firefly_forge.source_budget_output import validate_output_parent

        return validate_output_parent(entries, event, actor)
    receipt, candidate = _validated_parent(entries, event, actor)
    body = SourceBudgetAllowanceInput.model_validate(receipt["human_request"])
    intent = next(e for e in entries if e["digest"] == receipt["intent_digest"])
    proof = candidate["source_budget_readmission"]
    link = SourceBudgetLink.model_validate(proof["link"])
    budget = proof["budget_review"]
    policy = budget["policy"]
    if policy["digest"] != digest({k: v for k, v in policy.items() if k != "digest"}):
        raise ValueError("The historical source budget policy snapshot changed")
    parameters = checked_parameters(
        body.proposal,
        SourceBudgetPolicy.model_validate(policy["settings"]),
        previous_attempt=link.from_attempt,
        used_allowances=budget["prior_allowance_count"],
    )
    marker = proof["parent_marker"]
    parent = next(
        (
            e
            for e in entries
            if e["kind"] == "workspace_reconciliation_receipt"
            and e["idempotency_key"] == marker["request_id"]
            and e["digest"] == marker["receipt_digest"]
        ),
        None,
    )
    resumes = [
        e
        for e in entries
        if e["kind"] == "resume"
        and intent["sequence"] < e["sequence"] < event["sequence"]
        and e["payload"].get("source_budget_continuation") == link.model_dump()
    ]
    grant_count = sum(
        e["kind"] == "workspace_reconciliation_receipt"
        and bool(e["payload"].get("candidate", {}).get("source_budget_readmission"))
        for e in entries
        if e["sequence"] < event["sequence"]
    )
    scope = candidate["source_edit"]
    if (
        parent is None
        or parent["sequence"] >= intent["sequence"]
        or len(resumes) != 1
        or body.request_id != receipt["request"]["request_id"]
        or receipt["human_request"] != intent["payload"]["human_request"]
        or receipt["expires_at_ms"] != intent["payload"]["expires_at_ms"]
        or body.candidate_digest != proof["review_digest"]
        or body.proposal.model_dump() != budget["proposal"]
        or budget["parameters"] != parameters
        or budget["prior_allowance_count"] != grant_count
        or not 0 <= grant_count < 3
        or candidate["previous_task"]["attempt"] != link.to_attempt
        or proof["previous_task"]["attempt"] != link.from_attempt
        or link.to_attempt != link.from_attempt + 1
        or link.to_attempt != parameters["activation_ceiling"]
        or candidate["previous_run_digest"] != digest(candidate["previous_run"])
        or candidate["previous_run"]["source_edit_continuation"] != marker
        or candidate["previous_run"]["worker_response"]["request_id"] != link.request_id
        or budget["history"]["digest"] != digest({"rows": budget["history"]["rows"]})
        or scope["budget_allowance"]["policy_digest"] != policy["digest"]
        or any(scope["budget_allowance"][k] != parameters[k] for k in parameters)
        or not int(datetime.fromisoformat(intent["created_at"]).timestamp() * 1000)
        < receipt["expires_at_ms"]
        <= int(datetime.fromisoformat(intent["created_at"]).timestamp() * 1000) + body.valid_for_seconds * 1000
        or int(datetime.fromisoformat(event["created_at"]).timestamp() * 1000) >= receipt["expires_at_ms"]
    ):
        raise ValueError("The source budget receipt lost its exact finite parent, answer, policy or resume")
    if candidate["previous_run"].get("provider_recovery", {}).get("status") == "consumed":
        _validate_paused_provider_observation(
            candidate["previous_run"],
            proof["previous_task"],
            [e for e in entries if e["sequence"] < intent["sequence"]],
            budget,
        )
    elif budget.get("paused_provider_observation") is not None:
        raise ValueError("A native paused-turn observation requires its consumed historical source reservation")
    _pair(entries, link)
    _ancestral_unknown(entries, parent, actor)
    return parent


def unwrap_budget_parents(entries: list[Data], event: Data, actor: Data) -> Data:
    for _ in range(3):
        if not event["payload"].get("candidate", {}).get("source_budget_readmission"):
            return event
        event = validate_budget_parent(entries, event, actor)
    if event["payload"].get("candidate", {}).get("source_budget_readmission"):
        raise ValueError("The source budget ancestry exceeds its finite additional-activation bound")
    return event


def assert_budget_resume(
    run: Data, task: Data, entries: list[Data], actor: Data, request_id: str, private: ConfigurationStore
) -> Data:
    from firefly_forge.project_access import CURRENT_PRINCIPAL
    from firefly_forge.source_edit_continuations import clock_ms
    from firefly_forge.store import identifier

    identifier(request_id)
    principal = CURRENT_PRINCIPAL.get()
    intent = next(
        (e for e in entries if e["kind"] == "workspace_reconciliation_intent" and e["idempotency_key"] == request_id),
        None,
    )
    if principal is None or intent is None:
        raise ValueError("The current native administrator and exact source budget intent are required")
    value = intent["payload"]
    candidate = value["candidate"]
    proof = candidate["source_budget_readmission"]
    expected_actor = {**actor, "identity_digest": digest({"issuer": principal.issuer, "subject": principal.subject})}
    link = budget_continuation_link(
        run, task, entries, {k: expected_actor[k] for k in ("user_id", "authentication_method")}, proof["budget_review"]
    )
    if (
        value["actor"] != expected_actor
        or candidate["previous_run_digest"] != digest(run)
        or proof["previous_task"] != task
        or proof["parent_marker"] != run.get("source_edit_continuation")
        or proof["link"] != link
        or proof["admission_contract_digest"] != admission_contract_digest()
        or candidate["digest"] != digest({k: v for k, v in candidate.items() if k != "digest"})
        or value["expires_at_ms"] <= clock_ms()
        or policy_snapshot(private).get("digest") != proof["budget_review"]["policy"]["digest"]
    ):
        raise ValueError("The source budget intent no longer binds its exact current answer and authority")
    if any(e["kind"] == "workspace_reconciliation_receipt" and e["idempotency_key"] == request_id for e in entries):
        raise ValueError("The source budget admission is already complete")
    return link


def current_budget_receipt(store: Any, run: Data, entries: list[Data]) -> Data | None:
    from firefly_forge.source_edit_continuations import clock_ms

    marker = run.get("source_edit_continuation") or {}
    event = next(
        (
            e
            for e in entries
            if e["kind"] == "workspace_reconciliation_receipt"
            and e["idempotency_key"] == marker.get("request_id")
            and e["digest"] == marker.get("receipt_digest")
        ),
        None,
    )
    if event is None or not event["payload"].get("candidate", {}).get("source_budget_readmission"):
        return None
    receipt: Data = event["payload"]
    validate_budget_parent(entries, event, receipt["actor"])
    proof = receipt["candidate"]["source_budget_readmission"]
    if (
        receipt["expires_at_ms"] <= clock_ms()
        or policy_snapshot(store.configuration)["digest"] != proof["budget_review"]["policy"]["digest"]
    ):
        raise ValueError("The current source budget allowance policy was revoked or expired")
    return receipt


def apply_current_budget_scope(store: Any, run: Data, entries: list[Data], scope: Data) -> Data:
    receipt = current_budget_receipt(store, run, entries)
    if receipt is None:
        return scope
    from firefly_forge.source_edit_qualification import qualified_profile, source_budget_limits

    profile = qualified_profile(
        scope["executable"], store.configuration.load_snapshot(run["config_revision"])["FORGE_CODEX_MODEL"]
    )
    ceiling = source_budget_limits(profile)
    allowance = receipt["candidate"]["source_edit"]["budget_allowance"]
    if any(allowance[k] > ceiling[k] for k in ("read_file_lifetime_limit", "activation_ceiling")):
        raise ValueError("The active source budget allowance exceeds its qualified profile")
    return {**scope, "budget_allowance": copy.deepcopy(allowance)}


async def effective_source_read_limits(
    store: Any, journal: Any, run: Data, rows: list[Any]
) -> tuple[dict[str, int], Data | None]:
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.source_edit_continuations import _assert_source_administrator
    from firefly_forge.source_edit_models import READ_TOOLS

    entries = [
        {**copy.deepcopy(row.data), "digest": row.digest} for row in await journal.records.find_for_run(UUID(run["id"]))
    ]
    if not journal.verify_chain(entries):
        raise ValueError("Source read allowance requires its intact native journal")
    receipt = current_budget_receipt(store, run, entries)
    limits = {name: min(10, run["tool_policy_snapshot"]["settings"]["per_tool_limit"]) for name in READ_TOOLS}
    if receipt is None:
        return limits, None
    await _assert_source_administrator(store, receipt["actor"])
    await ProjectAccess(store).authorize_execution(run)
    current = await store.get_run(run["id"])
    if current.get("source_edit_continuation") != run.get("source_edit_continuation"):
        raise ValueError("The source read allowance changed during current authority verification")
    expected = receipt["candidate"]["source_budget_readmission"]["budget_review"]["history"]["rows"]
    actual = {
        row.correlation_id: {
            "correlation_id": row.correlation_id,
            "request_digest": row.data["request_digest"],
            "status": row.status,
            "tool": row.tool,
        }
        for row in rows
    }
    if any(actual.get(row["correlation_id"]) != row for row in expected):
        raise ValueError("The original lifetime tool receipts changed after source allowance admission")
    current_budget_receipt(store, current, entries)
    await store.assert_ownership()
    allowance = receipt["candidate"]["source_edit"]["budget_allowance"]
    limits["read_file"] = allowance["read_file_lifetime_limit"]
    return limits, {
        "protocol": PROTOCOL,
        "request_id": receipt["request"]["request_id"],
        "receipt_digest": run["source_edit_continuation"]["receipt_digest"],
        "policy_digest": allowance["policy_digest"],
        "limits": limits,
    }


def feedback_binding(run: Data, task: Data, entries: list[Data], store: Any) -> Data | None:
    receipt = current_budget_receipt(store, run, entries)
    if receipt is None:
        return None
    link = receipt["candidate"]["source_budget_readmission"]["link"]
    from uuid import NAMESPACE_URL, uuid5

    expected = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{link['to_attempt']}").hex
    if (
        task["attempt"] != link["to_attempt"]
        or task.get("status") not in {"queued", "running", "paused", "done"}
        or task.get("idempotency_key") != expected
    ):
        raise ValueError("The source budget allowance is not its exact canonical current activation")
    return {
        "protocol": PROTOCOL,
        "attempt": link["to_attempt"],
        "allowance_id": receipt["request"]["request_id"],
        "allowance_digest": run["source_edit_continuation"]["receipt_digest"],
    }


async def active_feedback_binding(r: Any, run: Data) -> Data | None:
    from firefly_forge.source_edit_continuations import _assert_source_administrator

    async with r.store._unit():
        entries = await r._entries(run["id"])
        receipt = current_budget_receipt(r.store, run, entries)
        if receipt is None:
            return None
        await _assert_source_administrator(r.store, receipt["actor"])
        await r.store.assert_ownership()
        current = await r.store.get_run(run["id"])
        if current.get("source_edit_continuation") != run.get("source_edit_continuation"):
            raise ValueError("The current source budget authority changed while checking its actor")
        task = r._task_view(await r.journal._task(run["id"], "implement"))
        await r.store.assert_ownership()
        return feedback_binding(current, task, entries, r.store)


async def queued_paused_provider_successor(r: Any, run: Data, state: Data) -> bool:
    """A sealed new activation uses normal dispatch without rewriting its consumed ancestor."""
    import asyncio
    from uuid import NAMESPACE_URL, uuid5

    from firefly_forge.workspace_reconciliations import WorkspaceReconciliations

    original = WorkspaceReconciliations._original
    async with r.store._unit():
        entries = await r._entries(run["id"])
        receipt = current_budget_receipt(r.store, run, entries)
    if receipt is None:
        return False
    proof = receipt["candidate"]["source_budget_readmission"]
    known_output = proof.get("basis") == "known_completed_output"
    if not known_output and proof["budget_review"].get("paused_provider_observation") is None:
        return False
    previous = receipt["candidate"]["previous_run"].get("provider_recovery")
    link = proof["link"]
    old_attempt = previous.get("attempt") if known_output and isinstance(previous, dict) else link["from_attempt"]
    old_key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{old_attempt}").hex
    new_key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{link['to_attempt']}").hex
    async with r.store.source_budget_admission_unit(), r._locked(run["id"], receipt["actor"]) as row:
        initial = copy.deepcopy(row.data)
        task = r._task_view(await r.journal._task(run["id"], "implement"))
        entries = await r._entries(run["id"])
        if (
            initial != original(run)
            or initial.get("status") not in {"queued", "preparing"}
            or initial.get("active_task") != "implement"
            or not initial.get("resume_existing")
            or state != previous
            or state != initial.get("provider_recovery")
            or state.get("status") != "consumed"
            or state.get("task_key") != old_key
            or state.get("attempt") != old_attempt
            or task.get("status") != "queued"
            or task.get("attempt") != link["to_attempt"]
            or task.get("idempotency_key") != new_key
            or current_budget_receipt(r.store, initial, entries) != receipt
            or feedback_binding(initial, task, entries, r.store) is None
            or any(e["kind"] == "provider_dispatch_intent" and e["payload"].get("task_key") == new_key for e in entries)
        ):
            raise ValueError("The queued source budget activation lost its exact consumed predecessor")
        active = r.engine.active.get(run["id"])
        if active is not None and not active.done() and active is not asyncio.current_task():
            raise ValueError("Another task owns the source budget activation")
        # Engine has claimed preparing and may already be the scheduler's current active task.
        await r._settled(initial, idle=False)
        await r.store.assert_ownership()
        current = original(await r.store.get_run(run["id"]))
        current_task = r._task_view(await r.journal._task(run["id"], "implement"))
        current_entries = await r._entries(run["id"])
        from firefly_forge.source_edit_continuations import _assert_source_administrator

        await _assert_source_administrator(r.store, receipt["actor"])
        final_run = original(await r.store.get_run(run["id"]))
        final_task = r._task_view(await r.journal._task(run["id"], "implement"))
        await r.store.assert_ownership()
        active = r.engine.active.get(run["id"])
        if (
            current != initial
            or current_task != task
            or current_entries != entries
            or final_run != initial
            or final_task != task
            or row.data != initial
            or (active is not None and not active.done() and active is not asyncio.current_task())
            or current_budget_receipt(r.store, final_run, current_entries) != receipt
        ):
            raise ValueError("The source budget activation changed while checking its current authority")
    return True
