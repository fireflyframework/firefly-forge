# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable native admissions; only the execution owner dispatches board effects."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select

from firefly_forge.checkpoint_test_models import frozen_checkpoint_source
from firefly_forge.collaboration import Collaboration
from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity
from firefly_forge.execution_request_persistence import ForgeExecutionRequestEntity
from firefly_forge.execution_requests import ExecutionRequests, digest
from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.github_projects import KIND, GitHubProjects
from firefly_forge.github_projects_models import BoardRequestReceiptV1, BoardSyncInputV1
from firefly_forge.github_projects_projection import project_run
from firefly_forge.github_projects_references import checkpoint_pull_requests
from firefly_forge.identity import Principal
from firefly_forge.journal import Journal
from firefly_forge.journal import digest as journal_digest
from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.store import identifier, now

Data = dict[str, Any]


def checked_board_request(data: Data, entries: list[Data]) -> None:
    try:
        admitted = {"request_digest": data["request_digest"]}
        if (
            data["kind"] != KIND
            or not entries
            or digest(data["request"]) != data["request_digest"]
            or data["request"]["input"]["request_id"] != data["id"]
            or data["request"]["run_id"] != data["run_id"]
            or not ExecutionRequests.verify_chain(entries)
            or any(entry["request_id"] != data["id"] for entry in entries)
            or entries[0]["kind"] != "request_admitted"
            or entries[0]["evidence"] != admitted
        ):
            raise ValueError
        claims = [index for index, entry in enumerate(entries) if entry["kind"] == "worker_claim"]
        terminal = [
            index
            for index, entry in enumerate(entries)
            if entry["kind"] in {"worker_completed", "worker_unknown", "worker_conflict", "worker_failed"}
        ]
        if data["status"] == "queued":
            if len(entries) != 1 or data["result"] is not None:
                raise ValueError
        elif claims != [1] or entries[1]["evidence"] != admitted:
            raise ValueError
        elif data["status"] == "executing":
            if terminal or data["result"] is not None:
                raise ValueError
        elif data["status"] in {"completed", "unknown", "conflict", "failed"}:
            if len(terminal) != 1 or terminal[0] <= 1:
                raise ValueError
            closed = entries[terminal[0]]
            if closed["kind"] != "worker_" + data["status"] or closed["evidence"] != data["result"]:
                raise ValueError
            if any(
                entry["kind"] != "read_only_observation" or data["status"] != "unknown"
                for entry in entries[terminal[0] + 1 :]
            ):
                raise ValueError
        else:
            raise ValueError
    except (KeyError, TypeError, IndexError, ValueError):
        raise ValueError("Board admission, custody state or immutable outcome history changed") from None


def source_facts(run: Data) -> Data:
    return {
        key: copy.deepcopy(run.get(key))
        for key in (
            "id",
            "repository_id",
            "project_id",
            "component_id",
            "operation",
            "status",
            "issue_context",
            "source_checkpoint",
            "source_checkpoint_snapshot",
            "campaign_context",
            "branch",
            "base_revision",
            "source_tree",
            "checkpoint_revision",
            "pr_url",
            "publication_status",
            "git_failed_unit_adoption",
        )
    }


