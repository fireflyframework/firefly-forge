# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Canonical stronger modes retain the original configuration and provider grant."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from firefly_forge.checkpoint_mode_models import CheckpointModeReceiptV1
from firefly_forge.checkpoint_mode_proof import ADOPTION, CLAIM, required_settings
from firefly_forge.journal import digest

Data = dict[str, Any]

if TYPE_CHECKING:
    from firefly_forge.original_selection_runtime import PreparedOriginalSelection


def effective_revision(run: Data, field: str = "config_revision") -> str | None:
    reference = run.get("checkpoint_mode_adoption")
    if not reference or field != "config_revision":
        return run.get(field)
    from firefly_forge.store import identifier

    if (
        set(reference)
        != {"request_id", "record_digest", "original_config_revision", "execution_config_revision", "policy_digest"}
        or reference["original_config_revision"] != run.get("config_revision")
        or reference["execution_config_revision"] == run.get("config_revision")
    ):
        raise ValueError("Mode adoption cannot replace the original frozen configuration")
    identifier(reference["request_id"])
    identifier(reference["execution_config_revision"])
    selected = run.get("original_selection_readmission")
    if selected is not None:
        if set(selected) != {"request_id", "record_digest", "execution_config_revision", "policy_digest"}:
            raise ValueError("Original selection requires an exact native readmission reference")
        identifier(selected["request_id"])
        identifier(selected["execution_config_revision"])
        if selected["execution_config_revision"] in {
            run.get("config_revision"),
            reference["execution_config_revision"],
        }:
            raise ValueError("Original selection must preserve both prior immutable revisions")
        return str(selected["execution_config_revision"])
    return str(reference["execution_config_revision"])


def execution_settings(configuration: Any, run: Data) -> dict[str, str]:
    if run.get("checkpoint_mode_adoption"):
        prepared = _CURRENT.get()
        if prepared is not None and prepared.record["run_id"] == run["id"]:
            if configuration.settings_generation is not prepared.settings_generation:
                raise ValueError("The current provider settings changed after preparation")
            return dict(
                prepared.selection.selected_settings if prepared.selection is not None else prepared.effective_settings
            )
    from firefly_forge.bridge_completions import _outside_unit

    _outside_unit()
    if run.get("original_selection_readmission"):
        raise ValueError("The original selection must be prepared before resolving effective execution settings")
    original: dict[str, str] = configuration.load_snapshot(run["config_revision"])
    if not run.get("checkpoint_mode_adoption"):
        return original
    selected: dict[str, str] = configuration.load_snapshot(effective_revision(run))
    if selected != required_settings(original) or configuration.resolve("bridge") != selected:
        raise ValueError("Only the original provider settings plus both required modes are admitted")
    return selected


@dataclass(frozen=True)
class PreparedModeAdoption:
    record: Data
    policy_digest: str
    acknowledgement_policy_digest: str
    original_settings: dict[str, str] = field(repr=False)
    effective_settings: dict[str, str] = field(repr=False)
    settings_generation: object = field(repr=False)
    tool_policy_digest: str
    selection: PreparedOriginalSelection | None = field(default=None, repr=False)


_CURRENT: ContextVar[PreparedModeAdoption | None] = ContextVar("forge_checkpoint_mode_adoption", default=None)


@contextmanager
def mode_adoption_scope(prepared: PreparedModeAdoption) -> Iterator[None]:
    token = _CURRENT.set(prepared)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def prepared_mode(run: Data) -> PreparedModeAdoption | None:
    if not run.get("checkpoint_mode_adoption"):
        return None
    prepared = _CURRENT.get()
    if (
        prepared is None
        or prepared.record["run_id"] != run["id"]
        or prepared.record["digest"] != run["checkpoint_mode_adoption"].get("record_digest")
    ):
        raise ValueError("Canonical checkpoint mode adoption must be prepared before execution")
    return prepared


def effective_model_selection(run: Data, field: str = "model_selection") -> Data | None:
    if field == "model_selection" and run.get("original_selection_readmission"):
        prepared = prepared_mode(run)
        if prepared is None or prepared.selection is None:
            raise ValueError("The effective model selection requires its prepared native readmission")
        return copy.deepcopy(prepared.selection.model_selection)
    value = run.get(field)
    return copy.deepcopy(value) if value is not None else None


def effective_public_config(run: Data, field: str = "execution_config") -> Data:
    if field == "execution_config" and run.get("original_selection_readmission"):
        selection = effective_model_selection(run)
        assert selection is not None
        return copy.deepcopy(selection["execution_config"])
    return copy.deepcopy(run.get(field) or {})


