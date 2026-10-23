# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit source-only continuation after observed output failed downstream validation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.activity_models import validate_descriptor
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.source_edit_continuations import clock_ms, source_edit_scope
from firefly_forge.source_edit_models import SourceEditContinuationInput
from firefly_forge.source_patch_admission import SourcePatch
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]


class SourceCompletedOutputInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    admission_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_catalog_path: str = Field(min_length=1, max_length=4096)
    valid_for_seconds: int = Field(ge=1, le=1800)
    acknowledge_output_not_success: Literal[True]
    acknowledge_no_exit_attestation: Literal[True]
    acknowledge_original_budgets: Literal[True]
    acknowledge_no_replay: Literal[True]
    acknowledge_new_finite_attempt: Literal[True]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Completed output admission requires exact version one")
        return value

    @field_validator(
        "acknowledge_output_not_success",
        "acknowledge_no_exit_attestation",
        "acknowledge_original_budgets",
        "acknowledge_no_replay",
        "acknowledge_new_finite_attempt",
        mode="before",
    )
    @classmethod
    def explicit_consent(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicit completed-output acknowledgements are required")
        return value


def admission_contract_digest() -> str:
    root = Path(__file__).parent
    return digest(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "source_result_readmission.py",
                "source_human_readmission.py",
                "provider_recoveries.py",
                "engine.py",
                "activity_models.py",
                "activity_persistence.py",
                "source_edit_continuations.py",
                "workspace_reconciliation_controllers.py",
            )
        }
    )


def _one(entries: list[Data], kind: str, *, key: str | None = None, fingerprint: str | None = None) -> Data:
    rows = [
        e
        for e in entries
        if e["kind"] == kind
        and (key is None or e["idempotency_key"] == key)
        and (fingerprint is None or e["digest"] == fingerprint)
    ]
    if len(rows) != 1:
        raise ValueError("Completed output requires one exact immutable native record")
    return rows[0]


def validate_completed_output_parent(entries: list[Data], event: Data, actor: Data) -> Data:
    from firefly_forge.source_human_readmission import _validated_parent

    receipt, candidate = _validated_parent(entries, event, actor)
    marker = candidate["completed_output_readmission"]
    body = SourceCompletedOutputInput.model_validate(receipt["completed_output_request"])
    observed = _one(entries, "source_output_readmission", fingerprint=receipt["readmission_digest"])
    intent = _one(entries, "workspace_reconciliation_intent", fingerprint=receipt["intent_digest"])
    parent = _one(
        entries,
        "workspace_reconciliation_receipt",
        key=marker["parent_marker"]["request_id"],
        fingerprint=marker["parent_marker"]["receipt_digest"],
    )
    _validated_parent(entries, parent, actor)
    proof = observed["payload"]
    review = proof["review"]
    if (
        not parent["sequence"] < observed["sequence"] < intent["sequence"] < event["sequence"]
        or body.request_id != receipt["request"]["request_id"]
        or observed["idempotency_key"] != body.request_id
        or review["parent_request_id"] != marker["parent_marker"]["request_id"]
        or review["parent_receipt_digest"] != marker["parent_marker"]["receipt_digest"]
        or review["previous_task"] != marker["previous_task"]
        or receipt["expires_at_ms"] != intent["payload"]["expires_at_ms"]
        or int(datetime.fromisoformat(event["created_at"]).timestamp() * 1000) >= receipt["expires_at_ms"]
        or proof["request"] != body.model_dump()
        or proof["actor"] != actor
        or proof["parent_marker"] != marker["parent_marker"]
        or proof["previous_task"] != marker["previous_task"]
        or proof["new_candidate_digest"] != candidate["digest"]
        or review["digest"] != digest({k: v for k, v in review.items() if k != "digest"})
        or body.candidate_digest != review["digest"]
        or marker["review_digest"] != review["digest"]
        or body.admission_contract_digest != marker["admission_contract_digest"]
        or review["admission_contract_digest"] != marker["admission_contract_digest"]
        or proof["observation"] != review["observation"]
        or marker["observation_digest"] != digest(proof["observation"])
        or review["source_edit"] != candidate["source_edit"]
        or review["plan"] != candidate["plan"]
        or review["cut"] != candidate["cut"]
        or review["previous_run_digest"] != candidate["previous_run_digest"]
        or marker["from_attempt"] != parent["payload"]["candidate"]["previous_task"]["attempt"]
        or marker["to_attempt"] != marker["from_attempt"] + 1
        or not 2 <= marker["to_attempt"] <= 4
        or candidate["previous_task"]["attempt"] != marker["to_attempt"]
        or receipt["expires_at_ms"] != proof["expires_at_ms"]
        or not 0
        < proof["expires_at_ms"] - int(datetime.fromisoformat(observed["created_at"]).timestamp() * 1000)
        <= body.valid_for_seconds * 1000
        or proof["no_environment_effect"] is not True
        or proof["no_provider_dispatch"] is not True
        or receipt["effect"]
        != {"version": 1, "no_environment_effect": True, "completed_output_observation": marker["observation_digest"]}
    ):
        raise ValueError("Completed output ancestry lost its exact native observation and consent")
    return parent


