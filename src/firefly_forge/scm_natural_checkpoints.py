# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Natural requests enter the same native Git owner queue without inheriting admin rights."""

from __future__ import annotations

import copy
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.git_delivery_models import GitDeliveryInputV1, GitDeliveryPolicyV1
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_natural_effects import intake_digest
from firefly_forge.store import identifier

Data = dict[str, Any]


class SCMNaturalCheckpoints:
    def __init__(self, natural: Any) -> None:
        self.natural, self.collaboration, self.store = natural, natural.collaboration, natural.store

    async def _retain(self, expected: Data, state: str, **fields: Any) -> Data:
        async with self.store._unit():
            row = await self.collaboration.inbox.find_by_id(identifier(expected["id"]), lock=LockMode.PESSIMISTIC_WRITE)
            if row is not None and row.data == expected:
                await self.collaboration._write_row(
                    row, natural_turn={**row.data["natural_turn"], "state": state}, **fields
                )
        return dict(await self.collaboration.receipt(expected["id"]))

    async def apply(self, receipt_id: str) -> Data:
        parent: Data = await self.collaboration.receipt(receipt_id)
        # Even a lost queue acknowledgement consumes this message's one admission attempt.
        if parent.get("natural_checkpoint") is not None:
            return dict(parent)
        try:
            self.natural.require_effect_transport(parent)
            actor = await self.collaboration.polling.resolve_checkpoint(parent)
            run_id = parent["natural_turn"]["resolution"]["run_id"]
            run = await self.store.get_run(run_id)
            policy = GitDeliveryPolicyV1.model_validate(run.get("git_delivery_policy") or {})
            if not policy.enabled or run["status"] not in {"paused", "failed"}:
                raise AccessDenied("A native closed unit with its frozen Git policy and source tree is required")
            tickets = getattr(self.store.execution_requests, "git_deliveries", None)
            if tickets is None:
                raise AccessDenied("The native checkpoint owner queue is unavailable")
            token = CURRENT_PRINCIPAL.set(actor)
            try:
                candidate = await tickets.deliveries.candidate(run_id)
                body = GitDeliveryInputV1(
                    version=1,
                    request_id=digest({"receipt_id": receipt_id, "intake_digest": intake_digest(parent)})[:32],
                    expected_head=candidate["expected_head"],
                    expected_source_digest=candidate["expected_source_digest"],
                )
                marker = {
                    "version": 1,
                    "state": "admitting",
                    "run_id": run_id,
                    "intake_digest": intake_digest(parent),
                    "request": body.model_dump(mode="json"),
                    "actor": ProjectAccess.actor_snapshot(actor),
                }
                scopes = await self.natural.conversations.scopes(parent, remote=True)
                async with self.natural.conversations._locked(scopes, parent["repository_id"]):
                    row = await self.collaboration.inbox.find_by_id(
                        identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if row is None or row.data != parent:
                        return dict(await self.collaboration.receipt(receipt_id))
                    if await self.collaboration.polling.resolve_checkpoint(row.data, remote=False) != actor:
                        raise AccessDenied("The exact checkpoint caller changed before queue admission")
                    await self.collaboration._write_row(row, natural_checkpoint=marker)
                    retained = copy.deepcopy(row.data)
                try:
                    admitted = await tickets.request(run_id, body)
                except Exception:
                    return await self._retain(
                        retained,
                        "needs_reconciliation",
                        feedback_status="held",
                        natural_checkpoint={**marker, "state": "needs_reconciliation"},
                    )
                except BaseException:
                    await self._retain(
                        retained,
                        "needs_reconciliation",
                        feedback_status="held",
                        natural_checkpoint={**marker, "state": "needs_reconciliation"},
                    )
                    raise
                return await self._retain(
                    retained,
                    "checkpoint_queued",
                    natural_checkpoint={**marker, "state": "queued", "request_digest": admitted["request_digest"]},
                )
            finally:
                CURRENT_PRINCIPAL.reset(token)
        except (AccessDenied, ValueError, KeyError):
            current = await self.collaboration.receipt(receipt_id)
            if current.get("natural_checkpoint") is not None:
                return dict(current)
            return await self._retain(parent, "checkpoint_pending_permission")
