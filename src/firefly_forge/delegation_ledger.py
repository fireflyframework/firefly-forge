# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Serialize team reservations under the native family-root run lock and WAL."""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

from firefly_forge.delegation_budget import derive_family_budget
from firefly_forge.delegation_models import FactoryTeamV1
from firefly_forge.delegation_waits import wait_history
from firefly_forge.journal import Journal, digest

Data = dict[str, Any]
EVENT = "factory_team_event"


async def verified_native_history(journal: Any, run_id: str) -> list[Data]:
    rows: list[Data] = list(await journal.entries(run_id))
    native_rows = await journal.records.find_for_run(UUID(run_id))
    if len(native_rows) != len(rows):
        raise ValueError("Native family scalar membership changed")
    for native, row in zip(native_rows, rows, strict=True):
        scalars = {
            "run_id": native.run_id.hex,
            "sequence": native.sequence,
            "task_id": native.task_id,
            "kind": native.kind,
            "idempotency_key": native.idempotency_key,
            "previous_hash": native.previous_hash,
            "digest": native.digest,
        }
        if digest(scalars) != digest({key: row.get(key) for key in scalars}) or digest(native.data) != digest(row):
            raise ValueError("Native family scalar custody changed")
    if rows and not Journal.verify_chain(rows):
        raise ValueError("Native family journal does not verify")
    return rows


class FamilyLedger:
    def __init__(self, store: Any, journal: Any) -> None:
        self.store, self.journal = store, journal

    async def events_locked(self, family_id: str) -> tuple[Data, list[Data]]:
        root = await self.journal._run(family_id)
        if root.id.hex != family_id or root.data.get("id") != family_id:
            raise ValueError("Native family identity changed")
        team = FactoryTeamV1.model_validate(root.data.get("factory_team")).model_dump(mode="json")
        rows = await verified_native_history(self.journal, family_id)
        events = []
        for row in rows:
            if row["kind"] != EVENT:
                continue
            payload = row["payload"]
            if (
                set(payload) != {"version", "team_digest", "event"}
                or type(payload["version"]) is not int
                or payload["version"] != 1
                or payload["team_digest"] != digest(team)
            ):
                raise ValueError("Frozen family approval changed")
            events.append(copy.deepcopy(payload["event"]))
        derive_family_budget(family_id, team, events)
        wait_history(events)
        return team, events

    async def state(self, family_id: str) -> Data:
        await self.store.assert_ownership()
        async with self.store._unit():
            team, events = await self.events_locked(family_id)
            state = derive_family_budget(family_id, team, events)
            assert state is not None
            return state

    async def append_locked(self, family_id: str, kind: str, key: str, payload: Data) -> Data:
        team, events = await self.events_locked(family_id)
        existing = next((event for event in events if event["key"] == key), None)
        if existing is not None:
            if digest({"kind": kind, "payload": payload}) != digest(
                {"kind": existing["kind"], "payload": existing["payload"]}
            ):
                raise ValueError("Family nonce already names a different intention")
            return copy.deepcopy(existing)
        event = {
            "family_id": family_id,
            "sequence": len(events) + 1,
            "previous_hash": events[-1]["digest"] if events else "0" * 64,
            "kind": kind,
            "key": key,
            "payload": copy.deepcopy(payload),
        }
        event["digest"] = digest(event)
        derive_family_budget(family_id, team, [*events, event])
        wait_history([*events, event])
        await self.journal._append(
            family_id,
            "implement",
            digest({"family": family_id, "event_key": key}),
            EVENT,
            {"version": 1, "team_digest": digest(team), "event": event},
        )
        return event

    async def reserve(
        self, family_id: str, member_run_id: str, key: str, resource: str, *, tool: str | None = None
    ) -> Data:
        await self.store.assert_ownership()
        payload: Data = {"member_run_id": member_run_id, "resource": resource, "units": 1}
        if tool is not None:
            payload["tool"] = tool
        async with self.store._unit():
            return await self.append_locked(family_id, "budget_reserved", key, payload)

    async def observe(self, family_id: str, reservation_key: str, kind: str) -> Data:
        if kind not in {"budget_dispatched", "budget_settled", "budget_unknown"}:
            raise ValueError("Unsupported resource observation")
        await self.store.assert_ownership()
        async with self.store._unit():
            team, events = await self.events_locked(family_id)
            state = derive_family_budget(family_id, team, events)
            assert state is not None
            reservation = state["reservations"].get(reservation_key)
            if kind == "budget_dispatched" and reservation is not None and reservation["state"] != "reserved":
                raise ValueError("A claimed family reservation cannot dispatch again")
            return await self.append_locked(
                family_id, kind, f"{kind}:{reservation_key}", {"reservation_key": reservation_key}
            )
