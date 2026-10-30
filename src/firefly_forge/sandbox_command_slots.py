# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Allocate a new command identity and its create WAL atomically under the run writer."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from firefly_forge.journal import digest
from firefly_forge.project_access import ProjectAccess
from firefly_forge.runner_factory_models import validate_factory_pool
from firefly_forge.runner_pool_models import pool_digest
from firefly_forge.sandbox_models import capture_sandbox, validate_sandbox_snapshot

if TYPE_CHECKING:
    from firefly_forge.journal import Journal

Data = dict[str, Any]
COMMAND_CONTRACT = "forge-command-ordinal/v2"


def assert_command_boundary() -> None:
    from pyfly.data.transaction import current_unit_of_work

    if current_unit_of_work() is not None:
        raise ValueError("Command allocation and dispatch require no ambient transaction")


def _refuse() -> None:
    from firefly_forge.sandbox import SandboxReconciliationRequired

    raise SandboxReconciliationRequired("Durable command identity or effect requires reconciliation")


def _canonical(row: Any, entries: list[Data], run_id: str) -> Data:
    identity = uuid5(NAMESPACE_URL, f"forge:sandbox:{run_id}:{row.task_id}:{row.parent_attempt}:{row.ordinal}").hex
    if (
        row.run_id.hex != run_id
        or row.task_id not in {"implement", "verify"}
        or type(row.parent_attempt) is not int
        or not 1 <= row.parent_attempt <= 100
        or type(row.ordinal) is not int
        or not 1 <= row.ordinal <= 100
        or row.id.hex != identity
        or row.container_name not in {"forge-sandbox-" + identity, "pool-" + identity}
        or set(row.data) != {"request", "effects"}
    ):
        _refuse()
    request, effects = row.data["request"], row.data["effects"]
    if (
        not isinstance(request, dict)
        or not isinstance(effects, dict)
        or "create" not in effects
        or set(effects) - {"create", "start", "cleanup"}
        or type(request.get("parent_attempt")) is not int
        or type(request.get("ordinal")) is not int
        or request.get("executor_contract", COMMAND_CONTRACT) != COMMAND_CONTRACT
        or request.get("parent_attempt") != row.parent_attempt
        or request.get("ordinal") != row.ordinal
        or request.get("container_name") != row.container_name
        or request.get("snapshot_digest") != row.snapshot_digest
        or not isinstance(request.get("request"), dict)
        or request.get("request_digest")
        != (pool_digest(request["request"]) if row.container_name.startswith("pool-") else digest(request["request"]))
    ):
        _refuse()
    for effect, state in effects.items():
        intents = [
            entry
            for entry in entries
            if entry["kind"] == "sandbox_intent"
            and entry["payload"].get("execution_id") == identity
            and entry["payload"].get("effect") == effect
        ]
        outcomes = [
            entry
            for entry in entries
            if entry["kind"] in {"sandbox_receipt", "sandbox_reconcile"}
            and entry["payload"].get("execution_id") == identity
            and entry["payload"].get("effect") == effect
        ]
        if (
            not isinstance(state, dict)
            or state.get("status") not in {"intent", "done"}
            or len(intents) != 1
            or intents[0]["task_id"] != row.task_id
            or intents[0]["payload"].get("request") != request
        ):
            _refuse()
        if state["status"] == "intent":
            if outcomes:
                _refuse()
            continue
        evidence = copy.deepcopy(state.get("evidence"))
        if not isinstance(evidence, dict) or len(outcomes) != 1 or outcomes[0]["sequence"] <= intents[0]["sequence"]:
            _refuse()
        if isinstance(evidence.get("output"), str):
            output = evidence.pop("output")
            evidence.update(
                output_sha256=hashlib.sha256(output.encode()).hexdigest(),
                output_bytes=len(output.encode()),
                output_excerpt=output[:2000],
            )
        if outcomes[0]["payload"].get("evidence") != evidence or outcomes[0]["task_id"] != row.task_id:
            _refuse()
    related = [
        entry
        for entry in entries
        if entry["kind"] in {"sandbox_intent", "sandbox_receipt", "sandbox_reconcile"}
        and entry["payload"].get("execution_id") == identity
    ]
    if {entry["payload"].get("effect") for entry in related if entry["kind"] == "sandbox_intent"} != set(effects):
        _refuse()
    last = related[-1]
    expected_status = (
        {"create": "creating", "start": "running", "cleanup": "cleaning"}
        if last["kind"] == "sandbox_intent"
        else {"create": "created", "start": "finished", "cleanup": "removed"}
    )[last["payload"]["effect"]]
    if last["payload"].get("evidence", {}).get("outcome") == "interrupted":
        expected_status = "interrupted"
    if row.status != expected_status:
        _refuse()
    if row.container_name.startswith("pool-"):
        from firefly_forge.runner_pool_models import RunnerProjectInvocation

        invocation = RunnerProjectInvocation.model_validate(request["request"])
        if invocation.execution_id != identity or invocation.run_id.hex != run_id:
            _refuse()
    return {"execution_id": identity, "request": copy.deepcopy(request), "effects": copy.deepcopy(effects)}


