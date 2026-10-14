# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Repository-private learning records established by immutable native evidence."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta
from typing import Any

from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.journal import Journal, digest
from firefly_forge.learning_configuration import frozen_learning
from firefly_forge.learning_context import observe_context, query_digest
from firefly_forge.learning_models import (
    LearningCandidate,
    LearningContext,
    LearningEnvelope,
    LearningPolicy,
    eligibility,
)
from firefly_forge.learning_persistence import (
    ForgeLearningEventEntity,
    ForgeLearningEventRepository,
    ForgeLearningProjectionEntity,
    ForgeLearningProjectionRepository,
    ForgeLearningRecordEntity,
    ForgeLearningRepository,
)
from firefly_forge.project_access import CURRENT_PRINCIPAL, Permission, ProjectAccess
from firefly_forge.store import Store, identifier

Data = dict[str, Any]


@service
class LearningLedger:
    def __init__(
        self,
        store: Store,
        journal: Journal,
        records: ForgeLearningRepository | None = None,
        events: ForgeLearningEventRepository | None = None,
        projections: ForgeLearningProjectionRepository | None = None,
    ) -> None:
        self.store, self.journal = store, journal
        self.records_repository = records or ForgeLearningRepository()
        self.events = events or ForgeLearningEventRepository()
        self.projections = projections or ForgeLearningProjectionRepository()
        self.engine: Any = None

    @staticmethod
    def _view(row: ForgeLearningRecordEntity) -> Data:
        return {"id": row.id.hex, "state": row.state, "kind": row.kind, **copy.deepcopy(row.data)}

    async def _scope(self, repository_id: str, permission: Permission = "read") -> None:
        actor = CURRENT_PRINCIPAL.get()
        if actor is not None:
            await ProjectAccess(self.store).require_repository(actor, repository_id, permission)
        await self.store.repository(repository_id)

    async def records(self, repository_id: str) -> list[Data]:
        await self._scope(repository_id)
        async with self.store._unit():
            rows = await self.records_repository.for_repository(identifier(repository_id))
            origins = [await self.store.get_run(row.run_id.hex) for row in rows]
            visible = {run["id"] for run in await ProjectAccess(self.store).filter_runs(origins)}
            return [self._view(row) for row in rows if row.run_id.hex in visible]

    async def _append(self, row: ForgeLearningRecordEntity, request_id: str, kind: str, payload: Data) -> Data:
        existing = await self.events.for_record(row.id)
        prior = next((item for item in existing if item.request_id == request_id), None)
        if prior:
            if prior.data["kind"] != kind or prior.data["payload"] != payload:
                raise ValueError("Learning request identity already belongs to another decision")
            return copy.deepcopy(prior.data)
        if len(existing) >= 100:
            raise ValueError("Learning evidence history exceeds its bound")
        at = await self.records_repository.database_time()
        event = {
            "record_id": row.id.hex,
            "run_id": row.run_id.hex,
            "sequence": len(existing) + 1,
            "request_id": request_id,
            "kind": kind,
            "payload": copy.deepcopy(payload),
            "created_at": at.isoformat(),
            "previous_hash": existing[-1].digest if existing else "0" * 64,
        }
        event["digest"] = digest(event)
        await self.events.save(
            ForgeLearningEventEntity(
                id=uuid.uuid4(),
                record_id=row.id,
                sequence=event["sequence"],
                request_id=request_id,
                digest=event["digest"],
                data=event,
            )
        )
        return event

    async def _outcome_evidence(self, run: Data) -> list[Data]:
        entries = await self.journal.entries(run["id"])
        if not entries or not Journal.verify_chain(entries):
            raise ValueError("Verified learning requires immutable native source and command receipts")
        receipts = [item for item in entries if item["kind"] == "receipt"]
        verify = next((item for item in reversed(receipts) if item["task_id"] == "verify"), None)
        checkpoint = next((item for item in reversed(receipts) if item["task_id"] == "checkpoint"), None)
        if verify is None or checkpoint is None:
            raise ValueError("Verified learning requires immutable verification and checkpoint receipts")
        evidence = verify["payload"]
        checks = evidence.get("checks", [])
        if (
            not checks
            or evidence.get("failed_checks")
            or any(type(item.get("exit_code")) is not int or item["exit_code"] != 0 for item in checks)
            or checks != run.get("final_verification")
            or evidence.get("source_tree") != run.get("source_tree")
            or checkpoint["payload"].get("source_tree") != run.get("source_tree")
            or checkpoint["payload"].get("checkpoint_revision") != run.get("checkpoint_revision")
        ):
            raise ValueError("Verified learning receipts do not match passing checks and final source")
        if any(
            item["kind"] in {"tool_intent", "tool_unknown"}
            and not any(
                (receipt["kind"] == "tool_receipt" and receipt["idempotency_key"] == item["idempotency_key"])
                or (
                    receipt["kind"] == "tool_reconciled"
                    and receipt["payload"].get("correlation_id") == item["payload"].get("correlation_id")
                    and receipt["payload"].get("decision") == "abandoned_without_replay"
                )
                for receipt in entries
            )
            for item in entries
        ):
            raise ValueError("Learning cannot verify an unresolved tool effect")
        selected = [verify, checkpoint]
        if run.get("assurance", {}).get("review_agent"):
            review = next((item for item in reversed(receipts) if item["task_id"] == "review"), None)
            if review is None or review["payload"].get("source_tree") != run["source_tree"]:
                raise ValueError("Verified learning requires the configured independent review receipt")
            selected.append(review)
        return selected

    async def capture_outcome(self, run_id: str) -> Data:
        async with self.store._unit():
            run_row = await self.journal._run(run_id)
            run = copy.deepcopy(run_row.data)
            await ProjectAccess(self.store).authorize_execution(run)
            receipts = await self._outcome_evidence(run)
            if run["status"] != "succeeded":
                raise ValueError("Only a completed verified source outcome can become learning evidence")
            if self.engine is None:
                raise ValueError("Verified learning requires an actual native workspace inspector")
            workspace = await self.engine.validated_workspace(run_id)
            source = await self.engine._inspect_workspace(run_id, workspace, {})
            expected = {
                "base_revision": run["base_revision"],
                "head": run["checkpoint_revision"],
                "source_tree": run["source_tree"],
            }
            if any(source.get(key) != value for key, value in expected.items()):
                raise ValueError("Learning source changed since its immutable verification receipts")
            context = await observe_context(run, source, workspace, run["brief"], immutable_source=True)
            retained = await self.engine._inspect_workspace(run_id, workspace, {})
            if any(retained.get(key) != value for key, value in expected.items()):
                raise ValueError("Learning source changed during declaration observation")
            correlation = uuid.uuid5(uuid.NAMESPACE_URL, "forge-local-outcome:" + run_id + ":" + run["source_tree"]).hex
            existing = next(
                (
                    item
                    for item in await self.records_repository.for_run(run_row.id)
                    if item.correlation_id == correlation
                ),
                None,
            )
            if existing:
                return self._view(existing)
            checks = run["final_verification"]
            statement = (
                "Observed local verification at source tree "
                + run["source_tree"]
                + ": "
                + "; ".join(" ".join(item["argv"])[:500] + " returned exit code 0" for item in checks[:8])
                + ". This records a local procedure outcome; external references remain unverified."
            )
            envelope = LearningEnvelope.model_validate(
                {
                    "version": 1,
                    "type": "local_outcome",
                    "correlation_id": correlation,
                    "run_id": run_id,
                    "task_id": "verify",
                    "role": "qa",
                    "context": context.model_dump(),
                    "source": {key: source[key] for key in ("base_revision", "head", "source_tree")},
                    "purpose": "verification",
                    "question": run["brief"],
                    "statement": statement,
                    "receipts": [
                        {
                            "kind": item["kind"],
                            "key": item["idempotency_key"],
                            "sequence": item["sequence"],
                            "digest": item["digest"],
                        }
                        for item in receipts
                    ],
                }
            )
            await self.store.admit_credentials(envelope.model_dump())
            policy = LearningPolicy.model_validate(frozen_learning(run.get("learning_snapshot"))["settings"])
            if not policy.enabled:
                raise ValueError("Learning requires an enabled policy captured by this run")
            at = await self.records_repository.database_time()
            row = ForgeLearningRecordEntity(
                id=uuid.uuid4(),
                repository_id=run_row.repository_id,
                run_id=run_row.id,
                correlation_id=correlation,
                state="verified",
                kind="local_outcome",
                data={
                    "envelope": envelope.model_dump(mode="json"),
                    "verified_at": at.isoformat(),
                    "expires_at": (at + timedelta(hours=policy.max_age_hours)).isoformat(),
                },
            )
            await self.records_repository.save(row)
            await self._append(
                row,
                correlation,
                "local_outcome",
                {"envelope_digest": digest(row.data["envelope"]), "state": "verified"},
            )
            await self.journal._append(
                run_id,
                "verify",
                correlation,
                "learning_outcome",
                {
                    "record_id": row.id.hex,
                    "envelope_digest": digest(row.data["envelope"]),
                    "source_tree": run["source_tree"],
                },
            )
            await self.projections.save(
                ForgeLearningProjectionEntity(
                    id=uuid.uuid4(),
                    record_id=row.id,
                    state="pending",
                    payload_digest=digest(row.data["envelope"]),
                    data={"payload": copy.deepcopy(row.data["envelope"])},
                )
            )
            return self._view(row)

    async def capture_failure(self, run_id: str) -> Data | None:
        async with self.store._unit():
            run_row = await self.journal._run(run_id)
            run = copy.deepcopy(run_row.data)
            await ProjectAccess(self.store).authorize_execution(run)
            policy = LearningPolicy.model_validate(frozen_learning(run.get("learning_snapshot"))["settings"])
            if not policy.enabled:
                return None
            entries = await self.journal.entries(run_id)
            if not Journal.verify_chain(entries):
                raise ValueError("Observed learning requires an intact native receipt chain")
            receipt = next(
                (item for item in reversed(entries) if item["kind"] == "receipt" and item["task_id"] == "verify"), None
            )
            if receipt is None:
                raise ValueError("Observed learning requires a verification receipt")
            failures = [
                check
                for check in receipt["payload"].get("checks", [])
                if type(check.get("exit_code")) is int and check["exit_code"] == 1
            ]
            if not failures:
                return None
            if self.engine is None:
                raise ValueError("Observed learning requires an actual workspace inspector")
            await self.engine.assert_ownership()
            workspace = self.engine.workspaces / run_id
            secrets = (
                self.store.configuration.load_snapshot(run["config_revision"]) if run.get("config_revision") else {}
            )
            source = await self.engine._inspect_workspace(run_id, workspace, secrets)
            if source["source_tree"] != receipt["payload"].get("source_tree"):
                raise ValueError("Observed check receipt differs from the current source")
            context = await observe_context(run, source, workspace, run["brief"])
            correlation = uuid.uuid5(uuid.NAMESPACE_URL, "forge-observed-check:" + receipt["digest"]).hex
            existing = next(
                (
                    item
                    for item in await self.records_repository.for_run(run_row.id)
                    if item.correlation_id == correlation
                ),
                None,
            )
            if existing:
                return self._view(existing)
            envelope = LearningEnvelope.model_validate(
                {
                    "type": "observed_failure",
                    "correlation_id": correlation,
                    "run_id": run_id,
                    "task_id": "verify",
                    "role": "qa",
                    "context": context.model_dump(),
                    "source": {key: source[key] for key in ("base_revision", "head", "source_tree")},
                    "purpose": "verification",
                    "question": run["brief"],
                    "statement": "Observed check returned exit code 1 at source tree "
                    + source["source_tree"]
                    + ": "
                    + "; ".join(" ".join(item["argv"])[:500] for item in failures[:8])
                    + ". Its cause has not been classified; this observation cannot authorize repair suppression.",
                    "receipts": [
                        {
                            "kind": "receipt",
                            "key": receipt["idempotency_key"],
                            "sequence": receipt["sequence"],
                            "digest": receipt["digest"],
                        }
                    ],
                }
            )
            await self.store.admit_credentials(envelope.model_dump())
            at = await self.records_repository.database_time()
            row = ForgeLearningRecordEntity(
                id=uuid.uuid4(),
                repository_id=run_row.repository_id,
                run_id=run_row.id,
                correlation_id=correlation,
                state="candidate",
                kind="observed_failure",
                data={
                    "envelope": envelope.model_dump(mode="json"),
                    "observed_at": at.isoformat(),
                    "expires_at": (at + timedelta(hours=policy.max_age_hours)).isoformat(),
                },
            )
            await self.records_repository.save(row)
            await self._append(
                row,
                correlation,
                "observed_check",
                {
                    "envelope_digest": digest(row.data["envelope"]),
                    "state": "candidate",
                },
            )
            await self.journal._append(
                run_id,
                "verify",
                correlation,
                "learning_observation",
                {
                    "record_id": row.id.hex,
                    "envelope_digest": digest(row.data["envelope"]),
                    "source_tree": source["source_tree"],
                    "state": "candidate",
                },
            )
            return self._view(row)

    async def lookup(self, run_id: str, query: str, observed: Data) -> Data:
        context = LearningContext.model_validate(observed)
        if context.query_digest != query_digest(query):
            raise ValueError("Learning query differs from its observed context")
        async with self.store._unit():
            run = await self.journal._run(run_id)
            await ProjectAccess(self.store).authorize_execution(run.data)
            if (context.repository_id, context.project_id, context.component_id) != (
                run.repository_id.hex,
                run.data.get("project_id"),
                run.data.get("component_id"),
            ):
                raise ValueError("Learning context differs from the authorized run scope")
            at = await self.records_repository.database_time()
            policy = LearningPolicy.model_validate(frozen_learning(run.data.get("learning_snapshot"))["settings"])
            if not policy.enabled:
                return {"eligible": False, "reason": "scope_denied", "refresh_required": False}
            rows = await self.records_repository.for_repository(run.repository_id)
            fallback = {"eligible": False, "reason": "no_candidate", "refresh_required": False}
            for row in rows:
                original = row.data["envelope"]["context"]
                if (
                    original["query_digest"] != context.query_digest
                    or original.get("project_id") != context.project_id
                    or original.get("component_id") != context.component_id
                ):
                    continue
                origin = await self.store.get_run(row.run_id.hex)
                try:
                    receipts = await self._outcome_evidence(origin)
                    expected = row.data["envelope"]["receipts"]
                    receipt_verified = all(
                        any(
                            item["sequence"] == ref["sequence"]
                            and item["digest"] == ref["digest"]
                            and item["idempotency_key"] == ref["key"]
                            for item in receipts
                        )
                        for ref in expected
                    ) and bool(expected)
                except ValueError:
                    receipt_verified = False
                candidate = LearningCandidate.model_validate(
                    {
                        "id": row.id.hex,
                        "state": row.state,
                        "kind": row.kind,
                        "context": original,
                        "verified_at": datetime.fromisoformat(row.data.get("verified_at") or row.data["observed_at"]),
                        "expires_at": min(
                            datetime.fromisoformat(row.data["expires_at"]),
                            datetime.fromisoformat(row.data.get("verified_at") or row.data["observed_at"])
                            + timedelta(hours=policy.max_age_hours),
                        ),
                        "receipt_verified": receipt_verified,
                        "unknown_effect": origin.get("status") == "needs_reconciliation",
                    }
                )
                decision = eligibility(candidate, context, at).model_dump()
                if decision["eligible"]:
                    return {**decision, "outcome": self._view(row)}
                fallback = decision
            return fallback

    async def invalidate(self, record_id: str, *, reason: str, request_id: str) -> Data:
        identifier(request_id)
        if not reason.strip() or len(reason) > 2000:
            raise ValueError("Learning invalidation requires a bounded reason")
        await self.store.admit_credentials(reason)
        async with self.store._unit():
            row = await self.records_repository.find_by_id(identifier(record_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Learning record not found")
            actor = CURRENT_PRINCIPAL.get()
            if actor is not None:
                project_id = row.data["envelope"]["context"].get("project_id")
                if project_id:
                    await ProjectAccess(self.store).require_project(actor, project_id, "review")
                elif not await ProjectAccess(self.store).current_administrator(actor):
                    raise ValueError("Unassigned learning reviews require a factory administrator")
            await self._append(row, request_id, "invalidation", {"reason": reason, "state": "invalidated"})
            row.state = "invalidated"
            await self.records_repository.save(row)
            return self._view(row)
