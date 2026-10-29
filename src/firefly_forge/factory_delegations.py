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
from firefly_forge.delegation_lifecycle import TERMINAL, ChildResult, child_lifecycle
from firefly_forge.delegation_models import DelegationStartInput, FactoryTeamV1
from firefly_forge.delegation_sources import capture_child_source, verify_child_source
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
        self._claims: dict[tuple[str, str], object] = {}

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

    async def children_locked(self, session: DelegationSession) -> tuple[str, list[Data], dict[str, Data]]:
        parent = await self.store.get_run(session.run_id)
        family_id = parent.get("factory_context", {}).get("family_id", parent["id"])
        await self.ledger.events_locked(family_id)
        current = await self.authority(session)
        _, events = await self.member_locked(current)
        children = {
            identity: child
            for identity, child in child_lifecycle(events).items()
            if child["parent_run_id"] == session.run_id and child["parent_task_key"] == session.task_key
        }
        return family_id, events, children

    @staticmethod
    def observation(child: Data) -> Data:
        return copy.deepcopy(
            {
                name: child[name]
                for name in (
                    "version",
                    "delegation_id",
                    "task_id",
                    "state",
                    "admission_digest",
                    "claim_digest",
                    "receipt_digest",
                    "summary",
                    "changes",
                    "delivery_verified",
                )
            }
        )

    async def observe(self, session: DelegationSession, child_ids: list[str]) -> Data:
        if not child_ids or len(set(child_ids)) != len(child_ids):
            raise ValueError("Select each original child once")
        async with self.store._unit():
            family_id, events, children = await self.children_locked(session)
            if not set(child_ids) <= children.keys():
                raise ValueError("The selection includes a foreign child or parent attempt")
            return {
                "version": 1,
                "family_id": family_id,
                "after_sequence": len(events),
                "children": [self.observation(children[identity]) for identity in child_ids],
            }

    async def claim_child(self, session: DelegationSession, child_id: str) -> Data:
        async with self.store._unit():
            _, _, children = await self.children_locked(session)
            child = children.get(child_id)
            if child is None or child["state"] != "queued":
                raise ValueError("Child is foreign, terminal or already claimed; no automatic replay")
            run = await self.store.get_run(child_id)
            await self._child_admission(session, run, child)
            source = copy.deepcopy(run["factory_context"]["source"])
        await verify_child_source(source)
        owner = self.store.execution_ownership_token
        if owner is None:
            raise ValueError("A child dispatch requires current native factory ownership")
        async with self.store._unit():
            family_id, _, children = await self.children_locked(session)
            child = children.get(child_id)
            if child is None or child["state"] != "queued":
                raise ValueError("Child dispatch is already spent")
            run = await self.store.get_run(child_id)
            await self._child_admission(session, run, child)
            if self.store.execution_ownership_token is not owner:
                raise ValueError("Factory ownership changed during child preparation")
            event = await self.ledger.append_locked(
                family_id,
                "child_claimed",
                "child-claim:" + child_id,
                {
                    "child_run_id": child_id,
                    "admission_digest": child["admission_digest"],
                    "generation": uuid.uuid4().hex,
                    "owner_pid": self.store._ownership_pid,
                    "source_digest": child["source_digest"],
                },
            )
        self._claims[(child_id, event["digest"])] = owner
        return await self._observed_child(session, child_id)

    async def _child_admission(
        self, session: DelegationSession, run: Data, child: Data, *, require_queued: bool = True
    ) -> None:
        context = run.get("factory_context") or {}
        parent = await self.authority(session)
        if (
            (require_queued and run.get("status") != "queued")
            or context.get("parent_run_id") != session.run_id
            or context.get("parent_task_key") != session.task_key
            or context.get("parent_attempt") != parent["attempt"]
            or context.get("source", {}).get("digest") != child["source_digest"]
            or digest({"request": context.get("request")}) != digest({"request": child["request"]})
            or digest({key: value for key, value in context.items() if key != "digest"}) != context.get("digest")
        ):
            raise ValueError("Child admission differs from its original parent, source or task")
        _, events = await self.member_locked(run)
        admission = next(event for event in events if event["digest"] == child["admission_digest"])
        if admission["payload"]["approval_digest"] != approval_digest(run):
            raise ValueError("Child frozen approval changed")
        await ProjectAccess(self.store).authorize_execution(run)

    async def _observed_child(self, session: DelegationSession, child_id: str) -> Data:
        observed = await self.observe(session, [child_id])
        return dict(observed["children"][0])

    async def settle_child(
        self, session: DelegationSession, child_id: str, claim_digest: str, state: str, result: Data | None = None
    ) -> Data:
        if state not in TERMINAL:
            raise ValueError("Select a terminal child observation")
        clean = ChildResult.model_validate(result).model_dump(mode="json") if result is not None else None
        if (state == "completed") != (clean is not None):
            raise ValueError("Only a known completed child has a structured result")
        if clean is not None:
            await self.store.admit_credentials(clean)
        async with self.store._unit():
            family_id, events, children = await self.children_locked(session)
            child = children.get(child_id)
            if child is None or child["claim_digest"] != claim_digest:
                raise ValueError("Child outcome lacks its original native dispatch claim")
            payload = {"child_run_id": child_id, "claim_digest": claim_digest, "result": clean}
            if child["state"] in TERMINAL:
                terminal = next(event for event in events if event["digest"] == child["receipt_digest"])
                if terminal["kind"] != "child_" + state or digest(terminal["payload"]) != digest(payload):
                    raise ValueError("A known child outcome cannot change")
                return self.observation(child)
            owner = self._claims.get((child_id, claim_digest))
            if (
                owner is None
                or self.store.execution_ownership_token is not owner
                or self.store._ownership_pid != child["claim"]["owner_pid"]
            ):
                raise ValueError("A new factory owner cannot adopt a spent child callback")
            run = await self.store.get_run(child_id)
            await self._child_admission(session, run, child, require_queued=False)
            if clean is not None:
                original_files = run["factory_context"]["source"]["files"]
                if any(
                    change["base_sha256"] != original_files.get(change["path"], {}).get("sha256")
                    or change["base_executable"] != original_files.get(change["path"], {}).get("executable")
                    for change in clean["changes"]
                ):
                    raise ValueError("Child result changed its original source baseline")
            if clean is not None:
                self.store.reject_credentials(clean)
            await self.ledger.append_locked(family_id, "child_" + state, "child-terminal:" + child_id, payload)
            _, _, current = await self.children_locked(session)
            observed = self.observation(current[child_id])
        self._claims.pop((child_id, claim_digest), None)
        return observed

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
