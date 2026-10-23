# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Causal, effect-free review cuts for a single answered clarification."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from firefly_forge.agent_roles import worker_feedback_model
from firefly_forge.journal import Journal, digest
from firefly_forge.models import RunInput
from firefly_forge.scm_authority_models import SCMRespondingActorV2
from firefly_forge.scm_poll_models import SCMRespondingActorV3

Data = dict[str, Any]
FROZEN_FIELDS = frozenset(RunInput.model_fields) | {
    "branding_snapshot",
    "design_snapshot",
    "execution_actor",
    "component_snapshot",
    "scm_snapshot",
    "repair_budget",
    "retry_count",
    "model_selection",
    "review_model_selection",
    "review_config_revision",
    "review_execution_config",
    "policy_snapshot",
    "role_snapshot",
    "role_selection_source",
    "sandbox_snapshot",
    "tool_policy_snapshot",
    "config_revision",
    "execution_config",
    "knowledge_snapshot",
    "learning_snapshot",
    "mcp_snapshot",
    "campaign_context",
    "base_revision",
    "branch",
    "source_tree",
    "checkpoint_revision",
    "git_metadata_digest",
}


def resume_cut(
    *,
    run: Data,
    plan: Data,
    tasks: list[Data],
    entries: list[Data],
    grant_digest: str,
    binding_digest: str,
    native_user_id: str,
) -> Data:
    return _resume_cut(
        run=run,
        plan=plan,
        tasks=tasks,
        entries=entries,
        grant_digest=grant_digest,
        binding_digest=binding_digest,
        native_user_id=native_user_id,
        polling=False,
    )


def polling_resume_cut(
    *,
    run: Data,
    plan: Data,
    tasks: list[Data],
    entries: list[Data],
    grant_digest: str,
    binding_digest: str,
    native_user_id: str,
) -> Data:
    return _resume_cut(
        run=run,
        plan=plan,
        tasks=tasks,
        entries=entries,
        grant_digest=grant_digest,
        binding_digest=binding_digest,
        native_user_id=native_user_id,
        polling=True,
    )


