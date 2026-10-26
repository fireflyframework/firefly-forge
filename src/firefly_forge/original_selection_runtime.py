# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared connection authority is prospective; old provider and mode receipts stay exact."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from firefly_forge.checkpoint_mode_models import CheckpointModeReceiptV1
from firefly_forge.journal import digest
from firefly_forge.original_selection_models import OriginalSelectionReceiptV1

Data = dict[str, Any]
READMISSION = "original_selection_readmission"
INTENT = "original_selection_intent"
CLAIM = "original_selection_claim"


def policy_digest() -> str:
    from firefly_forge.bridge_completions import _outside_unit
    from firefly_forge.checkpoint_mode_runtime import policy_digest as mode_policy

    _outside_unit()
    root = Path(__file__).parent
    names = (
        "original_selection_readmissions.py",
        "original_selection_models.py",
        "original_selection_proof.py",
        "original_selection_runtime.py",
        "original_selection_controllers.py",
        "configuration.py",
        "model_profiles.py",
        "task_routing.py",
        "verification_handoffs.py",
        "verification_models.py",
    )
    return digest(
        {
            "mode_policy": mode_policy(),
            "selection": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names},
        }
    )


def selection_reference(record: Data) -> Data:
    receipt = OriginalSelectionReceiptV1.model_validate(record["payload"])
    return {
        "request_id": receipt.request_id,
        "record_digest": record["digest"],
        "execution_config_revision": receipt.candidate.execution_config_revision,
        "policy_digest": receipt.candidate.policy_digest,
    }


def mode_record(run: Data, entries: list[Data]) -> Data:
    records = [
        e
        for e in entries
        if e["kind"] == "checkpoint_mode_adoption"
        and e["digest"] == (run.get("checkpoint_mode_adoption") or {}).get("record_digest")
    ]
    if len(records) != 1:
        raise ValueError("The retained checkpoint mode receipt is unavailable or duplicated")
    CheckpointModeReceiptV1.model_validate(records[0]["payload"])
    return copy.deepcopy(records[0])


def retained_selection(run: Data, entries: list[Data], mode: Data) -> Data:
    receipt = CheckpointModeReceiptV1.model_validate(mode["payload"])
    original = [e for e in entries if e["digest"] == receipt.candidate.proof.implementation_intent_digest]
    handoffs = [
        e
        for e in entries
        if len(original) == 1 and e["kind"] == "handoff" and e["sequence"] == original[0]["sequence"] - 1
    ]
    if len(handoffs) != 1 or not isinstance(handoffs[0]["payload"].get("previous_model_selection"), dict):
        raise ValueError("The exact original native named selection is missing")
    return copy.deepcopy(handoffs[0]["payload"]["previous_model_selection"])


async def current_selection(store: Any, selection: Data) -> tuple[Data, Data, dict[str, str]]:
    from firefly_forge.bridge_completions import _outside_unit
    from firefly_forge.verification_models import validate_coding_readiness

    _outside_unit()
    if store.task_router is None:
        raise ValueError("The original named model profile is not configured")
    profiles = store.task_router.profiles
    settings = profiles.validate_snapshot(selection)
    profile, _ = await profiles.validate_selection(selection["profile_id"], "implementation")
    routing = store.task_router.current()
    validate_coding_readiness("bridge", settings)
    if profile["settings"] != settings:
        raise ValueError("The original named provider settings are no longer current")
    return profile, routing, settings


async def assert_retained_mode_actor(journal: Any, run: Data, mode: Data, *, lock: bool = False) -> None:
    from firefly_forge.bridge_completions import BridgeCompletions
    from firefly_forge.project_access import AccessDenied, ProjectAccess
    from firefly_forge.store import identifier

    receipt = CheckpointModeReceiptV1.model_validate(mode["payload"])
    actor = receipt.candidate.actor.model_dump()
    if lock:
        from pyfly.data.relational.sqlalchemy import LockMode

        from firefly_forge.identity_persistence import ForgeIdentityUserRepository

        identity = await ForgeIdentityUserRepository().find_by_id(
            identifier(actor["user_id"]), lock=LockMode.PESSIMISTIC_WRITE
        )
        if identity is None or not identity.enabled:
            raise AccessDenied("The original mode operator is no longer available")
    else:
        identity = await BridgeCompletions(journal.store, journal)._identity(actor["user_id"])
    if digest({"issuer": identity.issuer, "subject": identity.subject}) != actor["identity_digest"]:
        raise AccessDenied("The original mode operator identity changed")
    access = ProjectAccess(journal.store)
    if not await access.authorize_actor(actor, None, "members"):
        raise AccessDenied("The original mode operator is no longer a native administrator")
    await access.authorize_actor(actor, run.get("project_id"), "execute")
    await access.authorize_execution(run)


