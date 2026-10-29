# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Factory-owned child admissions retain the original family approval and native WAL."""

from __future__ import annotations

import asyncio
import copy
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from firefly_forge.delegation_contract import FROZEN_FIELDS, child_run
from firefly_forge.delegation_ledger import FamilyLedger, verified_native_history
from firefly_forge.delegation_models import DelegationStartInput, FactoryTeamV1
from firefly_forge.delegation_sources import capture_child_source
from firefly_forge.journal import digest
from firefly_forge.persistence import ForgeRunEntity
from firefly_forge.project_access import ProjectAccess
from firefly_forge.store import Store, identifier
from firefly_forge.workspace_tools import WorkspaceTools

Data = dict[str, Any]
Readiness = Callable[[Data], Awaitable[Data]]


def approval_digest(run: Data) -> str:
    return digest(
        {
            name: run.get(name)
            for name in (*FROZEN_FIELDS, "id", "repository_id", "brief", "commands", "factory_context")
        }
    )


class FactoryDelegations:
    def __init__(self, store: Store, journal: Any, *, sources: Path, readiness: Readiness) -> None:
        self.store, self.journal = store, journal
        self.ledger = FamilyLedger(store, journal)
        self.sources, self.readiness = sources, readiness
        if sources.is_symlink():
            raise ValueError("Factory child origins require a private owned directory")
        sources.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sources.stat().st_uid != os.geteuid() or sources.stat().st_mode & 0o077:
            raise ValueError("Factory child origins require a private owned directory")
        self._locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()

    def bind(self, run_id: str, task_key: str, workspace: Path) -> DelegationSession:
        return DelegationSession(self, run_id, task_key, workspace)

    async def authority(self, session: DelegationSession) -> Data:
        await self.store.assert_ownership()
        run = await self.store.get_run(session.run_id)
        FactoryTeamV1.model_validate(run.get("factory_team"))
        task = await self.journal._task(session.run_id, "implement")
        if (
            run["status"] != "developing"
            or run.get("active_task") != "implement"
            or run.get("cancel_request", {}).get("status") == "requested"
            or task.status != "running"
            or type(task.attempt) is not int
            or type(run.get("attempt")) is not int
            or task.attempt < 1
            or task.attempt != run.get("attempt")
            or task.data.get("idempotency_key") != session.task_key
            or session.task_key != uuid.uuid5(uuid.NAMESPACE_URL, f"forge:{run['id']}:implement:{task.attempt}").hex
        ):
            raise ValueError("Delegation requires its exact current parent implementation attempt")
        history = await verified_native_history(self.journal, session.run_id)
        intents = [
            entry
            for entry in history
            if entry["kind"] == "intent"
            and entry["task_id"] == "implement"
            and entry["idempotency_key"] == session.task_key
        ]
        if (
            len(intents) != 1
            or digest(intents[0]["payload"]) != digest(task.data.get("intent", {}))
            or any(entry["kind"] == "receipt" and entry["idempotency_key"] == session.task_key for entry in history)
        ):
            raise ValueError("Delegation requires its unique unfinished native parent intent")
        await ProjectAccess(self.store).authorize_execution(run)
        return run

    async def member_locked(self, run: Data) -> tuple[str, list[Data]]:
        family_id = run.get("factory_context", {}).get("family_id", run["id"])
        _, events = await self.ledger.events_locked(family_id)
        if family_id != run["id"]:
            admissions = [
                event
                for event in events
                if event["kind"] == "child_admitted" and event["payload"].get("child_run_id") == run["id"]
            ]
            if len(admissions) != 1 or admissions[0]["payload"].get("approval_digest") != approval_digest(run):
                raise ValueError("Child authority differs from its native admission")
        return family_id, events

    @staticmethod
    def existing(events: list[Data], parent_id: str, request: DelegationStartInput) -> Data | None:
        found = [
            event
            for event in events
            if event["kind"] == "child_admitted"
            and event["payload"].get("parent_run_id") == parent_id
            and event["payload"].get("request", {}).get("request_id") == request.request_id
        ]
        if not found:
            return None
        if len(found) != 1 or digest(found[0]["payload"]["request"]) != digest(request.model_dump(mode="json")):
            raise ValueError("Child nonce already names a different task")
        return found[0]

    @staticmethod
    def admitted(event: Data) -> Data:
        return {
            "version": 1,
            "delegation_id": event["payload"]["child_run_id"],
            "task_id": event["payload"]["request"]["task_id"],
            "state": "queued",
            "admission_digest": event["digest"],
            "family_id": event["family_id"],
            "delivery_verified": False,
        }

    async def start(self, session: DelegationSession, request: DelegationStartInput) -> Data:
        await self.store.admit_credentials(request.model_dump(mode="json"))
        lock = self._locks.setdefault((session.run_id, request.request_id), asyncio.Lock())
        async with lock:
            async with self.store._unit():
                parent = await self.authority(session)
                family_id, events = await self.member_locked(parent)
                if existing := self.existing(events, session.run_id, request):
                    return self.admitted(existing)
            ready = await self.readiness(parent)
            if ready.get("status") != "ready":
                raise ValueError("Selected factory child journey is unavailable: " + ",".join(ready.get("holds", [])))
            before = approval_digest(parent)
            source = await capture_child_source(WorkspaceTools(session.workspace), self.sources / uuid.uuid4().hex)
            child = child_run(parent, request, source)
            child["factory_context"]["parent_task_key"] = session.task_key
            child["factory_context"]["digest"] = digest(
                {key: value for key, value in child["factory_context"].items() if key != "digest"}
            )
            await self.store.admit_credentials(child)
            async with self.store._unit():
                await self.ledger.events_locked(family_id)
                await self.journal._run(session.run_id)
                current = await self.authority(session)
                _, events = await self.member_locked(current)
                if approval_digest(current) != before:
                    raise ValueError("Parent frozen approval changed during child preparation")
                if existing := self.existing(events, session.run_id, request):
                    return self.admitted(existing)
                if any(
                    event["kind"] == "child_admitted"
                    and event["payload"].get("parent_run_id") == parent["id"]
                    and event["payload"].get("request", {}).get("task_id") == request.task_id
                    for event in events
                ):
                    raise ValueError("A child task identity already has its original admitted attempt")
                await self.store.run_records.save(
                    ForgeRunEntity(
                        id=identifier(child["id"]),
                        repository_id=identifier(child["repository_id"]),
                        status="queued",
                        data=copy.deepcopy(child),
                    )
                )
                event = await self.ledger.append_locked(
                    family_id,
                    "child_admitted",
                    f"child:{parent['id']}:{request.request_id}",
                    {
                        "parent_run_id": parent["id"],
                        "child_run_id": child["id"],
                        "parent_task_key": session.task_key,
                        "request": request.model_dump(mode="json"),
                        "approval_digest": approval_digest(child),
                        "source_digest": source["digest"],
                    },
                )
                await self.store.event(child["id"], "queued", "Delegated task queued in its isolated workspace")
                await self.authority(session)
                return self.admitted(event)


class DelegationSession:
    def __init__(self, manager: FactoryDelegations, run_id: str, task_key: str, workspace: Path) -> None:
        self.manager, self.run_id, self.task_key, self.workspace = manager, run_id, task_key, workspace

    async def start(self, request: DelegationStartInput) -> Data:
        return await self.manager.start(self, request)
