# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit one-claim continuation tickets for closed implementation checkpoints."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn, cast

from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2, InvocationBinding, RestoreInput
from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
from firefly_forge.agentic_checkpoint_invocation import invocation_cut, runtime_contract, workspace_cut
from firefly_forge.bridge_completions import BridgeCompletions, _outside_unit, _request_id
from firefly_forge.execution_request_persistence import ForgeExecutionRequestEntity
from firefly_forge.identity import Principal
from firefly_forge.journal import Journal, digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import identifier, now, prepared_credential_rejection

KIND = "agent_restore"


class RestoreExecution:
    def __init__(self, service: AgentRestores, ticket: dict[str, Any], bundle: Any) -> None:
        self.service, self.ticket = service, copy.deepcopy(ticket)
        self.intent, self.record, self.result_record, self.body = bundle
        payload = CheckpointPayloadV2.model_validate(self.record["payload"])
        self.binding = payload.invocation
        self.checkpoint = BoundaryCheckpoint.decode(payload.snapshot.encode(), payload.checkpoint_digest)
        self.request = RestoreInput.model_validate(ticket["request"]["input"])
        self.generation = ticket["generation"]
        self.run_id, self.request_id = ticket["run_id"], ticket["request"]["completion_request_id"]
        self.reference = {
            "completion_request_id": self.request_id,
            "intent_digest": self.intent["digest"],
            "principal_id": self.intent["payload"]["descriptor"]["principal_id"],
        }

    async def guard(self) -> None:
        await self.service.guard(self.ticket)

    async def guard_source(self) -> None:
        _outside_unit()
        await self.guard()
        if self.binding.contract != runtime_contract() or self.binding.deadline_ms <= int(time.time() * 1000):
            raise ValueError("Original checkpoint runtime contract changed or deadline expired")
        actual = await workspace_cut(self.service.store, self.service.workspace(self.run_id))
        if actual != self.binding.workspace:
            raise ValueError("Retained workspace drifted or is externally mutable; restoration refused")
        await self.guard()

    async def consume(self, checkpoint_digest: str, response_digest: str) -> None:
        if checkpoint_digest != self.checkpoint.digest or response_digest != hashlib.sha256(self.body).hexdigest():
            raise ValueError("Original completion gate differs from the selected checkpoint/result")
        await self.guard_source()

        run = await self.service.store.get_run(self.run_id)
        body = await self.service.checkpoints.consume_restored(
            run,
            self.request_id,
            self.request,
            self.generation,
            self.guard,
            reader=await self.service.operator(self.ticket["request"]["actor"]),
            outside_guard=self.guard_source,
        )
        if body != self.body:
            raise ValueError("Consumed result changed before exposure")
        await self.guard_source()
        self.service.store.reject_credentials(
            [self.service.checkpoints.credential_payload(self.record), self.body.decode("utf-8")]
        )

    async def receipt(self, engine: Any, key: str, evidence: dict[str, Any], checkpoint: dict[str, Any]) -> None:
        _outside_unit()
        from firefly_forge.native_terminal_completions import native_terminal_acceptance

        async with native_terminal_acceptance(
            self.service.journal, self.run_id, "receipt", key, evidence, checkpoint, restoration=self
        ) as terminal:
            with prepared_credential_rejection(self.service.store):
                await self.service.store.admit_credentials(evidence)
                operator = await self.service.operator(self.ticket["request"]["actor"])
                async with self.service.store._unit():
                    run = await self.service.store.get_run(self.run_id)
                    await self.service.completions._writer(run, operator)
                    await self.service.journal._run(self.run_id)
                    await self.guard()
                    await engine._receipt(
                        self.run_id, key, evidence, checkpoint=checkpoint, terminal=terminal, prepared=True
                    )
                    self.service.store.reject_credentials(evidence)
                await self.service.access(
                    await self.service.store.get_run(self.run_id), self.ticket["request"]["actor"]
                )
                self.service.store.reject_credentials(evidence)


