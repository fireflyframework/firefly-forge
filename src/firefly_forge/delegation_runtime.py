# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Execute single-claim children and yield parent compute through native waits."""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyfly.context.events import ApplicationEventBus

from firefly_forge.delegation_contract import deterministic_join
from firefly_forge.delegation_lifecycle import TERMINAL, ChildResult
from firefly_forge.delegation_models import (
    DelegationCancelInput,
    DelegationJoinInput,
    DelegationWaitInput,
    FamilyScheduling,
)
from firefly_forge.delegation_sources import _files
from firefly_forge.delegation_waits import wait_history
from firefly_forge.journal import digest
from firefly_forge.workspace_tools import WorkspaceTools

if TYPE_CHECKING:
    from firefly_forge.factory_delegations import DelegationSession, FactoryDelegations

Data = dict[str, Any]
ChildWorker = Callable[[Data], Coroutine[Any, Any, Data]]
AUTHORITY_CHECK_SECONDS = 1.0
CHILD_DRAIN_SECONDS = 10.0


@dataclass(frozen=True)
class FamilyChanged:
    manager: object


@dataclass
class ActiveWait:
    session: DelegationSession
    request: DelegationWaitInput
    owner: object
    generation: str
    stage: str = "yielded"
    reacquired: asyncio.Event = field(default_factory=asyncio.Event)


