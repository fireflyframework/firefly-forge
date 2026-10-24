# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read-only checkpoint facts for already authorized native conversation references."""

from __future__ import annotations

import copy
import json
from typing import Any

from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.journal import Journal
from firefly_forge.project_access import AccessDenied
from firefly_forge.store import identifier

Data = dict[str, Any]


def delivery_facts(run: Data, entries: list[Data]) -> Data:
    if (
        not entries
        or len(entries) > 10000
        or len(json.dumps(entries).encode()) > 16 * 1024 * 1024
        or not Journal.verify_chain(entries)
        or any(entry.get("run_id") != run["id"] for entry in entries)
    ):
        raise AccessDenied("Current checkpoint facts require the exact intact native run journal")
    receipts = GitDeliveries._records(entries)
    if not receipts:
        raise AccessDenied("The projected checkpoint has no exact native receipt")
    latest = receipts[-1]
    if latest["run_id"] != run["id"] or latest["revision"] != run.get("checkpoint_revision"):
        raise AccessDenied("Current checkpoint projection differs from the immutable receipt")
    GitDeliveries._remote_state(entries, allow_pending=True)
    intents = [
        e
        for e in entries
        if e["kind"] == "git_delivery_remote_intent" and e["payload"].get("request_id") == latest["request_id"]
    ]
    remote = [
        e
        for e in entries
        if e["kind"] == "git_delivery_remote_receipt" and e["payload"].get("request_id") == latest["request_id"]
    ]
    if any(
        any(intent["payload"].get(k) != latest[k] for k in ("revision", "checkpoint_digest", "branch"))
        or intent["payload"].get("repository_id") != run["repository_id"]
        for intent in intents
    ):
        raise AccessDenied("Remote checkpoint facts belong to another checkpoint or repository")
    if any(
        not any(
            result["idempotency_key"] == intent["idempotency_key"]
            and result["payload"].get("intent_digest") == intent["digest"]
            for intent in intents
        )
        or result["payload"].get("reference", {}).get("revision") != latest["revision"]
        for result in remote
    ):
        raise AccessDenied("Remote checkpoint facts require an exact preceding native intent")
    if len({result["idempotency_key"] for result in remote}) != len(remote):
        raise AccessDenied("Remote checkpoint facts contain duplicate native confirmations")
    confirmed = [r for r in remote if intents and r["idempotency_key"] == intents[-1]["idempotency_key"]]
    return {
        "evidence": "verified_native_journal",
        "checkpoint": {
            key: latest[key]
            for key in (
                "revision",
                "source_tree",
                "checkpoint_digest",
                "sequence",
                "changed_file_count",
                "branch",
                "created_at",
            )
        },
        "verification_status": latest.get("verification_status", "pending"),
        "remote": {
            "status": confirmed[-1]["payload"]["remote_status"]
            if confirmed
            else "unknown"
            if intents
            else "not_requested",
            "pr_url": remote[-1]["payload"]["reference"]["url"] if remote else None,
        },
        "product_acceptance": "not_established_by_checkpoint",
    }


async def current_delivery_facts(store: Any, journal: Any, run: Data) -> Data | None:
    if not run.get("checkpoint_revision") and not run.get("git_delivery_summary"):
        return None
    records = getattr(journal, "records", None)
    reader = getattr(records, "find_for_run", None)
    if not callable(reader):
        return {"evidence": "unavailable", "product_acceptance": "not_established_by_checkpoint"}
    # Native row reads join a caller's writer unit without suspending it for credential admission.
    async with store._unit():
        rows = await reader(identifier(run["id"]))
        entries = [{**copy.deepcopy(row.data), "digest": row.digest} for row in rows]
    return delivery_facts(run, entries)
