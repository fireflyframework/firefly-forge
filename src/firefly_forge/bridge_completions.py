# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Run-owned completion evidence. Exact remote reads never reenter an Agentic graph."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import text

from firefly_forge.bridge_completion_models import (
    CompletionDescriptor,
    CompletionIntentPayload,
    CompletionObservationPayload,
    CompletionResponsePayload,
    ObservationInput,
    validate_remote_snapshot,
)
from firefly_forge.bridge_completion_scope import BridgeCompletionScope
from firefly_forge.configuration import validate_endpoint
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal, digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, Permission, ProjectAccess
from firefly_forge.store import Store, identifier

Data = dict[str, Any]
INTENT = "bridge_request_intent"
OBSERVATION = "bridge_request_observation"
RESPONSE = "bridge_request_response"
MAX_BYTES = 16 * 1024 * 1024
RESERVATION = 532480


def _cut(run: Data, field: str) -> str:
    return digest(
        {
            key: run.get(key)
            for key in (
                "id",
                "repository_id",
                "project_id",
                "component_snapshot",
                "execution_actor",
                "publish",
                "source_tree",
                "base_revision",
                "checkpoint_revision",
                "source_commit",
                "admission",
                field,
                *(("checkpoint_mode_adoption",) if run.get("checkpoint_mode_adoption") else ()),
            )
        }
    )


def _outside_unit() -> None:
    if current_unit_of_work() is not None:
        raise ValueError("Bridge remote admission requires no active caller transaction")


def _request_id(value: str) -> str:
    import re

    if not re.fullmatch(r"fbr1_[a-f0-9]{32}", value):
        raise ValueError("Invalid Bridge completion request identity")
    return value


