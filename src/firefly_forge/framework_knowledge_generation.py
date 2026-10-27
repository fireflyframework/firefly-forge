# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Generation defaults require a completed native validation journey, not an indexed tag."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from firefly_forge.framework_knowledge_conformance import conformance_commands, runtime_probe_command
from firefly_forge.framework_knowledge_requirements import runtime_satisfies
from firefly_forge.journal import Journal
from firefly_forge.product_provenance import content_digest as digest
from firefly_forge.sandbox import SandboxReconciliationRequired
from firefly_forge.sandbox_command_slots import _canonical, _complete

Data = dict[str, Any]


def require(value: object) -> None:
    if not value:
        raise ValueError("Generation target requires exact canonical native validation evidence")


def effect_window(entries: list[Data], execution_id: str) -> tuple[int, int]:
    effects = [
        entry
        for entry in entries
        if entry["kind"] in {"sandbox_intent", "sandbox_receipt", "sandbox_reconcile"}
        and entry["payload"].get("execution_id") == execution_id
    ]
    require(len(effects) == 6)
    for index, effect in enumerate(("create", "start", "cleanup")):
        intent, receipt = effects[index * 2 : index * 2 + 2]
        require(
            intent["kind"] == "sandbox_intent"
            and receipt["kind"] in {"sandbox_receipt", "sandbox_reconcile"}
            and intent["payload"]["effect"] == receipt["payload"]["effect"] == effect
        )
    return effects[0]["sequence"], effects[-1]["sequence"]


