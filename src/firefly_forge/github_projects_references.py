# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact Git-delivery references; unknown publication cannot inherit an older draft."""

from typing import Any

from firefly_forge.git_deliveries import GitDeliveries

Data = dict[str, Any]


def checkpoint_pull_requests(
    entries: list[Data], checkpoints: list[Data], repository_id: str, branch: str | None
) -> list[tuple[str, str]]:
    intents = [entry for entry in entries if entry["kind"] == "git_delivery_remote_intent"]
    receipts = [entry for entry in entries if entry["kind"] == "git_delivery_remote_receipt"]
    known = {checkpoint["request_id"]: checkpoint for checkpoint in checkpoints}
    if len(known) != len(checkpoints):
        raise ValueError("Checkpoint request identity is ambiguous")
    if len({entry["idempotency_key"] for entry in intents}) != len(intents) or len(
        {entry["idempotency_key"] for entry in receipts}
    ) != len(receipts):
        raise ValueError("Remote checkpoint effect identity is ambiguous")
    GitDeliveries._remote_state(entries)
    references = []
    for receipt in receipts:
        matching = [intent for intent in intents if intent["idempotency_key"] == receipt["idempotency_key"]]
        if len(matching) != 1:
            raise ValueError("A remote checkpoint receipt has no exact admitted intent")
        intent, payload = matching[0], receipt["payload"]
        checkpoint = known.get(payload.get("request_id"))
        if (
            checkpoint is None
            or payload.get("revision") != checkpoint["revision"]
            or intent["payload"].get("checkpoint_digest") != checkpoint["checkpoint_digest"]
            or intent["payload"].get("repository_id") != repository_id
            or intent["payload"].get("branch") != branch
        ):
            raise ValueError("Remote PR reference differs from the exact original checkpoint scope")
        url = payload.get("reference", {}).get("url")
        if not isinstance(url, str) or not 1 <= len(url) <= 2048:
            raise ValueError("Remote checkpoint receipt has no bounded PR URL")
        references.append((url, checkpoint["checkpoint_digest"]))
    return references
