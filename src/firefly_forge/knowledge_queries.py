# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""SQL-only Koinon requests commit custody before their one remote invocation."""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Any

import httpx
from koinon_sdk import ApiError
from pydantic import ValidationError

from firefly_forge.journal import Journal, digest
from firefly_forge.knowledge_platform import KoinonSqlInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
from firefly_forge.store import identifier

if TYPE_CHECKING:
    from firefly_forge.knowledge import KnowledgeManager

Data = dict[str, Any]
INTENT, RECEIPT = "koinon_sql_intent", "koinon_sql_receipt"


class KnowledgeQueries:
    def __init__(self, manager: KnowledgeManager) -> None:
        self.manager = manager

    async def state(
        self, run: Data, request_id: str, *, create: Data | None = None, receipt: Data | None = None
    ) -> Data | None:
        journal = self.manager.journal
        if journal is None:
            raise ValueError("Koinon SQL requires native durable journal custody")
        key = digest({"operation": "koinon_sql", "request_id": request_id})
        writing = create is not None or receipt is not None
        if writing:
            await self.manager.store.assert_ownership()
        async with self.manager.store._unit():
            current = (await journal._run(run["id"])).data if writing else await self.manager.store.get_run(run["id"])
            if any(current.get(k) != run.get(k) for k in ("repository_id", "knowledge_snapshot", "source_tree")):
                raise ValueError("Koinon SQL run binding changed")
            rows = await journal.records.find_for_run(identifier(run["id"]))
            entries = [copy.deepcopy(row.data) for row in rows]
            if not Journal.verify_chain(entries):
                raise ValueError("Koinon SQL journal chain changed")
            for native, entry in zip(rows, entries, strict=True):
                scalar = {
                    "run_id": native.run_id.hex,
                    "sequence": native.sequence,
                    "task_id": native.task_id,
                    "idempotency_key": native.idempotency_key,
                    "kind": native.kind,
                    "digest": native.digest,
                    "previous_hash": native.previous_hash,
                }
                if any(entry.get(k) != v for k, v in scalar.items()):
                    raise ValueError("Koinon SQL journal scalar custody changed")
            intents = [e for e in entries if e["kind"] == INTENT and e["idempotency_key"] == key]
            results = [e for e in entries if e["kind"] == RECEIPT and e["idempotency_key"] == key]
            if not intents:
                if results or create is None:
                    if results:
                        raise ValueError("Koinon SQL receipt has no original intent")
                    return None
                pending = [
                    e
                    for e in entries
                    if e["kind"] == INTENT
                    and not any(
                        r["kind"] == RECEIPT
                        and r["idempotency_key"] == e["idempotency_key"]
                        and r["payload"].get("intent_digest") == e["digest"]
                        for r in entries
                    )
                ]
                if pending:
                    raise ValueError("A prior Koinon SQL outcome is uncertain; reconcile it before a fresh request")
                event = await journal._append(run["id"], "knowledge", key, INTENT, create)
                return {"dispatch": True, "intent": event, "receipt": None}
            if len(intents) != 1 or len(results) > 1:
                raise ValueError("Koinon SQL request custody is ambiguous")
            intent = intents[0]
            if create is not None and intent["payload"] != create:
                raise ValueError("Koinon SQL request identity already belongs to another input or actor")
            if receipt is not None:
                evidence = {**receipt, "intent_digest": intent["digest"]}
                if results and results[0]["payload"] != evidence:
                    raise ValueError("Koinon SQL request identity already has a different receipt")
                event = await journal._append(run["id"], "knowledge", key, RECEIPT, evidence)
                return {"dispatch": False, "intent": intent, "receipt": event["payload"]}
            value = results[0]["payload"] if results else None
            if value is not None and value.get("intent_digest") != intent["digest"]:
                raise ValueError("Koinon SQL receipt binding changed")
            return {"dispatch": False, "intent": intent, "receipt": value}

    async def execute(self, run: Data, body: KoinonSqlInput) -> Data:
        settings = self.manager.settings(run)
        if settings.get("provider") != "koinon":
            raise ValueError("Koinon SQL requires a versioned Koinon knowledge binding")
        if self.manager.safe(body.sql, settings) != body.sql:
            raise ValueError("Remove registered credentials before submitting SQL")
        repository = run["repository_id"]
        platform = self.manager.platform
        scope = platform.scope(settings, repository)
        if body.dataset_id not in scope.dataset_ids:
            raise ValueError("Choose an explicitly mapped dataset")
        verify = await platform.authority(repository, settings, run, permission="execute")
        actor = CURRENT_PRINCIPAL.get()
        actor_snapshot = ProjectAccess.actor_snapshot(actor) if actor is not None else run.get("execution_actor")
        bound = {
            "version": 1,
            "request_id": body.request_id,
            "input_sha256": digest(body.model_dump()),
            "binding_sha256": digest(settings),
            "repository_id": repository,
            "dataset_id": body.dataset_id,
            "actor": actor_snapshot,
            "generation_requested": False,
        }
        state = await self.state(run, body.request_id, create=bound)
        assert state is not None
        if not state["dispatch"]:
            if state["receipt"] is None:
                raise ValueError("The original Koinon SQL outcome is uncertain; inspect it before any new request")
            await verify()
            return {
                "status": "completed",
                "request_id": body.request_id,
                "receipt": state["receipt"],
                "result": None,
                "result_state": "not_retained",
            }
        try:
            async with platform.client(settings) as client:
                await platform.grant(client, settings, scope, "query:execute")
                await platform.dataset(client, settings, scope, body.dataset_id)
                await verify()
                # Supplying SQL selects Koinon's native read-only SQL path, never its model-generation path.
                result = await client.query(settings["tenant_id"], body.dataset_id, sql=body.sql)
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result.query_id):
                    raise ValueError("Koinon SQL response identity is unavailable")
                if self.manager.safe(result.query_id, settings) != result.query_id:
                    raise ValueError("Koinon SQL response identity contains a registered credential")
                if self.manager.safe(
                    result.model_dump_json(include={"snapshot_pins"}), settings
                ) != result.model_dump_json(include={"snapshot_pins"}):
                    raise ValueError("Koinon SQL snapshot identity contains a registered credential")
                evidence = {
                    "version": 1,
                    "request_id": body.request_id,
                    "query_id": result.query_id,
                    "execution_status": result.execution_status,
                    "snapshot_pins": result.snapshot_pins,
                    "row_count": result.row_count,
                    "truncated": result.truncated,
                    "result_sha256": digest(result.model_dump(mode="json")),
                    "generation_requested": False,
                }
                completed = await self.state(run, body.request_id, receipt=evidence)
                assert completed is not None
                await platform.grant(client, settings, scope, "query:execute")
                await verify()
        except (ApiError, httpx.HTTPError, ValidationError):
            raise ValueError("The original Koinon SQL outcome is uncertain; inspect it without resubmitting") from None
        import json

        return {
            "status": "completed",
            "request_id": body.request_id,
            "receipt": completed["receipt"],
            "result": json.loads(self.manager.safe(result.model_dump_json(), settings)),
            "result_state": "observed",
        }

    async def status(self, run: Data, request_id: str) -> Data:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("Use the original Koinon SQL request identity")
        settings = self.manager.settings(run)
        if settings.get("provider") != "koinon":
            raise ValueError("Koinon SQL requires a versioned Koinon knowledge binding")
        verify = await self.manager.platform.authority(run["repository_id"], settings, run)
        state = await self.state(run, request_id)
        await verify()
        receipt = (state or {}).get("receipt")
        return {
            "status": "completed" if receipt else "outcome_unknown" if state else "not_found",
            "request_id": request_id,
            "query_id": receipt["query_id"] if receipt else None,
            "receipt": receipt,
            "status_read_started_no_remote_request": True,
        }
