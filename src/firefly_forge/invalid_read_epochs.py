# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Scoped recognition of frozen policy epochs for one qualified negative-read migration."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from firefly_forge.checkpoint_mode_models import CheckpointModeReceiptV1
from firefly_forge.invalid_read_continuity import LEGACY_RUNTIME_CONTRACT
from firefly_forge.invalid_read_lease import policy_snapshot
from firefly_forge.journal import Journal, digest
from firefly_forge.original_selection_models import OriginalSelectionReceiptV1
from firefly_forge.unknown_command_proof import continuation_scope

# Independently computed from registered source A; these are package policy identities, never customer/run identities.
LEGACY_EPOCH = {
    "selection": "416866efc98469c6d0e7cb657753fd1a0834075b42ca93077840c5d83bf11087",
    "mode": "fcfd369fbb83f215d7883ccc88bafafefa957276fc70ef20e7d3aaabd0836f75",
    "acknowledgement": "db48415de2326ec66a36b918812453bf7d4840a758551cb6a247eb037cfd8f16",
    "original_mode": "5f9913f45be3652345556dd5070164078f8849672413bb7830ea1522b7add56d",
    "original_acknowledgement": "67cc870b173a5bc3a210904738cd714f6fc4681255c3ee051d6c300c7bd9563e",
}
_CURRENT: ContextVar[dict[str, Any] | None] = ContextVar("invalid_read_policy_epoch", default=None)


def historical_policies(candidate: Any) -> dict[str, str]:
    return {
        "selection": candidate.policy_digest,
        "mode": candidate.checkpoint_mode_policy_digest,
        "acknowledgement": candidate.acknowledgement_policy_digest,
        "original_mode": candidate.original_mode_policy_digest,
        "original_acknowledgement": candidate.original_acknowledgement_policy_digest,
    }


def epoch_snapshot(store: Any, run: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    from firefly_forge.checkpoint_mode_runtime import policy_digest as mode_policy
    from firefly_forge.invalid_read_continuities import current_profile_seal
    from firefly_forge.original_selection_runtime import mode_record, selection_reference
    from firefly_forge.original_selection_runtime import policy_digest as selection_policy
    from firefly_forge.unknown_command_acknowledgements import policy_digest as ack_policy

    if not run.get("original_selection_readmission"):
        return None
    if not Journal.verify_chain(entries) or any(e["run_id"] != run["id"] for e in entries):
        raise ValueError("Policy epoch requires intact original native custody")
    records = [
        e
        for e in entries
        if e["kind"] == "original_selection_readmission"
        and e["digest"] == run["original_selection_readmission"].get("record_digest")
    ]
    if len(records) != 1 or selection_reference(records[0]) != run["original_selection_readmission"]:
        raise ValueError("Policy epoch has no exact original-selection receipt")
    receipt = OriginalSelectionReceiptV1.model_validate(records[0]["payload"])
    mode = mode_record(run, entries)
    prior = CheckpointModeReceiptV1.model_validate(mode["payload"])
    policies = historical_policies(receipt.candidate)
    current = {"selection": selection_policy(), "mode": mode_policy(), "acknowledgement": ack_policy()}
    if {k: policies[k] for k in current} == current:
        return None
    if (
        policies != LEGACY_EPOCH
        or receipt.candidate.original_scope_digest != continuation_scope(run)
        or receipt.candidate.proof.mode_receipt_digest != mode["digest"]
        or prior.candidate.policy_digest != policies["original_mode"]
        or prior.candidate.acknowledgement_policy_digest != policies["original_acknowledgement"]
    ):
        raise ValueError("No compatible frozen policy epoch is registered for this scope")
    policy = policy_snapshot(store.configuration)
    if not policy["revision"] or policy["settings"]["enabled"] is not True:
        raise ValueError("Historical epoch migration requires explicit current finite-lease policy")
    value = {
        "version": 1,
        "run_id": run["id"],
        "scope_digest": continuation_scope(run),
        "legacy_runtime_contract": LEGACY_RUNTIME_CONTRACT,
        "legacy": policies,
        "current": current,
        "mode_reference": copy.deepcopy(run["checkpoint_mode_adoption"]),
        "selection_reference": copy.deepcopy(run["original_selection_readmission"]),
        "current_profile": current_profile_seal(),
        "lease_policy_digest": policy["digest"],
    }
    return {**value, "digest": digest(value)}


@contextmanager
def epoch_scope(snapshot: dict[str, Any] | None) -> Iterator[None]:
    token = _CURRENT.set(copy.deepcopy(snapshot))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def _active(run: dict[str, Any]) -> dict[str, Any] | None:
    value = _CURRENT.get()
    if value is None:
        return None
    if (
        value["run_id"] != run["id"]
        or value["scope_digest"] != continuation_scope(run)
        or value["mode_reference"] != run.get("checkpoint_mode_adoption")
        or value["selection_reference"] != run.get("original_selection_readmission")
        or value["legacy"] != LEGACY_EPOCH
        or digest({k: v for k, v in value.items() if k != "digest"}) != value["digest"]
    ):
        raise ValueError("Qualified continuity policy epoch scope changed")
    return value


def selection_epoch_matches(run: dict[str, Any], candidate: Any, current: dict[str, str], configuration: Any) -> bool:
    value = _active(run)
    if value is not None and policy_snapshot(configuration)["digest"] != value["lease_policy_digest"]:
        raise ValueError("Current finite-lease policy changed after continuity epoch preparation")
    return bool(value is not None and historical_policies(candidate) == value["legacy"] and current == value["current"])


def mode_epoch(run: dict[str, Any], selection: Any, mode: str, acknowledgement: str) -> tuple[str, str]:
    value = _active(run)
    if value is None:
        return mode, acknowledgement
    if (
        selection is None
        or selection.policy_digest != value["legacy"]["selection"]
        or mode != value["current"]["mode"]
        or acknowledgement != value["current"]["acknowledgement"]
    ):
        raise ValueError("Current policy closure differs from the reviewed continuity epoch")
    return value["legacy"]["mode"], value["legacy"]["acknowledgement"]


def acknowledgement_epoch_matches(run: dict[str, Any], prepared: Any, current_policy: str) -> bool:
    value = _active(run)
    return bool(
        value is not None
        and prepared.acknowledgement_policy_digest == value["legacy"]["acknowledgement"]
        and current_policy == value["current"]["acknowledgement"]
    )