def policy_digest() -> str:
    from firefly_forge.bridge_completions import _outside_unit

    _outside_unit()
    root = Path(__file__).parent
    names = (
        "checkpoint_mode_adoptions.py",
        "checkpoint_mode_models.py",
        "checkpoint_mode_proof.py",
        "checkpoint_mode_runtime.py",
        "checkpoint_mode_custody.py",
        "checkpoint_mode_handoff_custody.py",
        "checkpoint_mode_controllers.py",
        "engine.py",
        "journal.py",
        "bridge_completions.py",
        "agentic_checkpoint_invocation.py",
        "verification_models.py",
        "unknown_command_proof.py",
        "known_worker_completion_proof.py",
        "native_terminal_completions.py",
        "git_deliveries.py",
        "sandbox.py",
        "sandbox_command_slots.py",
        "agent_restores.py",
    )
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})


def mode_reference(record: Data) -> Data:
    value = CheckpointModeReceiptV1.model_validate(record["payload"])
    return {
        "request_id": value.request_id,
        "record_digest": record["digest"],
        "original_config_revision": value.candidate.proof.original_config_revision,
        "execution_config_revision": value.execution_config_revision,
        "policy_digest": value.candidate.policy_digest,
    }


async def assert_mode_actors(journal: Any, run: Data, entries: list[Data]) -> None:
    from firefly_forge.bridge_completions import BridgeCompletions
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.unknown_command_proof import continuation_scope

    prepared = prepared_mode(run)
    if prepared is None:
        return
    record = prepared.record
    receipt = CheckpointModeReceiptV1.model_validate(record["payload"])
    candidate = receipt.candidate
    mediated = run.get("original_selection_readmission") is not None
    if mediated:
        from firefly_forge.original_selection_runtime import assert_readmitted_current

        await assert_readmitted_current(journal, run, entries, prepared)
    if (
        run.get("agent") != "bridge"
        or run.get("model_selection") is not None
        or (run.get("cancel_request") or {}).get("status") == "requested"
        or run.get("status") == "cancelled"
        or not any(e["digest"] == record["digest"] and e["kind"] == ADOPTION for e in entries)
        or run["checkpoint_mode_adoption"] != mode_reference(record)
        or candidate.original_scope_digest != continuation_scope(run)
        or (not mediated and candidate.policy_digest != prepared.policy_digest)
        or (not mediated and candidate.acknowledgement_policy_digest != prepared.acknowledgement_policy_digest)
        or journal.store.configuration.settings_generation is not prepared.settings_generation
        or digest(prepared.original_settings) != candidate.proof.original_settings_digest
        or digest(prepared.effective_settings) != candidate.proof.required_settings_digest
    ):
        raise ValueError("Checkpoint mode admission scope, policy or configuration changed")
    intents = [
        e
        for e in entries
        if e["kind"] == "checkpoint_mode_intent"
        and e["idempotency_key"] == "checkpoint-mode-intent:" + receipt.request_id
    ]
    if (
        len(intents) != 1
        or intents[0]["digest"] != receipt.adoption_intent_digest
        or intents[0]["previous_hash"] != candidate.proof.journal_digest
        or record["previous_hash"] != intents[0]["digest"]
        or intents[0]["payload"]
        != {
            "candidate_digest": candidate.digest,
            "request": receipt.request.model_dump(mode="json"),
            "actor": candidate.actor.model_dump(mode="json"),
        }
    ):
        raise ValueError("Checkpoint mode receipt has no exact forward native intent lineage")
    actor = candidate.actor.model_dump()
    identity = await BridgeCompletions(journal.store, journal)._identity(actor["user_id"])
    if digest({"issuer": identity.issuer, "subject": identity.subject}) != candidate.actor.identity_digest:
        raise ValueError("The native checkpoint mode operator identity changed")
    access = ProjectAccess(journal.store)
    if not await access.authorize_actor(actor, None, "members"):
        raise ValueError("Current native administrator authority is required")
    await access.authorize_actor(actor, run.get("project_id"), "execute")
    await access.authorize_execution(run)
    for acknowledgement in candidate.acknowledgements:
        original = [
            e
            for e in entries
            if e["digest"] == acknowledgement.record_digest and e["kind"] == "unknown_command_acknowledgement"
        ]
        if (
            len(original) != 1
            or original[0]["payload"]["candidate"]["policy_digest"] != acknowledgement.old_policy_digest
        ):
            raise ValueError("Original unknown-command acknowledgement custody changed")
        from firefly_forge.sandbox_command_slots import _canonical
        from firefly_forge.store import identifier
        from firefly_forge.unknown_command_proof import unknown_execution

        row = await journal.sandboxes.find_by_id(identifier(acknowledgement.execution_id))
        if row is None or row.run_id.hex != run["id"]:
            raise ValueError("Acknowledged command disappeared from native custody")
        value = _canonical(row, entries, run["id"])
        unknown_execution(row, value)
        if digest(value) != acknowledgement.execution_digest:
            raise ValueError("Original acknowledged command custody changed")
        await access.authorize_actor(acknowledgement.actor.model_dump(), run.get("project_id"), "execute")
    await journal.store.assert_ownership()