class BridgeCompletions:
    def __init__(self, store: Store, journal: Journal) -> None:
        self.store, self.journal = store, journal

    async def _budget(
        self,
        run_id: str,
        *,
        reserve: bool = False,
        payload_bytes: int = 0,
        consume: bool = False,
        checkpoint_consume: bool = False,
    ) -> None:
        budget = await self.journal.records.bridge_budget(identifier(run_id))
        # Reserve the escaped transport response and its journal envelope. No eviction or replacement.
        if (
            budget["rows"] > 10000
            or budget["max_intent_bytes"] > 16384
            or budget["bytes"]
            + max(budget["intents"] - budget["responses"] - int(consume), 0) * RESERVATION
            + max(budget.get("checkpoints", 0) - budget.get("checkpoint_consumes", 0) - int(checkpoint_consume), 0)
            * 4096
            + payload_bytes
            + (RESERVATION if reserve else 0)
            > MAX_BYTES
            or (reserve and budget["intents"] >= 100)
        ):
            raise ValueError("Run completion evidence retention is full; inspect the retained requests")

    async def _record(self, run_id: str, request_id: str, kind: str = INTENT) -> Data:
        _request_id(request_id) if kind in {INTENT, RESPONSE} else identifier(request_id)
        await self._budget(run_id)
        row = await self.journal.records.bridge_record(identifier(run_id), request_id, kind)
        if row is None:
            raise KeyError("Bridge completion evidence not found")
        return self._validate_record(
            copy.deepcopy(row.data),
            run_id,
            request_id,
            kind,
            {
                "digest": row.digest,
                "task_id": row.task_id,
                "sequence": row.sequence,
                "run_id": row.run_id.hex,
                "kind": row.kind,
                "idempotency_key": row.idempotency_key,
            },
        )

    @staticmethod
    def _validate_record(data: Data, run_id: str, request_id: str, kind: str, scalar: Data) -> Data:
        if any(data.get(key) != scalar[key] for key in ("run_id", "kind", "idempotency_key")):
            raise ValueError("Completion row scope differs from its immutable envelope")
        if (
            data.get("digest") != scalar["digest"]
            or digest({key: value for key, value in data.items() if key != "digest"}) != scalar["digest"]
            or data.get("run_id") != run_id
            or data.get("kind") != kind
            or data.get("idempotency_key") != request_id
            or data.get("task_id") != scalar["task_id"]
            or data.get("sequence") != scalar["sequence"]
        ):
            raise ValueError("Bridge evidence record has invalid immutable bindings")
        if kind == INTENT:
            payload = CompletionIntentPayload.model_validate(data["payload"])
            if (
                payload.run_id != run_id
                or payload.task_id != data["task_id"]
                or payload.descriptor.request_id != request_id
            ):
                raise ValueError("Completion payload and journal identity differ")
        elif kind == RESPONSE:
            response_payload = CompletionResponsePayload.model_validate(data["payload"])
            if response_payload.request_id != request_id:
                raise ValueError("Response payload and journal identity differ")
        elif kind == OBSERVATION:
            payload_raw = data["payload"]
            CompletionObservationPayload.model_validate(payload_raw)
            if payload_raw["input"]["observation_id"] != request_id:
                raise ValueError("Observation payload and journal identity differ")
        return data

    async def _writer(self, run: Data, reader: Any = None) -> None:
        caller = current_unit_of_work()
        if caller is None:
            raise ValueError("Completion retention requires its native transaction")
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": self.store.schema + ":members:" + run["project_id"]},
            )
        users = {run["execution_actor"]["user_id"]}
        if reader is not None:
            users.add(reader.id.hex)
        for user_id in sorted(users):
            user = await ForgeIdentityUserRepository().find_by_id(identifier(user_id), lock=LockMode.PESSIMISTIC_WRITE)
            if user is None or not user.enabled:
                raise AccessDenied("Completion retention identity is unavailable or disabled")

    async def _identity(self, user_id: str) -> Any:
        async with self.store._unit():
            user = await ForgeIdentityUserRepository().find_by_id(identifier(user_id))
            if user is None or not user.enabled:
                raise AccessDenied("Completion evidence identity is unavailable or disabled")
            return user

    async def _execution(self, run: Data, initial_reader: Any) -> None:
        if CURRENT_PRINCIPAL.get() != initial_reader:
            raise AccessDenied("Bridge execution context changed during admission")
        if not run.get("execution_actor") or not run.get("project_id"):
            raise AccessDenied("Required recovery needs an explicitly admitted project execution identity")
        actor = run["execution_actor"]
        user = await self._identity(actor["user_id"])
        access = ProjectAccess(self.store)
        await access.authorize_execution(run)
        if not await access.authorize_actor(actor, run["project_id"], "execute"):
            principal = Principal(
                user.id,
                user.issuer,
                user.subject,
                authentication_method=actor["authentication_method"],
                scm_delegation=copy.deepcopy(actor.get("scm_delegation")),
            )
            await access.require_repository(principal, run["repository_id"], "execute")
        if run.get("publish"):
            await ProjectAccess(self.store).authorize_execution(run, "release")
        if CURRENT_PRINCIPAL.get() != initial_reader:
            raise AccessDenied("Bridge execution context changed during authorization")
        if run.get("cancel_request", {}).get("status") == "requested" or run.get("status") in {
            "cancelled",
            "failed",
            "interrupted",
            "succeeded",
        }:
            raise asyncio.CancelledError()

    async def _current(
        self,
        frozen: Data,
        field: str,
        reader: Any,
        assert_ownership: Callable[[], Awaitable[None]] | None = None,
    ) -> Data:
        if assert_ownership is not None:
            await assert_ownership()
        await self.store.assert_ownership()
        value = await self.store.get_run(frozen["id"])
        if _cut(value, field) != _cut(frozen, field):
            raise AccessDenied("Bridge completion owner or source/configuration cut changed")
        await self._execution(value, reader)
        if value.get("checkpoint_mode_adoption"):
            from firefly_forge.checkpoint_mode_runtime import assert_active_mode

            await assert_active_mode(self.journal, value["id"])
        return value

    def scope(
        self,
        run: Data,
        task: Data,
        *,
        review: bool = False,
        assert_ownership: Callable[[], Awaitable[None]] | None = None,
        retain_intent: Callable[[Data, bytes], Awaitable[None]] | None = None,
    ) -> BridgeCompletionScope:
        frozen = copy.deepcopy(run)
        field = "review_config_revision" if review else "config_revision"
        task_id = "review" if review else "implement"
        key = task.get("idempotency_key")
        key = key if isinstance(key, str) else ""
        from firefly_forge.checkpoint_mode_runtime import effective_revision

        revision = effective_revision(frozen, field)
        revision = revision if isinstance(revision, str) else ""
        reader = CURRENT_PRINCIPAL.get()
        binding = {
            "run_id": frozen["id"],
            "task_id": task_id,
            "project_id": frozen.get("project_id"),
            "repository_id": frozen.get("repository_id"),
            "task_key": key,
            "config_field": field,
            "config_revision": revision,
            "profile_id": (frozen.get("review_model_selection" if review else "model_selection") or {}).get(
                "profile_id"
            ),
            "scope_digest": _cut(frozen, field),
        }

        async def current() -> Data:
            return await self._current(frozen, field, reader, assert_ownership)

        async def prepare(body: bytes) -> None:
            _outside_unit()
            if not key or not revision:
                raise ValueError("Required recovery needs a frozen task intent and model revision")
            await current()
            await self.store.admit_credentials(body.decode("utf-8"))
            await current()
            self.store.reject_credentials(body.decode("utf-8"))

        async def admit(raw: Data, body: bytes) -> Data:
            _outside_unit()
            descriptor = CompletionDescriptor.model_validate(raw).model_dump(mode="json")
            settings = self.store.configuration.load_snapshot(revision)
            endpoint = validate_endpoint(settings.get("FORGE_BRIDGE_URL", ""))
            endpoint = endpoint if endpoint.endswith("/v1") else endpoint + "/v1"
            if (
                settings.get("FORGE_BRIDGE_COMPLETION_RECOVERY") != "required"
                or descriptor["endpoint"] != endpoint
                or descriptor["model"] != settings.get("FORGE_BRIDGE_MODEL", "bridge/default")
                or descriptor["wire_sha256"] != hashlib.sha256(body).hexdigest()
                or descriptor["wire_bytes"] != len(body)
            ):
                raise ValueError("Completion descriptor differs from the frozen native request")
            payload = {"version": 1, **binding, "descriptor": descriptor}
            await prepare(body)
            async with self.store._unit():
                await self._writer(frozen, reader if retain_intent is not None else None)
                row = await self.journal._run(frozen["id"])
                await current()
                existing = await self.journal.records.bridge_record(row.id, descriptor["request_id"], INTENT)
                if existing is not None:
                    original = await self._record(frozen["id"], descriptor["request_id"])
                    payload["task_attempt"] = original["payload"]["task_attempt"]
                    if original["payload"] != payload:
                        raise ValueError("Completion identity was already bound to another request")
                    if retain_intent is not None:
                        await retain_intent(original, body)
                        await current()
                        self.store.reject_credentials(body.decode("utf-8"))
                    return original
                actual = await self.journal._effect_task(frozen["id"], key)
                if actual.task_id != task_id or actual.status != "running":
                    raise ValueError("Completion needs the exact current running task intent")
                payload["task_attempt"] = actual.attempt
                await self._budget(frozen["id"], reserve=True, payload_bytes=len(json.dumps(payload).encode()))
                record = await self.journal._append(frozen["id"], task_id, descriptor["request_id"], INTENT, payload)
                if not row.data.get("bridge_completion_hold"):
                    row.data = {
                        **row.data,
                        "bridge_completion_hold": {
                            "version": 1,
                            "reason": "graph_checkpoint_unavailable",
                            "request_id": descriptor["request_id"],
                            "intent_digest": record["digest"],
                        },
                    }
                    await self.store.run_records.save(row)
                if retain_intent is not None:
                    await retain_intent(record, body)
                await current()
                self.store.reject_credentials(body.decode("utf-8"))
            await current()
            self.store.reject_credentials(body.decode("utf-8"))
            return record

        async def guard(intent: Data, body: bytes) -> None:
            _outside_unit()
            await current()
            async with self.store._unit():
                retained = await self._record(frozen["id"], intent["idempotency_key"])
                if retained != intent or retained["payload"]["scope_digest"] != binding["scope_digest"]:
                    raise ValueError("Completion intent changed before dispatch")
                actual = await self.journal._effect_task(frozen["id"], key)
                if actual.status != "running" or actual.attempt != retained["payload"]["task_attempt"]:
                    raise ValueError("Completion task changed before dispatch")
            await current()
            self.store.reject_credentials(body.decode("utf-8"))

        async def response(intent: Data, body: bytes) -> None:
            _outside_unit()
            if len(body) > 262144:
                raise ValueError("Bridge transport response exceeds 256 KiB")
            text = body.decode("utf-8")
            await self.store.admit_credentials(text)
            await current()
            request_id = intent["idempotency_key"]
            payload = {
                "version": 1,
                "request_id": request_id,
                "intent_digest": intent["digest"],
                "response": text,
                "response_sha256": hashlib.sha256(body).hexdigest(),
                "response_bytes": len(body),
            }
            async with self.store._unit():
                await self._writer(frozen)
                await self.journal._run(frozen["id"])
                await current()
                original = await self._record(frozen["id"], request_id)
                if original != intent:
                    raise ValueError("Response differs from its original native completion intent")
                prior = await self.journal.records.bridge_record(identifier(frozen["id"]), request_id, RESPONSE)
                if prior is not None:
                    prior_record = await self._record(frozen["id"], request_id, RESPONSE)
                    if prior_record["payload"] != payload:
                        raise ValueError("A completion response cannot replace an earlier response")
                else:
                    await self._budget(
                        frozen["id"],
                        consume=True,
                        payload_bytes=len(json.dumps(payload, ensure_ascii=False).encode()) + 4096,
                    )
                    await self.journal._append(frozen["id"], task_id, request_id, RESPONSE, payload)
                await current()
                self.store.reject_credentials(text)
            await current()
            self.store.reject_credentials(text)

        return BridgeCompletionScope(prepare, admit, guard, response)

    async def _reader(self, run_id: str, reader: Any, *, execute: bool = False) -> Data:
        if reader is None or CURRENT_PRINCIPAL.get() != reader:
            raise AccessDenied("Sign in with current project access to inspect completion evidence")
        run = await self.store.get_run(run_id)
        await self._identity(reader.id.hex)
        access = ProjectAccess(self.store)
        permission: Permission = "execute" if execute else "read"
        if not run.get("project_id"):
            raise AccessDenied("Completion evidence requires its original authorized project")
        await access.require_project(reader, run["project_id"], permission)
        await access.require_repository(reader, run["repository_id"], permission)
        if execute and run.get("publish"):
            await access.require_project(reader, run["project_id"], "release")
        if CURRENT_PRINCIPAL.get() != reader:
            raise AccessDenied("Completion evidence reader changed during authorization")
        return run

    async def _admitted_read(self, run_id: str, value: Data, reader: Any, scope: str, *, execute: bool = False) -> Data:
        _outside_unit()
        await self.store.admit_credentials(value)
        async with self.store._unit():
            current = await self._reader(run_id, reader, execute=execute)
            if digest({"scope": self.journal._worker_scope(current)}) != scope:
                raise AccessDenied("Completion evidence scope changed during credential admission")
            self.store.reject_credentials(value)
        return value

    async def exact(self, run_id: str, request_id: str) -> Data:
        run_id = identifier(run_id).hex
        reader = CURRENT_PRINCIPAL.get()
        async with self.store._unit():
            run = await self._reader(run_id, reader)
            scope = digest({"scope": self.journal._worker_scope(run)})
            value = await self._record(run_id, request_id)
            if any(value["payload"][key] != run.get(key) for key in ("project_id", "repository_id")):
                raise AccessDenied("Completion intent belongs to another project or repository scope")
        return await self._admitted_read(run_id, value, reader, scope)

    async def list(self, run_id: str, after: int = 0) -> Data:
        if type(after) is not int or after < 0:
            raise ValueError("Completion cursor must be a nonnegative sequence")
        run_id = identifier(run_id).hex
        reader = CURRENT_PRINCIPAL.get()
        async with self.store._unit():
            run = await self._reader(run_id, reader)
            scope = digest({"scope": self.journal._worker_scope(run)})
            await self._budget(run_id)
            rows = await self.journal.records.bridge_intents(identifier(run_id), after, 51)
            entries = []
            previous = after
            for row in rows:
                entry = row["data"]
                if row["sequence"] <= previous or entry.get("sequence") != row["sequence"]:
                    raise ValueError("Completion cursor bindings are invalid")
                self._validate_record(entry, run_id, entry["idempotency_key"], INTENT, row)
                if any(entry["payload"][key] != run.get(key) for key in ("project_id", "repository_id")):
                    raise AccessDenied("Completion metadata belongs to another project or repository scope")
                entries.append(entry)
                previous = row["sequence"]
            page = {
                "version": 1,
                "items": entries[:50],
                "next_after": entries[49]["sequence"] if len(entries) > 50 else None,
            }
        return await self._admitted_read(run_id, page, reader, scope)

    async def observation(self, run_id: str, request_id: str, observation_id: str) -> Data:
        intent = await self.exact(run_id, request_id)
        reader = CURRENT_PRINCIPAL.get()
        async with self.store._unit():
            run = await self._reader(run_id, reader)
            scope = digest({"scope": self.journal._worker_scope(run)})
            value = await self._record(run_id, identifier(observation_id).hex, OBSERVATION)
            if value["payload"]["intent_digest"] != intent["digest"]:
                raise ValueError("Observation belongs to another completion")
            validate_remote_snapshot(value["payload"]["snapshot"], intent["payload"]["descriptor"])
        return await self._admitted_read(run_id, value, reader, scope)

    def _credentials(self, payload: Data, descriptor: Data) -> dict[str, str]:
        if payload.get("profile_id"):
            if self.store.task_router is None:
                raise ValueError("The original named Bridge profile is unavailable")
            provider, settings = self.store.task_router.profiles.resolve(payload["profile_id"])
            if provider != "bridge":
                raise ValueError("The original named profile no longer selects Bridge")
        else:
            settings = self.store.configuration.resolve("bridge")
        endpoint = validate_endpoint(settings.get("FORGE_BRIDGE_URL", ""))
        endpoint = endpoint if endpoint.endswith("/v1") else endpoint + "/v1"
        if (
            endpoint != descriptor["endpoint"]
            or settings.get("FORGE_BRIDGE_MODEL", "bridge/default") != descriptor["model"]
            or not settings.get("FORGE_BRIDGE_TOKEN")
        ):
            raise ValueError("Current credentials must retain the original Bridge endpoint and model")
        return {"endpoint": endpoint, "token": settings["FORGE_BRIDGE_TOKEN"]}

    async def observe(self, run_id: str, request_id: str, request: ObservationInput) -> Data:
        _outside_unit()
        request = ObservationInput.model_validate(request.model_dump(mode="json"))
        intent = await self.exact(run_id, request_id)
        if request.expected_intent_digest != intent["digest"]:
            raise ValueError("Observation does not match the selected completion intent")
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("Sign in before observing an original Bridge result")
        async with self.store._unit():
            run = await self._reader(run_id, reader, execute=True)
            scope = digest({"scope": self.journal._worker_scope(run)})
            old = await self.journal.records.bridge_record(identifier(run_id), request.observation_id, OBSERVATION)
            if old is not None:
                value = await self._record(run_id, request.observation_id, OBSERVATION)
                if (
                    value["payload"]["input"] != request.model_dump(mode="json")
                    or value["payload"]["request_id"] != request_id
                    or value["payload"]["observed_by"] != reader.id.hex
                ):
                    raise ValueError("Observation identity is already bound to another request")
                replay = value
            else:
                await self._budget(run_id, payload_bytes=RESERVATION)
                replay = None
        if replay is not None:
            return await self._admitted_read(run_id, replay, reader, scope, execute=True)
        payload = intent["payload"]
        descriptor = CompletionDescriptor.model_validate(payload["descriptor"]).model_dump(mode="json")
        credentials = self._credentials(payload, descriptor)
        await self.store.admit_credentials(intent)
        await self._reader(run_id, reader, execute=True)
        if self._credentials(payload, descriptor) != credentials:
            raise ValueError("Bridge credentials changed before the exact read; retain the observation identity")
        endpoint = credentials["endpoint"]
        async with (
            httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client,
            client.stream(
                "GET",
                endpoint + "/bridge/completion-requests/" + request_id,
                headers={"Authorization": "Bearer " + credentials["token"]},
            ) as response,
        ):
            if response.status_code != 200:
                raise ValueError("Exact Bridge result is unavailable; retain this identity and do not resubmit")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 262144:
                    raise ValueError("Bridge observation exceeds 256 KiB")
        snapshot = validate_remote_snapshot(json.loads(body), descriptor)
        value = {
            "version": 1,
            "request_id": request_id,
            "intent_digest": intent["digest"],
            "input": request.model_dump(mode="json"),
            "observed_by": reader.id.hex,
            "snapshot": snapshot,
        }
        await self.store.admit_credentials(value)
        if self._credentials(payload, descriptor) != credentials:
            raise ValueError("Bridge credentials changed during the exact read; retain the observation identity")
        async with self.store._unit():
            await self._writer(run, reader)
            await self.journal._run(run_id)
            current = await self._reader(run_id, reader, execute=True)
            if digest({"scope": self.journal._worker_scope(current)}) != scope:
                raise AccessDenied("Completion observation scope changed before retention")
            old = await self.journal.records.bridge_record(identifier(run_id), request.observation_id, OBSERVATION)
            if old is not None:
                retained = await self._record(run_id, request.observation_id, OBSERVATION)
                if (
                    retained["payload"]["input"] != request.model_dump(mode="json")
                    or retained["payload"]["request_id"] != request_id
                    or retained["payload"]["observed_by"] != reader.id.hex
                ):
                    raise ValueError("Concurrent observation identity conflicts with another request")
            else:
                await self._budget(run_id, payload_bytes=len(json.dumps(value, ensure_ascii=False).encode()) + 4096)
                retained = await self.journal._append(
                    run_id, intent["task_id"], request.observation_id, OBSERVATION, value
                )
            await self._reader(run_id, reader, execute=True)
            self.store.reject_credentials(value)
        return await self._admitted_read(run_id, retained, reader, scope, execute=True)