@dataclass(frozen=True)
class PreparedOriginalSelection:
    record: Data
    model_selection: Data
    selected_settings: dict[str, str] = field(repr=False)
    settings_generation: object = field(repr=False)
    policy_digest: str
    mode_policy_digest: str
    acknowledgement_policy_digest: str


async def prepare_readmitted_selection(
    journal: Any, run: Data, entries: list[Data]
) -> PreparedOriginalSelection | None:
    from firefly_forge.bridge_completions import _outside_unit
    from firefly_forge.checkpoint_mode_proof import required_settings
    from firefly_forge.checkpoint_mode_runtime import policy_digest as mode_policy
    from firefly_forge.unknown_command_acknowledgements import policy_digest as ack_policy

    _outside_unit()
    reference = run.get("original_selection_readmission")
    if not reference:
        return None
    matches = [e for e in entries if e["kind"] == READMISSION and e["digest"] == reference.get("record_digest")]
    if len(matches) != 1 or reference != selection_reference(matches[0]):
        raise ValueError("Original selection readmission has no exact native receipt")
    record = matches[0]
    receipt = OriginalSelectionReceiptV1.model_validate(record["payload"])
    candidate = receipt.candidate
    mode = mode_record(run, entries)
    original = CheckpointModeReceiptV1.model_validate(mode["payload"])
    selected = retained_selection(run, entries, mode)
    generation = journal.store.configuration.settings_generation
    profile, routing, settings = await current_selection(journal.store, selected)
    old = journal.store.configuration.load_snapshot(run["config_revision"])
    ineffective = journal.store.configuration.load_snapshot(original.execution_config_revision)
    if (
        candidate.policy_digest != policy_digest()
        or candidate.checkpoint_mode_policy_digest != mode_policy()
        or candidate.acknowledgement_policy_digest != ack_policy()
        or candidate.original_mode_policy_digest != original.candidate.policy_digest
        or candidate.original_acknowledgement_policy_digest != original.candidate.acknowledgement_policy_digest
        or candidate.proof.mode_receipt_digest != mode["digest"]
        or digest(profile) != candidate.proof.profile_digest
        or routing["digest"] != candidate.proof.routing_digest
        or digest(selected) != candidate.proof.selection_digest
        or selected["config_revision"] != candidate.execution_config_revision
        or selected["profile_id"] != candidate.profile_id
        or digest(settings) != candidate.proof.current_settings_digest
        or digest(old) != candidate.proof.original_settings_digest
        or digest(ineffective) != candidate.proof.ineffective_settings_digest
        or ineffective != required_settings(old)
        or {k: v for k, v in settings.items() if k not in {"FORGE_BRIDGE_URL", "FORGE_BRIDGE_TOKEN"}}
        != {k: v for k, v in ineffective.items() if k not in {"FORGE_BRIDGE_URL", "FORGE_BRIDGE_TOKEN"}}
        or journal.store.configuration.settings_generation is not generation
    ):
        raise ValueError("Original selection, configuration or reviewed policy epoch changed")
    return PreparedOriginalSelection(
        copy.deepcopy(record),
        copy.deepcopy(selected),
        settings,
        generation,
        candidate.policy_digest,
        candidate.checkpoint_mode_policy_digest,
        candidate.acknowledgement_policy_digest,
    )