def _resume_cut(
    *,
    run: Data,
    plan: Data,
    tasks: list[Data],
    entries: list[Data],
    grant_digest: str,
    binding_digest: str,
    native_user_id: str,
    polling: bool,
) -> Data:
    """Seal this declared frozen projection; never claim that a mutable run is a receipt."""
    if (
        run.get("status") != "paused"
        or run.get("publish") is not False
        or not run.get("execution_actor")
        or run.get("publication_status") not in {None, "not_requested"}
        or bool(run.get("cancel_request"))
        or (run.get("pause") or {}).get("kind") != "worker_request"
        or (run.get("pause") or {}).get("automatic") is not False
    ):
        raise ValueError("Only an owned, answered clarification can resume without changing its execution contract")
    for key in ("id", "repository_id", "project_id"):
        if not isinstance(run.get(key), str) or not re.fullmatch(r"[a-f0-9]{32}", run[key]):
            raise ValueError("Resume requires an exact run, project and repository")
    if not run.get("component_id") or not re.fullmatch(r"[a-f0-9]{32}", native_user_id):
        raise ValueError("Resume requires a native user and component")
    if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in (grant_digest, binding_digest)):
        raise ValueError("Resume requires the exact V1 binding and V2 grant")
    if (
        not 1 <= len(entries) <= 2000
        or len(tasks) > 2000
        or len(json.dumps({"entries": entries, "plan": plan, "tasks": tasks}, ensure_ascii=False).encode())
        > 4 * 1024 * 1024
        or any(type(item.get("sequence")) is not int or item.get("run_id") != run["id"] for item in entries)
        or not Journal.verify_chain(entries)
    ):
        raise ValueError("Resume evidence is oversized, foreign or has an invalid journal chain")
    raw_plan = {key: value for key, value in plan.items() if key != "digest"}
    plans = [item for item in entries if item["kind"] == "plan"]
    if (
        digest(raw_plan) != plan.get("digest")
        or type(plan.get("version")) is not int
        or plan["version"] != 1
        or type(plan.get("revision")) is not int
        or any(
            plan.get(key) != run.get(key)
            for key in ("run_id", "repository_id", "project_id", "component_id")
            if key != "run_id"
        )
        or plan.get("run_id") != run["id"]
        or not plans
        or plans[-1]["payload"] != {"revision": plan.get("revision"), "digest": plan["digest"]}
    ):
        raise ValueError("The current plan has no matching immutable journal reference")
    if any(run.get(key) != plan[key] for key in FROZEN_FIELDS.intersection(plan)):
        raise ValueError("The mutable execution contract contradicts the original sealed plan")
    question = run.get("worker_request") or {}
    feedbacks = [item for item in entries if item["kind"] == "worker_feedback" and item["payload"].get("request")]
    responses = [item for item in entries if item["kind"] == "worker_response"]
    if not feedbacks or not responses:
        raise ValueError("Resume needs immutable question and answer records")
    feedback, response = feedbacks[-1], responses[-1]
    body = worker_feedback_model(feedback["payload"]).model_validate(feedback["payload"])
    request = body.request
    assert request is not None
    expected_question = {
        **request.model_dump(mode="json"),
        "status": "answered",
        "role_id": body.role_id,
        "task_id": body.task_id,
        "attempt": body.attempt,
    }
    attribution = response["payload"].get("responding_actor") or {}
    if (
        body.disposition != "clarification"
        or question != expected_question
        or run.get("worker_feedback") != feedback["payload"]
        or run.get("worker_response") != response["payload"]
        or response["sequence"] <= feedback["sequence"]
        or response["payload"].get("request_id") != request.id
        or feedback["task_id"] != body.task_id
        or response["task_id"] != body.task_id
        or feedback["idempotency_key"] != "worker-feedback:" + body.id
        or response["idempotency_key"] != "worker-response:" + str(response["payload"].get("id"))
        or attribution.get("user_id") != native_user_id
        or type(attribution.get("version")) is not int
        or attribution.get("version") not in ({1, 3} if polling else {1, 2})
    ):
        raise ValueError("The mutable question or answer does not match the causal, attributed journal cut")
    if polling and attribution["version"] == 3:
        polling_actor = SCMRespondingActorV3.model_validate(attribution)
        if polling_actor.provenance.binding_digest != binding_digest:
            raise ValueError("The answer belongs to a different polling delegation revision")
    elif attribution["version"] == 2:
        actor = SCMRespondingActorV2.model_validate(attribution)
        if actor.provenance.binding_digest != binding_digest:
            raise ValueError("The answer belongs to a different delegation revision")
    elif set(attribution) != {"version", "user_id", "authentication_method"} or attribution[
        "authentication_method"
    ] not in {
        "oidc_session",
        "oidc_bearer",
        "local_bootstrap",
    }:
        raise ValueError("The answer has no supported authenticated native attribution")
    planned = {item["id"] for item in plan.get("tasks", [])}
    target = next((item for item in tasks if item["task_id"] == body.task_id), None)
    if (
        body.task_id not in planned
        or target is None
        or target["status"] != "paused"
        or target["attempt"] != body.attempt
        or target["data"].get("known_worker_response") is not True
        or any(item["task_id"] != body.task_id and item["status"] not in {"queued", "done"} for item in tasks)
        or any(item["task_id"] not in planned and item["status"] != "done" for item in tasks)
    ):
        raise ValueError("The current task attempt or another effect remains unresolved")
    completions = {
        "intent": {"receipt"},
        "tool_intent": {"tool_receipt"},
        "sandbox_intent": {"sandbox_receipt", "sandbox_reconcile"},
    }
    terminal_sequences = {(item["kind"], item["idempotency_key"]): item["sequence"] for item in entries}
    stage_key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:{body.task_id}:{body.attempt}").hex
    for item in entries:
        terminal = completions.get(item["kind"])
        if terminal is None:
            continue
        closed = any(terminal_sequences.get((kind, item["idempotency_key"]), 0) > item["sequence"] for kind in terminal)
        known_paused_stage = (
            item["kind"] == "intent"
            and item["task_id"] == body.task_id
            and item["sequence"] < feedback["sequence"]
            and item["idempotency_key"] == target["data"].get("idempotency_key") == stage_key
        )
        if not closed and not known_paused_stage:
            raise ValueError("An external or unacknowledged effect remains unknown")
    cut = {
        "version": 2,
        "run_id": run["id"],
        "repository_id": run["repository_id"],
        "project_id": run["project_id"],
        "component_id": run["component_id"],
        "question_id": request.id,
        "feedback_sequence": feedback["sequence"],
        "feedback_digest": feedback["digest"],
        "response_sequence": response["sequence"],
        "response_digest": response["digest"],
        "plan_revision": plan["revision"],
        "plan_digest": plan["digest"],
        "frozen_run_digest": digest({key: run.get(key) for key in sorted(FROZEN_FIELDS)}),
        "journal_head": entries[-1]["digest"],
        "journal_event_count": len(entries),
        "grant_digest": grant_digest,
        "binding_digest": binding_digest,
    }
    return {**cut, "digest": digest(cut)}
