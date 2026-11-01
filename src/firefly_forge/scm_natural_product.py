# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Retained product answers enter the existing separately bounded native dialogue authority."""

from __future__ import annotations

import copy
import json
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.channel_actions import ChannelCommand
from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_natural_effects import NaturalReadmission, intake_digest
from firefly_forge.store import identifier, now

Data = dict[str, Any]
SOURCE_FIELDS = (
    "repository_id",
    "provider",
    "remote_id",
    "actor",
    "digest",
    "issue",
    "verification",
    "source_snapshot",
)


def product_command(parent: Data, conversation_id: str | None = None) -> Data:
    resolution = parent["natural_turn"]["resolution"]
    if (
        resolution["state"] not in {"product_answer", "product_request"}
        or not resolution.get("answer")
        or resolution["answer"] != parent["natural_turn"]["human_message"]
        or parent["issue"]["kind"] != "issue"
    ):
        raise AccessDenied("Only the original retained product request or answer can enter native dialogue")
    if resolution["state"] == "product_answer":
        if not resolution.get("conversation_id") or not resolution.get("turn_id"):
            raise AccessDenied("A product answer requires its exact original typed question")
        if conversation_id is not None and conversation_id != resolution["conversation_id"]:
            raise AccessDenied("The product answer cannot select another conversation")
        conversation_id = resolution["conversation_id"]
    values = dict(
        action="assistant",
        operation="turn" if conversation_id else "start",
        project_id=resolution["scope"]["project_id"],
    )
    if conversation_id:
        values.update(
            conversation_id=conversation_id,
            request_id=digest({"natural_product_answer": parent["id"], "intake_digest": intake_digest(parent)})[:32],
            message=resolution["answer"],
        )
    return ChannelCommand(**values).model_dump(exclude_none=True)


def projected_product(parent: Data, cut: Data, admission_id: str | None = None) -> Data:
    command = product_command(parent, cut["conversation_id"])
    marker = {
        "version": 1,
        "admission_id": admission_id,
        "receipt_id": parent["id"],
        "intake_digest": intake_digest(parent),
        "command_digest": digest(command),
        "grant_digest": cut["grant_digest"],
        "binding_digest": cut["binding_digest"],
        "scope_digest": digest(cut["scopes"]),
        "question_digest": parent["natural_turn"]["resolution"].get("question_digest"),
        "operation": command["operation"],
        "conversation_id": command.get("conversation_id"),
    }
    child_id = digest(marker)[:32]
    return {
        **{key: copy.deepcopy(parent[key]) for key in SOURCE_FIELDS},
        "id": child_id,
        "delivery_id": "natural-product:" + child_id,
        "command": command,
        "status": "received",
        "action_status": "not_dispatched",
        "feedback_status": "not_requested",
        "run_id": None,
        "reason": None,
        "global_ingress": False,
        "natural_product_parent": marker,
    }


def validate_product_projection(child: Data, parent: Data) -> None:
    marker = child.get("natural_product_parent")
    if (
        not isinstance(marker, dict)
        or set(marker)
        != {
            "version",
            "receipt_id",
            "intake_digest",
            "command_digest",
            "grant_digest",
            "binding_digest",
            "scope_digest",
            "question_digest",
            "operation",
            "conversation_id",
            "admission_id",
        }
        or type(marker["version"]) is not int
        or marker["version"] != 1
        or parent.get("status") != "processed"
        or parent["natural_turn"].get("classifier_outcome") != "confirmed_valid"
        or parent["natural_turn"].get("state")
        not in {"product_answer", "product_request", "product_pending_permission", "product_completed"}
        or (parent.get("natural_product_effects") or {}).get(child["id"]) != marker
        or marker["receipt_id"] != parent["id"]
        or marker["intake_digest"] != intake_digest(parent)
        or marker["question_digest"] != parent["natural_turn"]["resolution"].get("question_digest")
    ):
        raise AccessDenied("The exact retained product answer admission changed")
    if marker["admission_id"] is not None and marker["admission_id"] not in parent.get(
        "natural_product_admissions", {}
    ):
        raise AccessDenied("The explicit product answer admission receipt is unavailable")
    command = product_command(parent, marker["conversation_id"])
    if marker["operation"] != command["operation"]:
        raise AccessDenied("The product continuation operation changed")
    if marker["command_digest"] != digest(command) or child["command"] != command or digest(marker)[:32] != child["id"]:
        raise AccessDenied("The product continuation command changed")
    if any(child.get(key) != parent.get(key) for key in SOURCE_FIELDS):
        raise AccessDenied("The product continuation lost its exact original human source")


