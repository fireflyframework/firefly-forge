# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit native acknowledgement of unknown custody for one fresh verification scope."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from pyfly.container import service

from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completions import KnownWorkerCompletions, _actor, _entries, _task
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.sandbox_command_slots import _canonical
from firefly_forge.store import Store, identifier, prepared_credential_rejection
from firefly_forge.unknown_command_models import (
    UnknownCommandAcknowledgementV1,
    UnknownCommandCandidateV1,
    UnknownCommandReceiptV1,
    VerificationFollowupV1,
)
from firefly_forge.unknown_command_proof import ACKNOWLEDGEMENT, continuation_scope, unknown_execution
from firefly_forge.verification_handoffs import VerificationHandoffs, verification_intent
from firefly_forge.verification_models import VerificationAdoption, check_verification_authority
from firefly_forge.verification_source_preparation import (
    PreparedVerificationSource,
    prepare_verification_source,
    verification_catalog_root,
    verification_source_scope,
)

Data = dict[str, Any]


def policy_digest() -> str:
    _outside_unit()
    root = Path(__file__).parent
    return digest(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "unknown_command_acknowledgements.py",
                "unknown_command_models.py",
                "unknown_command_proof.py",
                "unknown_command_controllers.py",
                "scm_authority_models.py",
                "sandbox_command_slots.py",
                "verification_models.py",
                "verification_handoffs.py",
                "sandbox.py",
                "journal.py",
                "engine.py",
                "git_deliveries.py",
            )
        }
    )