async def assert_readmitted_current(journal: Any, run: Data, entries: list[Data], prepared: Any) -> None:
    from firefly_forge.bridge_completions import BridgeCompletions
    from firefly_forge.project_access import AccessDenied, ProjectAccess
    from firefly_forge.unknown_command_proof import continuation_scope

    selected = prepared.selection
    if selected is None or run.get("original_selection_readmission") != selection_reference(selected.record):
        raise ValueError("The current original selection requires its prepared native receipt")
    record = selected.record
    receipt = OriginalSelectionReceiptV1.model_validate(record["payload"])
    c = receipt.candidate
    mode = CheckpointModeReceiptV1.model_validate(prepared.record["payload"])
    intents = [
        e
        for e in entries
        if e["kind"] == INTENT and e["idempotency_key"] == "original-selection-intent:" + receipt.request_id
    ]
    if (
        not any(e == record for e in entries)
        or len(intents) != 1
        or intents[0]["digest"] != receipt.readmission_intent_digest
        or intents[0]["previous_hash"] != c.proof.journal_digest
        or record["previous_hash"] != intents[0]["digest"]
        or intents[0]["payload"]
        != {
            "candidate_digest": c.digest,
            "request": receipt.request.model_dump(mode="json"),
            "actor": c.actor.model_dump(mode="json"),
        }
        or c.proof.mode_receipt_digest != prepared.record["digest"]
        or c.original_mode_policy_digest != mode.candidate.policy_digest
        or c.original_acknowledgement_policy_digest != mode.candidate.acknowledgement_policy_digest
        or c.checkpoint_mode_policy_digest != prepared.policy_digest
        or c.acknowledgement_policy_digest != prepared.acknowledgement_policy_digest
        or c.policy_digest != selected.policy_digest
        or c.original_scope_digest != continuation_scope(run)
        or journal.store.configuration.settings_generation is not selected.settings_generation
        or digest(selected.selected_settings) != c.proof.current_settings_digest
    ):
        raise ValueError("Prospective original selection policy or native scope changed")
    actor = c.actor.model_dump()
    identity = await BridgeCompletions(journal.store, journal)._identity(actor["user_id"])
    if digest({"issuer": identity.issuer, "subject": identity.subject}) != c.actor.identity_digest:
        raise AccessDenied("The original selection operator identity changed")
    access = ProjectAccess(journal.store)
    if not await access.authorize_actor(actor, None, "members"):
        raise AccessDenied("The original selection operator is no longer a native administrator")
    await access.authorize_actor(actor, run.get("project_id"), "execute")
    await journal.store.assert_ownership()


def current_claim(run: Data, entries: list[Data], prepared: Any) -> bool:
    if prepared.selection is None:
        return False
    record = prepared.selection.record
    receipt = OriginalSelectionReceiptV1.model_validate(record["payload"])
    matches = [
        e
        for e in entries
        if e["kind"] == CLAIM and e["idempotency_key"] == "original-selection-claim:" + receipt.request_id
    ]
    return len(matches) == 1 and matches[0]["payload"] == {
        "version": 1,
        "readmission_digest": record["digest"],
        "mode_claim_digest": receipt.candidate.proof.mode_claim_digest,
        "task_key": receipt.candidate.proof.task_key,
        "attempt": receipt.candidate.proof.attempt,
        "execution_config_revision": receipt.candidate.execution_config_revision,
    }


async def claim_readmitted_intent(
    journal: Any, run: Any, task: Any, original: Any, entries: list[Data], prepared: Any
) -> Data | None:
    if not run.data.get("original_selection_readmission"):
        return None
    await assert_readmitted_current(journal, run.data, entries, prepared)
    if current_claim(run.data, entries, prepared):
        return None
    record = prepared.selection.record
    receipt = OriginalSelectionReceiptV1.model_validate(record["payload"])
    mode = CheckpointModeReceiptV1.model_validate(prepared.record["payload"])
    if (
        entries[-1]["digest"] != record["digest"]
        or task.attempt != receipt.candidate.proof.attempt
        or task.data.get("idempotency_key") != receipt.candidate.proof.task_key
        or task.status not in {"running", "needs_reconciliation"}
        or original.digest != mode.candidate.proof.implementation_intent_digest
    ):
        raise ValueError("Original selection execution requires its exact unfinished native task")
    claim = await journal._append(
        run.id.hex,
        "implement",
        "original-selection-claim:" + receipt.request_id,
        CLAIM,
        {
            "version": 1,
            "readmission_digest": record["digest"],
            "mode_claim_digest": receipt.candidate.proof.mode_claim_digest,
            "task_key": receipt.candidate.proof.task_key,
            "attempt": receipt.candidate.proof.attempt,
            "execution_config_revision": receipt.candidate.execution_config_revision,
        },
    )
    task.status = "running"
    task.data = {**task.data, "original_selection_claim_digest": claim["digest"]}
    await journal.tasks.save(task)
    return {
        "idempotency_key": receipt.candidate.proof.task_key,
        "disposition": "execute",
        "entry": copy.deepcopy(original.data),
        "original_selection_claim_digest": claim["digest"],
    }
