# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Charge public Agentic model and function-tool attempts to their original family."""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart

from firefly_forge.journal import digest

if TYPE_CHECKING:
    from firefly_forge.factory_delegations import DelegationSession, FactoryDelegations


class FamilyAgentCapability(AbstractCapability[Any]):
    def __init__(self, manager: FactoryDelegations, session: DelegationSession, child_id: str) -> None:
        self.manager, self.session, self.child_id = manager, session, child_id
        self.owner = manager.store.execution_ownership_token
        self.pending: dict[str, tuple[str, str]] = {}
        self.seen_calls: set[str] = set()
        self.closed = False

    async def _authority(self) -> str:
        if self.closed or self.owner is None or self.manager.store.execution_ownership_token is not self.owner:
            raise ValueError("Family Agentic calls require their original active factory owner")
        async with self.manager.store._unit():
            family_id, _, children = await self.manager.children_locked(self.session)
            child = children.get(self.child_id)
            if (
                child is None
                or child["state"] != "running"
                or self.manager._claims.get((self.child_id, child["claim_digest"])) is not self.owner
            ):
                raise ValueError("Family Agentic calls require their original native child dispatch")
            await self.manager._child_admission(
                self.session, await self.manager.store.get_run(self.child_id), child, require_queued=False
            )
        return family_id

    async def wrap_model_request(self, ctx: Any, *, request_context: Any, handler: Any) -> Any:
        family_id = await self._authority()
        key = "child-model:" + self.child_id + ":" + uuid.uuid4().hex
        await self.manager.ledger.reserve(family_id, self.child_id, key, "requests")
        await self.manager.ledger.observe(family_id, key, "budget_dispatched")
        try:
            response = await handler(request_context)
            await self._authority()
            for index, call in enumerate(response.parts):
                if not isinstance(call, ToolCallPart):
                    continue
                tool = call.tool_name if re.fullmatch(r"[a-z][a-z0-9_]*", call.tool_name) else "invalid_tool"
                tool_key = f"{key}:tool:{index}"
                await self.manager.ledger.reserve(family_id, self.child_id, tool_key, "tool_calls", tool=tool)
                await self.manager.ledger.observe(family_id, tool_key, "budget_dispatched")
                if not call.tool_call_id or call.tool_call_id in self.seen_calls:
                    await self.manager.ledger.observe(family_id, tool_key, "budget_unknown")
                    raise ValueError("A child function-tool attempt requires its unique retained call identity")
                self.seen_calls.add(call.tool_call_id)
                self.pending[call.tool_call_id] = tool_key, digest({"name": call.tool_name, "args": call.args})
            await self.manager.ledger.observe(family_id, key, "budget_settled")
            return response
        except BaseException:
            await self.manager.ledger.observe(family_id, key, "budget_unknown")
            raise

    async def wrap_tool_execute(self, ctx: Any, *, call: ToolCallPart, tool_def: Any, args: Any, handler: Any) -> Any:
        family_id = await self._authority()
        retained = self.pending.pop(call.tool_call_id, None)
        if retained is None:
            raise ValueError("A child tool callback cannot replay an absent or spent family attempt")
        key, call_digest = retained
        try:
            if call.tool_name != tool_def.name or digest({"name": call.tool_name, "args": call.args}) != call_digest:
                raise ValueError("Child tool arguments differ from their independently returned model call")
            result = await handler(args)
            await self._authority()
            await self.manager.ledger.observe(family_id, key, "budget_settled")
            return result
        except BaseException:
            await self.manager.ledger.observe(family_id, key, "budget_unknown")
            raise

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for key, _ in self.pending.values():
            parent = await self.manager.store.get_run(self.session.run_id)
            family_id = parent.get("factory_context", {}).get("family_id", parent["id"])
            await self.manager.ledger.observe(family_id, key, "budget_unknown")
        self.pending.clear()
