# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Required scoped references and exact native outcomes precede bounded web search."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service

from firefly_forge.journal import Journal, digest
from firefly_forge.learning import LearningLedger
from firefly_forge.learning_configuration import frozen_learning
from firefly_forge.learning_context import observe_context
from firefly_forge.learning_models import ToolObservation
from firefly_forge.learning_projection import LearningProjection
from firefly_forge.search_tools import SearchTools
from firefly_forge.store import Store
from firefly_forge.tool_broker import ToolInvocationBroker

Data = dict[str, Any]


class KnowledgeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4000)
    max_results: int = Field(default=5, ge=1, le=8, strict=True)


@service
class KnowledgeFirst:
    def __init__(
        self,
        store: Store,
        journal: Journal,
        ledger: LearningLedger,
        broker: ToolInvocationBroker,
        search: SearchTools,
        projection: LearningProjection | None = None,
    ) -> None:
        self.store, self.journal, self.ledger, self.broker, self.web = store, journal, ledger, broker, search
        self.knowledge: Any = None
        self.projection = projection

    async def search(
        self,
        run_id: str,
        task_id: str,
        query: str,
        count: int,
        *,
        source: Data,
        workspace: Path,
        allow_external: bool = True,
        refresh: bool = False,
        refresh_request_id: str | None = None,
    ) -> Data:
        run = await self.store.get_run(run_id)
        policy = frozen_learning(run.get("learning_snapshot"))
        if not policy["settings"]["enabled"]:
            raise ValueError("This run has no captured learning and knowledge-lookup permission")
        if (
            refresh
            and (
                not refresh_request_id
                or len(refresh_request_id) != 32
                or any(c not in "0123456789abcdef" for c in refresh_request_id)
            )
            or not refresh
            and refresh_request_id is not None
        ):
            raise ValueError("Explicit refresh requires its own stable 32-character request identity")
        if run.get("framework_knowledge_request"):
            if self.knowledge is None or self.knowledge.frameworks is None:
                raise ValueError("Required version-isolated framework knowledge is unavailable")
            await self.knowledge.frameworks.assert_run_pin(run)
        context = await observe_context(run, source, workspace, query)
        async with self.store._unit():
            current = await self.journal._run(run_id)
            task = await self.journal._task(run_id, task_id)
            at = await self.ledger.records_repository.database_time()
            key = digest(
                {
                    "run_id": run_id,
                    "task": task_id,
                    "intent": task.data.get("idempotency_key"),
                    "context": context.model_dump(),
                    "source": source,
                    "count": count,
                    "policy": policy["digest"],
                    "search": run.get("search_snapshot"),
                    "knowledge": run.get("knowledge_snapshot"),
                    **(
                        {"framework_target": run["framework_knowledge_pin"]["digest"]}
                        if run.get("framework_knowledge_pin")
                        else {}
                    ),
                    "external": allow_external,
                    "refresh": refresh_request_id,
                }
            )
            if current.data.get("learning_snapshot") != run.get("learning_snapshot"):
                raise ValueError("Learning policy changed during invocation admission")
        correlation = uuid.uuid5(uuid.NAMESPACE_URL, "forge-knowledge-first:" + key).hex
        observation = ToolObservation(
            origin="knowledge_first" if allow_external else "knowledge_lookup",
            observed_at=at.isoformat(),
            web_provider_dispatched=False,
            classification="lookup_started",
        ).model_dump()

        async def retained(result: Data) -> Callable[[], None] | None:
            decision = result.get("knowledge_decision", {})
            if decision.get("reason") == "verified_exact_match":
                outcome, guard = await self.ledger.retain(run_id, query, context.model_dump(), decision["candidate_id"])
                if (
                    decision.get("eligible") is not True
                    or result.get("origin") != "verified_local_outcome"
                    or result.get("results")
                    != [
                        {
                            "citation_id": "learning-" + outcome["id"],
                            "statement": outcome["envelope"]["statement"],
                            "source_tree": outcome["envelope"]["context"]["source_tree"],
                            "run_id": outcome["envelope"]["run_id"],
                        }
                    ]
                ):
                    raise ValueError("Retained local evidence is no longer applicable; request an explicit refresh")
                projected = decision.get("projection", {})
                if projected.get("state") == "verified":
                    if self.projection is None or projected.get("record_id") != decision["candidate_id"]:
                        raise ValueError("Retained projected evidence changed; request an explicit refresh")
                    projection_guard = await self.projection.retain(run_id, context.model_dump(), projected)

                    def combined() -> None:
                        guard()
                        projection_guard()

                    return combined
                return guard
            return None

        async def fresh(result: Data) -> Callable[[], None] | None:
            if result.get("knowledge_decision", {}).get("reason") == "verified_exact_match":
                return await retained(result)
            return None

        async def request() -> Data:
            if self.knowledge is None:
                observation["classification"] = "required_service_failure"
                raise ValueError("The factory knowledge service is unavailable")
            try:
                references = await self.knowledge.search(run["repository_id"], query, run=run)
            except ValueError:
                observation["classification"] = "required_service_failure"
                raise ValueError(
                    "Required project knowledge retrieval failed; no web provider was dispatched"
                ) from None
            decision = await self.ledger.lookup(run_id, query, context.model_dump())
            if refresh:
                decision = {"eligible": False, "reason": "refresh_required", "refresh_required": True}
            stamp = {key: value for key, value in decision.items() if key != "outcome"}
            stamp.update(
                observed_at=at.isoformat(),
                web_provider_dispatched=False,
                reference_citations=copy.deepcopy(references.get("citations", []))[:16],
            )
            observation["reason"] = decision["reason"]
            if decision["eligible"]:
                outcome = decision["outcome"]
                if policy["settings"].get("projected_retrieval", False):
                    projected = (
                        await self.projection.retrieve(run_id, query, context.model_dump(), outcome["id"])
                        if self.projection is not None
                        else {"state": "unavailable", "reason": "projection_service_unavailable"}
                    )
                    stamp["projection"] = projected
                    if projected.get("state") == "verified":
                        stamp["reference_citations"] = (stamp["reference_citations"] + projected["citations"])[:24]
                observation["classification"] = "verified_outcome"
                return {
                    "untrusted": True,
                    "origin": "verified_local_outcome",
                    "knowledge_decision": stamp,
                    "results": [
                        {
                            "citation_id": "learning-" + outcome["id"],
                            "statement": outcome["envelope"]["statement"],
                            "source_tree": outcome["envelope"]["context"]["source_tree"],
                            "run_id": outcome["envelope"]["run_id"],
                        }
                    ],
                }
            if not allow_external:
                observation["classification"] = "candidate_reference"
                return {
                    "untrusted": True,
                    "origin": "candidate_reference",
                    "knowledge_decision": stamp,
                    "results": copy.deepcopy(references.get("hits", []))[: policy["settings"]["max_candidates"]],
                }

            def dispatched() -> None:
                observation.update(web_provider_dispatched=True, classification="provider_dispatch")

            result = await self.web.search(query, count, snapshot=run["search_snapshot"], on_dispatch=dispatched)
            stamp["web_provider_dispatched"] = observation["web_provider_dispatched"]
            return {**result, "knowledge_decision": stamp, "origin": "untrusted_web_reference"}

        tool = "internet_search" if allow_external else "knowledge_lookup"
        arguments = {"query": query, "max_results": count}
        if refresh:
            arguments.update(refresh=True, refresh_request_id=refresh_request_id)
        selected = (
            run.get("search_snapshot")
            if allow_external
            else {
                "knowledge_snapshot": run.get("knowledge_snapshot"),
                "learning_snapshot": run.get("learning_snapshot"),
            }
        )
        if not isinstance(selected, dict):
            raise ValueError("This tool requires an actual frozen server configuration")
        configuration: Data = selected
        return await self.broker.invoke(
            run_id,
            task_id,
            tool,
            correlation,
            arguments,
            request,
            source=source,
            configuration=configuration,
            observation=lambda: observation,
            validate_receipt=retained,
            validate_result=fresh,
        )