class DelegationRuntime:
    def __init__(self, manager: FactoryDelegations, *, bus: ApplicationEventBus | None = None) -> None:
        self.manager = manager
        self.bus = bus or ApplicationEventBus()
        self.worker: ChildWorker | None = None
        self.sessions: dict[str, DelegationSession] = {}
        self.waits: dict[tuple[str, str], ActiveWait] = {}
        self.changed = asyncio.Event()
        self.executions: dict[str, asyncio.Task[Any]] = {}
        self.workers: dict[str, asyncio.Task[Data]] = {}

    def register(self, session: DelegationSession) -> None:
        current = self.sessions.get(session.run_id)
        if (
            current is not None
            and current.task_key != session.task_key
            and any(value.session.run_id == session.run_id for value in self.waits.values())
        ):
            raise ValueError("An active wait cannot adopt a different parent attempt")
        self.sessions[session.run_id] = session

    async def notify(self) -> None:
        self.changed.set()
        await self.bus.publish(FamilyChanged(self.manager))

    async def scheduling_scope(self) -> FamilyScheduling:
        waiting: set[str] = set()
        ready: list[str] = []
        for session in tuple(self.sessions.values()):
            try:
                async with self.manager.store._unit():
                    _, _, children = await self.manager.children_locked(session)
                if any(value.session is session and value.stage != "active" for value in self.waits.values()):
                    waiting.add(session.run_id)
                if self.worker is not None:
                    ready.extend(identity for identity, child in children.items() if child["state"] == "queued")
            except ValueError:
                continue
        return FamilyScheduling(waiting_parents=frozenset(waiting), ready_children=tuple(sorted(ready)))

    async def reacquire(self, available: int) -> int:
        if type(available) is not int or available < 0:
            raise ValueError("Parent reacquisition requires a nonnegative native scheduler allowance")
        granted = 0
        for value in tuple(self.waits.values()):
            if granted == available:
                break
            if value.stage != "reacquiring":
                continue
            await self._owned(value.session, value.owner)
            value.stage = "active"
            value.reacquired.set()
            granted += 1
        return granted

    async def _owned(self, session: DelegationSession, owner: object) -> None:
        async with self.manager.store._unit():
            await self.manager.authority(session)
            if self.manager.store.execution_ownership_token is not owner:
                raise ValueError("Parent or child work lost its original factory ownership")

    async def execute_child(self, child_id: str, _ordinary_callback: object = None) -> Data:
        worker = self.worker
        if worker is None:
            raise ValueError("A configured factory child worker is required before dispatch")
        selected = None
        for session in tuple(self.sessions.values()):
            async with self.manager.store._unit():
                _, _, children = await self.manager.children_locked(session)
                if child_id in children:
                    selected = session
                    break
        if selected is None:
            raise ValueError("Child dispatch lacks its current native parent session")
        claimed = await self.manager.claim_child(selected, child_id)
        claim_digest = claimed["claim_digest"]
        task = asyncio.current_task()
        if task is None:
            raise ValueError("Child execution requires its owned scheduler task")
        self.executions[child_id] = task
        await self.notify()
        try:
            run = await self.manager.store.get_run(child_id)
            result = ChildResult.model_validate(await self._run_worker(selected, worker, run)).model_dump(mode="json")
            settled = await self.manager.settle_child(selected, child_id, claim_digest, "completed", result)
        except asyncio.CancelledError:
            # The worker has unwound before this terminal record; cancellation never claims completion.
            await self.manager.settle_child(selected, child_id, claim_digest, "cancelled")
            await self.notify()
            raise
        except Exception:
            settled = await self.manager.settle_child(selected, child_id, claim_digest, "needs_reconciliation")
        finally:
            self.executions.pop(child_id, None)
        await self.notify()
        return settled

    async def cancel(self, session: DelegationSession, request: DelegationCancelInput) -> Data:
        async with self.manager.store._unit():
            family_id, events, children = await self.manager.children_locked(session)
            child = children.get(request.delegation_id)
            if child is None:
                raise ValueError("Child cancellation includes a foreign child or parent attempt")
            key = f"child-cancel:{session.run_id}:{request.request_id}"
            existing = next((event for event in events if event["key"] == key), None)
            if existing is not None and digest(existing["payload"]["request"]) != digest(
                request.model_dump(mode="json")
            ):
                raise ValueError("Child cancellation nonce already selects a different child")
            if child["state"] in TERMINAL:
                return self.manager.observation(child)
            await self.manager.ledger.append_locked(
                family_id,
                "child_cancel_requested",
                key,
                {
                    "parent_task_key": session.task_key,
                    "request": request.model_dump(mode="json"),
                    "claim_digest": child["claim_digest"],
                },
            )
            if child["state"] == "queued":
                await self.manager.ledger.append_locked(
                    family_id,
                    "child_cancelled",
                    "child-terminal:" + request.delegation_id,
                    {"child_run_id": request.delegation_id, "claim_digest": None, "result": None},
                )
        await self.notify()
        task = self.executions.get(request.delegation_id)
        if child["state"] == "running":
            if task is None:
                return {"version": 1, "outcome": "unavailable", "reason": "original_child_worker_unavailable"}
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), CHILD_DRAIN_SECONDS)
            except asyncio.CancelledError:
                caller = asyncio.current_task()
                if caller is not None and caller.cancelling():
                    raise
            except TimeoutError:
                return {"version": 1, "outcome": "unavailable", "reason": "child_drain_unconfirmed"}
        return await self.manager._observed_child(session, request.delegation_id)

    async def _run_worker(self, session: DelegationSession, worker: ChildWorker, run: Data) -> Data:
        owner = self.manager.store.execution_ownership_token
        await self._owned(session, owner)
        task = asyncio.create_task(worker(copy.deepcopy(run)))
        self.workers[run["id"]] = task

        def finished(current: asyncio.Task[Data]) -> None:
            if self.workers.get(run["id"]) is current:
                self.workers.pop(run["id"])
            if not current.cancelled():
                current.exception()

        task.add_done_callback(finished)
        try:
            while True:
                await self._owned(session, owner)
                try:
                    return await asyncio.wait_for(asyncio.shield(task), AUTHORITY_CHECK_SECONDS)
                except TimeoutError:
                    if task.done():
                        return task.result()
        finally:
            if not task.done():
                task.cancel()
                deadline = asyncio.get_running_loop().time() + CHILD_DRAIN_SECONDS
                cancelled = False
                while not task.done():
                    remaining = max(0, deadline - asyncio.get_running_loop().time())
                    try:
                        done, _ = await asyncio.wait([task], timeout=remaining)
                    except asyncio.CancelledError:
                        cancelled = True
                        continue
                    if not done:
                        raise ValueError("Child cancellation drain is unavailable; its dispatch remains spent")
                if not task.cancelled():
                    task.result()
                if cancelled:
                    raise asyncio.CancelledError
            elif not task.cancelled():
                task.exception()

    async def wait_status(self, session: DelegationSession, request_id: str) -> Data:
        async with self.manager.store._unit():
            _, events, _ = await self.manager.children_locked(session)
            record = wait_history(events).get((session.run_id, request_id))
            if record is None or record["start"]["parent_task_key"] != session.task_key:
                return {"version": 1, "outcome": "unavailable", "reason": "wait_not_admitted"}
            return self._result(record)

    async def join(self, session: DelegationSession, request: DelegationJoinInput) -> Data:
        key = f"join:{session.run_id}:{request.request_id}"
        async with self.manager.store._unit():
            _, events, children = await self.manager.children_locked(session)
            existing = next((event for event in events if event["key"] == key), None)
            if existing is not None:
                if existing["payload"]["parent_task_key"] != session.task_key or digest(
                    existing["payload"]["request"]
                ) != digest(request.model_dump(mode="json")):
                    raise ValueError("Join nonce already selects a different child result")
                return copy.deepcopy(existing["payload"]["result"])
            if not set(request.delegation_ids) <= children.keys():
                raise ValueError("Join includes a foreign child or parent attempt")
        source = await _files(WorkspaceTools(session.workspace))
        async with self.manager.store._unit():
            family_id, _, children = await self.manager.children_locked(session)
            selected = [self.manager.observation(children[identity]) for identity in request.delegation_ids]
            result = deterministic_join(selected, {name: row["sha256"] for name, row in source.items()})
            if any(
                source.get(change["path"], {}).get("executable") != change["base_executable"]
                for child in selected
                for change in child["changes"]
            ):
                result.update(status="held", holds=sorted(set(result["holds"]) | {"parent_source_changed"}), changes=[])
            if digest(source) != digest(await _files(WorkspaceTools(session.workspace))):
                raise ValueError("Parent source changed during deterministic fan-in")
            await self.manager.store.admit_credentials(result)
            event = await self.manager.ledger.append_locked(
                family_id,
                "join",
                key,
                {"parent_task_key": session.task_key, "request": request.model_dump(mode="json"), "result": result},
            )
            return copy.deepcopy(event["payload"]["result"])

    @staticmethod
    def _result(record: Data) -> Data:
        if record["result"] is None:
            return {"version": 1, "outcome": "pending", "wait_digest": record["digest"], "resumed": False}
        result = record["result"]
        return {
            "version": 1,
            "outcome": result["outcome"],
            "wait_digest": record["digest"],
            "result_digest": record["result_digest"],
            "resumed": record["resumed"],
            **copy.deepcopy(result["observation"]),
        }

    async def wait(self, session: DelegationSession, request: DelegationWaitInput) -> Data:
        from firefly_forge.agentic_checkpoint_invocation import current_checkpoint_invocation

        if current_checkpoint_invocation() is not None:
            raise ValueError("Durable parked SDK tool-result custody is required before checkpoint child waits")
        key = session.run_id, request.request_id
        owner = self.manager.store.execution_ownership_token
        if owner is None:
            raise ValueError("A parent wait requires current native factory ownership")
        active = ActiveWait(session, request, owner, uuid.uuid4().hex)
        wake = asyncio.Event()

        async def changed(event: FamilyChanged) -> None:
            if event.manager is self.manager:
                wake.set()

        # Subscribe before reading native state; notifications are hints, never evidence.
        self.bus.subscribe(FamilyChanged, changed, owner=active)
        admitted = False
        try:
            async with self.manager.store._unit():
                family_id, events, children = await self.manager.children_locked(session)
                records = wait_history(events)
                if existing := records.get(key):
                    if existing["start"]["parent_task_key"] != session.task_key or digest(
                        existing["start"]["request"]
                    ) != digest(request.model_dump(mode="json")):
                        raise ValueError("Wait nonce already names a different child observation")
                    if not existing["resumed"]:
                        raise ValueError("A restarted or concurrent callback cannot adopt a pending wait")
                    return self._result(existing)
                if any(value.session.run_id == session.run_id for value in self.waits.values()):
                    raise ValueError("A parent yields compute through one current wait")
                if not set(request.delegation_ids) <= children.keys() or request.after_sequence > len(events):
                    raise ValueError("Wait selects a foreign child or future native cursor")
                await self.manager.ledger.append_locked(
                    family_id,
                    "wait_started",
                    f"wait:{session.run_id}:{request.request_id}",
                    {
                        "parent_run_id": session.run_id,
                        "parent_task_key": session.task_key,
                        "request": request.model_dump(mode="json"),
                        "generation": active.generation,
                        "owner_pid": self.manager.store._ownership_pid,
                    },
                )
            self.waits[key] = active
            admitted = True
            await self.notify()
            loop = asyncio.get_running_loop()
            deadline = None if request.timeout_seconds is None else loop.time() + request.timeout_seconds
            while True:
                wake.clear()
                async with self.manager.store._unit():
                    family_id, events, children = await self.manager.children_locked(session)
                    if self.manager.store.execution_ownership_token is not owner:
                        raise ValueError("A new factory owner cannot adopt a parent wait")
                    selected = [children[identity] for identity in request.delegation_ids]
                    outcome = (
                        "unavailable"
                        if any(child["state"] == "needs_reconciliation" for child in selected)
                        else "satisfied"
                        if all(child["state"] in TERMINAL for child in selected)
                        else "timed_out"
                        if deadline is not None and loop.time() >= deadline
                        else None
                    )
                    if outcome is not None:
                        await self._record_result(session, request, family_id, events, children, outcome)
                        break
                delay = (
                    AUTHORITY_CHECK_SECONDS
                    if deadline is None
                    else max(0, min(AUTHORITY_CHECK_SECONDS, deadline - loop.time()))
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), delay)
            active.stage = "reacquiring"
            await self.notify()
            while not active.reacquired.is_set():
                await self._owned(session, owner)
                with suppress(TimeoutError):
                    await asyncio.wait_for(active.reacquired.wait(), AUTHORITY_CHECK_SECONDS)
            async with self.manager.store._unit():
                family_id, events, _ = await self.manager.children_locked(session)
                record = wait_history(events)[key]
                await self.manager.ledger.append_locked(
                    family_id,
                    "wait_resumed",
                    f"wait-resume:{session.run_id}:{request.request_id}",
                    {
                        "wait_digest": record["digest"],
                        "result_digest": record["result_digest"],
                        "generation": active.generation,
                    },
                )
            return await self.wait_status(session, request.request_id)
        except asyncio.CancelledError:
            if admitted:
                async with self.manager.store._unit():
                    family_id, events, children = await self.manager.children_locked(session)
                    if wait_history(events)[key]["result"] is None:
                        await self._record_result(session, request, family_id, events, children, "cancelled")
            raise
        finally:
            if admitted:
                self.waits.pop(key, None)
            self.bus.unsubscribe_owners([active])
            await self.notify()

    async def _record_result(
        self,
        session: DelegationSession,
        request: DelegationWaitInput,
        family_id: str,
        events: list[Data],
        children: dict[str, Data],
        outcome: str,
    ) -> None:
        record = wait_history(events)[(session.run_id, request.request_id)]
        await self.manager.ledger.append_locked(
            family_id,
            "wait_result",
            f"wait-result:{session.run_id}:{request.request_id}",
            {
                "wait_digest": record["digest"],
                "outcome": outcome,
                "observation": {
                    "version": 1,
                    "family_id": family_id,
                    "after_sequence": len(events),
                    "children": [self.manager.observation(children[identity]) for identity in request.delegation_ids],
                },
            },
        )
