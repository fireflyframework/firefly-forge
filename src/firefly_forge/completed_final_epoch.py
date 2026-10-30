# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recognize completed native final custody for later ordinary authority checks."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from firefly_forge.invalid_read_continuity import _bytes
from firefly_forge.journal import digest

Data = dict[str, Any]


@dataclass(frozen=True)
class PreparedCompletedFinal:
    record: Data
    terminal: Data
    ticket: Data
    epoch: Data | None
    _current: Callable[[], Awaitable[None]] = field(repr=False)

    async def current(self) -> None:
        await self._current()


async def _read(journal: Any, run: Data) -> tuple[Data, Data, Data, Data | None, Data] | None:
    from firefly_forge.final_outcome_continuations import final_record
    from firefly_forge.invalid_read_continuities import current_profile_seal
    from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed
    from firefly_forge.invalid_read_epochs import _active, epoch_scope, epoch_snapshot
    from firefly_forge.invalid_read_lease import policy_snapshot
    from firefly_forge.invalid_read_recovery_events import read_recovery_events
    from firefly_forge.known_worker_completions import _entries
    from firefly_forge.native_terminal_completion_models import validate_native_receipt
    from firefly_forge.run_evidence import LIMITS
    from firefly_forge.store import identifier
    from firefly_forge.unknown_command_proof import continuation_scope

    marker = run.get("invalid_read_continuity")
    if not marker:
        return None
    store = journal.store
    async with store._unit():
        entries = _entries(await journal.records.find_for_run(identifier(run["id"])), run["id"])
    if not any(e["kind"] == "final_outcome_continuation" for e in entries):
        return None
    requests = getattr(store, "execution_requests", None)
    if requests is None:
        return None
    if requests.store is not store:
        raise ValueError("Completed final request service belongs to another native store")
    async with store._unit():
        ticket = await requests.get(marker["request_id"])
    # The ordinary final-result worker enters this scope before completing its own ticket.
    # An in-flight action supplies no archived authority; its existing claim guard remains mandatory.
    if ticket["status"] != "completed":
        return None
    await assert_generic_dispatch_allowed(store, journal, run)
    async with store._unit():
        current_ticket = await requests.get(marker["request_id"])
        if _bytes(current_ticket) != _bytes(ticket):
            raise ValueError("Completed final ticket changed during inspection")
        events = await read_recovery_events(requests, ticket["id"], maximum=LIMITS["journal"])
        current = await store.get_run(run["id"])
        after = _entries(await journal.records.find_for_run(identifier(run["id"])), run["id"])
    if _bytes(current) != _bytes(run) or _bytes(after) != _bytes(entries):
        raise ValueError("Completed final custody changed during inspection")
    record = final_record(ticket, entries, events)
    if record is None or ticket["status"] != "completed":
        raise ValueError("Historical epoch requires a completed native final action")
    p = record["payload"]
    prior = p["policy_epoch"]
    if _bytes(policy_snapshot(store.configuration)) != _bytes(p["lease_policy"]):
        raise ValueError("Completed final current continuity policy changed")
    if (
        (run.get("cancel_request") or {}).get("status") == "requested"
        or continuation_scope(run) != continuation_scope(p["run"])
        or _bytes(run.get("checkpoint_mode_adoption")) != _bytes(p["run"].get("checkpoint_mode_adoption"))
        or _bytes(run.get("original_selection_readmission")) != _bytes(p["run"].get("original_selection_readmission"))
        or any(
            _bytes(run.get(key)) != _bytes(p["run"].get(key))
            for key in ("agent", "config_revision", "execution_config", "model_selection", "model_profile_id")
        )
        or type(run.get("attempt")) is not int
        or run["attempt"] < p["candidate"]["attempt"]
        or any(
            type((run.get("usage") or {}).get(key)) is not int or (run.get("usage") or {})[key] < p["run"]["usage"][key]
            for key in ("requests", "input_tokens", "output_tokens")
        )
    ):
        raise ValueError("Completed final scope, native references or spent usage changed")
    terminals = [
        e
        for e in entries
        if e["kind"] == "terminal_completion_receipt"
        and e["sequence"] > record["sequence"]
        and e["payload"]["proof"]["terminal_report_sha256"] == p["candidate"]["report_digest"]
        and any(
            c["consume_record_digest"] == p["candidate"]["consume_record_digest"]
            for c in e["payload"]["proof"]["completions"]
        )
    ]
    if len(terminals) != 1:
        raise ValueError("Completed final action requires its unique accepted terminal receipt")
    validate_native_receipt(terminals[0], entries)
    profile = current_profile_seal()
    epoch = None
    if prior is not None:
        if type(prior.get("version")) is not int or prior["version"] != 1:
            raise ValueError("Completed final policy epoch version changed")
        with epoch_scope(prior):
            _active(p["run"])
        epoch = epoch_snapshot(store, run, entries, legacy_runtime_contract=prior["legacy_runtime_contract"])
        if epoch is not None and (
            epoch["legacy"] != prior["legacy"]
            or epoch["scope_digest"] != prior["scope_digest"]
            or epoch["mode_reference"] != prior["mode_reference"]
            or epoch["selection_reference"] != prior["selection_reference"]
            or epoch["lease_policy_digest"] != p["lease_policy"]["digest"]
        ):
            raise ValueError("Completed final historical authority or current continuity policy changed")
    return record, terminals[0], ticket, epoch, profile


async def validated_completed_final(journal: Any, run: Data) -> PreparedCompletedFinal | None:
    value = await _read(journal, run)
    if value is None:
        return None
    expected = copy.deepcopy(value)

    async def current() -> None:
        live = await _read(journal, await journal.store.get_run(run["id"]))
        if live is None or digest({"custody": live}) != digest({"custody": expected}):
            raise ValueError("Completed final native custody or qualified epoch changed")

    record, terminal, ticket, epoch, _ = copy.deepcopy(value)
    return PreparedCompletedFinal(record, terminal, ticket, epoch, current)
