# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit native readmission of one lost original Bridge selection."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from pyfly.container import service

from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.checkpoint_mode_adoptions import CheckpointModeAdoptions
from firefly_forge.checkpoint_mode_models import CheckpointModeReceiptV1
from firefly_forge.checkpoint_mode_proof import prepare_closed_history
from firefly_forge.checkpoint_mode_runtime import policy_digest as mode_policy
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completions import _entries, _task
from firefly_forge.original_selection_models import (
    OriginalSelectionCandidateV1,
    OriginalSelectionReadmissionInput,
    OriginalSelectionReceiptV1,
)
from firefly_forge.original_selection_proof import prepare_selection_readmission
from firefly_forge.original_selection_runtime import (
    INTENT,
    READMISSION,
    assert_retained_mode_actor,
    current_selection,
    mode_record,
    policy_digest,
    retained_selection,
    selection_reference,
)
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.store import Store, identifier, now, prepared_credential_rejection
from firefly_forge.unknown_command_acknowledgements import policy_digest as ack_policy
from firefly_forge.unknown_command_proof import continuation_scope
from firefly_forge.verification_source_preparation import prepare_verification_source, verification_catalog_root

Data = dict[str, Any]


@dataclass(frozen=True)
class SelectionPreparation:
    candidate: Data
    source: Any
    history: Any
    settings_generation: object = field(repr=False)
    inputs: Data = field(repr=False)