class NaturalProductContinuation:
    def __init__(self, natural: Any) -> None:
        self.natural, self.collaboration, self.store = natural, natural.collaboration, natural.store

    async def _question(self, parent: Data, *, remote: bool) -> tuple[list[Data], Data]:
        from firefly_forge.scm_natural_tasks import natural_policy_digest

        turn = parent.get("natural_turn") or {}
        if parent["status"] != "processed" or turn.get("policy_digest") != natural_policy_digest():
            raise AccessDenied("The retained product answer policy or custody changed")
        product_command(parent)
        await self.natural._model(turn["model_selection"])
        scopes = await self.natural.conversations.scopes(parent, remote=remote)
        context = await self.natural._context(parent, scopes)
        resolution = turn["resolution"]
        if scopes[0]["native_user_id"] != turn["context"]["scope"]["native_user_id"]:
            raise AccessDenied("The original product conversation owner mapping changed")
        if resolution["state"] == "product_request":
            return scopes, {}
        rows = [row for row in context["candidates"] if row.get("question_id") == resolution["question_id"]]
        if len(rows) != 1 or any(
            rows[0].get(key) != resolution.get(key) for key in ("question_digest", "conversation_id", "turn_id")
        ):
            raise AccessDenied("The exact published product question or conversation changed")
        if rows[0].get("origin") != "product_assistant_turn":
            raise AccessDenied("A product turn requires its typed native question producer")
        if scopes[0]["native_user_id"] != turn["context"]["scope"]["native_user_id"]:
            raise AccessDenied("The original product question owner mapping changed")
        # Completed replies from another turn must not be silently superseded by an old answer.
        manager = self.collaboration.product_assistant
        async with self.store._unit():
            events = [row.data for row in await manager.events.events(identifier(resolution["conversation_id"]))]
        origin = next(
            (
                i
                for i, event in enumerate(events)
                if event["kind"] == "turn_completed" and event["payload"].get("turn_id") == resolution["turn_id"]
            ),
            None,
        )
        if origin is None:
            raise AccessDenied("The exact original product-question completion is unavailable")
        request_id = product_command(parent)["request_id"]
        owned = {
            event["payload"].get("turn_id")
            for event in events[origin + 1 :]
            if event["kind"] == "turn_intent" and event["payload"].get("request_id") == request_id
        }
        if any(
            event["kind"] == "turn_completed" and event["payload"].get("turn_id") not in owned
            for event in events[origin + 1 :]
        ):
            raise AccessDenied("A later completed product reply changed the question context")
        return scopes, rows[0]

    async def review(self, receipt_id: str, *, remote: bool = True) -> Data:
        from firefly_forge.scm_polling import poll_key

        parent = await self.collaboration.receipt(receipt_id)
        self.natural.require_effect_transport(parent)
        scopes, question = await self._question(parent, remote=remote)
        authority = self.collaboration.poll_functional_authority
        if authority is None:
            raise AccessDenied("The native functional dialogue authority is unavailable")
        project = scopes[0]["project_id"]
        grant = await authority._grant(project, parent["repository_id"], key=poll_key(project, parent["verification"]))
        if grant is None:
            raise AccessDenied("A fresh separately bounded product model allowance is required")
        request = authority.request_type.model_validate(grant["request"])
        binding = await authority._binding(project, parent["repository_id"], request)
        if "assistant_turn" not in request.allowed_actions or not request.active:
            raise AccessDenied("The current product allowance does not permit a model turn")
        conversation_id = parent["natural_turn"]["resolution"].get("conversation_id")
        if parent["natural_turn"]["resolution"]["state"] == "product_request":
            from firefly_forge.scm_poll_functional import functional_state

            async with self.store._unit():
                settings, _ = await authority.polling._state(parent["repository_id"])
                usage = functional_state(settings.data)
            expected = dict(
                project_id=project,
                repository_id=parent["repository_id"],
                issue_number=request.issue_number,
                binding_key=grant["binding_key"],
                model=authority._model(request),
            )
            matches = [key for key, value in usage["conversations"].items() if value == expected]
            starts = [
                key for key, value in parent.get("natural_product_effects", {}).items() if value["operation"] == "start"
            ]
            completed = []
            for key in starts:
                original = await self.collaboration.receipt(key)
                if original["status"] == "processed" and original.get("action_status") == "completed":
                    validate_product_projection(original, parent)
                    completed.append(original["action_result"]["conversation_id"])
            if completed:
                if len(completed) != 1 or completed[0] not in matches:
                    raise AccessDenied("The original created product conversation or model cut changed")
                conversation_id = completed[0]
            elif len(matches) > 1:
                raise AccessDenied("Multiple exact issue-bound conversations require a human clarification")
            elif matches:
                conversation_id = matches[0]
        command = product_command(parent, conversation_id)
        if "assistant_" + command["operation"] not in request.allowed_actions:
            raise AccessDenied("The fresh product allowance does not permit this native dialogue operation")
        cut = {
            "version": 1,
            "receipt_id": receipt_id,
            "intake_digest": intake_digest(parent),
            "scopes": scopes,
            "question": question,
            "grant_digest": grant["digest"],
            "binding_digest": binding["digest"],
            "effects": parent.get("natural_product_effects", {}),
            "conversation_id": conversation_id,
        }
        return {**cut, "digest": digest(cut)}

    async def apply(self, receipt_id: str, *, reviewed: Data | None = None, admission_id: str | None = None) -> Data:
        for _ in range(2):
            parent = await self.collaboration.receipt(receipt_id)
            if parent["natural_turn"].get("state") in {"product_completed", "needs_reconciliation"}:
                return dict(parent)
            try:
                cut = await self.review(receipt_id)
                if reviewed is not None and cut != reviewed:
                    raise AccessDenied("The reviewed product continuation cut changed")
            except (AccessDenied, ValueError, KeyError):
                return await self._retain(parent, "product_pending_permission")
            reviewed = None
            child = projected_product(parent, cut, admission_id)
            async with self.natural.conversations._locked(cut["scopes"], parent["repository_id"]):
                row = await self.collaboration.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                if row is None or row.data != parent or await self.review(receipt_id, remote=False) != cut:
                    raise AccessDenied("The exact product answer changed before native turn reservation")
                effects = row.data.get("natural_product_effects", {})
                existing = await self.collaboration.inbox.find_by_id(
                    identifier(child["id"]), lock=LockMode.PESSIMISTIC_WRITE
                )
                for key, marker in effects.items():
                    old = await self.collaboration.inbox.find_by_id(identifier(key), lock=LockMode.PESSIMISTIC_WRITE)
                    if old is None:
                        raise AccessDenied("The original product continuation receipt is unavailable")
                    validate_product_projection(old.data, row.data)
                    if key != child["id"] and not (
                        old.status == "denied"
                        and old.data.get("action_status") == "not_dispatched"
                        or marker["operation"] == "start"
                        and old.status == "processed"
                        and old.data.get("action_status") == "completed"
                    ):
                        raise AccessDenied("The original product continuation is unknown or completed; never replay it")
                if existing is not None:
                    validate_product_projection(existing.data, row.data)
                    child = copy.deepcopy(existing.data)
                    if child["status"] == "denied":
                        return await self._retain(row.data, "product_pending_permission")
                else:
                    from firefly_forge.scm_polling import MAX_BYTES, inbox_size

                    count, size = await inbox_size(self.store, parent["repository_id"])
                    if count >= 5000 or size + len(json.dumps(child).encode()) > MAX_BYTES or len(effects) >= 50:
                        raise AccessDenied("Product continuation receipt retention is full")
                    child.update(created_at=now(), updated_at=now())
                    await self.collaboration.inbox.save(
                        ForgeWebhookInboxEntity(
                            id=identifier(child["id"]),
                            repository_id=identifier(parent["repository_id"]),
                            provider=parent["provider"],
                            delivery_id=child["delivery_id"],
                            source_key=child["delivery_id"],
                            status="received",
                            data=child,
                        )
                    )
                    await self.collaboration._write_row(
                        row, natural_product_effects={**effects, child["id"]: child["natural_product_parent"]}
                    )
                expected = copy.deepcopy(row.data)
            try:
                result = await self.collaboration.poll_functional_authority.process(child["id"])
            except (AccessDenied, ValueError):
                # A confirmed refusal before the existing service intent is safe to retain for fresh admission.
                async with self.store._unit():
                    failed = await self.collaboration.inbox.find_by_id(
                        identifier(child["id"]), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if failed is not None and failed.status == "received" and failed.data == child:
                        await self.collaboration._write_row(
                            failed,
                            status="denied",
                            action_status="not_dispatched",
                            reason="natural_product_admission_denied",
                        )
                        return await self._retain(expected, "product_pending_permission")
                return await self._retain(expected, "needs_reconciliation")
            except Exception:
                return await self._retain(expected, "needs_reconciliation")
            except BaseException:
                await self._retain(expected, "needs_reconciliation")
                raise
            if result["status"] != "processed" or result.get("action_status") != "completed":
                return await self._retain(expected, "needs_reconciliation")
            if child["command"]["operation"] == "start":
                continue
            await self.collaboration.feedback(child["id"])
            return await self._retain(expected, "product_completed")
        return dict(await self.collaboration.receipt(receipt_id))

    async def _retain(self, expected: Data, state: str) -> Data:
        async with self.store._unit():
            row = await self.collaboration.inbox.find_by_id(identifier(expected["id"]), lock=LockMode.PESSIMISTIC_WRITE)
            if row is not None and row.data == expected:
                await self.collaboration._write_row(
                    row,
                    natural_turn={**row.data["natural_turn"], "state": state},
                    feedback_status=("sent" if row.data.get("feedback_status") == "sent" else "not_requested")
                    if state == "product_completed"
                    else "held"
                    if state == "needs_reconciliation"
                    else row.data["feedback_status"],
                )
        return dict(await self.collaboration.receipt(expected["id"]))

    async def readmit(self, receipt_id: str, request: NaturalReadmission) -> Data:
        parent = await self.collaboration.receipt(receipt_id)
        actor = CURRENT_PRINCIPAL.get()
        if actor is None:
            raise AccessDenied("A current native project editor must admit this product answer")
        original = parent.get("natural_product_admissions", {}).get(request.request_id)
        if original is not None:
            if original["request"] != request.model_dump(mode="json") or original[
                "operator"
            ] != ProjectAccess.actor_snapshot(actor):
                raise AccessDenied("The original product admission belongs to another input or actor")
            await ProjectAccess(self.store).require_project(actor, original["project_id"], "edit")
            return dict(parent)
        cut = await self.review(receipt_id)
        if request.expected_intake_digest != cut["intake_digest"] or request.expected_cut_digest != cut["digest"]:
            raise AccessDenied("Review the exact retained product answer and fresh allowance")
        project = cut["scopes"][0]["project_id"]
        await ProjectAccess(self.store).require_project(actor, project, "edit")
        await self.store.admit_credentials(request.model_dump(mode="json"))
        async with self.natural.conversations._locked(cut["scopes"], parent["repository_id"]):
            identity = await self.collaboration.polling.users.find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
            if (
                identity is None
                or not identity.enabled
                or (identity.issuer, identity.subject) != (actor.issuer, actor.subject)
                or CURRENT_PRINCIPAL.get() != actor
            ):
                raise AccessDenied("The reviewing product identity changed")
            await ProjectAccess(self.store).require_project(actor, project, "edit")
            if await self.review(receipt_id, remote=False) != cut:
                raise AccessDenied("The reviewed product allowance changed before admission")
            row = await self.collaboration.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.data != parent:
                raise AccessDenied("The retained product answer custody changed")
            admissions = copy.deepcopy(row.data.get("natural_product_admissions", {}))
            if len(admissions) >= 50:
                raise AccessDenied("Product answer admission retention is full")
            admissions[request.request_id] = {
                "request": request.model_dump(mode="json"),
                "operator": ProjectAccess.actor_snapshot(actor),
                "operator_identity": {"issuer": actor.issuer, "subject": actor.subject},
                "project_id": project,
                "cut_digest": cut["digest"],
            }
            await self.collaboration._write_row(row, natural_product_admissions=admissions)
        return await self.apply(receipt_id, reviewed=cut, admission_id=request.request_id)


async def authorize_product_projection(authority: Any, child: Data, *, remote: bool) -> Data:
    from firefly_forge.scm_natural_tasks import SCMNaturalTasks

    marker = child.get("natural_product_parent") or {}
    parent = await authority.polling.collaboration.receipt(marker["receipt_id"])
    validate_product_projection(child, parent)
    if marker["admission_id"] is not None:
        admission = parent["natural_product_admissions"][marker["admission_id"]]
        operator = admission["operator"]
        async with authority.store._unit():
            identity = await authority.polling.users.find_by_id(
                identifier(operator["user_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            if (
                identity is None
                or not identity.enabled
                or {"issuer": identity.issuer, "subject": identity.subject} != admission["operator_identity"]
            ):
                raise AccessDenied("The product answer reviewing identity changed")
            await ProjectAccess(authority.store).authorize_actor(operator, admission["project_id"], "edit")
    service = NaturalProductContinuation(SCMNaturalTasks(authority.polling.collaboration))
    cut = await service.review(parent["id"], remote=remote)
    if any(marker[key] != cut[key] for key in ("grant_digest", "binding_digest")) or marker["scope_digest"] != digest(
        cut["scopes"]
    ):
        raise AccessDenied("The exact current product continuation allowance changed")
    return cut
