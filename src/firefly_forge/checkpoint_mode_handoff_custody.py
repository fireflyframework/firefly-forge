# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pure canonical custody for a completed verification and its native coding grant."""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from firefly_forge.agent_roles import WorkerResponse
from firefly_forge.checkpoint_work_duration import selected_work_duration
from firefly_forge.journal import Journal, digest
from firefly_forge.models import Command, NewWorkCommand, configured_commands
from firefly_forge.sandbox import SandboxReconciliationRequired
from firefly_forge.sandbox_command_slots import _canonical, _complete
from firefly_forge.sandbox_models import SandboxInput, validate_sandbox_snapshot
from firefly_forge.verification_models import verification_scope
from firefly_forge.work_duration_models import effective_work_timeout

Data = dict[str, Any]


def verification_commands(
    run: Data, sandbox: SandboxInput
) -> tuple[list[Command | NewWorkCommand], list[int | None], int]:
    policy = selected_work_duration(run)
    commands = configured_commands(run)
    timeouts: list[int | None] = []
    for command in commands:
        if policy is None:
            if command.timeout is None:
                raise ValueError("Legacy verification requires its original finite command timeout")
            timeouts.append(min(command.timeout, sandbox.timeout))
        else:
            timeouts.append(effective_work_timeout(command.timeout, policy.timeout_seconds))
    return commands, timeouts, 1 if policy is None else 2


def _one(entries: list[Data], kind: str, key: str, handoff_id: str | None = None) -> Data:
    found = [
        e
        for e in entries
        if e["kind"] == kind
        and (e["idempotency_key"] == key or (handoff_id is not None and e["payload"].get("id") == handoff_id))
    ]
    if len(found) != 1 or found[0]["idempotency_key"] != key or found[0]["task_id"] != "implement":
        raise ValueError("Completed verification has no unique canonical producer record")
    return found[0]


