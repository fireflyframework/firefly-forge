# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit native admission of stronger checkpoint modes after a known pre-dispatch failure."""

from __future__ import annotations

import copy
from typing import Any

from pyfly.container import service

from firefly_forge.bridge_completions import BridgeCompletions, _outside_unit
from firefly_forge.checkpoint_mode_models import (
    CheckpointModeAdoptionInput,
    CheckpointModeCandidateV1,
    CheckpointModeReceiptV1,
)
from firefly_forge.checkpoint_mode_proof import ADOPTION, prepare_adoption, prepare_closed_history, required_settings
from firefly_forge.checkpoint_mode_runtime import mode_reference, policy_digest
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completions import _entries, _task
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.provider_recoveries import ProviderRecoveries
from firefly_forge.store import Store, identifier, now, prepared_credential_rejection
from firefly_forge.unknown_command_models import UnknownCommandReceiptV1
from firefly_forge.unknown_command_proof import ACKNOWLEDGEMENT, continuation_scope
from firefly_forge.verification_source_preparation import (
    PreparedVerificationSource,
    prepare_verification_source,
    verification_catalog_root,
)

Data = dict[str, Any]


@service
class CheckpointModeAdoptions:
    def __init__(self, store: Store, journal: Journal, engine: Any) -> None:
        self.store, self.journal, self.engine = store, journal, engine
        self.native = ProviderRecoveries(store, journal, engine)
        self.completions = BridgeCompletions(store, journal)

    async def _access(self, run_id: str, actor: Any) -> Data:
        current = await self.native._operator(run_id)
        if current != actor:
            raise AccessDenied("The checkpoint mode reviewing operator changed")
        return await self.completions._reader(run_id, actor, execute=True)

    async def _read(self, run_id: str, actor: Any) -> tuple[Data, Data, list[Data]]:
        run = await self._access(run_id, actor)
        active = self.engine.active.get(run_id)
        if active is not None and not active.done():
            raise ValueError("The original execution must be idle before mode adoption")
        async with self.store._unit():
            current = await self._access(run_id, actor)
            if current != run:
                raise ValueError("Run changed during checkpoint mode preparation")
            task = _task(await self.journal._task(run_id, "implement"))
            entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
        if self.engine.tool_broker and await self.engine.tool_broker.unresolved(run_id):
            raise ValueError("Unknown native tools must be reconciled before mode adoption")
        return run, task, entries

    async def _prepare(self, run_id: str, actor: Any) -> tuple[Data, PreparedVerificationSource, Any]:
        from firefly_forge.unknown_command_acknowledgements import policy_digest as acknowledgement_policy

        run, task, entries = await self._read(run_id, actor)
        settings = self.store.configuration.load_snapshot(run["config_revision"])
        if self.store.configuration.resolve("bridge") != required_settings(settings):
            raise ValueError("Current Bridge settings must equal the frozen settings plus only both required modes")
        history = prepare_closed_history(run, entries, self.store.configuration)
        proof = prepare_adoption(run, task, entries, settings, history=history)
        actor_snapshot = {
            **ProjectAccess.actor_snapshot(actor),
            "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
        }
        policy, ack_policy = policy_digest(), acknowledgement_policy()

        async def current() -> None:
            await self.store.assert_ownership()
            latest, latest_task, latest_entries = await self._read(run_id, actor)
            if (
                latest != run
                or latest_task != task
                or latest_entries != entries
                or self.store.configuration.resolve("bridge") != required_settings(settings)
            ):
                raise ValueError("Checkpoint mode source or frozen admission changed")

        prepared = await prepare_verification_source(
            self.engine.workspaces / run_id,
            verification_catalog_root(self.store.path, run_id),
            budget=self.journal.source_catalog_budget,
            assert_current=current,
        )
        if prepared.source_digest != run.get("verification_handoff", {}).get("source_digest"):
            raise ValueError("The source differs from the completed native verification cut")
        bound, verification_custody = await self._command_custody(run, task, entries)
        value = {
            "version": 1,
            "run_id": run_id,
            "actor": actor_snapshot,
            "policy_digest": policy,
            "acknowledgement_policy_digest": ack_policy,
            "run_digest": digest(run),
            "source_digest": prepared.source_digest,
            "verification_custody_digest": verification_custody,
            "original_scope_digest": continuation_scope(run),
            "proof": proof,
            "acknowledgements": bound,
        }
        value["digest"] = digest(value)
        candidate = CheckpointModeCandidateV1.model_validate(value).model_dump(mode="json")
        await self.store.admit_credentials(candidate)
        await current()
        await prepared.revalidate(assert_current=current)
        self.store.reject_credentials(candidate)
        return candidate, prepared, history

    async def _command_custody(
        self, run: Data, task: Data, entries: list[Data], *, lock: bool = False
    ) -> tuple[list[Data], str]:
        from types import SimpleNamespace

        from firefly_forge.checkpoint_mode_handoff_custody import validate_completed_handoff
        from firefly_forge.sandbox_command_slots import _canonical, _complete, _recovered
        from firefly_forge.unknown_command_proof import acknowledged_continuation

        async with self.store._unit():
            rows = await self.journal.sandboxes.for_run(identifier(run["id"]))
            tasks = await self.journal.tasks.find_for_run(identifier(run["id"]))
            projected = [
                SimpleNamespace(
                    task_id=item.task_id,
                    attempt=item.attempt,
                    data=copy.deepcopy(item.data),
                    status="running"
                    if item.task_id == "implement" and item.attempt == task["attempt"]
                    else item.status,
                )
                for item in tasks
            ]
            values = {row.id.hex: _canonical(row, entries, run["id"]) for row in rows}
            verification = validate_completed_handoff(run, entries, rows, values)
            bound = []
            handoffs = [
                entry
                for entry in entries
                if entry["kind"] == "verification_handoff_intent"
                and entry["payload"].get("id") == run.get("verification_handoff", {}).get("id")
            ]
            for row in rows:
                if _complete(row, values[row.id.hex]) or _recovered(row, values[row.id.hex], entries, tasks):
                    continue
                if len(handoffs) != 1 or not handoffs[0]["payload"].get("adoption_request_id"):
                    raise ValueError("Unfinished command custody requires its exact current verification adoption")
                candidates = [
                    entry
                    for entry in entries
                    if entry["kind"] == ACKNOWLEDGEMENT
                    and entry["payload"].get("execution_id") == row.id.hex
                    and entry["payload"].get("followup", {}).get("adoption_request_id")
                    == handoffs[0]["payload"]["adoption_request_id"]
                ]
                matches = []
                for entry in candidates:
                    receipt = UnknownCommandReceiptV1.model_validate(entry["payload"])
                    actor = acknowledged_continuation(
                        row, values, rows, entries, run, projected, "implement", receipt.candidate.policy_digest
                    )
                    if actor is not None and actor == receipt.actor.model_dump():
                        matches.append((entry, receipt))
                if len(matches) != 1:
                    raise ValueError(
                        "Every unfinished command requires its exact current acknowledged native continuation"
                    )
                entry, receipt = matches[0]
                if lock:
                    from pyfly.data.relational.sqlalchemy import LockMode

                    from firefly_forge.identity_persistence import ForgeIdentityUserRepository

                    identity = await ForgeIdentityUserRepository().find_by_id(
                        identifier(receipt.actor.user_id), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if identity is None or not identity.enabled:
                        raise AccessDenied("The original command acknowledgement operator is unavailable")
                await ProjectAccess(self.store).authorize_actor(
                    receipt.actor.model_dump(), run.get("project_id"), "execute"
                )
                bound.append(
                    {
                        "execution_id": row.id.hex,
                        "execution_digest": receipt.candidate.execution_digest,
                        "record_digest": entry["digest"],
                        "old_policy_digest": receipt.candidate.policy_digest,
                        "actor": receipt.actor.model_dump(mode="json"),
                    }
                )
            return bound, verification["digest"]

    async def candidate(self, run_id: str) -> Data:
        _outside_unit()
        run_id = identifier(run_id).hex
        actor = await self.native._operator(run_id)
        with prepared_credential_rejection(self.store):
            return (await self._prepare(run_id, actor))[0]

    @staticmethod
    def _existing(
        entries: list[Data], run_id: str, request_id: str, request: CheckpointModeAdoptionInput | None, actor: Data
    ) -> Data | None:
        matches = [
            e for e in entries if e["kind"] == ADOPTION and e["idempotency_key"] == "checkpoint-mode:" + request_id
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("Duplicate checkpoint mode receipt identity")
        result = CheckpointModeReceiptV1.model_validate(matches[0]["payload"])
        if (
            result.run_id != run_id
            or result.request_id != request_id
            or result.candidate.actor.model_dump() != actor
            or (request is not None and result.request != request)
        ):
            raise AccessDenied("Checkpoint mode identity belongs to another request or operator")
        return copy.deepcopy(matches[0])

    async def _readback(self, run_id: str, record: Data, actor: Any) -> Data:
        with prepared_credential_rejection(self.store):
            await self.store.admit_credentials(record)
            async with self.store._unit():
                await self._access(run_id, actor)
                entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
                if not any(e == record for e in entries):
                    raise ValueError("Checkpoint mode readback lost its exact native receipt")
                self.store.reject_credentials(record)
        return record

    async def request(self, run_id: str, request_id: str, request: CheckpointModeAdoptionInput) -> Data:
        _outside_unit()
        if identifier(run_id).hex != run_id or identifier(request_id).hex != request_id:
            raise ValueError("Use exact lowercase native run and request identities")
        request = CheckpointModeAdoptionInput.model_validate(request.model_dump(mode="json"))
        actor = await self.native._operator(run_id)
        snapshot = {
            **ProjectAccess.actor_snapshot(actor),
            "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
        }
        with prepared_credential_rejection(self.store):
            await self._access(run_id, actor)
            async with self.store._unit():
                entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
                existing = self._existing(entries, run_id, request_id, request, snapshot)
            if existing is not None:
                return await self._readback(run_id, existing, actor)
            candidate, prepared, history = await self._prepare(run_id, actor)
        if request.candidate_digest != candidate["digest"] or request.policy_digest != candidate["policy_digest"]:
            raise ValueError("The reviewed checkpoint mode candidate changed")
        original_settings = self.store.configuration.load_snapshot(candidate["proof"]["original_config_revision"])
        frozen = self.store.configuration.snapshot("bridge")
        revision = frozen["config_revision"]
        if self.store.configuration.load_snapshot(revision) != required_settings(original_settings):
            raise ValueError("Derived checkpoint mode snapshot differs from the reviewed settings")
        settings_generation = self.store.configuration.settings_generation
        configuration_generation = self.store.configuration.write_generation

        async def final_current() -> None:
            await self._access(run_id, actor)
            if (
                policy_digest() != candidate["policy_digest"]
                or self.store.configuration.resolve("bridge") != required_settings(original_settings)
                or self.store.configuration.settings_generation is not settings_generation
                or self.store.configuration.write_generation is not configuration_generation
            ):
                raise ValueError("Checkpoint mode policy or prepared configuration changed")

        with prepared_credential_rejection(self.store):
            await prepared.revalidate(assert_current=final_current)
            async with self.native._locked(run_id, snapshot) as row:
                current = await self._access(run_id, actor)
                entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
                existing = self._existing(entries, run_id, request_id, request, snapshot)
                if existing is not None:
                    record = existing
                else:
                    task = _task(await self.journal._task(run_id, "implement"))
                    proof = prepare_adoption(current, task, entries, original_settings, history=history)
                    bound, verification_custody = await self._command_custody(current, task, entries, lock=True)
                    if (
                        digest(current) != candidate["run_digest"]
                        or proof != candidate["proof"]
                        or bound != candidate["acknowledgements"]
                        or verification_custody != candidate["verification_custody_digest"]
                        or self.store.configuration.settings_generation is not settings_generation
                        or self.store.configuration.write_generation is not configuration_generation
                    ):
                        raise ValueError("Checkpoint mode admission changed before its native commit")
                    prepared.fence()
                    intent = await self.journal._append(
                        run_id,
                        "implement",
                        "checkpoint-mode-intent:" + request_id,
                        "checkpoint_mode_intent",
                        {
                            "candidate_digest": candidate["digest"],
                            "request": request.model_dump(mode="json"),
                            "actor": snapshot,
                        },
                    )
                    payload = CheckpointModeReceiptV1.model_validate(
                        {
                            "version": 1,
                            "run_id": run_id,
                            "request_id": request_id,
                            "candidate": candidate,
                            "request": request.model_dump(mode="json"),
                            "adoption_intent_digest": intent["digest"],
                            "execution_config_revision": revision,
                        }
                    ).model_dump(mode="json")
                    record = await self.journal._append(
                        run_id, "implement", "checkpoint-mode:" + request_id, ADOPTION, payload
                    )
                    row.status = "queued"
                    row.data = {
                        **row.data,
                        "status": "queued",
                        "error": None,
                        "resume_existing": False,
                        "updated_at": now(),
                        "checkpoint_mode_adoption": mode_reference(record),
                    }
                    await self.store.run_records.save(row)
                    await self.store.assert_ownership()
                    await self._access(run_id, actor)
                    self.store.reject_credentials(record)
            return await self._readback(run_id, record, actor)

    async def get(self, run_id: str, request_id: str) -> Data:
        _outside_unit()
        run_id, request_id = identifier(run_id).hex, identifier(request_id).hex
        actor = await self.native._operator(run_id)
        snapshot = {
            **ProjectAccess.actor_snapshot(actor),
            "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
        }
        await self._access(run_id, actor)
        async with self.store._unit():
            entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
            record = self._existing(entries, run_id, request_id, None, snapshot)
        if record is None:
            raise KeyError("Checkpoint mode adoption not found")
        return await self._readback(run_id, record, actor)