async def prepare_current_mode(journal: Any, run_id: str) -> PreparedModeAdoption | None:
    from firefly_forge.known_worker_completions import _entries
    from firefly_forge.store import identifier
    from firefly_forge.unknown_command_acknowledgements import policy_digest as acknowledgement_policy

    current = await journal.store.get_run(run_id)
    if not current.get("checkpoint_mode_adoption"):
        return None
    settings_generation = journal.store.configuration.settings_generation
    policy, acknowledgement = policy_digest(), acknowledgement_policy()
    original_settings = journal.store.configuration.load_snapshot(current["config_revision"])
    selected_settings = journal.store.configuration.load_snapshot(
        current["checkpoint_mode_adoption"]["execution_config_revision"]
    )
    if selected_settings != required_settings(original_settings):
        raise ValueError("The original checkpoint mode snapshot changed")
    if (
        not current.get("original_selection_readmission")
        and journal.store.configuration.resolve("bridge") != selected_settings
    ):
        raise ValueError("Only the original provider settings plus both required modes are admitted")
    from firefly_forge.tool_broker import tool_policy_snapshot

    tool_policy_digest = tool_policy_snapshot(journal.store.configuration)["digest"]
    if journal.store.configuration.settings_generation is not settings_generation:
        raise ValueError("Checkpoint mode configuration changed during preparation")
    async with journal.store._unit():
        entries = _entries(await journal.records.find_for_run(identifier(run_id)), run_id)
    from firefly_forge.original_selection_runtime import prepare_readmitted_selection

    selection = await prepare_readmitted_selection(journal, current, entries)
    from firefly_forge.invalid_read_epochs import mode_epoch

    policy, acknowledgement = mode_epoch(current, selection, policy, acknowledgement)
    async with journal.store._unit():
        if (
            await journal.store.get_run(run_id) != current
            or _entries(await journal.records.find_for_run(identifier(run_id)), run_id) != entries
            or journal.store.configuration.settings_generation is not settings_generation
        ):
            raise ValueError("Checkpoint mode scope changed during configuration preparation")
        records = [
            e
            for e in entries
            if e["kind"] == ADOPTION and e["digest"] == current["checkpoint_mode_adoption"].get("record_digest")
        ]
        if len(records) != 1:
            raise ValueError("Checkpoint mode adoption has no exact native receipt")
        prepared = PreparedModeAdoption(
            copy.deepcopy(records[0]),
            policy,
            acknowledgement,
            original_settings,
            selected_settings,
            settings_generation,
            tool_policy_digest,
            selection,
        )
        with mode_adoption_scope(prepared):
            await assert_mode_actors(journal, current, entries)
        return prepared


def provider_modes_match(store: Any, run: Data, intent: Data) -> bool:
    prepared = prepared_mode(run)
    if prepared is None:
        return False
    receipt = CheckpointModeReceiptV1.model_validate(prepared.record["payload"])
    if prepared.selection is not None and (
        intent.get("request_id") == (run.get("worker_request") or {}).get("id")
        and intent.get("agent") in {None, "bridge"}
        and intent.get("provider_digest")
        == digest({"agent": "bridge", "settings": prepared.selection.selected_settings})
    ):
        return store.configuration.settings_generation is prepared.settings_generation
    return (
        intent.get("id") == run.get("verification_handoff", {}).get("id")
        and intent.get("provider_digest") == receipt.candidate.proof.provider_digest
        and receipt.candidate.proof.original_config_revision == run.get("config_revision")
        and store.configuration.settings_generation is prepared.settings_generation
        and prepared.effective_settings == required_settings(prepared.original_settings)
    )


def acknowledgement_policy_matches(run: Data, record: Data, current_policy: str) -> bool:
    prepared = prepared_mode(run)
    from firefly_forge.invalid_read_epochs import acknowledgement_epoch_matches

    if prepared is None or (
        prepared.acknowledgement_policy_digest != current_policy
        and not acknowledgement_epoch_matches(run, prepared, current_policy)
    ):
        return False
    receipt = CheckpointModeReceiptV1.model_validate(prepared.record["payload"])
    return any(
        item.record_digest == record["digest"]
        and item.old_policy_digest == record["payload"]["candidate"]["policy_digest"]
        for item in receipt.candidate.acknowledgements
    )