def _validate(
    run: Data, entries: list[Data], rows: list[Any], values: dict[str, Data], *, before_dispatch: bool = False
) -> dict[str, str]:
    if not entries or any(e["run_id"] != run["id"] for e in entries) or not Journal.verify_chain(entries):
        raise ValueError("Completed verification requires its intact native journal")
    state, request, response = (
        run["verification_handoff"],
        run["worker_request"],
        run["worker_response"],
    )
    handoff_id = state["id"]
    handoff = _one(entries, "verification_handoff_intent", handoff_id, handoff_id)
    intent = handoff["payload"]
    sandbox = validate_sandbox_snapshot(run["sandbox_snapshot"])
    commands, expected_timeouts, version = verification_commands(run, sandbox)
    total = len(commands)
    ordinals, timeouts = intent["ordinals"], intent["timeouts"]
    if (
        not 1 <= total <= 20
        or type(intent["commands_count"]) is not int
        or intent["commands_count"] != total
        or type(intent["parent_attempt"]) is not int
        or not 1 <= intent["parent_attempt"] < 4
        or type(intent["parent_round"]) is not int
        or intent["parent_round"] < 0
        or type(intent["continuation"]) is not int
        or type(intent["max_continuations"]) is not int
        or not 1 <= intent["continuation"] <= intent["max_continuations"]
        or len(ordinals) != total
        or any(type(v) is not int for v in ordinals)
        or ordinals != list(range(ordinals[0], ordinals[0] + total))
        or not 1 <= ordinals[0] <= ordinals[-1] <= sandbox.command_limit
        or timeouts != expected_timeouts
        or any(type(v) is not int and not (version == 2 and v is None) for v in timeouts)
        or intent["version"] != version
        or type(intent["version"]) is not int
        or intent["commands_digest"] != digest({"commands": run["commands"]})
        or intent["sandbox_digest"] != run["sandbox_snapshot"]["digest"]
        or intent["scope_digest"] != verification_scope(run)
        or intent.get("agent", run["agent"]) != run["agent"]
        or request.get("kind") != "verification"
        or request.get("task_id") != "implement"
        or request.get("status") != "answered"
        or request.get("response_source") != "factory_verification"
        or request.get("id") != intent["request_id"]
        or request.get("attempt") != intent["parent_attempt"]
        or digest(
            {
                **{k: v for k, v in request.items() if k != "response_source"},
                "status": "pending",
            }
        )
        != intent["request_digest"]
    ):
        raise ValueError("Completed verification differs from its frozen request, commands or scope")
    answer = _one(entries, "worker_response", "worker-response:" + handoff_id, handoff_id)
    outcome = _one(entries, "verification_handoff_outcome", handoff_id + ":outcome", handoff_id)
    resumed = [
        e
        for e in entries
        if e["kind"] in {"resume", "handoff"} and e["payload"].get("verification_handoff_id") == handoff_id
    ]
    if len(resumed) != 1:
        raise ValueError("Completed verification requires its unique native resume")
    resume = resumed[0]
    task_key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{intent['parent_round'] + 1}").hex
    coding = implementation = None
    if before_dispatch:
        if entries[-1]["digest"] != resume["digest"] or any(
            (e["kind"] == "intent" and e["idempotency_key"] == task_key)
            or (e["kind"] == "verification_continuation_intent" and e["payload"].get("id") == handoff_id)
            for e in entries
        ):
            raise ValueError("Pre-dispatch verification cannot contain later execution or admission")
    else:
        coding = _one(entries, "verification_continuation_intent", handoff_id + ":coding", handoff_id)
        implementation = _one(entries, "intent", task_key)
    responding_actor = {
        "version": 4,
        "authentication_method": "factory_verification",
        "handoff_id": handoff_id,
        **{
            k: intent[k]
            for k in (
                "source_digest",
                "commands_digest",
                "policy_digest",
                "provider_digest",
            )
        },
    }
    if (
        digest(answer["payload"]) != digest(response)
        or response.get("id") != handoff_id
        or response.get("request_id") != intent["request_id"]
        or response.get("source") != "factory_verification"
        or response.get("verification_handoff_id") != handoff_id
        or response.get("actor") != "Forge verification"
        or digest(response.get("responding_actor")) != digest(responding_actor)
        or WorkerResponse.model_validate({k: response[k] for k in ("id", "actor", "answer")}).answer
        != response["answer"]
        or resume["payload"].get("automatic") is not True
        or resume["payload"].get("agent") != run["agent"]
        or resume["task_id"] != resume["kind"]
        or resume["payload"].get("config_revision") != run["config_revision"]
        or not handoff["sequence"] < answer["sequence"] < outcome["sequence"] < resume["sequence"]
    ):
        raise ValueError("Completed verification lacks its exact response and native continuation grant")
    if not before_dispatch:
        assert implementation is not None and coding is not None
        if (
            implementation["payload"].get("config_revision") != run["config_revision"]
            or digest(coding["payload"])
            != digest(
                {
                    "version": 1,
                    "id": handoff_id,
                    "request_id": intent["request_id"],
                    "parent_round": intent["parent_round"],
                    "next_round": intent["parent_round"] + 1,
                    "provider_digest": intent["provider_digest"],
                }
            )
            or not resume["sequence"] < implementation["sequence"] < coding["sequence"]
        ):
            raise ValueError("Completed verification lacks its exact response and native continuation grant")
    command_records = [
        e for e in entries if e["kind"] == "verification_handoff_command" and e["payload"].get("id") == handoff_id
    ]
    if len(command_records) != total:
        raise ValueError("Every configured verification command needs one canonical receipt")
    refs, failures = [], 0
    for index, ordinal in enumerate(ordinals):
        matches = [
            r
            for r in rows
            if r.task_id == "implement" and r.parent_attempt == intent["parent_attempt"] and r.ordinal == ordinal
        ]
        if len(matches) != 1:
            raise ValueError("Verification command lost its unique native execution")
        row = matches[0]
        value = _canonical(row, entries, run["id"])
        if digest({"value": value}) != digest({"value": values.get(row.id.hex)}) or not _complete(row, value):
            raise ValueError("Verification requires canonical known completion and cleanup")
        frozen = value["request"]
        invocation = frozen["request"]
        from firefly_forge.verification_scope_attestation import mapped_argv, scope_record

        scope = scope_record(run, intent, entries)
        if (
            frozen["snapshot_digest"] != intent["sandbox_digest"]
            or invocation.get("argv") != mapped_argv(run, intent, entries, index)
            or invocation.get("timeout") != timeouts[index]
            or invocation.get("workspace") != intent["workspace"]
            or invocation.get("cwd") != intent["cwd"]
        ):
            raise ValueError("Verification command differs from the configured invocation")
        effects = []
        for effect in ("create", "start", "cleanup"):
            pair = [
                e
                for e in entries
                if e["kind"] in {"sandbox_intent", "sandbox_receipt", "sandbox_reconcile"}
                and e["payload"].get("execution_id") == row.id.hex
                and e["payload"].get("effect") == effect
            ]
            if len(pair) != 2 or pair[0]["kind"] != "sandbox_intent" or pair[1]["kind"] == "sandbox_intent":
                raise ValueError("Verification command effect custody is incomplete")
            effects.extend(pair)
        if scope is not None:
            witness = next(
                e
                for e in entries
                if e["kind"] == "verification_build_scope"
                and e["payload"]["handoff_intent_digest"] == handoff["digest"]
            )
            if witness["sequence"] >= effects[0]["sequence"]:
                raise ValueError("Build scope cannot be retrofitted after a command started")
        record = _one(entries, "verification_handoff_command", handoff_id + ":" + str(index))
        exit_code = value["effects"]["start"]["evidence"]["exit_code"]
        if (
            digest(record["payload"])
            != digest(
                {
                    "version": 1,
                    "id": handoff_id,
                    "request_id": intent["request_id"],
                    "command_index": index + 1,
                    "ordinal": ordinal,
                    "execution_id": row.id.hex,
                    "exit_code": exit_code,
                    "outcome": "completed",
                }
            )
            or not handoff["sequence"] < effects[0]["sequence"]
            or not all(a["sequence"] < b["sequence"] for a, b in zip(effects, effects[1:], strict=False))
            or not effects[-1]["sequence"] < record["sequence"] < answer["sequence"]
        ):
            raise ValueError("Verification command receipt or effect order changed")
        failures += exit_code != 0
        refs.append(
            {
                "execution_digest": digest(value),
                "record_digest": record["digest"],
                "effect_digests": [e["digest"] for e in effects],
            }
        )
    expected_state = {
        **{
            k: intent[k]
            for k in (
                "version",
                "id",
                "request_id",
                "source_digest",
                "commands_digest",
                "policy_digest",
                "provider_digest",
                "continuation",
                "max_continuations",
                "commands_count",
            )
        },
        "status": "completed",
        "completed_commands": total,
        "failed_commands": failures,
    }
    if (
        digest(state) != digest(expected_state)
        or digest(outcome["payload"]) != digest(state)
        or any(type(state[k]) is not int for k in ("commands_count", "completed_commands", "failed_commands"))
    ):
        raise ValueError("Completed verification projection differs from its canonical outcome")
    result = {
        "handoff_intent_digest": handoff["digest"],
        "response_digest": answer["digest"],
        "outcome_digest": outcome["digest"],
        "resume_digest": resume["digest"],
        **(
            {"implementation_intent_digest": implementation["digest"], "continuation_intent_digest": coding["digest"]}
            if implementation is not None and coding is not None
            else {}
        ),
        "command_custody_digest": digest({"commands": refs}),
    }
    return {**result, "digest": digest(result)}


def validate_completed_handoff(
    run: Data, entries: list[Data], rows: list[Any], values: dict[str, Data], *, before_dispatch: bool = False
) -> dict[str, str]:
    try:
        return _validate(run, entries, rows, values, before_dispatch=before_dispatch)
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        SandboxReconciliationRequired,
    ):
        raise ValueError("Canonical completed verification custody is unavailable") from None
