# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Same-run proposal limits from native verification facts, never inferred negative lessons."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from firefly_forge.journal import Journal, digest
from firefly_forge.learning_configuration import frozen_learning
from firefly_forge.learning_context import observe_context
from firefly_forge.learning_models import LearningContext, LearningPolicy, repeat_decision
from firefly_forge.models import Command
from firefly_forge.project_access import ProjectAccess

Data = dict[str, Any]


def failure_fingerprint(checks: list[Data]) -> str | None:
    if not checks or any(
        type(check.get("exit_code")) is not int or check["exit_code"] not in {0, 1} for check in checks
    ):
        return None
    failures: list[Data] = []
    for check in checks:
        if check["exit_code"] != 1:
            continue
        command = Command.model_validate({"argv": check["argv"]})
        output = check.get("output")
        if not isinstance(output, str) or len(output) > 32000:
            return None
        failures.append(
            {"argv": command.argv, "exit_code": 1, "output_digest": hashlib.sha256(output.encode()).hexdigest()}
        )
    return digest({"failed_checks": sorted(failures, key=digest)}) if failures else None


async def verification_context(run: Data, source: Data, workspace: Path) -> Data | None:
    try:
        observed = await observe_context(run, source, workspace, run["brief"], immutable_source=True)
    except ValueError:
        # Bounded declaration evidence is optional; actual QA must still retain its native receipt.
        return None
    return observed.model_dump(mode="json")


async def repair_proposal(
    journal: Journal, run_id: str, observed: Data | None, next_attempt: int, *, source_tree: str
) -> Data:
    """Read Git before entry; serialize policy, native receipt count and decision under the run lock."""
    context = LearningContext.model_validate(observed) if observed is not None else None
    async with journal.store._unit():
        row = await journal._run(run_id)
        run = row.data
        await ProjectAccess(journal.store).authorize_execution(run)
        snapshot = frozen_learning(run.get("learning_snapshot"))
        policy = LearningPolicy.model_validate(snapshot["settings"])
        plan = await journal.plan(run_id)
        if plan.get("learning_snapshot") != run.get("learning_snapshot"):
            raise ValueError("Frozen repair policy differs from the admitted plan")
        entries = await journal.entries(run_id)
        if not Journal.verify_chain(entries):
            raise ValueError("Repair proposals require an intact native verification chain")
        if any(item["status"] in {"running", "needs_reconciliation"} for item in plan["tasks"]):
            raise ValueError("A prior journal effect needs reconciliation before a repair proposal")
        receipts = [item for item in entries if item["kind"] == "receipt" and item["task_id"] == "verify"]
        if not receipts:
            raise ValueError("Repair proposals require a native verification receipt")
        last = receipts[-1]
        payload = last["payload"]
        fingerprint = failure_fingerprint(payload.get("checks", []))
        expected = payload.get("repair_context")
        if context is not None and (context.repository_id, context.project_id, context.component_id) != (
            row.repository_id.hex,
            run.get("project_id"),
            run.get("component_id"),
        ):
            raise ValueError("Repair proposal context differs from the admitted source scope")
        reason = None
        if not policy.enabled:
            reason = "scope_denied"
        elif context is None:
            reason = "missing_receipts"
        elif context.source_tree != source_tree:
            raise ValueError("Repair context differs from the currently inspected source")
        elif context.version == "unknown":
            reason = "version_drift"
        elif not fingerprint or not expected:
            reason = "missing_receipts"
        elif expected != observed or payload.get("source_tree") != source_tree:
            reason = "context_drift"
        rounds = (
            {
                item["payload"]["implementation_round"]
                for item in receipts
                if type(item["payload"].get("implementation_round")) is int
                and 1 <= item["payload"]["implementation_round"] < next_attempt
                and item["payload"].get("repair_context") == observed
                and item["payload"].get("source_tree") == source_tree
                and failure_fingerprint(item["payload"].get("checks", [])) == fingerprint
            }
            if reason is None
            else set()
        )
        decision = repeat_decision(
            "repair_generation",
            identical_verified_failures=len(rounds),
            max_repeated_attempts=min(2, policy.max_repeated_attempts),
        )
        allowed = reason is not None or decision.allowed
        summary = (
            "The frozen identical-failure repair proposal cap is exhausted; independent QA receipts are retained."
            if not allowed
            else "Another repair proposal is available within the existing safety budgets."
            if reason is None
            else (
                "The receipt-backed repeat cap does not apply; "
                "existing repair and reconciliation controls remain active."
            )
        )
        result = {
            "status": "held" if not allowed else "available" if reason is None else "not_applied",
            "summary": summary,
            "allowed": allowed,
            "reason": reason or decision.reason,
            "verified_failures": len(rounds),
            "effective_limit": min(2, policy.max_repeated_attempts),
            "next_attempt": next_attempt,
            "context": observed,
            "source_tree": source_tree,
            "fingerprint": fingerprint,
            "policy_digest": snapshot["digest"],
            "verification_receipt": {"digest": last["digest"], "sequence": last["sequence"]},
        }
        key = uuid.uuid5(
            uuid.NAMESPACE_URL,
            "forge-repair-proposal:"
            + digest(
                {
                    "run_id": run_id,
                    "receipt": last["digest"],
                    "next_attempt": next_attempt,
                    "context": observed,
                    "source_tree": source_tree,
                    "policy": snapshot["digest"],
                }
            ),
        ).hex
        event = await journal._append(run_id, "implement", key, "repair_proposal", result)
        if event["payload"] != result:
            raise ValueError("Retained repair decision differs from current verified evidence")
        if not allowed:
            await journal.store.update(run_id, repair_halt=summary)
        return dict(event["payload"])