async def claim_adopted_intent(journal: Any, run: Any, task: Any, original: Any) -> Data | None:
    if not run.data.get("checkpoint_mode_adoption") or task.task_id != "implement":
        return None
    from firefly_forge.known_worker_completions import _entries
    from firefly_forge.store import identifier

    entries = _entries(await journal.records.find_for_run(identifier(run.id.hex)), run.id.hex)
    await assert_mode_actors(journal, run.data, entries)
    prepared = prepared_mode(run.data)
    assert prepared is not None
    receipt = CheckpointModeReceiptV1.model_validate(prepared.record["payload"])
    proof = receipt.candidate.proof
    if (
        task.attempt != proof.attempt
        or task.data.get("idempotency_key") != proof.task_key
        or task.status not in {"running", "needs_reconciliation"}
        or original.digest != proof.implementation_intent_digest
    ):
        return None
    if any(
        e["kind"] == CLAIM and e["idempotency_key"] == "checkpoint-mode-claim:" + receipt.request_id for e in entries
    ):
        if run.data.get("original_selection_readmission"):
            from firefly_forge.original_selection_runtime import claim_readmitted_intent

            return await claim_readmitted_intent(journal, run, task, original, entries, prepared)
        return None
    if entries[-1]["digest"] != prepared.record["digest"]:
        raise ValueError("New execution evidence appeared after checkpoint mode admission")
    claim = await journal._append(
        run.id.hex,
        "implement",
        "checkpoint-mode-claim:" + receipt.request_id,
        CLAIM,
        {
            "version": 1,
            "adoption_digest": prepared.record["digest"],
            "implementation_intent_digest": original.digest,
            "continuation_intent_digest": proof.continuation_intent_digest,
            "task_key": proof.task_key,
            "attempt": proof.attempt,
        },
    )
    task.status = "running"
    task.data = {**task.data, "checkpoint_mode_claim_digest": claim["digest"]}
    await journal.tasks.save(task)
    return {
        "idempotency_key": proof.task_key,
        "disposition": "execute",
        "entry": copy.deepcopy(original.data),
        "checkpoint_mode_claim_digest": claim["digest"],
    }


def claimed_continuation(run: Data, entries: list[Any], continuation: Any) -> bool:
    prepared = prepared_mode(run)
    if prepared is None:
        return False
    receipt = CheckpointModeReceiptV1.model_validate(prepared.record["payload"])
    proof = receipt.candidate.proof
    claims = [
        e for e in entries if e.kind == CLAIM and e.idempotency_key == "checkpoint-mode-claim:" + receipt.request_id
    ]
    if run.get("original_selection_readmission"):
        from firefly_forge.original_selection_runtime import current_claim

        if not current_claim(run, [copy.deepcopy(e.data) for e in entries], prepared):
            return False
    return (
        continuation.digest == proof.continuation_intent_digest
        and len(claims) == 1
        and claims[0].data["payload"]
        == {
            "version": 1,
            "adoption_digest": prepared.record["digest"],
            "implementation_intent_digest": proof.implementation_intent_digest,
            "continuation_intent_digest": proof.continuation_intent_digest,
            "task_key": proof.task_key,
            "attempt": proof.attempt,
        }
    )


def prepared_tool_policy(run: Data) -> str:
    prepared = prepared_mode(run)
    assert prepared is not None
    return prepared.tool_policy_digest


async def assert_active_mode(journal: Any, run_id: str) -> None:
    from firefly_forge.known_worker_completions import _entries
    from firefly_forge.store import identifier

    async with journal.store._unit():
        current = await journal.store.get_run(run_id)
        if current.get("checkpoint_mode_adoption"):
            entries = _entries(await journal.records.find_for_run(identifier(run_id)), run_id)
            await assert_mode_actors(journal, current, entries)


@asynccontextmanager
async def mode_execution_scope(journal: Any, run_id: str) -> AsyncIterator[None]:
    run = await journal.store.get_run(run_id)
    if not run.get("checkpoint_mode_adoption"):
        yield
        return
    current = _CURRENT.get()
    if current is None:
        current = await prepare_current_mode(journal, run_id)
        assert current is not None
    with mode_adoption_scope(current):
        await assert_active_mode(journal, run_id)
        yield
        await assert_active_mode(journal, run_id)
