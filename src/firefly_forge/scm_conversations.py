# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native ticket-bound read conversations, separate from execution principals."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text

from firefly_forge.collaboration_feedback import TERMINAL
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.project_access import AccessDenied
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_conversation_models import AddressedContextV1, AddressedStatusV1, TicketContextReferenceV1
from firefly_forge.store import identifier

if TYPE_CHECKING:
    from firefly_forge.collaboration import Collaboration

Data = dict[str, Any]


def observation_cut(delivery: Data) -> Data:
    return {
        k: copy.deepcopy(delivery.get(k))
        for k in (
            "id",
            "repository_id",
            "provider",
            "actor",
            "digest",
            "issue",
            "command",
            "source_snapshot",
            "verification",
            "source_conflict",
            "global_ingress",
            "addressed_source",
        )
    }


class SCMConversations:
    def __init__(self, collaboration: Collaboration) -> None:
        self.collaboration = collaboration
        self.store = collaboration.store

    async def scopes(self, delivery: Data, *, remote: bool) -> list[Data]:
        if (delivery.get("verification") or {}).get("transport") == "github_rest_poll_v1":
            if self.collaboration.polling is None:
                raise AccessDenied("The native polling conversation authority is unavailable")
            return [await self.collaboration.polling.conversation_scope(delivery, remote=remote)]
        return await SCMActorAuthority(self.store, self.collaboration.scm).conversation_scopes(delivery, remote=remote)

    @asynccontextmanager
    async def _locked(self, scopes: list[Data], repository: str) -> AsyncIterator[None]:
        async with self.store._unit() as session:
            projects = sorted({scope["project_id"] for scope in scopes})
            for project in projects:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                    {"key": self.store.schema + ":members:" + project},
                )
            for user_id in sorted({scope["native_user_id"] for scope in scopes}):
                user = await ForgeIdentityUserRepository().find_by_id(
                    identifier(user_id), lock=LockMode.PESSIMISTIC_WRITE
                )
                if user is None or not user.enabled:
                    raise AccessDenied("The current native conversation identity is unavailable")
            for project in projects:
                if (
                    await ForgeProjectRepository().find_by_id(identifier(project), lock=LockMode.PESSIMISTIC_WRITE)
                    is None
                ):
                    raise AccessDenied("The current native conversation project is unavailable")
            await self.collaboration._repo_lock(repository)
            yield

    async def references(self, delivery: Data, scopes: list[Data], *, durable: bool = False) -> tuple[list[Data], Data]:
        references, runs = [], {}
        async with self.store._unit():
            rows = await (
                self.collaboration.inbox.conversation_context(delivery)
                if durable
                else self.collaboration.inbox.ticket_context(delivery)
            )
            if (
                len(rows) > (5000 if durable else 200)
                or len(json.dumps([row.data for row in rows]).encode()) > 16 * 1024 * 1024
            ):
                raise AccessDenied("Ticket context exceeds the bounded lookup; review its exact native receipts")
            candidates = [(copy.deepcopy(row.data), row.sequence) for row in rows]
        for previous, sequence in candidates:
            run_id = previous.get("run_id")
            if (
                not run_id
                or previous["id"] == delivery["id"]
                or previous.get("natural_parent")
                or (previous.get("command") or {}).get("adapter")
            ):
                continue
            try:
                run = await self.store.get_run(run_id)
                scope = next(
                    (
                        s
                        for s in scopes
                        if s["project_id"] == run.get("project_id") and run.get("component_id") in s["component_ids"]
                    ),
                    None,
                )
                if scope is None or run.get("repository_id") != delivery["repository_id"]:
                    continue
                if (previous.get("verification") or {}).get("transport") == "github_rest_poll_v1":
                    polling = self.collaboration.polling or getattr(self.store, "scm_polling", None)
                    if polling is None:
                        continue
                    await polling.ticket_reference(previous, run)
                else:
                    await SCMActorAuthority(self.store, self.collaboration.scm).ticket_reference(previous, run)
            except (AccessDenied, KeyError, ValueError, TypeError):
                continue
            if run_id in runs:
                continue
            runs[run_id] = run
            if len(runs) > 200:
                raise AccessDenied("The current ticket has too many distinct run references for one natural choice")
            references.append(
                TicketContextReferenceV1(
                    receipt_id=previous["id"],
                    sequence=sequence,
                    run_id=run_id,
                    project_id=run["project_id"],
                    component_id=run["component_id"],
                    context_digest=digest(
                        {
                            "observation": observation_cut(previous),
                            "actor": previous.get("scm_execution_actor"),
                            "run_id": run_id,
                            "action_status": previous.get("action_status"),
                        }
                    ),
                ).model_dump(mode="json")
            )
        return references, runs

    async def selection(self, delivery: Data, scopes: list[Data]) -> Data:
        message = AddressedStatusV1.model_validate(delivery["command"])
        references, runs = await self.references(delivery, scopes) if message.intent == "status" else ([], {})
        active = [r for r in references if runs[r["run_id"]]["status"] not in TERMINAL]
        eligible = active if active else references[:1]
        selection = (
            "clarify"
            if message.intent == "clarify"
            else "missing"
            if not eligible
            else "ambiguous"
            if len(active) > 1
            else "run"
        )
        return {
            "version": 1,
            "selection": selection,
            "scopes": scopes,
            "references": references,
            "candidate_ids": sorted(r["run_id"] for r in eligible),
            "selected_run_id": eligible[0]["run_id"] if selection == "run" else None,
        }

    async def process(self, receipt_id: str) -> Data:
        initial = await self.collaboration.receipt(receipt_id)
        if initial["status"] != "received":
            return initial
        if (
            initial["command"].get("adapter") in {"addressed_status_v1", "retained_question_reply_v1"}
            and (initial.get("verification") or {}).get("transport") == "github_rest_poll_v1"
        ):
            from firefly_forge.scm_natural_effects import NaturalEffects
            from firefly_forge.scm_natural_tasks import SCMNaturalTasks

            natural = SCMNaturalTasks(self.collaboration)
            try:
                result = await natural.process(receipt_id)
            except (AccessDenied, ValueError, KeyError):
                async with self.store._unit():
                    row = await self.collaboration.inbox.find_by_id(
                        identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if (
                        row is not None
                        and row.status == "received"
                        and observation_cut(row.data) == observation_cut(initial)
                    ):
                        await self.collaboration._write_row(
                            row,
                            status="denied",
                            action_status="not_dispatched",
                            reason="natural_current_read_authority_or_question_changed",
                        )
                return await self.collaboration.receipt(receipt_id)
            if result["status"] == "processed" and (result["natural_turn"].get("resolution") or {}).get("state") in {
                "answer",
                "resume",
            }:
                result = await NaturalEffects(natural).apply(receipt_id)
            if result["status"] == "processed" and (result["natural_turn"].get("resolution") or {}).get("state") in {
                "product_answer",
                "product_request",
            }:
                from firefly_forge.scm_natural_product import NaturalProductContinuation

                result = await NaturalProductContinuation(natural).apply(receipt_id)
            await self.collaboration.feedback(receipt_id)
            return await self.collaboration.receipt(receipt_id)
        try:
            scopes = await self.scopes(initial, remote=True)
            selected = await self.selection(initial, scopes)
            async with self._locked(scopes, initial["repository_id"]):
                row = await self.collaboration.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                if row is None or observation_cut(row.data) != observation_cut(initial):
                    raise AccessDenied("The exact original conversation observation changed")
                if row.status != "received":
                    return copy.deepcopy(row.data)
                current = copy.deepcopy(row.data)
                if (
                    await self.scopes(current, remote=False) != scopes
                    or await self.selection(current, scopes) != selected
                ):
                    raise AccessDenied("The current native conversation context changed before admission")
                sealed = AddressedContextV1.model_validate({**selected, "digest": digest(selected)}).model_dump(
                    mode="json"
                )
                run = await self.store.get_run(selected["selected_run_id"]) if selected["selected_run_id"] else {}
                await self.collaboration._write_row(
                    row,
                    status="processed",
                    reason=None,
                    action_status="completed",
                    run_id=selected["selected_run_id"],
                    conversation=sealed,
                    feedback_status="pending",
                    feedback_stage=run.get("status", selected["selection"]),
                )
        except (AccessDenied, ValueError, KeyError):
            async with self.store._unit():
                row = await self.collaboration.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                if row is not None and row.status == "received":
                    await self.collaboration._write_row(
                        row,
                        status="denied",
                        reason="conversation_status_consent_required",
                        action_status="not_dispatched",
                    )
        await self.collaboration.feedback(receipt_id)
        return await self.collaboration.receipt(receipt_id)

    async def authorize_feedback(self, expected: Data, *, remote: bool) -> None:
        if expected.get("natural_turn") is not None:
            from firefly_forge.scm_natural_tasks import SCMNaturalTasks

            await SCMNaturalTasks(self.collaboration).authorize_feedback(expected, remote=remote)
            return
        current = await self.collaboration.receipt(expected["id"])
        retained = AddressedContextV1.model_validate(current.get("conversation")).model_dump(mode="json")
        unsigned = {k: v for k, v in retained.items() if k != "digest"}
        if (
            observation_cut(current) != observation_cut(expected)
            or retained != expected.get("conversation")
            or digest(unsigned) != retained["digest"]
        ):
            raise AccessDenied("The retained conversation context changed")
        scopes = await self.scopes(current, remote=remote)
        selected = await self.selection(current, scopes)
        if selected != unsigned or current.get("run_id") != retained["selected_run_id"]:
            raise AccessDenied("The native ticket selection or status-only authority changed")