@service
class OriginalSelectionReadmissions:
    def __init__(self, store: Store, journal: Journal, engine: Any) -> None:
        self.store, self.journal, self.engine = store, journal, engine
        self.modes = CheckpointModeAdoptions(store, journal, engine)
        self.native = self.modes.native

    async def _access(self, run_id: str, actor: Any) -> Data:
        return await self.modes._access(run_id, actor)

    async def _read(self, run_id: str, actor: Any) -> tuple[Data, Data, list[Data]]:
        return await self.modes._read(run_id, actor)

    async def _command_custody(
        self, run: Data, task: Data, entries: list[Data], *, lock: bool = False
    ) -> tuple[list[Data], str]:
        return await self.modes._command_custody(run, task, entries, lock=lock)

    async def _readback(self, run_id: str, record: Data, actor: Any) -> Data:
        return await self.modes._readback(run_id, record, actor)

    async def _prepare_selection(self, run_id: str, actor: Any) -> SelectionPreparation:
        run, task, entries = await self._read(run_id, actor)
        if run.get("original_selection_readmission"):
            raise ValueError("The original selection already has a native readmission")
        generation = self.store.configuration.settings_generation
        mode = mode_record(run, entries)
        old_mode = CheckpointModeReceiptV1.model_validate(mode["payload"])
        selected = retained_selection(run, entries, mode)
        profile, routing, settings = await current_selection(self.store, selected)
        original = self.store.configuration.load_snapshot(run["config_revision"])
        ineffective = self.store.configuration.load_snapshot(old_mode.execution_config_revision)
        history = prepare_closed_history(run, entries, self.store.configuration)
        inputs = {
            "original_settings": original,
            "ineffective_settings": ineffective,
            "current_selection": selected,
            "current_profile": profile,
            "current_routing": routing,
            "current_settings": settings,
            "mode_receipt": mode["payload"],
        }
        proof = prepare_selection_readmission(run, task, entries, **inputs)
        bound, verification = await self._command_custody(run, task, entries)
        if (
            digest({"closures": list(history.closures)}) != old_mode.candidate.proof.historical_custody_digest
            or bound != [x.model_dump(mode="json") for x in old_mode.candidate.acknowledgements]
            or verification != old_mode.candidate.verification_custody_digest
        ):
            raise ValueError("Original closed model or command custody changed")
        await assert_retained_mode_actor(self.journal, run, mode)

        async def current() -> None:
            latest, current_task, current_entries = await self._read(run_id, actor)
            if (
                latest != run
                or current_task != task
                or current_entries != entries
                or self.store.configuration.settings_generation is not generation
            ):
                raise ValueError("Original selection source or authority changed during preparation")

        prepared = await prepare_verification_source(
            self.engine.workspaces / run_id,
            verification_catalog_root(self.store.path, run_id),
            budget=self.journal.source_catalog_budget,
            assert_current=current,
        )
        if prepared.source_digest != run.get("verification_handoff", {}).get("source_digest"):
            raise ValueError("Source changed after the original native verification")
        candidate = {
            "version": 1,
            "run_id": run_id,
            "actor": {
                **ProjectAccess.actor_snapshot(actor),
                "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
            },
            "policy_digest": policy_digest(),
            "checkpoint_mode_policy_digest": mode_policy(),
            "acknowledgement_policy_digest": ack_policy(),
            "original_mode_policy_digest": old_mode.candidate.policy_digest,
            "original_acknowledgement_policy_digest": old_mode.candidate.acknowledgement_policy_digest,
            "original_scope_digest": continuation_scope(run),
            "source_digest": prepared.source_digest,
            "historical_custody_digest": digest({"closures": list(history.closures)}),
            "verification_custody_digest": verification,
            "execution_config_revision": selected["config_revision"],
            "profile_id": selected["profile_id"],
            "proof": proof,
        }
        candidate["digest"] = digest(candidate)
        candidate = OriginalSelectionCandidateV1.model_validate(candidate).model_dump(mode="json")
        await self.store.admit_credentials(candidate)
        await prepared.revalidate(assert_current=current)
        self.store.reject_credentials(candidate)
        return SelectionPreparation(candidate, prepared, history, generation, inputs)

    async def candidate(self, run_id: str) -> Data:
        _outside_unit()
        run_id = identifier(run_id).hex
        actor = await self.native._operator(run_id)
        with prepared_credential_rejection(self.store):
            return (await self._prepare_selection(run_id, actor)).candidate

    @staticmethod
    def _existing_selection(
        entries: list[Data],
        run_id: str,
        request_id: str,
        request: OriginalSelectionReadmissionInput | None,
        actor: Data,
    ) -> Data | None:
        matches = [
            e
            for e in entries
            if e["kind"] == READMISSION and e["idempotency_key"] == "original-selection:" + request_id
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("Duplicate original selection readmission identity")
        receipt = OriginalSelectionReceiptV1.model_validate(matches[0]["payload"])
        if (
            receipt.run_id != run_id
            or receipt.request_id != request_id
            or receipt.candidate.actor.model_dump() != actor
            or (request is not None and receipt.request != request)
        ):
            raise AccessDenied("The original selection identity belongs to another request or operator")
        return copy.deepcopy(matches[0])

    async def request(self, run_id: str, request_id: str, request: OriginalSelectionReadmissionInput) -> Data:
        _outside_unit()
        if identifier(run_id).hex != run_id or identifier(request_id).hex != request_id:
            raise ValueError("Use exact lowercase native run and request identities")
        request = OriginalSelectionReadmissionInput.model_validate(request.model_dump(mode="json"))
        actor = await self.native._operator(run_id)
        snapshot = {
            **ProjectAccess.actor_snapshot(actor),
            "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
        }
        with prepared_credential_rejection(self.store):
            await self._access(run_id, actor)
            async with self.store._unit():
                entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
                existing = self._existing_selection(entries, run_id, request_id, request, snapshot)
            if existing is not None:
                return await self._readback(run_id, existing, actor)
            prepared = await self._prepare_selection(run_id, actor)
            candidate = prepared.candidate
            if request.candidate_digest != candidate["digest"] or request.policy_digest != candidate["policy_digest"]:
                raise ValueError("The reviewed original selection candidate changed")

            async def current() -> None:
                await self._access(run_id, actor)
                if (
                    self.store.configuration.settings_generation is not prepared.settings_generation
                    or policy_digest() != candidate["policy_digest"]
                    or mode_policy() != candidate["checkpoint_mode_policy_digest"]
                    or ack_policy() != candidate["acknowledgement_policy_digest"]
                ):
                    raise ValueError("Original selection policy or prepared configuration changed")

            await prepared.source.revalidate(assert_current=current)
            async with self.native._locked(run_id, snapshot) as row:
                current_run = await self._access(run_id, actor)
                entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
                existing = self._existing_selection(entries, run_id, request_id, request, snapshot)
                if existing is not None:
                    record = existing
                else:
                    task = _task(await self.journal._task(run_id, "implement"))
                    proof = prepare_selection_readmission(current_run, task, entries, **prepared.inputs)
                    bound, verification = await self._command_custody(current_run, task, entries, lock=True)
                    mode = mode_record(current_run, entries)
                    old_mode = CheckpointModeReceiptV1.model_validate(mode["payload"])
                    await assert_retained_mode_actor(self.journal, current_run, mode, lock=True)
                    if (
                        proof != candidate["proof"]
                        or self.store.configuration.settings_generation is not prepared.settings_generation
                        or bound != [x.model_dump(mode="json") for x in old_mode.candidate.acknowledgements]
                        or verification != candidate["verification_custody_digest"]
                    ):
                        raise ValueError("Original selection readmission changed before native commit")
                    prepared.source.fence()
                    intent = await self.journal._append(
                        run_id,
                        "implement",
                        "original-selection-intent:" + request_id,
                        INTENT,
                        {
                            "candidate_digest": candidate["digest"],
                            "request": request.model_dump(mode="json"),
                            "actor": snapshot,
                        },
                    )
                    payload = OriginalSelectionReceiptV1.model_validate(
                        {
                            "version": 1,
                            "run_id": run_id,
                            "request_id": request_id,
                            "candidate": candidate,
                            "request": request.model_dump(mode="json"),
                            "readmission_intent_digest": intent["digest"],
                        }
                    ).model_dump(mode="json")
                    record = await self.journal._append(
                        run_id, "implement", "original-selection:" + request_id, READMISSION, payload
                    )
                    row.status = "queued"
                    row.data = {
                        **row.data,
                        "status": "queued",
                        "error": None,
                        "resume_existing": False,
                        "updated_at": now(),
                        "original_selection_readmission": selection_reference(record),
                    }
                    await self.store.run_records.save(row)
                    await self.store.assert_ownership()
                    await assert_retained_mode_actor(self.journal, row.data, mode, lock=True)
                    await self._access(run_id, actor)
                    self.store.reject_credentials(record)
            return await self._readback(run_id, record, actor)

    async def get(self, run_id: str, request_id: str) -> Data:
        _outside_unit()
        if identifier(run_id).hex != run_id or identifier(request_id).hex != request_id:
            raise ValueError("Use exact lowercase native run and request identities")
        actor = await self.native._operator(run_id)
        snapshot = {
            **ProjectAccess.actor_snapshot(actor),
            "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
        }
        with prepared_credential_rejection(self.store):
            await self._access(run_id, actor)
            async with self.store._unit():
                entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
                record = self._existing_selection(entries, run_id, request_id, None, snapshot)
            if record is None:
                raise KeyError("Original selection readmission not found")
            return await self._readback(run_id, record, actor)
