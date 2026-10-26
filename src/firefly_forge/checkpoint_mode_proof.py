# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact pre-dispatch reconciliation; original outcomes and grants remain immutable."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from firefly_forge.journal import Journal, digest

Data = dict[str, Any]
ADOPTION = "checkpoint_mode_adoption"
CLAIM = "checkpoint_mode_claim"
FLAGS = {"FORGE_AGENTIC_CHECKPOINTS": "required", "FORGE_BRIDGE_COMPLETION_RECOVERY": "required"}
ERRORS = {"native_agentic_checkpoint_wal_required", "bridge_completion_wal_required"}


def required_settings(settings: dict[str, str]) -> dict[str, str]:
    if settings.get("FORGE_AGENTIC_CHECKPOINTS", "disabled") not in {"disabled", "required"} or settings.get(
        "FORGE_BRIDGE_COMPLETION_RECOVERY", "legacy"
    ) not in {"legacy", "required"}:
        raise ValueError("Only supported checkpoint and completion modes can be strengthened")
    return {**settings, **FLAGS}


_HISTORY_SEAL = object()


@dataclass(frozen=True)
class PreparedClosedHistory:
    journal_digest: str
    scope_digest: str
    closures: tuple[Data, ...]
    seal: object


def prepare_closed_history(run: Data, entries: list[Data], configuration: Any) -> PreparedClosedHistory:
    from firefly_forge.bridge_completions import _outside_unit
    from firefly_forge.checkpoint_mode_custody import validate_closed_custody

    _outside_unit()
    closures = validate_closed_custody(
        entries,
        run["id"],
        configuration.load_snapshot,
        project_id=run.get("project_id"),
        repository_id=run["repository_id"],
    )
    return PreparedClosedHistory(
        entries[-1]["digest"],
        digest({k: run.get(k) for k in ("id", "project_id", "repository_id")}),
        tuple(closures),
        _HISTORY_SEAL,
    )


def prepare_adoption(
    run: Data,
    task: Data,
    entries: list[Data],
    settings: dict[str, str],
    *,
    history: PreparedClosedHistory | None = None,
) -> Data:
    if not Journal.verify_chain(entries):
        raise ValueError("Checkpoint mode adoption requires an intact native journal")
    if (
        run.get("agent") != "bridge"
        or run.get("status") != "failed"
        or run.get("error") not in ERRORS
        or run.get("active_task") != "implement"
        or run.get("implementation_pending") is not True
        or task.get("id") != "implement"
        or task.get("status") not in {"running", "needs_reconciliation"}
        or type(task.get("attempt")) is not int
        or task["attempt"] != run.get("attempt")
        or type(run.get("implementation_rounds")) is not int
        or task["attempt"] != run["implementation_rounds"] + 1
        or type(run.get("repair_budget")) is not int
        or run["repair_budget"] < 0
        or run.get("model_selection")
        or (run.get("cancel_request") or {}).get("status") == "requested"
        or run.get("bridge_completion_hold")
        or run.get("checkpoint_mode_adoption")
        or all(settings.get(k) == v for k, v in FLAGS.items())
    ):
        raise ValueError("Only the current known missing-mode pre-dispatch failure is eligible")
    required_settings(settings)
    key = task.get("idempotency_key")
    if key != uuid5(NAMESPACE_URL, "forge:" + run["id"] + ":implement:" + str(task["attempt"])).hex:
        raise ValueError("The implementation task key is not its canonical native identity")
    intents = [
        e for e in entries if e["kind"] == "intent" and e["task_id"] == "implement" and e["idempotency_key"] == key
    ]
    if len(intents) != 1 or intents[0]["payload"].get("config_revision") != run.get("config_revision"):
        raise ValueError("The original unfinished implementation intent must remain exact")
    original = intents[0]
    tail = [e for e in entries if e["sequence"] > original["sequence"]]
    recovery = None
    if task["status"] == "needs_reconciliation":
        if (
            len(tail) != 2
            or tail[-1]["kind"] != "recovery"
            or tail[-1]["task_id"] != "implement"
            or tail[-1]["idempotency_key"] != key
            or tail[-1]["payload"]
            != {"reason": "Worker stopped without a durable receipt; inspect the retained source and external target"}
        ):
            raise ValueError("Only the exact native startup recovery annotation is eligible")
        recovery = tail[-1]
        tail = tail[:-1]
    state = run.get("verification_handoff") or {}
    response = run.get("worker_response") or {}
    if (
        state.get("status") != "completed"
        or response.get("source") != "factory_verification"
        or response.get("verification_handoff_id") != state.get("id")
        or len(tail) != 1
        or tail[0]["kind"] != "verification_continuation_intent"
        or tail[0]["idempotency_key"] != str(state.get("id")) + ":coding"
    ):
        raise ValueError("The exact factory continuation intent must have no later provider or tool dispatch")
    continuation = tail[0]
    expected = {
        "id": state["id"],
        "parent_round": run["implementation_rounds"],
        "next_round": task["attempt"],
        "provider_digest": digest({"agent": "bridge", "settings": settings}),
    }
    if any(continuation["payload"].get(k) != v for k, v in expected.items()):
        raise ValueError("The original factory provider and continuation grant changed")
    has_custody = any(
        e["kind"] in {"bridge_request_intent", "agent_checkpoint", "bridge_request_response", "agent_consume"}
        for e in entries
    )
    closures: tuple[Data, ...]
    if history is None:
        if has_custody:
            raise ValueError("Historical Bridge custody must be prepared outside the native writer")
        closures = ()
    elif (
        history.seal is not _HISTORY_SEAL
        or history.journal_digest != entries[-1]["digest"]
        or history.scope_digest != digest({k: run.get(k) for k in ("id", "project_id", "repository_id")})
    ):
        raise ValueError("Prepared historical Bridge custody changed")
    else:
        closures = history.closures
    result = {
        "version": 1,
        "run_id": run["id"],
        "task_key": key,
        "attempt": task["attempt"],
        "original_config_revision": run["config_revision"],
        "original_settings_digest": digest(settings),
        "historical_custody_digest": digest({"closures": closures}),
        "required_settings_digest": digest(required_settings(settings)),
        "implementation_intent_digest": original["digest"],
        "continuation_intent_digest": continuation["digest"],
        "continuation_key": continuation["idempotency_key"],
        "provider_digest": expected["provider_digest"],
        "startup_recovery_digest": recovery["digest"] if recovery else None,
        "journal_digest": entries[-1]["digest"],
        "original_failure": run["error"],
        "frozen_budget_digest": digest(
            {k: run.get(k) for k in ("repair_budget", "tool_policy_snapshot", "sandbox_snapshot", "commands")}
        ),
        "original_task": task,
    }
    result["digest"] = digest(result)
    return result