_NOTICE_PREFIX = (
    "Under-development features enabled: skip_host_skill_discovery. "
    "Under-development features are incomplete and may behave unpredictably. "
    "To suppress this warning, set `suppress_unstable_features_warning = true` in "
)


def _unstable_feature_notice(payload: Data) -> str | None:
    item = payload.get("item") or {}
    if (
        set(payload) != {"type", "item"}
        or payload.get("type") != "item.completed"
        or set(item) != {"id", "type", "message"}
        or item.get("type") != "error"
        or not isinstance(item.get("id"), str)
        or not re.fullmatch(r"item_[0-9]{1,12}", item["id"])
    ):
        return None
    message = item.get("message")
    if not isinstance(message, str) or len(message.encode()) > 4096 or not message.startswith(_NOTICE_PREFIX):
        return None
    path = message[len(_NOTICE_PREFIX) : -1]
    if (
        not message.endswith(".")
        or not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*config\.toml", path)
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        return None
    return hashlib.sha256(message.encode()).hexdigest()


def completed_output_observation(run: Data, dispatch: Data, contents: dict[str, bytes]) -> Data:
    """Observe one complete native turn without treating it as an OS exit receipt."""
    boundary = datetime.fromisoformat(dispatch["created_at"])
    events = [
        e
        for e in run.get("events", [])
        if e.get("stage") == "stdout" and datetime.fromisoformat(e["created_at"]) > boundary
    ]
    if not 1 <= len(events) <= 256 or len({e["id"] for e in events}) != len(events):
        raise ValueError("Completed output requires one bounded, unique native activity interval")
    events.sort(key=lambda e: (e["created_at"], e["id"]))
    payloads, descriptors, total = [], [], 0
    for event in events:
        descriptor = validate_descriptor(event.get("activity_content"))
        if (
            descriptor["status"] != "available"
            or descriptor["run_id"] != run["id"]
            or descriptor["event_id"] != event["id"]
            or descriptor["stage"] != "stdout"
        ):
            raise ValueError("Completed output lost its exact retained activity descriptor")
        raw = contents.get(event["id"])
        if (
            not isinstance(raw, bytes)
            or len(raw) != descriptor["sanitized_message_bytes"]
            or hashlib.sha256(raw).hexdigest() != descriptor["sanitized_message_sha256"]
        ):
            raise ValueError("Completed output requires its full unchanged archived content")
        total += len(raw)
        if total > 4 * 1024 * 1024:
            raise ValueError("Completed native output exceeds its observation bound")
        try:
            payload = json.loads(raw)
        except (UnicodeError, ValueError):
            raise ValueError("Completed output is not a structured native observation") from None
        if not isinstance(payload, dict):
            raise ValueError("Completed output is not a structured native observation")
        if "item" in payload and not isinstance(payload["item"], dict):
            raise ValueError("Completed output item has an unsupported shape")
        payloads.append(payload)
        descriptors.append({"created_at": event["created_at"], **descriptor})
    starts = [i for i, p in enumerate(payloads) if p.get("type") == "turn.started"]
    completions = [i for i, p in enumerate(payloads) if p.get("type") == "turn.completed"]
    messages = [
        i
        for i, p in enumerate(payloads)
        if p.get("type") == "item.completed" and (p.get("item") or {}).get("type") == "agent_message"
    ]
    initial = 1 if payloads[0].get("type") == "thread.started" else 0
    diagnostics = []
    if (
        run.get("agent") == "codex"
        and initial < len(payloads)
        and (message_digest := _unstable_feature_notice(payloads[initial])) is not None
    ):
        diagnostics.append(
            {
                "kind": "codex_unstable_feature_notice_v1",
                "feature": "skip_host_skill_discovery",
                "event_id": descriptors[initial]["event_id"],
                "message_sha256": message_digest,
                "frame_sha256": descriptors[initial]["sanitized_message_sha256"],
            }
        )
        initial += 1
    if (
        starts != [initial]
        or completions != [len(payloads) - 1]
        or any(_unstable_feature_notice(payload) is not None for payload in payloads[initial:])
        or not messages
        or any(p.get("type") == "thread.started" for p in payloads[initial:])
        or not starts[0] < messages[-1] < completions[0]
        or any(p.get("type") in {"turn.failed", "error"} for p in payloads)
        or payloads[messages[-1]]["item"].get("text") != run.get("summary")
    ):
        raise ValueError("No single completed native turn binds the retained final output")
    summary = run["summary"]
    if not isinstance(summary, str) or not summary or len(summary.encode()) > 256000:
        raise ValueError("Completed output requires a bounded retained final message")
    usage = payloads[-1].get("usage")
    if not isinstance(usage, dict) or not usage or any(type(v) is not int or v < 0 for v in usage.values()):
        raise ValueError("Completed output has no exact structured native usage observation")
    return {
        "version": 1,
        "protocol": "native_completed_turn_v1",
        "dispatch_digest": dispatch["digest"],
        "events": descriptors,
        "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(),
        "usage_digest": digest(usage),
        **({"diagnostics": diagnostics} if diagnostics else {}),
        "implementation_success": False,
        "process_exit_attested": False,
    }


class SourceCompletedOutputReadmissions:
    def __init__(self, reconciliations: Any) -> None:
        self.service = reconciliations
        self.r = reconciliations.recoveries

    async def _observation(self, run: Data, dispatch: Data) -> Data:
        records = await self.r.store.event_records.provider_budget_events(identifier(run["id"]))
        events = [copy.deepcopy(row.data) for row in records]
        boundary = datetime.fromisoformat(dispatch["created_at"])
        selected = [
            e for e in events if e.get("stage") == "stdout" and datetime.fromisoformat(e["created_at"]) > boundary
        ]
        if not 1 <= len(selected) <= 256:
            raise ValueError("Completed output requires a bounded native activity interval")
        contents, total = {}, 0
        for event in selected:
            descriptor = validate_descriptor(event.get("activity_content"))
            size = descriptor["sanitized_message_bytes"]
            if type(size) is not int or size < 0:
                raise ValueError("Completed output activity size is not exact")
            total += size
            if total > 4 * 1024 * 1024:
                raise ValueError("Completed output activity exceeds its aggregate bound")
            contents[event["id"]] = await self.r.store.event_content_records.payload(
                identifier(run["id"]), identifier(event["id"]), size, descriptor["sanitized_message_sha256"]
            )
        return completed_output_observation({**run, "events": selected}, dispatch, contents)

    async def _review(self, run: Data, parent_request: str, actor: Data, previous_path: str) -> Data:
        from firefly_forge.source_edit_observed_reconciliation import _configuration_relocation
        from firefly_forge.source_human_readmission import (
            _RETAINED,
            _ancestral_unknown,
            _index_semantics,
            _validated_parent,
            validated_source_plan,
        )

        await self.r.store.assert_ownership()
        await self.r._settled(run, idle=True)
        entries = await self.r._entries(run["id"])
        event = _one(entries, "workspace_reconciliation_receipt", key=parent_request)
        marker = {"request_id": parent_request, "receipt_digest": event["digest"]}
        if run.get("source_edit_continuation") != marker:
            raise ValueError("The completed current source activation is required")
        parent, original = _validated_parent(entries, event, actor)
        unknown = _ancestral_unknown(entries, event, actor)
        completed = {e["idempotency_key"] for e in entries if e["kind"] == "workspace_reconciliation_receipt"}
        if any(
            e["kind"] == "workspace_reconciliation_intent"
            and e["idempotency_key"] not in completed
            and e["digest"] not in unknown
            for e in entries
        ):
            raise ValueError("An unknown source admission cannot grant another dispatch")
        task = self.r._task_view(await self.r.journal._task(run["id"], "implement"))
        attempt = original["previous_task"]["attempt"]
        if type(attempt) is not int or not 1 <= attempt < 4:
            raise ValueError("The completed output has no finite ordinary successor")
        key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{attempt}").hex
        next_key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{attempt + 1}").hex
        dispatches = [e for e in entries if e["kind"] == "provider_dispatch_intent"]
        dispatch = _one(entries, "provider_dispatch_intent", key=key + ":provider")
        state = run.get("provider_dispatch") or {}
        recovery = run.get("provider_recovery") or {}
        if recovery and (recovery.get("task_key") != key or recovery.get("status") not in {"consumed", "completed"}):
            raise ValueError("A different or unconsumed provider reservation holds output readmission")
        if recovery.get("task_key") == key:
            state = recovery
        if (
            run.get("status") != "failed"
            or run.get("active_task") != "implement"
            or task["attempt"] != attempt
            or task["status"] != "running"
            or task.get("idempotency_key") != key
            or state.get("task_key") != key
            or state.get("actor") != actor
            or dispatches[-1]["digest"] != dispatch["digest"]
            or len(dispatches) > 4
            or dispatch["sequence"] <= event["sequence"]
            or dispatch["payload"].get("task_key") != key
            or dispatch["payload"].get("attempt") != attempt
            or not any(
                e["kind"] == "intent" and e["task_id"] == "implement" and e["idempotency_key"] == key for e in entries
            )
            or any(
                e["kind"] == "receipt" and e["task_id"] == "implement" and e["idempotency_key"] == key for e in entries
            )
            or any(
                e["kind"] in {"intent", "provider_dispatch_intent"}
                and (e["idempotency_key"] == next_key or e["payload"].get("task_key") == next_key)
                for e in entries
            )
        ):
            raise ValueError("Only the known completed current failed source output may receive its next attempt")
        cut = self.r._cut(run)
        semantics = None
        if (
            cut["index_digest"] != parent["completed_cut"]["index_digest"]
            or original.get("human_readmission", {}).get("index_semantics") is not None
            or original.get("completed_output_readmission", {}).get("index_semantics") is not None
        ):
            semantics = _index_semantics(entries, event, actor, unknown, self.r.engine.workspaces / run["id"], run, cut)
        ignored = {"retained_state_digest"} | ({"index_digest"} if semantics else set())
        if {k: v for k, v in cut.items() if k not in ignored} != {
            k: v for k, v in parent["completed_cut"].items() if k not in ignored
        }:
            raise ValueError("The source, Git, provider or configuration differs from its completed activation")
        prior = {k: original["previous_run"].get(k) for k in _RETAINED}
        if (
            prior["implementation_rounds"] != attempt - 1
            or digest(prior) != parent["completed_cut"]["retained_state_digest"]
            or digest({**prior, "implementation_rounds": attempt}) != cut["retained_state_digest"]
        ):
            raise ValueError("The failed output changed retained state beyond its exact implementation round")
        if {k: v for k, v in state.get("cut", {}).items() if k not in ignored} != {
            k: v for k, v in parent["completed_cut"].items() if k not in ignored
        }:
            raise ValueError("The retained dispatch does not bind its completed source activation")
        observation = await self._observation(run, dispatch)
        scope = await source_edit_scope(self.r, run, archive_cut=original["source_edit"]["archive_cut"])
        relocation = _configuration_relocation(self.r, run, original["source_edit"], scope, previous_path)
        if self.r._cut(run) != cut:
            raise ValueError("Source output review changed during native qualification")
        source_plan = validated_source_plan(entries, event, actor, run)
        value = {
            "version": 1,
            "run_id": run["id"],
            "parent_request_id": parent_request,
            "parent_receipt_digest": event["digest"],
            "actor": actor,
            "previous_task": task,
            "previous_run_digest": digest(run),
            "cut": cut,
            "source_edit": scope,
            "configuration_relocation": relocation,
            "plan": source_plan["plan"],
            "source_plan_receipt_digest": source_plan["receipt_digest"],
            "observation": observation,
            "from_attempt": attempt,
            "to_attempt": attempt + 1,
            "admission_contract_digest": admission_contract_digest(),
            "journal_digest": entries[-1]["digest"],
        }
        if semantics is not None:
            value["index_semantics"] = semantics
        self.r.store.reject_credentials(value)
        return {**value, "digest": digest(value)}

    async def candidate(self, run_id: str, parent_request: str, previous_catalog_path: str) -> Data:
        identifier(parent_request)
        actor = await self.r._operator(run_id)
        async with self.r._locked(run_id, self.service._actor(actor)) as row:
            value = await self._review(row.data, parent_request, self.service._actor(actor), previous_catalog_path)
        if await self.r._operator(run_id) != actor:
            raise AccessDenied("The completed-output administrator changed")
        return value

    @staticmethod
    def _existing(entries: list[Data], parent_request: str, request_id: str, actor: Data) -> Data | None:
        intents = [
            e for e in entries if e["kind"] == "workspace_reconciliation_intent" and e["idempotency_key"] == request_id
        ]
        if not intents:
            if any(e["kind"] == "source_output_readmission" and e["idempotency_key"] == request_id for e in entries):
                raise ValueError("An incomplete observed-output admission cannot be replayed")
            return None
        if len(intents) != 1:
            raise ValueError("The output admission identity is not unique")
        event = _one(entries, "workspace_reconciliation_receipt", key=request_id)
        value: Data = event["payload"]
        if (
            value.get("actor") != actor
            or value.get("candidate", {})
            .get("completed_output_readmission", {})
            .get("parent_marker", {})
            .get("request_id")
            != parent_request
        ):
            raise AccessDenied("The completed-output receipt belongs to another native scope")
        validate_completed_output_parent(entries, event, actor)
        return copy.deepcopy(value)

    async def get(self, run_id: str, parent_request: str, request_id: str) -> Data:
        identifier(parent_request)
        identifier(request_id)
        actor = await self.r._operator(run_id)
        async with self.r._locked(run_id, self.service._actor(actor)):
            value = self._existing(
                await self.r._entries(run_id), parent_request, request_id, self.service._actor(actor)
            )
            if value is None:
                raise KeyError("The completed-output readmission was not found")
            return value

    async def admit(self, run_id: str, parent_request: str, body: SourceCompletedOutputInput) -> Data:
        from firefly_forge.configuration import redact_secrets
        from firefly_forge.engine import redact
        from firefly_forge.workspace_reconciliations import _source_patch

        body = SourceCompletedOutputInput.model_validate(body.model_dump())
        identifier(parent_request)
        actor = await self.r._operator(run_id)
        snapshot = self.service._actor(actor)
        async with self.r._locked(run_id, snapshot):
            prior = self._existing(await self.r._entries(run_id), parent_request, body.request_id, snapshot)
            if prior is not None:
                if prior["completed_output_request"] != body.model_dump():
                    raise ValueError("The output admission identity already contains different consent")
                return prior
        review = await self.candidate(run_id, parent_request, body.previous_catalog_path)
        if (
            body.request_id == parent_request
            or body.candidate_digest != review["digest"]
            or body.admission_contract_digest != review["admission_contract_digest"]
        ):
            raise ValueError("Completed-output admission requires its exact fresh candidate and contract")
        if body.valid_for_seconds > review["source_edit"]["provider_timeout"]:
            raise ValueError("Completed-output admission exceeds the retained provider deadline")
        run = await self.r.store.get_run(run_id)
        patch = _source_patch(self.r.engine.workspaces / run_id, run["base_revision"])
        settings = self.r.store.configuration.load_snapshot(run["config_revision"])
        if redact_secrets(redact(patch), settings) != patch:
            raise ValueError("Completed source contains a configured credential")
        await Store.admit_source_patch(self.r.store, SourcePatch(patch))
        if await self.r._operator(run_id) != actor:
            raise AccessDenied("The completed-output administrator changed during admission")
        async with self.r._locked(run_id, snapshot) as row:
            current = await self._review(row.data, parent_request, snapshot, body.previous_catalog_path)
            if (
                current != review
                or _source_patch(self.r.engine.workspaces / run_id, run["base_revision"]) != patch
                or CURRENT_PRINCIPAL.get() != actor
            ):
                raise ValueError("The completed-output admission changed during credential admission")
            marker = {
                "parent_marker": copy.deepcopy(row.data["source_edit_continuation"]),
                "previous_task": review["previous_task"],
                "from_attempt": review["from_attempt"],
                "to_attempt": review["to_attempt"],
                "admission_contract_digest": review["admission_contract_digest"],
                "observation_digest": digest(review["observation"]),
                "review_digest": review["digest"],
            }
            if "index_semantics" in review:
                marker["index_semantics"] = review["index_semantics"]
            key = uuid5(NAMESPACE_URL, f"forge:{run_id}:implement:{review['to_attempt']}").hex
            task = {
                "id": "implement",
                "status": "queued",
                "attempt": review["to_attempt"],
                "idempotency_key": key,
                "provider_recovery_id": body.request_id,
            }
            candidate = {
                "version": 1,
                "run_id": run_id,
                "actor": snapshot,
                "previous_run": copy.deepcopy(row.data),
                "previous_run_digest": digest(row.data),
                "previous_task": task,
                "cut": review["cut"],
                "plan": copy.deepcopy(review["plan"]),
                "source_edit": review["source_edit"],
                "completed_output_readmission": marker,
                "journal_digest": review["journal_digest"],
            }
            candidate["digest"] = digest(candidate)
            request = SourceEditContinuationInput(
                version=1,
                request_id=body.request_id,
                candidate_digest=candidate["digest"],
                acknowledge_environment_only=True,
                acknowledge_resume_existing_task=True,
                acknowledge_existing_read_scope=True,
                acknowledge_no_commands=True,
            ).model_dump()
            expires = clock_ms() + body.valid_for_seconds * 1000
            observed = await self.r.journal._append(
                run_id,
                "implement",
                body.request_id,
                "source_output_readmission",
                {
                    "version": 1,
                    "request": body.model_dump(),
                    "actor": snapshot,
                    "parent_marker": marker["parent_marker"],
                    "observation": review["observation"],
                    "review": review,
                    "previous_task": review["previous_task"],
                    "new_candidate_digest": candidate["digest"],
                    "expires_at_ms": expires,
                    "no_environment_effect": True,
                    "no_provider_dispatch": True,
                },
            )
            payload = {
                "version": 1,
                "request": request,
                "actor": snapshot,
                "candidate": candidate,
                "previous_task": task,
                "completed_output_request": body.model_dump(),
                "readmission_digest": observed["digest"],
                "expires_at_ms": expires,
                "public_patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            }
            intent = await self.r.journal._append(
                run_id, "implement", body.request_id, "workspace_reconciliation_intent", payload
            )
            await self.r.journal._append(
                run_id,
                "implement",
                key,
                "intent",
                {
                    "reason": "explicit_observed_source_output",
                    "admission_id": body.request_id,
                    "attempt": review["to_attempt"],
                },
            )
            queued = await self.r.journal._task(run_id, "implement")
            queued.status, queued.attempt = "queued", review["to_attempt"]
            queued.data = {"idempotency_key": key, "provider_recovery_id": body.request_id}
            await self.r.journal.tasks.save(queued)
            row.status = "queued"
            row.data = {
                **row.data,
                "status": "queued",
                "resume_existing": True,
                "updated_at": now(),
                "provider_recovery": {
                    "request_id": body.request_id,
                    "task_key": key,
                    "status": "reserved",
                    "attempt": review["to_attempt"],
                    "cut": review["cut"],
                    "actor": snapshot,
                    "parent_receipt_digest": review["parent_receipt_digest"],
                },
            }
            receipt = {
                **payload,
                "intent_digest": intent["digest"],
                "completed_cut": self.r._cut(row.data),
                "effect": {
                    "version": 1,
                    "no_environment_effect": True,
                    "completed_output_observation": marker["observation_digest"],
                },
            }
            self.r.store.reject_credentials(receipt)
            event = await self.r.journal._append(
                run_id, "implement", body.request_id, "workspace_reconciliation_receipt", receipt
            )
            row.data = {
                **row.data,
                "source_edit_continuation": {"request_id": body.request_id, "receipt_digest": event["digest"]},
            }
            await self.r.store.run_records.save(row)
            await self.r.store.assert_ownership()
            if await self.r._operator(run_id) != actor or CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The native completed-output administrator changed before commit")
            if (
                self.r._cut(row.data) != review["cut"]
                or _source_patch(self.r.engine.workspaces / run_id, run["base_revision"]) != patch
                or clock_ms() >= expires
            ):
                raise ValueError("The source or finite completed-output admission changed before commit")
            validate_completed_output_parent(await self.r._entries(run_id), event, snapshot)
            return copy.deepcopy(receipt)
