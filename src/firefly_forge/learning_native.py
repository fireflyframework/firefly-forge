# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""V2 operational evidence retains native receipt authority and scoped source versions."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

import httpx
from koinon_sdk import ApiError
from pydantic import ValidationError
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.journal import Journal, digest
from firefly_forge.knowledge_ingestion import KoinonIngestion, KoinonSourceInput
from firefly_forge.knowledge_native_sources import observe_source
from firefly_forge.learning_configuration import frozen_learning
from firefly_forge.learning_models import LearningContext, LearningEnvelope
from firefly_forge.learning_native_models import (
    NativeLearningCitation,
    NativeLearningObservationReceipt,
    OperationalEvidenceV2,
)
from firefly_forge.learning_persistence import ForgeLearningProjectionEntity
from firefly_forge.project_access import AccessDenied
from firefly_forge.store import identifier

if TYPE_CHECKING:
    from firefly_forge.learning_projection import LearningProjection

Data = dict[str, Any]


class NativeLearningProjection:
    def __init__(self, projection: LearningProjection) -> None:
        self.projection = projection
        self.store, self.ledger, self.knowledge = projection.store, projection.ledger, projection.knowledge

    async def evidence(self, record: Any) -> Data:
        envelope = LearningEnvelope.model_validate(record.data["envelope"])
        if (record.kind, record.state) not in {("local_outcome", "verified"), ("observed_failure", "candidate")}:
            raise ValueError("Only retained local outcomes and observed failures can be operational evidence")
        if envelope.type != record.kind or envelope.run_id != record.run_id.hex:
            raise ValueError("Operational evidence differs from its native record ownership")
        run = await self.store.get_run(record.run_id.hex)
        if envelope.context.repository_id != record.repository_id.hex or envelope.context.project_id != run.get(
            "project_id"
        ):
            raise ValueError("Operational evidence differs from its native project scope")
        entries = await self.projection.journal.entries(record.run_id.hex)
        from firefly_forge.known_worker_completions import _entries

        rows = await self.projection.journal.records.find_for_run(record.run_id)
        try:
            if _entries(rows, record.run_id.hex) != entries:
                raise ValueError("Changed native rows")
        except ValueError:
            raise ValueError("Native learning receipt custody changed") from None
        if not Journal.verify_chain(entries) or not envelope.receipts:
            raise ValueError("Operational evidence requires an intact native receipt chain")
        selected = []
        for ref in envelope.receipts:
            matches = [
                x
                for x in entries
                if x["sequence"] == ref.sequence
                and x["digest"] == ref.digest
                and x["idempotency_key"] == ref.key
                and x["kind"] == ref.kind
                and x.get("run_id") == record.run_id.hex
            ]
            if len(matches) != 1 or matches[0]["payload"].get("source_tree") != envelope.context.source_tree:
                raise ValueError("Operational evidence receipt differs from its admitted source")
            selected.extend(matches)
        if record.kind == "local_outcome":
            current = await self.ledger._outcome_evidence(run)
            if any(x not in current for x in selected):
                raise ValueError("Operational evidence no longer matches its passing native checks")
        else:
            from firefly_forge.repair_proposals import failure_fingerprint

            if not any(
                x["task_id"] == "verify" and failure_fingerprint(x["payload"].get("checks", [])) for x in selected
            ):
                raise ValueError("Observed failure requires a bounded native failed-check receipt")
            if run.get("status") == "needs_reconciliation":
                raise ValueError("Unknown native effects cannot become operational hints")
        at = await self.ledger.records_repository.database_time()
        expiry = datetime.fromisoformat(record.data["expires_at"])
        observed_at = datetime.fromisoformat(record.data.get("verified_at") or record.data["observed_at"])
        if not observed_at <= at < expiry:
            raise ValueError("Operational evidence has expired")
        if not frozen_learning(run.get("learning_snapshot"))["settings"]["enabled"]:
            raise ValueError("Operational evidence requires an enabled captured learning policy")
        return OperationalEvidenceV2(
            kind=record.kind,
            record_id=record.id.hex,
            envelope=envelope,
            envelope_sha256=digest(envelope.model_dump(mode="json")),
            expires_at=expiry,
            failure_cause="not_classified" if record.kind == "observed_failure" else None,
        ).model_dump(mode="json")

    async def _locked(self, record_id: str) -> tuple[Any, ForgeLearningProjectionEntity | None]:
        record = await self.projection._record(record_id, review=True)
        await self.projection.journal._run(record.run_id.hex)
        record = await self.projection._record(record_id, review=True, locked=True)
        return record, await self.ledger.projections.for_record(record.id)

    async def submit(self, record_id: str, request_id: str) -> Data:
        identifier(request_id)
        async with self.store._unit():
            record, row = await self._locked(record_id)
            payload = await self.evidence(record)
            run = await self.store.get_run(record.run_id.hex)
            settings = self.knowledge.settings(run)
            if settings.get("provider") != "koinon":
                raise ValueError("Native operational evidence requires the originating run's frozen Koinon binding")
            if row is not None and row.state != "pending":
                if row.data.get("version") != 2:
                    raise ValueError("Retained V1 projections require an explicit migration; they cannot be relabeled")
                if row.state in {"source_admitted", "verified"}:
                    return await self.projection.status(record_id)
                raise ValueError("Native projection is uncertain; reconcile its original source without replay")
            if row is not None and (row.data.get("target") or row.payload_digest != digest(record.data["envelope"])):
                raise ValueError("Native projection payload differs from retained evidence")
        platform = self.knowledge.platform
        scope = platform.scope(settings, run["repository_id"])
        if scope.collection_id is None:
            raise ValueError("Map an existing collection for native operational evidence")
        verify = await platform.authority(run["repository_id"], settings, run, permission="edit")
        async with platform.client(settings) as client:
            await platform.grant(client, settings, scope, "ingest:write")
            await platform.bind(client, settings, scope)
            await verify()
        body = self.projection._body(payload)
        await self.store.admit_credentials(payload)
        async with self.store._unit():
            record, row = await self._locked(record_id)
            if await self.evidence(record) != payload or row is not None and row.state != "pending":
                raise ValueError("Operational evidence changed before native admission")
            if row is None:
                row = ForgeLearningProjectionEntity(
                    id=uuid.uuid4(),
                    record_id=record.id,
                    state="pending",
                    payload_digest=digest(record.data["envelope"]),
                    data={"payload": copy.deepcopy(record.data["envelope"])},
                )
            row.state = "executing"
            row.data = {
                **row.data,
                "version": 2,
                "provider": "koinon",
                "operational_evidence": payload,
                "request_id": request_id,
                "target": copy.deepcopy(run["knowledge_snapshot"]),
                "workspace_id": scope.workspace_id,
                "scope": scope.model_dump(),
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "upstream_authority": "unverified",
            }
            await verify()
            await self.ledger.projections.save(row)
            await self.ledger._append(
                record,
                request_id,
                "projection_intent_v2",
                {
                    "projection_id": row.id.hex,
                    "payload_digest": row.payload_digest,
                    "content_sha256": row.data["content_sha256"],
                    "target": row.data["target"],
                    "scope": scope.model_dump(),
                },
            )

        async def before_effect() -> None:
            async with self.store._unit():
                current, admitted = await self._locked(record_id)
                if await self.evidence(current) != payload or admitted is None or admitted.state != "executing":
                    raise ValueError("Operational evidence changed before upstream dispatch")
            await verify()

        try:
            result = await KoinonIngestion(self.knowledge).source(
                run,
                KoinonSourceInput(
                    request_id=request_id,
                    filename="forge-learning-" + record_id + "-v2.json",
                    content_base64=base64.b64encode(body).decode(),
                ),
                before_effect=before_effect,
            )
            async with self.store._unit():
                current, admitted = await self._locked(record_id)
                if admitted is None or admitted.state != "executing":
                    raise ValueError("Native projection admission changed")
                admitted.state = "source_admitted"
                admitted.data = {**admitted.data, **result["receipt"]}
                await self.ledger.projections.save(admitted)
            await verify()
            return await self.projection.status(record_id)
        except BaseException:
            async with self.store._unit():
                current = await self.projection._record(record_id, cleanup=True)
                await self.projection.journal._run(current.run_id.hex)
                admitted = await self.ledger.projections.for_record(current.id)
                if admitted is not None and admitted.state == "executing":
                    admitted.state = "unknown"
                    await self.ledger.projections.save(admitted)
            raise

    async def observe(self, record_id: str, request_id: str, source_id: str | None) -> Data:
        identifier(request_id)
        async with self.store._unit():
            record, row = await self._locked(record_id)
            if (
                row is None
                or row.data.get("version") != 2
                or row.state not in {"unknown", "source_admitted", "verified"}
            ):
                raise ValueError("An admitted native projection is required for observation")
            selected = source_id or row.data.get("source_id")
            if not isinstance(selected, str):
                raise ValueError("Select the exact native source identifier for reconciliation")
            if row.data.get("source_id") and selected != row.data["source_id"]:
                raise ValueError("Native projection cannot change its admitted source")
            if row.state == "verified":
                return await self.projection.status(record_id)
            payload = await self.evidence(record)
            if payload != row.data["operational_evidence"]:
                raise ValueError("Operational evidence changed since native admission")
            run = await self.store.get_run(record.run_id.hex)
            identity = self.projection._identity(row)
            data = copy.deepcopy(row.data)
        # Ingestion readback uses the original one-shot WAL identity and rejects foreign sources.
        result = await KoinonIngestion(self.knowledge).observe(run, "source", data["request_id"], selected)
        receipt = result["receipt"]
        if receipt["version_state"] != "recorded":
            raise ValueError("Native operational source versions are not yet recorded")
        settings = self.knowledge.settings(run)
        verify = await self.knowledge.platform.authority(run["repository_id"], settings, run, permission="edit")
        citations = await self._citations(run, data, receipt, "Observed local procedure " + record_id, verify)
        async with self.store._unit():
            record, row = await self._locked(record_id)
            if row is None or self.projection._identity(row) != identity or await self.evidence(record) != payload:
                raise ValueError("Native projection changed during source observation")
            await verify()
            row.state = "verified"
            row.data = {**row.data, **receipt, "citations": citations, "reconciliation_id": request_id}
            await self.ledger.projections.save(row)
            await self.ledger._append(
                record,
                request_id,
                "projection_observation_v2",
                {
                    "version": 2,
                    "projection_id": row.id.hex,
                    "submission_request_id": data["request_id"],
                    "workspace_id": data["workspace_id"],
                    "target_revision": data["target"]["revision"],
                    "payload_digest": row.payload_digest,
                    "source_id": selected,
                    "content_sha256": data["content_sha256"],
                    "source_versions": receipt["source_versions"],
                    "citations": citations,
                    "state": "verified",
                    "upstream_authority": "unverified",
                },
            )
        await verify()
        return await self.projection.status(record_id)

    async def _citations(self, run: Data, data: Data, receipt: Data, query: str, verify: Any) -> list[Data]:
        settings = self.knowledge.settings(run)
        platform = self.knowledge.platform
        scope = platform.scope(settings, run["repository_id"])
        if scope.collection_id is None:
            raise ValueError("Native operational evidence requires its admitted collection")
        citations = []
        async with platform.client(settings) as client:
            await platform.grant(client, settings, scope, "knowledge:search")
            await platform.bind(client, settings, scope)
            fresh = await observe_source(
                platform,
                client,
                settings,
                scope,
                receipt["source_id"],
                "forge-learning-" + data["operational_evidence"]["record_id"] + "-v2.json",
                data["content_sha256"],
                verify,
                receipt["asset_ids"],
            )
            if fresh["source_versions"] != receipt["source_versions"]:
                raise ValueError("Native operational source version changed")
            for offset in range(0, len(receipt["asset_ids"]), 128):
                found = await client.search(
                    settings["tenant_id"],
                    query,
                    workspace_id=scope.workspace_id,
                    project_id=scope.project_id,
                    collection_id=scope.collection_id,
                    asset_ids=receipt["asset_ids"][offset : offset + 128],
                    limit=settings["top_k"],
                    answer=False,
                )
                for hit in found.hits:
                    version = next((x for x in receipt["source_versions"] if x["asset_id"] == hit.asset_id), None)
                    if version is None or hit.source_id != receipt["source_id"] or not hit.id:
                        raise AccessDenied("Native operational citation escaped its admitted source")
                    if any(
                        citation.asset_id not in {None, hit.asset_id}
                        or citation.document_id not in {None, hit.asset_id}
                        or citation.source_id not in {None, hit.source_id}
                        for citation in hit.citations
                    ):
                        raise AccessDenied("Native operational citation changed its scoped asset or source")
                    values = hit.model_dump(mode="json")
                    if any(
                        values.get(key) not in {None, version[key]} for key in ("source_version_id", "extraction_id")
                    ):
                        raise ValueError("Native operational citation version changed")
                    citations.append(
                        {
                            "source_id": hit.source_id,
                            "chunk_id": hit.id,
                            "asset_id": hit.asset_id,
                            "tenant_id": settings["tenant_id"],
                            "project_id": scope.project_id,
                            "workspace_id": scope.workspace_id,
                            **version,
                        }
                    )
                if any(
                    not any(
                        citation.asset_id in {None, hit.asset_id}
                        and citation.document_id in {None, hit.asset_id}
                        and citation.source_id in {None, hit.source_id}
                        and (citation.asset_id or citation.document_id or citation.source_id)
                        for hit in found.hits
                    )
                    for citation in found.citations
                ):
                    raise AccessDenied("Native operational response citation has no admitted retrieved asset")
            await platform.grant(client, settings, scope, "knowledge:search")
            await verify()
        if not citations:
            raise ValueError("Native operational evidence has no retrievable scoped citations")
        if self.knowledge.safe(json.dumps(citations), settings) != json.dumps(citations):
            raise ValueError("Native operational citation contains a registered credential")
        await self.store.admit_credentials(citations)
        return [NativeLearningCitation.model_validate(x).model_dump() for x in citations]

    async def retrieve(self, run_id: str, query: str, observed: Data, record_id: str, *, failure: bool = False) -> Data:
        async with self.store._unit():
            run = await self.store.get_run(run_id)
            policy = frozen_learning(run.get("learning_snapshot"))["settings"]
            if not policy.get("projected_retrieval"):
                return {"state": "disabled"}
            if not failure:
                decision = await self.ledger.lookup(run_id, query, observed)
                if not decision["eligible"] or decision.get("candidate_id") != record_id:
                    return {"state": "unavailable", "reason": "record_ineligible"}
            record = await self.projection._record(record_id)
            if failure and not await self._same_failure(record, run, query, observed):
                return {"state": "unavailable", "reason": "record_ineligible"}
            row = await self.ledger.projections.for_record(record.id)
            if row is None or row.state != "verified" or row.data.get("version") != 2:
                return {"state": "unavailable", "reason": "projection_unverified"}
            if not self.projection._same_target(run, row.data.get("target")):
                return {"state": "unavailable", "reason": "projection_identity_mismatch"}
            payload = await self.evidence(record)
            if row.data.get("operational_evidence") != payload or row.payload_digest != digest(record.data["envelope"]):
                raise ValueError("Native projection differs from its immutable operational evidence")
            identity, data = self.projection._identity(row), copy.deepcopy(row.data)
        settings = self.knowledge.settings(run)
        verify = await self.knowledge.platform.authority(run["repository_id"], settings, run)
        try:
            citations = await self._citations(run, data, data, query, verify)
        except (ApiError, httpx.HTTPError, ValidationError, ValueError):
            return {"state": "unavailable", "reason": "native_projection_unavailable"}
        async with self.store._unit():
            current = await self.projection._record(record_id)
            row = await self.ledger.projections.for_record(current.id)
            if row is None or self.projection._identity(row) != identity or await self.evidence(current) != payload:
                raise ValueError("Native projection changed during retrieval")
            await verify()
        return {
            **identity,
            "state": "verified",
            "version": 2,
            "citations": citations,
            "citations_digest": digest({"citations": citations}),
            "upstream_authority": "unverified",
        }

    async def _same_failure(self, record: Any, run: Data, query: str, observed: Data) -> bool:
        from firefly_forge.learning_context import query_digest

        context = LearningContext.model_validate(observed)
        policy = frozen_learning(run.get("learning_snapshot"))["settings"]
        if context.query_digest != query_digest(query) or context.version == "unknown":
            return False
        if (context.repository_id, context.project_id, context.component_id) != (
            run["repository_id"],
            run.get("project_id"),
            run.get("component_id"),
        ):
            return False
        if (record.kind, record.state) != ("observed_failure", "candidate") or record.data["envelope"][
            "context"
        ] != context.model_dump():
            return False
        at = await self.ledger.records_repository.database_time()
        observed_at = datetime.fromisoformat(record.data.get("observed_at") or record.data["verified_at"])
        expiry = min(
            datetime.fromisoformat(record.data["expires_at"]), observed_at + timedelta(hours=policy["max_age_hours"])
        )
        if not observed_at <= at < expiry:
            return False
        try:
            await self.evidence(record)
        except ValueError:
            return False
        return True

    async def failure_hints(self, run_id: str, query: str, observed: Data) -> list[Data]:
        async with self.store._unit():
            run = await self.store.get_run(run_id)
            policy = frozen_learning(run.get("learning_snapshot"))["settings"]
            if (
                not policy["enabled"]
                or not policy["projected_retrieval"]
                or self.knowledge.settings(run).get("provider") != "koinon"
            ):
                return []
            rows = await self.ledger.records_repository.for_repository(identifier(run["repository_id"]))
            candidates = [row for row in rows if await self._same_failure(row, run, query, observed)]
        hints = []
        for record in candidates[: policy["max_candidates"]]:
            projected = await self.retrieve(run_id, query, observed, record.id.hex, failure=True)
            if projected.get("state") == "verified":
                hints.append(
                    {
                        **projected,
                        "kind": "observed_failure",
                        "failure_cause": "not_classified",
                        "may_skip_independent_checks": False,
                        "statement": record.data["envelope"]["statement"],
                    }
                )
        return hints

    async def retain(self, run_id: str, observed: Data, selected: Data) -> Callable[[], None]:
        sessions = self.store.session_provider
        if sessions is None or sessions.current() is None:
            raise RuntimeError("Retained native evidence requires an active admission unit")
        session = sessions.current()
        message = "Retained native projected evidence changed; request an explicit refresh"
        async with self.store._unit():
            run = await self.store.get_run(run_id)
            policy = frozen_learning(run.get("learning_snapshot"))["settings"]
            if not policy["enabled"] or not policy["projected_retrieval"]:
                raise ValueError(message)
            await self.projection._record(selected["record_id"])
            record = await self.ledger.records_repository.find_by_id(
                identifier(selected["record_id"]), lock=LockMode.PESSIMISTIC_READ
            )
            row = await self.ledger.projections.find_by_id(
                identifier(selected["projection_id"]), lock=LockMode.PESSIMISTIC_READ
            )
            expected = {
                key: selected[key]
                for key in ("projection_id", "record_id", "state", "payload_digest", "mapping_digest")
            }
            if (
                record is None
                or row is None
                or row.record_id != record.id
                or row.data.get("version") != 2
                or self.projection._identity(row) != expected
                or not self.projection._same_target(run, row.data.get("target"))
                or not selected.get("citations")
                or selected.get("citations_digest") != digest({"citations": selected["citations"]})
            ):
                raise ValueError(message)
            context = LearningContext.model_validate(observed)
            if record.data["envelope"]["context"] != context.model_dump() or context.version == "unknown":
                raise ValueError(message)
            payload = await self.evidence(record)
            if row.data.get("operational_evidence") != payload or row.payload_digest != digest(record.data["envelope"]):
                raise ValueError(message)
            if selected.get("kind") == "observed_failure" and (
                (record.kind, record.state) != ("observed_failure", "candidate")
                or selected.get("failure_cause") != "not_classified"
                or selected.get("may_skip_independent_checks") is not False
                or selected.get("statement") != record.data["envelope"]["statement"]
            ):
                raise ValueError(message)
            for citation in selected["citations"]:
                native = NativeLearningCitation.model_validate(citation).model_dump()
                scope = row.data["scope"]
                if (
                    native["source_id"] != row.data["source_id"]
                    or native["workspace_id"] != scope["workspace_id"]
                    or native["project_id"] != scope["project_id"]
                    or native["tenant_id"] != self.knowledge.settings(run)["tenant_id"]
                    or {k: native[k] for k in ("asset_id", "native_source_id", "source_version_id", "extraction_id")}
                    not in row.data["source_versions"]
                ):
                    raise ValueError(message)
            record_digest = digest(self.ledger._view(record))
            target = copy.deepcopy(run.get("knowledge_snapshot"))
            expiry = min(
                datetime.fromisoformat(record.data["expires_at"]),
                datetime.fromisoformat(record.data.get("verified_at") or record.data["observed_at"])
                + timedelta(hours=policy["max_age_hours"]),
            )
            at, sampled = await self.ledger.records_repository.database_time(), monotonic()

            def guard() -> None:
                if (
                    sessions.current() is not session
                    or self.projection._identity(row) != expected
                    or digest(self.ledger._view(record)) != record_digest
                    or run.get("knowledge_snapshot") != target
                    or not self.projection._same_target(run, row.data.get("target"))
                    or expiry <= at + timedelta(seconds=monotonic() - sampled)
                ):
                    raise ValueError(message)

            guard()
            return guard

    async def observation_receipt(self, record_id: str, request_id: str) -> Data:
        async with self.store._unit():
            record = await self.projection._record(record_id, review=True)
            event = await self.ledger.events.for_request(record.id, request_id)
            if event is None or event.data.get("kind") != "projection_observation_v2":
                raise KeyError("Exact native projection observation request not found")
            data, payload = event.data, event.data.get("payload", {})
            if (
                event.digest != data.get("digest")
                or event.digest != digest({k: v for k, v in data.items() if k != "digest"})
                or (data.get("record_id"), data.get("run_id"), data.get("request_id"), data.get("sequence"))
                != (record.id.hex, record.run_id.hex, request_id, event.sequence)
            ):
                raise ValueError("Native projection observation receipt integrity changed")
            row = await self.projection._projection(record)
            result = {
                **{k: v for k, v in payload.items() if k != "state"},
                "event_id": event.id.hex,
                "record_id": record.id.hex,
                "repository_id": record.repository_id.hex,
                "run_id": record.run_id.hex,
                "request_id": request_id,
                "kind": "projection_observation_v2",
                "event_state": "verified",
                "current_record_state": record.state,
                "current_projection_state": row.state,
                "actor_id": data.get("actor_id"),
                "created_at": data["created_at"],
                "sequence": event.sequence,
                "digest": event.digest,
            }
            await self.store.admit_credentials(result)
            return NativeLearningObservationReceipt.model_validate(result).model_dump(mode="json")