def validate_generation_evidence(run: Data, entries: list[Data], rows: list[Any], corpus: Data) -> Data:
    require(run.get("status") == "succeeded" and Journal.verify_chain(entries) and entries)
    pin = run.get("framework_knowledge_pin")
    attestation = run.get("framework_runtime_attestation")
    if not isinstance(pin, dict) or not isinstance(attestation, dict):
        raise ValueError("Generation target requires its canonical native pin and runtime validation")
    require(
        pin["corpus_ref"] == corpus["public"]["corpus_ref"]
        and pin["corpus_manifest_sha256"] == corpus["public"]["manifest_sha256"]
        and pin["release_commit"] == corpus["public"]["commit"]
        and pin["runtime_requirements"] == corpus["runtime_requirements"]
    )
    require(all(entry["run_id"] == run["id"] for entry in entries))
    runtime = [
        entry
        for entry in entries
        if entry["kind"] == "framework_runtime_attestation" and entry["digest"] == attestation.get("event_digest")
    ]
    body = {k: v for k, v in attestation.items() if k != "event_digest"}
    require(len(runtime) == 1 and runtime[0]["payload"] == body and runtime[0]["idempotency_key"] == digest(body))
    require(
        body["run_id"] == run["id"]
        and body["pin_digest"] == pin["digest"]
        and body["requirements_digest"] == pin["runtime_requirements"]["digest"]
        and runtime_satisfies(pin["runtime_requirements"], body["observed"]["version"])
    )
    require(body["observed"]["runtime"] == pin["runtime_requirements"]["runtime"])
    if body["observed"]["runtime"] == "python":
        require(tuple(map(int, body["observed"]["version"].split("."))) >= (3, 13, 0))
    try:
        commands = {row.id.hex: (row, _canonical(row, entries, run["id"])) for row in rows}
    except SandboxReconciliationRequired as error:
        raise ValueError("Generation validation command custody requires reconciliation") from error
    require(
        type(body.get("version")) is int
        and body["version"] == 1
        and set(body)
        == {"version", "run_id", "task_key", "pin_digest", "requirements_digest", "observed", "command", "argv_digest"}
    )
    command_id = body["command"]["execution_id"]
    require(command_id in commands)
    row, command = commands[command_id]
    request = command["request"]
    expected_receipt = {
        "execution_id": command_id,
        "request_digest": request["request_digest"],
        "snapshot_digest": request["snapshot_digest"],
        "workspace_digest": request["request"].get("workspace_digest"),
        "start_key": command_id + ":start",
        "cleanup_key": command_id + ":cleanup",
    }
    require(
        body["command"] == expected_receipt
        and body["argv_digest"]
        == hashlib.sha256(json.dumps(request["request"]["argv"], separators=(",", ":")).encode()).hexdigest()
    )
    require(
        row.task_id == "implement"
        and _complete(row, command)
        and command["effects"]["start"]["evidence"]["exit_code"] == 0
        and request["request"]["argv"] == runtime_probe_command(pin["runtime_requirements"])
        and request["request_digest"] == body["command"]["request_digest"]
        and request["snapshot_digest"] == body["command"]["snapshot_digest"]
    )
    require(json.loads(command["effects"]["start"]["evidence"]["output"]) == body["observed"])
    require(body["task_key"] == uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{row.parent_attempt}").hex)
    implementation_intent = [
        entry
        for entry in entries
        if entry["kind"] == "intent"
        and entry["task_id"] == "implement"
        and entry["idempotency_key"] == body["task_key"]
    ]
    implementation_receipt = [
        entry
        for entry in entries
        if entry["kind"] == "receipt"
        and entry["task_id"] == "implement"
        and entry["idempotency_key"] == body["task_key"]
    ]
    require(
        len(implementation_intent) == len(implementation_receipt) == 1
        and implementation_intent[0]["sequence"] < runtime[0]["sequence"] < implementation_receipt[0]["sequence"]
        and implementation_receipt[0]["payload"].get("source_tree") == run.get("source_tree")
    )
    first_effect, last_effect = effect_window(entries, command_id)
    require(implementation_intent[0]["sequence"] < first_effect < last_effect < runtime[0]["sequence"])
    verification = [entry for entry in entries if entry["kind"] == "receipt" and entry["task_id"] == "verify"]
    require(verification)
    final = verification[-1]
    require(
        not any(
            entry["kind"] == "intent"
            and entry["task_id"] in {"implement", "verify"}
            and entry["sequence"] > final["sequence"]
            for entry in entries
        )
    )
    evidence = final["payload"]
    checks = evidence.get("checks")
    require(
        isinstance(checks, list)
        and checks
        and not evidence.get("failed_checks")
        and evidence.get("source_tree") == run.get("source_tree")
        and runtime[0]["sequence"] < final["sequence"]
    )
    require(all(type(check.get("exit_code")) is int and check["exit_code"] == 0 for check in checks))
    required_commands = conformance_commands(pin)
    require(all(any(check["argv"] == argv for check in checks) for argv in required_commands))
    intents = [entry for entry in entries if entry["kind"] == "intent" and entry["task_id"] == "verify"]
    require(
        intents
        and intents[-1]["idempotency_key"] == final["idempotency_key"]
        and implementation_receipt[0]["sequence"] < intents[-1]["sequence"] < final["sequence"]
    )
    selected = [
        (row, command)
        for row, command in commands.values()
        if row.task_id == "verify"
        and uuid5(NAMESPACE_URL, f"forge:{run['id']}:verify:{row.parent_attempt}").hex == final["idempotency_key"]
    ]
    require(len(selected) == len(checks) and {row.ordinal for row, _ in selected} == set(range(1, len(checks) + 1)))
    for row, command in selected:
        check = checks[row.ordinal - 1]
        require(
            _complete(row, command)
            and command["effects"]["start"]["evidence"]["exit_code"] == 0
            and command["request"]["request"]["argv"] == check["argv"]
        )
        first_effect, last_effect = effect_window(entries, row.id.hex)
        require(intents[-1]["sequence"] < first_effect < last_effect < final["sequence"])
    return {
        "version": 1,
        "run_id": run["id"],
        "pin_digest": pin["digest"],
        "runtime_attestation_digest": runtime[0]["digest"],
        "verification_receipt_digest": final["digest"],
        "source_tree": evidence["source_tree"],
        "corpus_ref": pin["corpus_ref"],
        "corpus_manifest_sha256": pin["corpus_manifest_sha256"],
        "checks_digest": digest(checks),
        "framework_requirements_digest": pin["runtime_requirements"]["digest"],
    }