def _complete(row: Any, value: Data) -> bool:
    effects = value["effects"]
    start = effects.get("start", {})
    evidence = start.get("evidence", {})
    return (
        row.status == "removed"
        and set(effects) == {"create", "start", "cleanup"}
        and all(
            state.get("status") == "done" and state.get("evidence", {}).get("outcome") != "interrupted"
            for state in effects.values()
        )
        and type(evidence.get("exit_code")) is int
        and isinstance(evidence.get("output"), str)
        and evidence.get("workspace_import") != "requires_reconciliation"
        and (not row.container_name.startswith("pool-") or evidence.get("workspace_import") == "complete")
    )


def _recovered(row: Any, value: Data, entries: list[Data], tasks: list[Any]) -> bool:
    effects = value["effects"]
    if (
        not {"create", "cleanup"} <= set(effects)
        or any(state.get("status") != "done" for state in effects.values())
        or any(
            state.get("evidence", {}).get("workspace_import") == "requires_reconciliation" for state in effects.values()
        )
        or (
            "start" in effects
            and not any(state.get("evidence", {}).get("outcome") == "interrupted" for state in effects.values())
        )
    ):
        return False
    for effect, state in effects.items():
        if state.get("evidence", {}).get("outcome") == "interrupted" and not any(
            entry["kind"] == "sandbox_reconcile"
            and entry["payload"].get("execution_id") == row.id.hex
            and entry["payload"].get("effect") == effect
            for entry in entries
        ):
            return False
    task = next((item for item in tasks if item.task_id == row.task_id), None)
    if task is None or task.attempt <= row.parent_attempt:
        return False
    key = uuid5(NAMESPACE_URL, f"forge:{row.run_id.hex}:{row.task_id}:{row.parent_attempt}").hex
    last_effect = max(
        entry["sequence"]
        for entry in entries
        if entry["kind"] in {"sandbox_intent", "sandbox_receipt", "sandbox_reconcile"}
        and entry["payload"].get("execution_id") == row.id.hex
    )
    return any(
        entry["kind"] == "reconciliation"
        and entry["task_id"] == row.task_id
        and entry["idempotency_key"] == key
        and entry["payload"].get("outcome") == "retry"
        and entry["payload"].get("evidence")
        and entry["sequence"] > last_effect
        for entry in entries
    )