class AgentRestores:
    def __init__(self, store: Any, requests: Any, engine: Any) -> None:
        self.store, self.requests, self.engine = store, requests, engine
        self.journal = engine.journal or Journal(store)
        self.completions = BridgeCompletions(store, self.journal)
        self.checkpoints = AgentCheckpoints(self.completions)

    async def candidates(self, run_id: str) -> dict[str, Any]:
        from firefly_forge.agent_checkpoint_candidates import candidates

        return await candidates(self, run_id)

    def workspace(self, run_id: str) -> Path:
        return Path(self.engine.workspaces) / identifier(run_id).hex

    async def operator(self, actor: dict[str, Any]) -> Principal:
        user = await self.completions._identity(actor["user_id"])
        return Principal(
            user.id,
            user.issuer,
            user.subject,
            authentication_method=actor["authentication_method"],
            scm_delegation=copy.deepcopy(actor.get("scm_delegation")),
        )

    async def access(self, run: dict[str, Any], actor: dict[str, Any]) -> None:
        access = ProjectAccess(self.store)
        if not run.get("execution_actor") or not run.get("project_id"):
            raise AccessDenied("Restoration requires its original explicit project execution actor")
        for selected in (actor, run["execution_actor"]):
            principal = await self.operator(selected)
            administrator = await access.authorize_actor(selected, run["project_id"], "execute")
            if not administrator:
                await access.require_repository(principal, run["repository_id"], "execute")
            if run.get("publish"):
                await access.authorize_actor(selected, run["project_id"], "release")
        await access.authorize_execution(run)
        await self.store.assert_campaign_current(run)

    async def validate_db(
        self, run: dict[str, Any], context: InvocationBinding, intent: dict[str, Any], *, active: bool
    ) -> None:
        if (
            run.get("agent") != "bridge"
            or invocation_cut(run) != context.run_cut
            or run.get("cancel_request", {}).get("status") == "requested"
            or run["status"] not in ({"developing"} if active else {"needs_reconciliation", "interrupted"})
            or not run.get("bridge_completion_hold")
        ):
            raise ValueError("Run is not the original eligible held implementation")
        await self.checkpoints._unconsumed(run["id"], intent["idempotency_key"])
        task = await self.journal._effect_task(run["id"], intent["payload"]["task_key"])
        if (
            task.task_id != "implement"
            or task.attempt != intent["payload"]["task_attempt"]
            or task.status not in ({"running"} if active else {"needs_reconciliation"})
        ):
            raise ValueError("Only the same unfinished implementation task attempt can restore")
        await self._command_restore(run, context)

    async def _command_restore(
        self, run: dict[str, Any], context: InvocationBinding, *, ticket: dict[str, Any] | None = None
    ) -> None:
        if "execute_command" not in context.grants:
            return
        from firefly_forge.agent_checkpoint_models import ConsumePayloadV2
        from firefly_forge.runner_factory_models import validate_factory_pool
        from firefly_forge.sandbox import SandboxReconciliationRequired
        from firefly_forge.sandbox_command_slots import COMMAND_CONTRACT, _canonical, _complete
        from firefly_forge.sandbox_models import capture_sandbox, validate_sandbox_snapshot

        def refuse() -> NoReturn:
            raise ValueError("Prior runner custody or pending effects prevent command-enabled checkpoint restore")

        async with self.store._unit():
            entries = [
                {**copy.deepcopy(row.data), "digest": row.digest}
                for row in await self.journal.records.find_for_run(identifier(run["id"]))
            ]
            rows = await self.journal.sandboxes.for_run(identifier(run["id"]))
            if len(entries) > 10000 or len(json.dumps(entries)) > 16 * 1024 * 1024 or len(rows) > 100:
                refuse()
            if not self.journal.verify_chain(entries):
                refuse()
            retained = {
                entry["payload"].get("execution_id")
                for entry in entries
                if entry["kind"] in {"sandbox_intent", "sandbox_receipt", "sandbox_reconcile"}
            }
            if retained != {row.id.hex for row in rows} or len({row.id for row in rows}) != len(rows):
                refuse()
            if not rows:
                return
            checkpoints = [
                entry
                for entry in entries
                if entry["kind"] == "agent_checkpoint"
                and entry["payload"].get("invocation") == context.model_dump(mode="json")
            ]
            if len(checkpoints) != 1:
                refuse()
            checkpoint = checkpoints[0]
            consumes = [
                entry
                for entry in entries
                if entry["kind"] == "agent_consume" and entry["idempotency_key"] == checkpoint["idempotency_key"]
            ]
            consumed_at = None
            if consumes:
                if ticket is None or len(consumes) != 1:
                    refuse()
                consumed = ConsumePayloadV2.model_validate(consumes[0]["payload"])
                if (
                    consumed.restore_request_id != ticket["id"]
                    or consumed.generation != ticket["generation"]
                    or consumed.checkpoint_record_digest != checkpoint["digest"]
                    or consumed.intent_digest != checkpoint["payload"].get("intent_digest")
                    or consumes[0]["sequence"] <= checkpoint["sequence"]
                ):
                    refuse()
                consumed_at = consumes[0]["sequence"]
            snapshot = run.get("sandbox_snapshot") or capture_sandbox(None)
            policy = (
                validate_factory_pool(snapshot)
                if snapshot.get("mode") == "pool"
                else validate_sandbox_snapshot(snapshot)
            )
            if len(rows) > policy.command_limit:
                refuse()
            browser_calls = 0
            for row in rows:
                try:
                    value = _canonical(row, entries, run["id"])
                except (SandboxReconciliationRequired, KeyError, TypeError, ValueError):
                    refuse()
                if (
                    value["request"].get("executor_contract") != COMMAND_CONTRACT
                    or row.snapshot_digest != snapshot["digest"]
                    or row.ordinal > policy.command_limit
                    or not _complete(row, value)
                ):
                    refuse()
                browser_calls += bool(value["request"]["request"].get("browser"))
                effects = [
                    entry["sequence"]
                    for entry in entries
                    if entry["kind"] in {"sandbox_intent", "sandbox_receipt", "sandbox_reconcile"}
                    and entry["payload"].get("execution_id") == row.id.hex
                ]
                # Before exposure, every effect must already belong to the selected checkpoint cut.
                if max(effects) >= checkpoint["sequence"] and (consumed_at is None or min(effects) <= consumed_at):
                    refuse()
            browser = getattr(policy, "browser", None)
            if browser_calls and (browser is None or browser_calls > browser.max_calls):
                refuse()

    async def request(self, run_id: str, completion_id: str, body: RestoreInput) -> dict[str, Any]:
        with prepared_credential_rejection(self.store):
            return await self._request(run_id, completion_id, body)

    async def _request(self, run_id: str, completion_id: str, body: RestoreInput) -> dict[str, Any]:
        _outside_unit()
        run_id, completion_id = identifier(run_id).hex, _request_id(completion_id)
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("Sign in before explicitly restoring a checkpoint")
        actor = ProjectAccess.actor_snapshot(reader)
        run = await self.completions._reader(run_id, reader, execute=True)
        await self.access(run, actor)
        incoming = {
            "run_id": run_id,
            "completion_request_id": completion_id,
            "input": body.model_dump(mode="json"),
            "actor": actor,
        }
        async with self.store._unit():
            old = await self.requests.records.find_by_id(identifier(body.request_id))
            if old is not None and (old.kind != KIND or old.run_id.hex != run_id or old.data["request"] != incoming):
                raise ValueError("Restore request identity is already bound to another input")
        if old is not None:
            return await self.get(run_id, completion_id, body.request_id)
        bundle = await self.checkpoints.restore_bundle(run_id, completion_id, body)
        context = CheckpointPayloadV2.model_validate(bundle[1]["payload"]).invocation
        if context.contract != runtime_contract() or context.deadline_ms <= int(time.time() * 1000):
            raise ValueError("Original checkpoint contract changed or deadline expired")
        if await workspace_cut(self.store, self.workspace(run_id)) != context.workspace:
            raise ValueError("Retained workspace drifted; no restore ticket admitted")
        credentials = [incoming, bundle[:3], self.checkpoints.credential_payload(bundle[1])]
        await self.store.admit_credentials(credentials)
        if CURRENT_PRINCIPAL.get() != reader:
            raise AccessDenied("Restore caller changed during admission")
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:execution-request-admission:" + self.store.schema},
            )
            await self.completions._writer(run, reader)
            locked = await self.journal._run(run_id)
            current = {**copy.deepcopy(locked.data), "id": run_id, "status": locked.status}
            await self.access(current, actor)
            await self.validate_db(current, context, bundle[0], active=False)
            old = await self.requests.records.find_by_id(identifier(body.request_id))
            if old is not None:
                if old.data["request"] != incoming or old.kind != KIND or old.run_id.hex != run_id:
                    raise ValueError("Concurrent restore request identity conflict")
            else:
                others = await self.requests.records.for_run(identifier(run_id), KIND)
                if any(row.request_key == completion_id for row in others):
                    raise ValueError("This checkpoint already has an explicit restore ticket")
                if len(others) >= 50 or await self.requests.records.pending_count() >= 500:
                    raise ValueError("The bounded checkpoint restore queue is full")
                data = {
                    "id": body.request_id,
                    "run_id": run_id,
                    "kind": KIND,
                    "status": "queued",
                    "request": incoming,
                    "request_digest": digest(incoming),
                    "generation": None,
                    "result": None,
                    "created_at": now(),
                    "updated_at": now(),
                }
                row = ForgeExecutionRequestEntity(
                    id=identifier(body.request_id),
                    run_id=identifier(run_id),
                    kind=KIND,
                    request_key=completion_id,
                    status="queued",
                    data=data,
                )
                await self.requests.records.save(row)
                await self.requests._event(row, "request_admitted", {"request_digest": data["request_digest"]})
            await self.access(current, actor)
            self.store.reject_credentials(credentials)
        await self.access(await self.store.get_run(run_id), actor)
        self.store.reject_credentials(credentials)
        return await self.get(run_id, completion_id, body.request_id)

    async def get(self, run_id: str, completion_id: str, request_id: str) -> dict[str, Any]:
        _outside_unit()
        run_id, completion_id = identifier(run_id).hex, _request_id(completion_id)
        reader = CURRENT_PRINCIPAL.get()
        run = await self.completions._reader(run_id, reader)
        async with self.store._unit():
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_READ)
            if row is None or row.kind != KIND or row.run_id.hex != run_id:
                raise KeyError("Checkpoint restore ticket not found")
            data = copy.deepcopy(row.data)
            if data.get("status") != row.status or data.get("id") != row.id.hex:
                raise ValueError("Restore row scalar identity or status differs")
        events = await self.requests.events(request_id)
        if (
            data["kind"] != KIND
            or data["run_id"] != run_id
            or data["id"] != request_id
            or data["request"]["completion_request_id"] != completion_id
            or digest(data["request"]) != data["request_digest"]
            or not events
            or not self.requests.verify_chain(events)
            or any(e["request_id"] != request_id for e in events)
            or events[0]["kind"] != "request_admitted"
            or events[0]["evidence"] != {"request_digest": data["request_digest"]}
        ):
            raise ValueError("Restore receipt identity or chain differs")
        latest = events[-1]
        if data["status"] in {"completed", "failed", "needs_reconciliation"}:
            valid = latest["kind"] == "worker_" + data["status"] and latest["evidence"] == data["result"]
        elif data["status"] == "queued":
            valid = latest["kind"] == "request_admitted" and data["result"] is None and data["generation"] is None
        else:
            valid = (
                data["status"] == "executing"
                and latest["kind"] == "worker_claim"
                and latest["evidence"]
                == {
                    "request_digest": data["request_digest"],
                    "generation": data["generation"],
                    "owner_pid": data["owner_pid"],
                }
            )
        if not valid:
            raise ValueError("Restore ticket outcome differs from its immutable event chain")
        await self.store.admit_credentials(data)
        current_run = await self.completions._reader(run_id, reader)
        if invocation_cut(current_run) != invocation_cut(run):
            raise AccessDenied("Restore reader scope changed")
        self.store.reject_credentials(data)
        return cast(dict[str, Any], data)

    async def guard(self, ticket: dict[str, Any]) -> None:
        await self.store.assert_ownership()
        current = await self.requests.get(ticket["id"])
        if (
            current["kind"],
            current["status"],
            current.get("generation"),
            current["request_digest"],
            current.get("owner_pid"),
        ) != (
            KIND,
            "executing",
            ticket["generation"],
            ticket["request_digest"],
            ticket.get("owner_pid"),
        ) or digest(current["request"]) != current["request_digest"]:
            raise ValueError("Restore ticket claim or generation is no longer current")
        if ticket.get("owner_pid") is None or ticket["owner_pid"] != getattr(self.store, "_ownership_pid", None):
            raise ValueError("Checkpoint restore execution owner changed")
        run = await self.store.get_run(ticket["run_id"])
        await self.access(run, ticket["request"]["actor"])
        body = RestoreInput.model_validate(ticket["request"]["input"])
        async with self.store._unit():
            checkpoint = await self.checkpoints._record(
                ticket["run_id"], ticket["request"]["completion_request_id"], "agent_checkpoint"
            )
            intent = await self.completions._record(ticket["run_id"], ticket["request"]["completion_request_id"])
            if (
                checkpoint is None
                or checkpoint["digest"] != body.expected_checkpoint_digest
                or intent["digest"] != body.expected_intent_digest
            ):
                raise ValueError("Restore checkpoint changed after claim")
            context = CheckpointPayloadV2.model_validate(checkpoint["payload"]).invocation
            if (
                invocation_cut(run) != context.run_cut
                or run["status"] != "developing"
                or run.get("cancel_request", {}).get("status") == "requested"
            ):
                raise ValueError("Restore execution authority or frozen run changed")
            task = await self.journal._effect_task(run["id"], intent["payload"]["task_key"])
            if task.status != "running" or task.attempt != intent["payload"]["task_attempt"]:
                raise ValueError("Restore original task attempt changed")
            await self._command_restore(run, context, ticket=ticket)

    async def execute(self, request_id: str) -> None:
        self.requests.runtime.require_execution()
        ticket = await self.requests.get(request_id)
        if ticket["kind"] != KIND:
            raise ValueError("Not a checkpoint restore ticket")
        if not await self.engine.reserve_agent_restore(ticket["run_id"]):
            return
        try:
            await self._execute(request_id)
        except BaseException as cause:
            observed = await self.requests.get(request_id)
            await asyncio.shield(
                self.finish(
                    request_id,
                    "failed" if observed["status"] == "queued" else "needs_reconciliation",
                    {"detail": "Checkpoint restore refused; inspect its exact ticket without replay"},
                )
            )
            if not isinstance(cause, Exception):
                raise
        finally:
            self.engine.release_agent_restore(ticket["run_id"])

    async def _execute(self, request_id: str) -> None:
        self.requests.runtime.require_execution()
        ticket = await self.requests.get(request_id)
        if ticket["kind"] != KIND:
            raise ValueError("Not a checkpoint restore ticket")
        run_id = ticket["run_id"]
        bundle = await self.checkpoints.restore_bundle(
            run_id, ticket["request"]["completion_request_id"], RestoreInput.model_validate(ticket["request"]["input"])
        )
        context = CheckpointPayloadV2.model_validate(bundle[1]["payload"]).invocation
        await self.access(await self.store.get_run(run_id), ticket["request"]["actor"])
        if (
            context.contract != runtime_contract()
            or context.deadline_ms <= int(time.time() * 1000)
            or await workspace_cut(self.store, self.workspace(run_id)) != context.workspace
        ):
            raise ValueError("Retained checkpoint source, deadline or runtime is unavailable")
        async with self.store._unit():
            run = await self.store.get_run(run_id)
            await self.completions._writer(run, await self.operator(ticket["request"]["actor"]))
            locked = await self.journal._run(run_id)
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.status != "queued":
                return
            if (
                row.data["request_digest"] != ticket["request_digest"]
                or digest(row.data["request"]) != ticket["request_digest"]
            ):
                raise ValueError("Queued restore input changed")
            await self.validate_db(await self.store.get_run(run_id), context, bundle[0], active=False)
            await self.access(await self.store.get_run(run_id), ticket["request"]["actor"])
            task = await self.journal._effect_task(run_id, bundle[0]["payload"]["task_key"])
            task.status = "running"
            await self.journal.tasks.save(task)
            locked.status = "developing"
            locked.data = {**locked.data, "status": "developing"}
            await self.store.run_records.save(locked)
            row.status = "executing"
            row.data = {
                **row.data,
                "status": "executing",
                "generation": uuid.uuid4().hex,
                "owner_pid": self.store._ownership_pid,
                "updated_at": now(),
            }
            await self.requests.records.save(row)
            await self.requests._event(
                row,
                "worker_claim",
                {
                    "request_digest": ticket["request_digest"],
                    "generation": row.data["generation"],
                    "owner_pid": row.data["owner_pid"],
                },
            )
            ticket = copy.deepcopy(row.data)
        try:
            restored = RestoreExecution(self, ticket, bundle)
            await restored.guard_source()
            await self.engine.continue_agent_checkpoint(restored)
            async with self.store._unit():
                await self.journal._bridge_read_bound(run_id)
                rows = await self.journal.records.find_for_run(identifier(run_id))
                if not self.journal.verify_chain([row.data for row in rows]):
                    raise ValueError("Restoration implementation receipt chain differs")
                receipts = [
                    row
                    for row in rows
                    if row.kind == "receipt" and row.idempotency_key == bundle[0]["payload"]["task_key"]
                ]
                if not receipts:
                    from firefly_forge.native_terminal_completion_models import validate_native_receipt

                    entries = [copy.deepcopy(row.data) for row in rows]
                    terminal = [
                        entry
                        for entry in entries
                        if entry["kind"] == "terminal_completion_receipt"
                        and entry["payload"].get("producer") == "native_worker_feedback"
                        and any(
                            item.get("consume_version") == 2
                            and item.get("restore_request_id") == request_id
                            and item.get("generation") == ticket["generation"]
                            for item in entry["payload"].get("proof", {}).get("completions", [])
                        )
                    ]
                    if len(terminal) != 1:
                        raise ValueError("Restoration did not reach its exact native output acceptance")
                    accepted = validate_native_receipt(terminal[0], entries)
                    current = await self.store.get_run(run_id)
                    feedbacks = [
                        entry for entry in entries if entry["digest"] == accepted.proof.acceptance_record_digest
                    ]
                    if (
                        accepted.proof.task_key != bundle[0]["payload"]["task_key"]
                        or accepted.proof.task_attempt != context.round
                        or feedbacks[0]["payload"] != current.get("worker_feedback")
                        or current.get("bridge_completion_hold")
                    ):
                        raise ValueError("Restored feedback acceptance differs from its current original task")
                elif len(receipts) != 1:
                    raise ValueError("Restoration did not reach its original implementation receipt")
                else:
                    row = receipts[0]
                    receipt = self.completions._validate_record(
                        row.data,
                        run_id,
                        bundle[0]["payload"]["task_key"],
                        "receipt",
                        {
                            "digest": row.digest,
                            "task_id": row.task_id,
                            "sequence": row.sequence,
                            "run_id": row.run_id.hex,
                            "kind": row.kind,
                            "idempotency_key": row.idempotency_key,
                        },
                    )
                    if (
                        receipt["task_id"] != "implement"
                        or receipt["payload"].get("implementation_round") != context.round
                    ):
                        raise ValueError("Restoration receipt does not belong to its original implementation round")
            await self.finish(
                request_id,
                "completed",
                {"task_key": bundle[0]["payload"]["task_key"], "attempt": bundle[0]["payload"]["task_attempt"]},
            )
        except BaseException:
            await asyncio.shield(
                self.finish(
                    request_id,
                    "needs_reconciliation",
                    {"detail": "Observe the exact checkpoint restore; never replay its consumed result"},
                )
            )
            raise

    async def finish(self, request_id: str, status: str, result: dict[str, Any]) -> None:
        async with self.store._unit():
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.kind != KIND or row.status not in {"queued", "executing"}:
                return
            if row.status == "queued" and status != "failed":
                raise ValueError("Unclaimed restore cannot report an execution effect")
            if status == "needs_reconciliation":
                run = await self.journal._run(row.run_id.hex)
                completion_id = row.data["request"]["completion_request_id"]
                intent = await self.completions._record(row.run_id.hex, completion_id)
                task = next(
                    (
                        task
                        for task in await self.journal.tasks.find_for_run(row.run_id)
                        if task.task_id == "implement"
                        and task.data.get("idempotency_key") == intent["payload"]["task_key"]
                        and task.attempt == intent["payload"]["task_attempt"]
                    ),
                    None,
                )
                if task is not None and task.status == "running":
                    task.status = "needs_reconciliation"
                    await self.journal.tasks.save(task)
                if run.status not in {"succeeded", "cancelled", "failed"}:
                    run.status = "needs_reconciliation"
                    run.data = {**run.data, "status": run.status}
                    await self.store.run_records.save(run)
            row.status = status
            row.data = {**row.data, "status": status, "result": result, "updated_at": now()}
            await self.requests.records.save(row)
            await self.requests._event(row, "worker_" + status, result)

    async def recover(self) -> None:
        async with self.store._unit():
            rows = await self.requests.records.state("executing", kind=KIND)
        for row in rows:
            await self.finish(
                row.id.hex,
                "needs_reconciliation",
                {"detail": "Execution owner restarted; no automatic checkpoint replay"},
            )