@service
class UnknownCommandAcknowledgements:
    def __init__(self, store: Store, journal: Journal, handoffs: VerificationHandoffs) -> None:
        self.store, self.journal, self.handoffs = store, journal, handoffs
        self.native = KnownWorkerCompletions(store, journal)

    async def _read(self, run_id: str, execution_id: str) -> tuple[Data, Data, list[Data], Data]:
        run, task, entries = await self.native._read(run_id)
        if (
            run.get("status") != "paused"
            or run.get("bridge_completion_hold")
            or run.get("verification_handoff", {}).get("status") != "held"
            or run.get("worker_request", {}).get("kind") != "verification"
            or run.get("worker_request", {}).get("status") != "pending"
            or task["id"] != "implement"
            or task["status"] != "paused"
            or task["attempt"] != run["worker_request"].get("attempt")
        ):
            raise ValueError("Unknown command acknowledgement requires a held pending native verification request")
        state = run["verification_handoff"]
        intents = [
            e
            for e in entries
            if e["kind"] == "verification_handoff_intent" and e["payload"].get("id") == state.get("id")
        ]
        held = [
            e
            for e in entries
            if e["kind"] == "verification_handoff_outcome"
            and e["payload"].get("id") == state.get("id")
            and e["payload"].get("status") == "held"
        ]
        if (
            len(intents) != 1
            or len(held) != 1
            or held[0]["sequence"] <= intents[0]["sequence"]
            or intents[0]["payload"].get("request_id") != run["worker_request"].get("id")
            or any(
                state.get(key) != intents[0]["payload"].get(key)
                for key in (
                    "id",
                    "request_id",
                    "source_digest",
                    "commands_digest",
                    "policy_digest",
                    "provider_digest",
                    "continuation",
                )
            )
        ):
            raise ValueError("The held verification state has no exact native intent and held outcome")
        async with self.store._unit():
            row = await self.journal.sandboxes.find_by_id(identifier(execution_id))
            if row is None or row.run_id.hex != run_id or row.parent_attempt != task["attempt"]:
                raise ValueError("Unknown command belongs to another execution or task attempt")
            value = _canonical(row, entries, run_id)
            unknown_execution(row, value)
            if row.snapshot_digest != run.get("sandbox_snapshot", {}).get("digest"):
                raise ValueError("Unknown command must retain its original frozen host runner policy")
        return run, task, entries, value

    async def _prepare(
        self, run_id: str, execution_id: str, *, agent: str, adoption_request_id: str
    ) -> tuple[Data, PreparedVerificationSource, Data]:
        actor = _actor()
        if agent not in {"agentic", "bridge", "codex", "claude"}:
            raise ValueError("Choose an explicit configured coding provider")
        run, task, entries, execution = await self._read(run_id, execution_id)
        from firefly_forge.tool_broker import tool_policy_snapshot
        from firefly_forge.verification_models import automatic_verification_grant

        grant = automatic_verification_grant(tool_policy_snapshot(self.store.configuration))
        if grant is None or sum(e["kind"] == "verification_handoff_intent" for e in entries) >= grant.max_continuations:
            raise ValueError("Automatic verification continuation budget exhausted")
        if any(
            e["kind"] == ACKNOWLEDGEMENT
            and e["payload"].get("execution_id") == execution_id
            and e["payload"].get("followup", {}).get("adoption_request_id") == adoption_request_id
            for e in entries
        ):
            raise ValueError("This future verification adoption already has an unknown-command acknowledgement")
        workspace = self.handoffs.engine.workspaces / run_id

        async def current() -> None:
            await self.store.assert_ownership()
            latest = await self.native._access(run_id, actor)
            if latest != run or CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("Unknown command operator or run changed during preparation")

        prepared = await prepare_verification_source(
            workspace,
            verification_catalog_root(self.store.path, run_id),
            budget=self.journal.source_catalog_budget,
            assert_current=current,
        )
        with verification_source_scope(prepared):
            selection = await self.handoffs.candidates(run_id, agent)
            body = VerificationAdoption.model_validate(
                {
                    "request_id": adoption_request_id,
                    **{
                        key: selection[key]
                        for key in (
                            "pending_request_digest",
                            "source_digest",
                            "commands_digest",
                            "policy_digest",
                            "provider_digest",
                            "agent",
                        )
                    },
                }
            )
            policy = tool_policy_snapshot(self.store.configuration)
            temporary = {
                **run,
                "verification_adoption": {"request": body.model_dump(), "policy_snapshot": policy},
                "verification_handoff": {},
                "verification_refusal": {},
            }
            intent = verification_intent(
                self.store, temporary, workspace, entries, await self.journal.sandbox_executions(run_id)
            )
        followup = VerificationFollowupV1.model_validate(
            {
                "adoption_request_id": adoption_request_id,
                "pending_request_digest": intent["request_digest"],
                "continuation_task_attempt": intent["parent_attempt"] + 1,
                "continuation_scope_digest": continuation_scope(run),
                **{
                    key: intent[key]
                    for key in (
                        "source_digest",
                        "commands_digest",
                        "policy_digest",
                        "provider_digest",
                        "sandbox_digest",
                        "scope_digest",
                        "agent",
                        "parent_attempt",
                        "continuation",
                        "ordinals",
                    )
                },
            }
        ).model_dump()
        start = next(
            (
                e
                for e in entries
                if e["kind"] == "sandbox_intent"
                and e["payload"].get("execution_id") == execution_id
                and e["payload"].get("effect") == "start"
            ),
            None,
        )
        value = {
            "version": 1,
            "run_id": run_id,
            "execution_id": execution_id,
            "actor_digest": digest(ProjectAccess.actor_snapshot(actor)),
            "policy_digest": policy_digest(),
            "run_digest": digest(run),
            "journal_digest": digest({"entries": entries}),
            "task_digest": digest(task),
            "execution_digest": digest(execution),
            "classification": "start_unknown" if start is not None else "closed_before_start",
            "start_intent_digest": start["digest"] if start is not None else None,
            "ordinal": execution["request"]["ordinal"],
            "followup": followup,
        }
        value["digest"] = digest(value)
        candidate = UnknownCommandCandidateV1.model_validate(value).model_dump()
        await self.store.admit_credentials(candidate)
        await prepared.revalidate(assert_current=current)
        latest, latest_task, latest_entries, latest_execution = await self._read(run_id, execution_id)
        if (
            digest(latest) != candidate["run_digest"]
            or latest_task != task
            or digest({"entries": latest_entries}) != candidate["journal_digest"]
            or digest(latest_execution) != candidate["execution_digest"]
        ):
            raise ValueError("Unknown command candidate changed during preparation")
        check_verification_authority(self.store, latest, intent)
        if policy_digest() != candidate["policy_digest"]:
            raise ValueError("Unknown command acknowledgement implementation changed during preparation")
        await current()
        return candidate, prepared, intent

    async def candidate(self, run_id: str, execution_id: str, *, agent: str, adoption_request_id: str) -> Data:
        _outside_unit()
        run_id, execution_id, adoption_request_id = (
            identifier(value).hex for value in (run_id, execution_id, adoption_request_id)
        )
        async with self.handoffs.engine.publication_lock:
            with prepared_credential_rejection(self.store):
                return (
                    await self._prepare(run_id, execution_id, agent=agent, adoption_request_id=adoption_request_id)
                )[0]

    @staticmethod
    def _existing(
        entries: list[Data], request_id: str, body: UnknownCommandAcknowledgementV1, actor: Any
    ) -> Data | None:
        records = [
            e
            for e in entries
            if e["kind"] == ACKNOWLEDGEMENT and e["idempotency_key"] == "sandbox-unknown-ack:" + request_id
        ]
        if not records:
            return None
        if len(records) != 1:
            raise ValueError("Duplicate unknown-command acknowledgement identity")
        receipt = UnknownCommandReceiptV1.model_validate(records[0]["payload"])
        if receipt.request != body or receipt.actor.model_dump() != ProjectAccess.actor_snapshot(actor):
            raise AccessDenied("Unknown-command acknowledgement belongs to another input or operator")
        return records[0]

    async def request(
        self, run_id: str, execution_id: str, request_id: str, body: UnknownCommandAcknowledgementV1
    ) -> Data:
        _outside_unit()
        run_id, execution_id, request_id = (identifier(value).hex for value in (run_id, execution_id, request_id))
        body = UnknownCommandAcknowledgementV1.model_validate(body.model_dump())
        actor = _actor()
        async with self.handoffs.engine.publication_lock:
            with prepared_credential_rejection(self.store):
                await self.native.completions._reader(run_id, actor, execute=True)
                entries = await self.journal.entries(run_id)
                existing = self._existing(entries, request_id, body, actor)
                if existing is not None:
                    if existing["payload"]["execution_id"] != execution_id:
                        raise ValueError("Unknown-command acknowledgement execution differs")
                    return await self._admitted(run_id, existing, actor)
                try:
                    candidate, prepared, intent = await self._prepare(
                        run_id,
                        execution_id,
                        agent=body.followup.agent,
                        adoption_request_id=body.followup.adoption_request_id,
                    )
                except ValueError:
                    existing = self._existing(await self.journal.entries(run_id), request_id, body, actor)
                    if existing is None:
                        raise
                    if existing["payload"]["execution_id"] != execution_id:
                        raise ValueError("Unknown-command acknowledgement execution differs") from None
                    return await self._admitted(run_id, existing, actor)
                if (
                    any(
                        candidate[key] != getattr(body, field)
                        for key, field in (
                            ("digest", "candidate_digest"),
                            ("policy_digest", "policy_digest"),
                            ("run_digest", "expected_run_digest"),
                            ("journal_digest", "journal_digest"),
                            ("execution_digest", "execution_digest"),
                        )
                    )
                    or body.followup.model_dump() != candidate["followup"]
                ):
                    raise ValueError("The reviewed unknown-command candidate changed")
                await self.store.assert_ownership()
                try:
                    async with self.store._unit():
                        initial = await self.native._access(run_id, actor)
                        await self.native.completions._writer(initial, actor)
                        row = await self.journal._run(run_id)
                        current = await self.native._access(run_id, actor)
                        task = await self.journal._task(run_id, "implement")
                        entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
                        retained = await self.journal.sandboxes.find_by_id(identifier(execution_id))
                        if (
                            digest(current) != candidate["run_digest"]
                            or digest({"entries": entries}) != candidate["journal_digest"]
                            or digest(_task(task)) != candidate["task_digest"]
                            or task.attempt != body.followup.parent_attempt
                            or retained is None
                            or digest(_canonical(retained, entries, run_id)) != candidate["execution_digest"]
                            or CURRENT_PRINCIPAL.get() != actor
                        ):
                            raise ValueError("Unknown command run, journal, task or custody changed before commit")
                        unknown_execution(retained, _canonical(retained, entries, run_id))
                        check_verification_authority(self.store, row.data, intent)
                        prepared.fence()
                        payload = UnknownCommandReceiptV1.model_validate(
                            {
                                "version": 1,
                                "disposition": {
                                    "start_unknown": "unknown_abandoned_for_verification",
                                    "closed_before_start": "closed_before_start_retired_for_verification",
                                }[candidate["classification"]],
                                "run_id": run_id,
                                "execution_id": execution_id,
                                "request_id": request_id,
                                "actor": ProjectAccess.actor_snapshot(actor),
                                "candidate": candidate,
                                "request": body.model_dump(),
                                "followup": body.followup.model_dump(),
                            }
                        ).model_dump()
                        self.store.reject_credentials(payload)
                        result = await self.journal._append(
                            run_id, "implement", "sandbox-unknown-ack:" + request_id, ACKNOWLEDGEMENT, payload
                        )
                        await self.store.assert_ownership()
                        await self.native._access(run_id, actor)
                        prepared.fence()
                        if CURRENT_PRINCIPAL.get() != actor:
                            raise AccessDenied("Unknown-command operator changed before commit")
                        self.store.reject_credentials(result)
                except ValueError:
                    existing = self._existing(await self.journal.entries(run_id), request_id, body, actor)
                    if existing is None:
                        raise
                    if existing["payload"]["execution_id"] != execution_id:
                        raise ValueError("Unknown-command acknowledgement execution differs") from None
                    return await self._admitted(run_id, existing, actor)
                return await self._admitted(run_id, result, actor)

    async def _admitted(self, run_id: str, record: Data, actor: Any) -> Data:
        await self.store.admit_credentials(record)
        async with self.store._unit():
            await self.native.completions._reader(run_id, actor, execute=True)
            entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
            exact = next((e for e in entries if e["digest"] == record["digest"] and e["kind"] == ACKNOWLEDGEMENT), None)
            if exact != record or CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("Unknown-command receipt or reader changed during admission")
            self.store.reject_credentials(record)
        return copy.deepcopy(record)

    async def get(self, run_id: str, execution_id: str, request_id: str) -> Data:
        actor = _actor()
        run_id, execution_id, request_id = (identifier(value).hex for value in (run_id, execution_id, request_id))
        await self.native.completions._reader(run_id, actor, execute=True)
        entries = await self.journal.entries(run_id)
        records = [
            e
            for e in entries
            if e["kind"] == ACKNOWLEDGEMENT and e["idempotency_key"] == "sandbox-unknown-ack:" + request_id
        ]
        if len(records) != 1:
            raise KeyError("Unknown-command acknowledgement not found")
        receipt = UnknownCommandReceiptV1.model_validate(records[0]["payload"])
        if receipt.run_id != run_id or receipt.execution_id != execution_id or receipt.request_id != request_id:
            raise ValueError("Unknown-command receipt scope differs")
        return await self._admitted(run_id, records[0], actor)