async def command_intent(
    journal: Journal,
    run_id: str,
    task_id: str,
    attempt: int,
    snapshot: Data,
    payload: Callable[[int, str, Data | None], Data],
    *,
    ordinal: int | None = None,
    replay_only: bool = False,
) -> Data:
    assert_command_boundary()
    from firefly_forge.runner_monitor_custody import monitor_admits
    from firefly_forge.unknown_command_acknowledgements import policy_digest as acknowledgement_policy_digest
    from firefly_forge.unknown_command_proof import acknowledged_continuation, acknowledged_followup

    acknowledgement_policy = acknowledgement_policy_digest()
    if task_id not in {"implement", "verify"} or type(attempt) is not int or not 1 <= attempt <= 100:
        raise ValueError("Command allocation requires an exact native task attempt")
    policy = validate_factory_pool(snapshot) if snapshot.get("mode") == "pool" else validate_sandbox_snapshot(snapshot)
    if ordinal is not None and (type(ordinal) is not int or not 1 <= ordinal <= policy.command_limit):
        raise ValueError("Frozen command budget is exhausted")
    await journal.store.assert_ownership()
    async with journal.store._unit():
        run = await journal._run(run_id)
        task = await journal._task(run_id, task_id)
        current = {**copy.deepcopy(run.data), "id": run.id.hex}
        if (run.data.get("sandbox_snapshot") or capture_sandbox(None)) != snapshot or task.attempt != attempt:
            raise ValueError("Frozen command configuration or task attempt changed")
        if task.status != "running":
            raise ValueError("Command allocation requires the active native stage")
        if run.data.get("cancel_request", {}).get("status") == "requested":
            import asyncio

            raise asyncio.CancelledError()
        await ProjectAccess(journal.store).authorize_execution(current)
        entries = [
            {**copy.deepcopy(row.data), "digest": row.digest} for row in await journal.records.find_for_run(run.id)
        ]
        if len(entries) > 10000 or len(json.dumps(entries)) > 16 * 1024 * 1024 or not journal.verify_chain(entries):
            _refuse()
        rows = await journal.sandboxes.for_run(run.id)
        if len(rows) > 100:
            _refuse()
        retained_ids = {
            entry["payload"].get("execution_id")
            for entry in entries
            if entry["kind"] in {"sandbox_intent", "sandbox_receipt", "sandbox_reconcile"}
        }
        if retained_ids != {row.id.hex for row in rows}:
            _refuse()
        values = {row.id.hex: _canonical(row, entries, run_id) for row in rows}
        if ordinal is None:
            ordinal = 1 + max(
                (row.ordinal for row in rows if row.task_id == task_id and row.parent_attempt == attempt), default=0
            )
        if ordinal > policy.command_limit:
            raise ValueError("Frozen command budget is exhausted")
        execution_id = uuid5(NAMESPACE_URL, f"forge:sandbox:{run_id}:{task_id}:{attempt}:{ordinal}").hex
        existing = values.get(execution_id)
        if existing is None:
            tasks = await journal.tasks.find_for_run(run.id)
            for row in rows:
                if (
                    _complete(row, values[row.id.hex])
                    or _recovered(row, values[row.id.hex], entries, tasks)
                    or monitor_admits(row, values[row.id.hex], entries)
                ):
                    continue
                actor = acknowledged_followup(
                    row, values[row.id.hex], entries, current, ordinal, acknowledgement_policy, rows=rows, values=values
                )
                if actor is not None:
                    if task_id != "implement" or task.data.get("verification_handoff") != current.get(
                        "verification_handoff", {}
                    ).get("id"):
                        _refuse()
                else:
                    actor = acknowledged_continuation(
                        row, values, rows, entries, current, tasks, task_id, acknowledgement_policy
                    )
                    if actor is None:
                        _refuse()
                await ProjectAccess(journal.store).authorize_actor(actor, current.get("project_id"), "execute")
                from firefly_forge.verification_models import check_verification_authority

                intent = next(
                    entry["payload"]
                    for entry in entries
                    if entry["kind"] == "verification_handoff_intent"
                    and entry["payload"].get("id") == current["verification_handoff"]["id"]
                )
                check_verification_authority(journal.store, current, intent)
                if current["verification_handoff"]["status"] == "completed":
                    from firefly_forge.verification_models import verification_provider_current

                    if not verification_provider_current(journal.store, current, intent):
                        _refuse()
            if len(rows) >= policy.command_limit:
                raise ValueError("Frozen total command budget is exhausted; attempts do not reset it")
        if replay_only and existing is None:
            return {"disposition": "absent"}
        request = payload(ordinal, execution_id, existing)
        if existing is None and request.get("request", {}).get("browser"):
            browser = getattr(policy, "browser", None)
            if (
                browser is None
                or sum(bool(value["request"]["request"].get("browser")) for value in values.values())
                >= browser.max_calls
            ):
                raise ValueError("Frozen browser call budget is exhausted")
        if existing is None:
            request = {**request, "executor_contract": COMMAND_CONTRACT}
        inner = request.get("request")
        prefix = "pool-" if snapshot.get("mode") == "pool" else "forge-sandbox-"
        if (
            type(request.get("parent_attempt")) is not int
            or type(request.get("ordinal")) is not int
            or request.get("parent_attempt") != attempt
            or request.get("ordinal") != ordinal
            or request.get("snapshot_digest") != snapshot["digest"]
            or request.get("container_name") != prefix + execution_id
            or not isinstance(inner, dict)
            or request.get("request_digest") != (pool_digest(inner) if prefix == "pool-" else digest(inner))
        ):
            raise ValueError("Command payload differs from its canonical allocated slot")
        if current.get("source_edit_continuation"):
            from firefly_forge.source_edit_continuations import assert_source_final_budget

            await assert_source_final_budget(journal.store, journal, current, task_id, execution_id)
        await journal.store.assert_ownership()
        result = await journal.sandbox_effect_intent(run_id, task_id, execution_id, effect="create", payload=request)
        return {**result, "fresh": existing is None}
