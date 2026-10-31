# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Single-owner native board effects; every dispatch has a committed retained intent."""

from __future__ import annotations

import asyncio
import copy
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import select, tuple_

from firefly_forge.checkpoint_test_models import frozen_checkpoint_source
from firefly_forge.execution_request_persistence import ForgeExecutionRequestEntity
from firefly_forge.execution_requests import digest
from firefly_forge.github_projects import KIND
from firefly_forge.github_projects_effects import decide_update, managed_value
from firefly_forge.github_projects_graphql import GitHubProjectsGraphQL
from firefly_forge.github_projects_models import BoardDiscoveryInputV1, BoardObserveInputV1, BoardSyncInputV1
from firefly_forge.github_projects_persistence import (
    ForgeGitHubProjectsBindingEntity,
    ForgeGitHubProjectsConfigurationEntity,
)
from firefly_forge.github_projects_requests import GitHubProjectsRequests, checked_board_request, source_facts
from firefly_forge.identity import Principal
from firefly_forge.persistence import ForgeRunEntity
from firefly_forge.project_access import AccessDenied
from firefly_forge.store import identifier, now

Data = dict[str, Any]


class BoardConflict(ValueError):
    pass


class GitHubProjectsWorker:
    def __init__(self, tickets: GitHubProjectsRequests) -> None:
        self.tickets, self.manager, self.requests, self.store = (
            tickets,
            tickets.manager,
            tickets.requests,
            tickets.store,
        )
        self._head_cursor: UUID | None = None
        self._run_cursors: dict[str, int] = {}

    async def fence(self, data: Data) -> Data:
        await self.store.assert_ownership()
        request = data["request"]
        run = await self.store.get_run(data["run_id"])
        binding = await self.manager.binding(request["project_id"], request["repository_id"])
        if (
            binding is None
            or not binding["public"]["enabled"]
            or binding["public"]["digest"] != request["binding_digest"]
            or source_facts(run) != request["source"]
            or await self.store.repository(request["repository_id"]) != binding["repository_snapshot"]
        ):
            raise ValueError("Admitted board configuration or native run facts changed")
        await self.manager.access.authorize_actor(request["actor"], request["project_id"], "release")
        await self.manager.access.authorize_actor(binding["actor"], request["project_id"], "release")
        frozen_checkpoint_source(run, binding["repository_snapshot"])
        await self.store.assert_campaign_current(run)
        await self.manager.resources.authorize(
            binding["resource"], run_id=run["id"], repository_id=run["repository_id"]
        )
        return binding

    async def finish(self, request_id: str, status: str, result: Data) -> None:
        await self.store.assert_ownership()
        await self.store.admit_credentials(result)
        async with self.store._unit():
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.kind != KIND or row.status != "executing":
                return
            row.status = status
            row.data = {**row.data, "status": status, "result": result, "updated_at": now()}
            await self.requests.records.save(row)
            await self.requests._event(row, "worker_" + status, result)

    async def history(self, data: Data, content: Data) -> str | None:
        request = data["request"]
        confirmed = None
        cursor = None
        while True:
            query = select(ForgeExecutionRequestEntity).where(
                ForgeExecutionRequestEntity.kind == KIND,
                ForgeExecutionRequestEntity.data["request"]["board_id"].astext == request["board_id"],
                ForgeExecutionRequestEntity.data["request"]["remote_repository_id"].astext
                == request["remote_repository_id"],
                ForgeExecutionRequestEntity.data["request"]["contents"].contains(
                    [{"kind": content["kind"], "number": content["number"]}]
                ),
            )
            if cursor is not None:
                query = query.where(
                    tuple_(ForgeExecutionRequestEntity.created_at, ForgeExecutionRequestEntity.id) > cursor
                )
            query = query.order_by(ForgeExecutionRequestEntity.created_at, ForgeExecutionRequestEntity.id).limit(100)
            async with self.store._unit() as session:
                rows = list(await session.scalars(query))
            for row in rows:
                if row.id.hex == data["id"]:
                    continue
                retained, entries = await self.tickets.retained(row.id.hex)
                relevant = any(
                    reference["kind"] == content["kind"] and reference["number"] == content["number"]
                    for reference in retained["request"]["contents"]
                )
                if relevant and retained["status"] in {"executing", "unknown"}:
                    raise BoardConflict(
                        "A prior item outcome is unknown; read-only observation cannot authorize replay"
                    )
                for entry in entries:
                    if (
                        retained["request"]["project_id"] == request["project_id"]
                        and retained["request"]["repository_id"] == request["repository_id"]
                        and retained["request"]["field_id"] == request["field_id"]
                        and entry["kind"] == "board_field_confirmed"
                        and entry["evidence"]["content_id"] == content["id"]
                    ):
                        confirmed = entry["evidence"]["option_id"]
            if len(rows) < 100:
                return confirmed
            cursor = (rows[-1].created_at, rows[-1].id)

    async def current_issue_projection(self, data: Data, reference: Data) -> None:
        if reference["kind"] != "issue":
            return
        async with self.store._unit() as session:
            current = await session.get(ForgeRunEntity, identifier(data["run_id"]))
        if current is None:
            raise ValueError("Original run is unavailable")
        sequence = current.sequence
        while True:
            async with self.store._unit() as session:
                newer = list(
                    await session.scalars(
                        select(ForgeRunEntity)
                        .where(
                            ForgeRunEntity.repository_id == current.repository_id,
                            ForgeRunEntity.sequence > sequence,
                            ForgeRunEntity.data["project_id"].astext == data["request"]["project_id"],
                        )
                        .order_by(ForgeRunEntity.sequence)
                        .limit(100)
                    )
                )
            for row in newer:
                candidate = await self.store.get_run(row.id.hex)
                try:
                    targets = await self.tickets.issue_references(candidate)
                except (AccessDenied, ValueError, KeyError, TypeError):
                    raise BoardConflict(
                        "Newer native issue linkage is unavailable; preserve the predecessor projection"
                    ) from None
                if any(target["kind"] == "issue" and target["number"] == reference["number"] for target in targets):
                    raise BoardConflict(
                        "A newer native unit owns this issue projection; the predecessor remains unchanged"
                    )
            if len(newer) < 100:
                return
            sequence = newer[-1].sequence

    @staticmethod
    def item(items: list[Data], content: Data) -> Data | None:
        if any(item.get("content") is None for item in items):
            raise ValueError("Board contains redacted items; absence is not complete evidence")
        found = [item for item in items if item["content"].get("id") == content["id"]]
        if len(found) > 1 or any(
            item["content"].get("repository", {}).get("id") != content["repository_id"] for item in found
        ):
            raise ValueError("Board item identity is ambiguous or leaves the original repository")
        return found[0] if found else None

    async def execute(self, request_id: str) -> None:
        await self._execute(request_id)

    async def _execute(self, request_id: str) -> None:
        if not await self.requests._claim(request_id):
            return
        data, _ = await self.tickets.retained(request_id)
        dispatched = False
        results: list[Data] = []
        try:
            entries = await self.requests.events(request_id)
            if not self.requests.verify_chain(entries) or digest(data["request"]) != data["request_digest"]:
                raise ValueError("Board admission integrity failed")
            binding = await self.fence(data)
            request, public, resource = data["request"], binding["public"], binding["resource"]
            remote = GitHubProjectsGraphQL(
                self.manager.resources,
                transport=self.manager.remote.inner,
                run_id=data["run_id"],
                repository_id=request["repository_id"],
            )
            repository = await self.store.repository(request["repository_id"])
            discovery = await remote.discover(
                resource,
                None,
                BoardDiscoveryInputV1.model_validate(
                    {"version": 1, **{key: public[key] for key in ("connection_name", "owner_kind", "owner", "number")}}
                ),
                repository,
            )
            await self.store.admit_credentials(discovery.model_dump(mode="json"))
            options = {field.id: {option.id for option in field.options} for field in discovery.fields}
            if (
                discovery.board.id != public["board_id"]
                or discovery.repository.id != binding["discovery"]["repository"]["id"]
                or discovery.viewer.id != binding["discovery"]["viewer"]["id"]
                or public["field_id"] not in options
                or not set(public["state_options"].values()) <= options[public["field_id"]]
            ):
                raise ValueError("The configured board/field/options changed")
            desired = public["state_options"][request["projection"]["state"]]
            for index, reference in enumerate(request["contents"]):
                await self.fence(data)
                await self.current_issue_projection(data, reference)
                content = await remote.content(resource, None, repository, reference["kind"], reference["number"])
                if content["repository_id"] != discovery.repository.id:
                    raise ValueError("Content repository node differs from the exact board admission")
                last = await self.history(data, content)
                item = await remote.find_item(resource, None, public["board_id"], content)
                if item is None:
                    await self.fence(data)
                    await self.current_issue_projection(data, reference)
                    intent_id = uuid5(NAMESPACE_URL, f"forge-board:{request_id}:{index}:add").hex
                    await self.requests._observation(
                        request_id,
                        "board_add_intent",
                        {
                            "intent_id": intent_id,
                            "board_id": public["board_id"],
                            "content_id": content["id"],
                            "request_digest": data["request_digest"],
                        },
                    )
                    dispatched = True
                    item_id = await remote.add(resource, None, public["board_id"], content["id"], intent_id)
                    await self.fence(data)
                    await self.requests._observation(
                        request_id, "board_add_result", {"intent_id": intent_id, "item_id": item_id}
                    )
                    item = await remote.find_item(resource, None, public["board_id"], content)
                    if item is None or item["id"] != item_id:
                        raise ValueError("The add-item result has no exact readback")
                decision = decide_update(managed_value(item, public["field_id"]), last, desired)
                if decision == "conflict":
                    raise BoardConflict(
                        "The managed field differs from its last confirmed value; preserve the human change"
                    )
                if decision == "update":
                    await self.fence(data)
                    await self.current_issue_projection(data, reference)
                    intent_id = uuid5(NAMESPACE_URL, f"forge-board:{request_id}:{index}:field").hex
                    await self.requests._observation(
                        request_id,
                        "board_field_intent",
                        {
                            "intent_id": intent_id,
                            "item_id": item["id"],
                            "field_id": public["field_id"],
                            "option_id": desired,
                            "observed_before": managed_value(item, public["field_id"]),
                            "remote_compare_and_swap": False,
                        },
                    )
                    dispatched = True
                    await remote.update(
                        resource, None, public["board_id"], item["id"], public["field_id"], desired, intent_id
                    )
                    await self.fence(data)
                    await self.requests._observation(
                        request_id, "board_field_result", {"intent_id": intent_id, "item_id": item["id"]}
                    )
                    observed = await remote.find_item(resource, None, public["board_id"], content)
                    if observed is None or managed_value(observed, public["field_id"]) != desired:
                        raise ValueError("The managed field has no exact post-write readback")
                await self.fence(data)
                confirmed = {
                    "content_id": content["id"],
                    "kind": reference["kind"],
                    "number": reference["number"],
                    "item_id": item["id"],
                    "field_id": public["field_id"],
                    "option_id": desired,
                }
                await self.requests._observation(request_id, "board_field_confirmed", confirmed)
                results.append(confirmed)
            await self.finish(request_id, "completed", {"items": results, "projection": request["projection"]})
        except asyncio.CancelledError:
            await asyncio.shield(
                self.finish(request_id, "unknown", {"reason": "worker_stopped_no_replay", "items": results})
            )
            raise
        except Exception as error:
            status = "unknown" if dispatched else "conflict" if isinstance(error, BoardConflict) else "failed"
            result = {
                "reason": "remote_outcome_unknown_no_replay"
                if dispatched
                else "human_or_prior_outcome_conflict"
                if isinstance(error, BoardConflict)
                else "pre_effect_admission_failed",
                "items": results,
            }
            if not dispatched and type(error) in {ValueError, AccessDenied, BoardConflict}:
                detail = str(error).replace("\r", " ").replace("\n", " ")[:300]
                try:
                    await self.store.admit_credentials(detail)
                except ValueError:
                    pass
                else:
                    result["detail"] = detail
            await self.finish(request_id, status, result)

    async def recover(self) -> None:
        self.requests.runtime.require_execution()
        await self.store.assert_ownership()
        async with self.store._unit():
            rows = await self.requests.records.state("executing", kind=KIND)
        for row in rows:
            await self.finish(row.id.hex, "unknown", {"reason": "worker_restarted_no_replay", "items": []})

    async def queue_automatic(self) -> None:
        try:
            async with asyncio.timeout(2):
                await self._queue_automatic()
        except TimeoutError:
            return

    async def _queue_automatic(self) -> None:
        self.requests.runtime.require_execution()
        await self.store.assert_ownership()
        query = select(ForgeGitHubProjectsBindingEntity).order_by(ForgeGitHubProjectsBindingEntity.id).limit(1)
        if self._head_cursor is not None:
            query = query.where(ForgeGitHubProjectsBindingEntity.id > self._head_cursor)
        async with self.store._unit() as session:
            heads = list(await session.scalars(query))
        if not heads:
            self._head_cursor = None
            return
        head = heads[0]
        self._head_cursor = head.id
        binding = await self.manager.binding(head.project_id.hex, head.repository_id.hex)
        if binding is None or not binding["public"]["enabled"]:
            return
        sequence = max(
            self._run_cursors.get(head.id.hex, 0),
            0 if binding["public"]["include_existing"] else binding["cut_sequence"],
        )
        async with self.store._unit() as session:
            rows = list(
                await session.scalars(
                    select(ForgeRunEntity)
                    .where(
                        ForgeRunEntity.repository_id == head.repository_id,
                        ForgeRunEntity.data["project_id"].astext == head.project_id.hex,
                        ForgeRunEntity.sequence > sequence,
                    )
                    .order_by(ForgeRunEntity.sequence)
                    .limit(10)
                )
            )
        if not rows:
            self._run_cursors.pop(head.id.hex, None)
            return
        for row in rows:
            self._run_cursors[head.id.hex] = row.sequence
            run = await self.store.get_run(row.id.hex)
            key = uuid5(
                NAMESPACE_URL, "forge-board-auto:" + binding["public"]["digest"] + ":" + digest(source_facts(run))
            ).hex
            async with self.store._unit():
                if await self.requests.records.find_by_id(identifier(key)) is not None:
                    continue
            try:
                await self.tickets.admit(
                    head.project_id.hex,
                    head.repository_id.hex,
                    row.id.hex,
                    BoardSyncInputV1(version=1, request_id=key, expected_binding_digest=binding["public"]["digest"]),
                )
                return
            except (ValueError, KeyError, AccessDenied):
                continue

    async def observe(
        self, project_id: str, repository_id: str, request_id: str, body: BoardObserveInputV1, actor: Principal
    ) -> None:
        await self.manager.authority(project_id, repository_id, actor, configure=True)
        await self.tickets.get(project_id, repository_id, request_id, actor)
        data = await self.requests.get(request_id)
        if data["request_digest"] != body.expected_request_digest or data["status"] != "unknown":
            raise ValueError("Review the exact retained unknown request before observing it")
        async with self.store._unit() as session:
            receipt = await session.scalar(
                select(ForgeGitHubProjectsConfigurationEntity).where(
                    ForgeGitHubProjectsConfigurationEntity.project_id == identifier(project_id),
                    ForgeGitHubProjectsConfigurationEntity.repository_id == identifier(repository_id),
                    ForgeGitHubProjectsConfigurationEntity.data["public"]["digest"].astext
                    == data["request"]["binding_digest"],
                )
            )
            if receipt is None:
                raise ValueError("The original board configuration receipt is unavailable")
            original = copy.deepcopy(receipt.data)
        repository = await self.store.repository(repository_id)
        if repository != original["repository_snapshot"]:
            raise ValueError("The registered repository changed since this unknown effect")
        resource = await self.manager.resources.bind(
            original["public"]["connection_name"],
            actor,
            project_id=project_id,
            resource="https://api.github.com/graphql",
        )
        observations = []
        for reference in data["request"]["contents"]:
            content = await self.manager.remote.content(
                resource, actor, repository, reference["kind"], reference["number"]
            )
            item = await self.manager.remote.find_item(resource, actor, original["public"]["board_id"], content)
            observations.append(
                {
                    "kind": reference["kind"],
                    "number": reference["number"],
                    "content_id": content["id"],
                    "item_id": item["id"] if item else None,
                    "observed_value": managed_value(item, original["public"]["field_id"]) if item else None,
                    "presence": "observed_present" if item else "not_observed",
                    "effect_attribution": "unknown",
                }
            )
        await self.manager.authority(project_id, repository_id, actor, configure=True)
        evidence = {
            "request_digest": data["request_digest"],
            "observations": observations,
            "replay_authorized": False,
            "actor": self.manager.access.actor_snapshot(actor),
        }
        await self.store.admit_credentials(evidence)
        async with self.manager.locked(project_id, repository_id):
            await self.manager.authority(project_id, repository_id, actor, configure=True)
            row = await self.requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.status != "unknown" or row.data["request_digest"] != body.expected_request_digest:
                raise ValueError("Unknown request changed during read-only observation")
            checked_board_request(row.data, await self.requests.events(request_id))
            await self.requests._event(row, "read_only_observation", evidence)