class GitHubProjectsRequests:
    def __init__(
        self,
        manager: GitHubProjects,
        requests: ExecutionRequests,
        deliveries: GitDeliveries,
        collaboration: Collaboration | None = None,
    ) -> None:
        self.manager, self.requests, self.store = manager, requests, manager.store
        self.deliveries = deliveries
        self.collaboration = collaboration

    async def verify_issue(self, delivery: Data, run: Data) -> None:
        if delivery.get("native_question") is not None:
            from firefly_forge.native_question_publications import NativeQuestionPublications

            if self.collaboration is None:
                raise ValueError("The native question reference verifier is unavailable")
            if delivery.get("digest") != journal_digest(delivery["native_question"]):
                raise AccessDenied("The native question receipt digest differs from its retained reference")
            publications = NativeQuestionPublications(self.collaboration)
            await publications.authorize_reference(delivery, run)
            canonical = await publications._context(
                run["id"], pending=False, archived_binding=delivery["native_question"]["binding_digest"]
            )
            if any(
                delivery.get(key) != canonical["source"].get(key)
                for key in (
                    "repository_id",
                    "provider",
                    "remote_id",
                    "actor",
                    "issue",
                    "verification",
                    "source_snapshot",
                )
            ):
                raise AccessDenied(
                    "The native question's copied issue, actor or repository differs from its canonical source"
                )
        elif (delivery.get("verification") or {}).get("transport") == "github_rest_poll_v1":
            if self.store.scm_polling is None:
                raise ValueError("The native authenticated polling receipt verifier is unavailable")
            await self.store.scm_polling.ticket_reference(delivery, run)
        else:
            await SCMActorAuthority(self.store).ticket_reference(delivery, run)

    async def retained(self, request_id: str) -> tuple[Data, list[Data]]:
        async with self.store._unit() as session:
            row = await session.scalar(
                select(ForgeExecutionRequestEntity)
                .where(ForgeExecutionRequestEntity.id == identifier(request_id))
                .with_for_update(read=True)
            )
            if row is None or row.kind != KIND:
                raise KeyError("Native board request not found")
            data = copy.deepcopy(row.data)
            if (
                row.id.hex != data.get("id")
                or row.kind != data.get("kind")
                or row.run_id.hex != data.get("run_id")
                or row.status != data.get("status")
            ):
                raise ValueError("Board request scalar identity or custody state changed")
            entries = await self.requests.events(request_id)
            checked_board_request(data, entries)
            return data, entries

    async def issue_references(self, run: Data) -> list[Data]:
        result: list[Data] = []
        origin = run
        seen = {run["id"]}
        while True:
            if origin.get("issue_context") or not origin.get("source_checkpoint_snapshot"):
                break
            frozen_checkpoint_source(origin, await self.store.repository(origin["repository_id"]))
            origin = await self.store.get_run(origin["source_checkpoint_snapshot"]["reference"]["run_id"])
            if any(origin.get(key) != run.get(key) for key in ("project_id", "repository_id", "component_id")):
                raise ValueError("Checkpoint issue lineage leaves the original project/repository/component")
            if origin["id"] in seen:
                raise ValueError("Checkpoint issue lineage contains a cycle")
            seen.add(origin["id"])
        context = origin.get("issue_context") or {}
        if context.get("provider") == "github":
            async with self.store._unit() as session:
                row = await session.get(ForgeWebhookInboxEntity, identifier(context["receipt_id"]))
                if row is None:
                    raise ValueError("The native signed issue receipt is unavailable")
                delivery = copy.deepcopy(row.data)
            await self.verify_issue(delivery, origin)
            if (
                context.get("receipt_id") != delivery.get("id")
                or context.get("provider") != delivery.get("provider")
                or context.get("actor") != delivery.get("actor")
                or context.get("kind") != delivery.get("issue", {}).get("kind")
                or context.get("number") != delivery.get("issue", {}).get("number")
            ):
                raise AccessDenied("The run issue context differs from its verified native receipt")
            result.append(
                {
                    "kind": delivery["issue"]["kind"],
                    "number": delivery["issue"]["number"],
                    "source_run_id": origin["id"],
                    "receipt_digest": delivery["digest"],
                }
            )
        elif not context:
            linked: dict[tuple[str, int], Data] = {}
            cursor = 0
            while True:
                async with self.store._unit() as session:
                    rows = list(
                        await session.scalars(
                            select(ForgeWebhookInboxEntity)
                            .where(
                                ForgeWebhookInboxEntity.repository_id == identifier(origin["repository_id"]),
                                ForgeWebhookInboxEntity.provider == "github",
                                ForgeWebhookInboxEntity.data["run_id"].astext == origin["id"],
                                ForgeWebhookInboxEntity.sequence > cursor,
                            )
                            .order_by(ForgeWebhookInboxEntity.sequence)
                            .limit(100)
                        )
                    )
                for row in rows:
                    try:
                        delivery = copy.deepcopy(row.data)
                        await self.verify_issue(delivery, origin)
                        kind, number = delivery["issue"]["kind"], delivery["issue"]["number"]
                        if kind not in {"issue", "pull_request"} or type(number) is not int or number < 1:
                            raise ValueError
                        linked.setdefault(
                            (kind, number),
                            {
                                "kind": kind,
                                "number": number,
                                "source_run_id": origin["id"],
                                "receipt_digest": delivery["digest"],
                            },
                        )
                    except (AccessDenied, ValueError, KeyError, TypeError):
                        continue
                    if len(linked) > 1:
                        raise ValueError(
                            "The run has several native issue targets; an original issue scope is required"
                        )
                if len(rows) < 100:
                    break
                cursor = rows[-1].sequence
            result.extend(linked.values())
        return result

    async def content_references(self, run: Data) -> list[Data]:
        result = await self.issue_references(run)
        checkpoints = await self.deliveries.view(run["id"], actor=run["execution_actor"])
        urls = checkpoint_pull_requests(
            await self.deliveries._entries(run["id"]), checkpoints["items"], run["repository_id"], run.get("branch")
        )
        if run.get("pr_url"):
            receipt = self.requests._publication_receipt(
                await Journal(self.store).entries(run["id"]), {"run_id": run["id"]}, run
            )
            if receipt is None:
                raise ValueError("The native publication receipt does not prove this pull request")
            urls.append((run["pr_url"], receipt["receipt_digest"]))
        from firefly_forge.github_projects_graphql import repository_coordinates

        owner, name = repository_coordinates(await self.store.repository(run["repository_id"]))
        prefix = f"https://github.com/{owner}/{name}/pull/"
        for url, receipt_digest in dict(urls[-1:]).items():
            if not url.startswith(prefix) or not url[len(prefix) :].isdigit():
                raise ValueError("Native pull request leaves the original GitHub repository")
            number = int(url[len(prefix) :])
            if not any(item["kind"] == "pull_request" and item["number"] == number for item in result):
                result.append(
                    {
                        "kind": "pull_request",
                        "number": number,
                        "source_run_id": run["id"],
                        "receipt_digest": receipt_digest,
                    }
                )
        return result

    async def admit(
        self, project_id: str, repository_id: str, run_id: str, body: BoardSyncInputV1, actor: Principal | None = None
    ) -> Data:
        if actor is not None:
            await self.manager.authority(project_id, repository_id, actor, configure=True)
        run = await self.store.get_run(run_id)
        frozen_checkpoint_source(run, await self.store.repository(repository_id))
        binding = await self.manager.binding(project_id, repository_id)
        if (
            binding is None
            or not binding["public"]["enabled"]
            or binding["public"]["digest"] != body.expected_binding_digest
            or run.get("project_id") != project_id
            or run["repository_id"] != repository_id
            or await self.store.repository(repository_id) != binding["repository_snapshot"]
        ):
            raise ValueError("Review an enabled current binding and exact original project/repository run")
        await self.manager.access.authorize_actor(binding["actor"], project_id, "release")
        await self.manager.resources.authorize(binding["resource"], run_id=run_id, repository_id=repository_id)
        references = await self.content_references(run)
        if not references:
            raise ValueError("This run has no retained signed GitHub issue or publication receipt")
        incoming = {
            "input": body.model_dump(mode="json"),
            "actor": self.manager.access.actor_snapshot(actor) if actor else binding["actor"],
            "project_id": project_id,
            "repository_id": repository_id,
            "run_id": run_id,
            "board_id": binding["public"]["board_id"],
            "remote_repository_id": binding["repository_snapshot"]["remote_repository_id"],
            "field_id": binding["public"]["field_id"],
            "binding_digest": binding["public"]["digest"],
            "source": source_facts(run),
            "projection": project_run(run).model_dump(mode="json"),
            "contents": references,
        }
        await self.store.admit_credentials(incoming)
        async with self.manager.locked(project_id, repository_id):
            current = await self.store.get_run(run_id)
            if source_facts(current) != incoming["source"]:
                raise ValueError("Run lifecycle/source changed during board admission")
            current_binding = await self.manager.binding(project_id, repository_id)
            if current_binding != binding:
                raise ValueError("Board configuration changed during admission")
            if actor is not None:
                await self.manager.authority(project_id, repository_id, actor, configure=True)
            await self.manager.access.authorize_actor(binding["actor"], project_id, "release")
            old = await self.requests.records.find_by_id(identifier(body.request_id))
            if old is not None:
                if old.kind != KIND or old.run_id.hex != run_id or old.data["request"] != incoming:
                    raise ValueError("Board request identity belongs to another body, scope, source or actor")
                return copy.deepcopy(old.data)
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
                request_key=body.request_id,
                status="queued",
                data=data,
            )
            await self.requests.records.save(row)
            await self.requests._event(row, "request_admitted", {"request_digest": data["request_digest"]})
            return copy.deepcopy(data)

    async def get(self, project_id: str, repository_id: str, request_id: str, actor: Principal) -> Data:
        await self.manager.authority(project_id, repository_id, actor)
        data, entries = await self.retained(request_id)
        if (
            data["kind"] != KIND
            or data["request"]["project_id"] != project_id
            or data["request"]["repository_id"] != repository_id
        ):
            raise KeyError("Board request not found in this original project/repository")
        request, source = data["request"], data["request"]["source"]
        result = BoardRequestReceiptV1.model_validate(
            {
                "id": request_id,
                "project_id": project_id,
                "repository_id": repository_id,
                "run_id": data["run_id"],
                "status": data["status"],
                "request_digest": data["request_digest"],
                "binding_digest": request["binding_digest"],
                "projection": request["projection"],
                "contents": request["contents"],
                "branch": source["branch"],
                "checkpoint_revision": source["checkpoint_revision"],
                "source_tree": source["source_tree"],
                "campaign_id": (source.get("campaign_context") or {}).get("campaign_id"),
                "events": entries,
                "result": data["result"],
                "created_at": data["created_at"],
            }
        ).model_dump(mode="json")
        await self.store.admit_credentials(result)
        await self.manager.authority(project_id, repository_id, actor)
        return result
