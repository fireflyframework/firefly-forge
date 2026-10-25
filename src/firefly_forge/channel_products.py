# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Signed-channel product actions with fresh authorization across current and frozen graphs."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.models import Command
from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.reverse_engineering import AnalysisInput, AnalysisReview
from firefly_forge.store import identifier

Data = dict[str, Any]


def configuration_action(command: Any) -> bool:
    return (
        command.action in {"product", "brand", "respond"}
        or (command.action == "design" and command.operation == "preview")
        or (
            command.action in {"project", "analysis", "assistant", "roles"}
            and command.operation in {"configure", "repositories", "start", "turn", "review"}
        )
    )


class ChannelProducts:
    def __init__(self, store: Any, collaboration: Any) -> None:
        self.store, self.collaboration = store, collaboration
        self.projects = Projects(store)

    async def _members(
        self,
        repo: Data,
        command: Any,
        receipt: Data,
        definitions: list[Data],
        *,
        configure: bool,
        grants: list[Data] | None = None,
    ) -> None:
        provider, actor = receipt["provider"], receipt["actor"]
        member_ids = {repo["id"]}
        for definition in definitions:
            ids = {item["repository_id"] for item in definition["components"]}
            if repo["id"] not in ids:
                raise ValueError("member_origin_not_in_project")
            member_ids.update(ids)
        if grants is None:
            grants = await self.collaboration.scm.repositories(provider)
        for member_id in sorted(member_ids):
            member = await self.store.repository(member_id)
            settings = await self.collaboration.settings(member_id)
            if member.get("connection_provider") != provider:
                raise ValueError("member_provider_identity_mismatch")
            if not any(
                grant["id"] == member.get("remote_repository_id")
                and grant["url"] == member["url"]
                and grant["name"] == member["name"]
                for grant in grants
            ):
                raise ValueError("member_repository_grant_unavailable")
            if (
                not settings["enabled"]
                or actor not in settings["trusted_actors"]
                or command.action not in settings["channel_commands"]
            ):
                raise ValueError("member_actor_not_authorized")
            if configure and actor not in settings["configuration_actors"]:
                raise ValueError("member_configuration_actor_required")
        if receipt.get("global_ingress"):
            with self.store.configuration.lock:
                global_settings = self.store.configuration._read(
                    self.store.configuration.directory / "channel-bootstrap.json"
                ).get(provider, {})
            if not global_settings.get("enabled") or actor not in global_settings.get("trusted_actors", []):
                raise ValueError("member_global_actor_not_authorized")
            if configure and actor not in global_settings.get("configuration_actors", []):
                raise ValueError("member_global_configuration_actor_required")
        engine = self.collaboration.deployments.engine if self.collaboration.deployments else None
        if engine is not None and engine.poller is not None:
            await self.store.assert_ownership()

    async def authorize(self, repo: Data, command: Any, receipt_id: str | None) -> Data:
        if receipt_id is None:
            raise ValueError("Product channel actions require an accepted signed receipt")
        receipt = await self.collaboration.receipt(receipt_id)
        if (
            receipt["repository_id"] != repo["id"]
            or receipt["status"] not in {"received", "needs_reconciliation"}
            or receipt["command"] != command.model_dump(exclude_none=True)
        ):
            raise ValueError("Product action must match its accepted signed receipt")
        current_origin = await self.store.repository(repo["id"])
        if (
            current_origin.get("connection_provider") != receipt["provider"]
            or current_origin.get("remote_repository_id") != receipt.get("remote_id")
            or current_origin["url"] != repo["url"]
            or current_origin["name"] != repo["name"]
        ):
            raise ValueError("Signed origin no longer matches its exact connected repository")
        await self.store.admit_credentials(command.model_dump(exclude_none=True))
        if command.action == "roles" and command.operation == "configure":
            self.store.roles.snapshot(ProjectRoles.model_validate(command.roles), self.store.policies.selection())
        project = await self.projects.project(command.project_id) if command.project_id else None
        graphs = [project["definition"]] if project else []
        if command.action == "project" and command.operation == "configure":
            candidate_model = ProjectDefinition.model_validate(command.project_definition)
            self.store.roles.snapshot(candidate_model.roles, self.store.policies.selection())
            candidate = candidate_model.model_dump()
            if any(component.get("verification") for component in candidate["components"]):
                raise ValueError("member_checks_must_come_from_operator_settings")
            graphs.append(candidate)
        configure = configuration_action(command)
        await self._members(repo, command, receipt, graphs, configure=configure)
        target_id = project["definition"]["product_repository_id"] if project else repo["id"]
        context: Data = {"receipt": receipt, "project": project, "graphs": graphs, "target_id": target_id}
        if command.action == "analysis":
            assert project is not None
            manager = self.collaboration.analysis_manager
            if manager is None:
                raise ValueError("Source inspection service is not configured")
            if set(command.component_ids or []) - {item["id"] for item in project["definition"]["components"]}:
                raise ValueError("Source inspection components must belong to this project")
            if command.analysis_id:
                async with self.store._unit():
                    row = await manager.records.find_by_id(identifier(command.analysis_id))
                    if row is None or row.project_id.hex != command.project_id:
                        raise ValueError("Analysis belongs to another product project")
                    report = copy.deepcopy(row.data)
                graphs.append(report["project_snapshot"]["definition"])
                if command.operation == "review" and (
                    report["status"] not in {"ready_for_review", "reviewed"} or report.get("digest") != command.digest
                ):
                    raise ValueError("Source review requires its exact ready evidence digest")
                context["analysis"] = report
                await self._members(repo, command, receipt, graphs, configure=configure)
        if command.action == "assistant":
            manager = self.collaboration.product_assistant
            if manager is None:
                raise ValueError("Product conversation service is not configured")
            if command.conversation_id:
                async with self.store._unit():
                    row = await manager.records.find_by_id(identifier(command.conversation_id))
                    if row is None or row.repository_id.hex != target_id:
                        raise ValueError("Conversation belongs to another product source")
                    conversation = copy.deepcopy(row.data)
                frozen = conversation.get("project_snapshot")
                if frozen:
                    if project is None or frozen["id"] != project["id"]:
                        raise ValueError("Conversation requires its exact logical project")
                    graphs.append(frozen["definition"])
                    await self._members(repo, command, receipt, graphs, configure=configure)
                elif project:
                    raise ValueError("Conversation is not bound to this logical project")
                context["conversation"] = conversation
        return context

    async def execute(self, repo: Data, command: Any, settings: Data, receipt_id: str | None) -> Data:
        context = await self.authorize(repo, command, receipt_id)
        if command.action == "project":
            if command.operation == "repositories":
                return await self._repositories(repo, command, context["receipt"])
            if command.operation == "status":
                return await self._project_status(repo, command, context)
            definition = ProjectDefinition.model_validate(command.project_definition)
            for component in definition.components:
                member_settings = await self.collaboration.settings(component.repository_id)
                component.verification = [Command.model_validate(value) for value in member_settings["test_commands"]]
            # Validate copied operator command DTOs before persisting the topology.
            definition = ProjectDefinition.model_validate(definition.model_dump())
            saved = await self.projects.configure(definition, command.project_id)
            return {
                "project_id": saved["id"],
                "revision": saved["revision"],
                "digest": saved["digest"],
                "status": "configured",
            }
        if command.action == "roles":
            if command.operation == "status":
                if command.project_id:
                    return {"project_id": command.project_id, "roles": context["project"]["definition"].get("roles")}
                return dict(await self.store.repository_roles(repo["id"]))
            roles = ProjectRoles.model_validate(command.roles)
            if command.project_id:
                value = {**context["project"]["definition"], "roles": roles.model_dump()}
                saved = await self.projects.configure(ProjectDefinition.model_validate(value), command.project_id)
                return {
                    "project_id": saved["id"],
                    "revision": saved["revision"],
                    "digest": saved["digest"],
                    "status": "configured",
                }
            return dict(await self.store.set_repository_roles(repo["id"], roles))
        if command.action == "analysis":
            manager = self.collaboration.analysis_manager
            if command.operation == "start":
                report = await manager.analyze(
                    command.project_id, AnalysisInput(component_ids=command.component_ids or [])
                )
            elif command.operation == "review":
                report = await manager.review(
                    command.project_id, command.analysis_id, AnalysisReview(digest=command.digest, reviewed=True)
                )
            elif command.analysis_id:
                report = context["analysis"]
            else:
                async with self.store._unit():
                    reports = [
                        copy.deepcopy(row.data) for row in await manager.records.latest(identifier(command.project_id))
                    ]
                visible = []
                for report in reports:
                    try:
                        await self._members(
                            repo,
                            command,
                            context["receipt"],
                            [*context["graphs"], report["project_snapshot"]["definition"]],
                            configure=False,
                        )
                    except ValueError:
                        continue
                    visible.append(self._analysis_summary(report))
                return {"project_id": command.project_id, "analyses": visible[:20], "shown": min(20, len(visible))}
            return self._analysis_summary(report)
        return await self._assistant(repo, command, settings, receipt_id, context)

    async def execute_functional(self, repo: Data, command: Any, settings: Data, receipt_id: str) -> Data:
        authority = self.collaboration.poll_functional_authority
        if authority is None:
            raise ValueError("The functional polling authority is unavailable")
        active = authority._active.get()
        if (
            active is None
            or active[1]["delivery"]["id"] != receipt_id
            or command != active[1]["command"]
            or repo["id"] != active[1]["delivery"]["repository_id"]
        ):
            raise ValueError("A verified functional polling action is required")
        from firefly_forge.project_access import ProjectAccess

        verified = active[1]
        await authority.authorize_snapshot(ProjectAccess.actor_snapshot(active[0]), command.project_id, "read")
        context = dict(
            receipt=verified["delivery"],
            project=verified["project"],
            graphs=[verified["project"]["definition"]],
            target_id=repo["id"],
            model_profile_id=verified["request"].model_profile_id,
            assistant_agent=verified["model"]["provider"],
        )
        return await self._assistant(repo, command, settings, receipt_id, context)

    async def _assistant(self, repo: Data, command: Any, settings: Data, receipt_id: str | None, context: Data) -> Data:
        manager = self.collaboration.product_assistant
        if command.operation == "start":
            conversation = await manager.create(
                context["target_id"],
                AssistantSessionInput.model_validate(
                    dict(
                        agent=context.get("assistant_agent", settings["assistant_agent"]),
                        project_id=command.project_id,
                        model_profile_id=context.get("model_profile_id"),
                    )
                ),
            )
            return {
                "conversation_id": conversation["id"],
                "repository_id": context["target_id"],
                "project_id": command.project_id,
                "status": "conversation_created",
                "model_invoked": False,
            }
        if command.operation == "status":
            if command.conversation_id:
                conversation = await manager.session(context["target_id"], command.conversation_id)
                return self._conversation_summary(conversation)
            async with self.store._unit():
                conversations = [
                    copy.deepcopy(row.data) for row in await manager.records.latest(identifier(context["target_id"]))
                ]
            visible = []
            for conversation in conversations:
                frozen = conversation.get("project_snapshot")
                if frozen and (context["project"] is None or frozen["id"] != context["project"]["id"]):
                    continue
                try:
                    await self._members(
                        repo,
                        command,
                        context["receipt"],
                        [*context["graphs"], *([frozen["definition"]] if frozen else [])],
                        configure=False,
                    )
                except ValueError:
                    continue
                visible.append(
                    {
                        "conversation_id": conversation["id"],
                        "project_id": conversation["project_id"],
                        "created_at": conversation["created_at"],
                    }
                )
            return {"repository_id": context["target_id"], "conversations": visible[:20]}
        turn = await manager.turn(
            context["target_id"],
            command.conversation_id,
            AssistantTurnInput(request_id=command.request_id, message=command.message),
        )
        result = {
            "conversation_id": command.conversation_id,
            "turn_id": turn["id"],
            "status": turn["status"],
            "proposal_reviewed": False,
            "reply": (turn.get("reply") or {}).get("message"),
            "questions": (turn.get("reply") or {}).get("questions", []),
            "detail": turn.get("detail"),
        }
        if "model_profile_id" in context:
            result["intent"] = (turn.get("reply") or {}).get("intent")
        if (turn.get("reply") or {}).get("proposal"):
            result["proposal_status"] = "unreviewed"
            if settings.get("remote_artifacts") and "model_profile_id" not in context:
                result["remote_reference"] = await self._proposal_reference(repo, command, context, turn, receipt_id)
            else:
                result["proposal_access"] = (
                    "Enable operator-owned remote_artifacts to publish the full unreviewed proposal"
                )
        return result

    async def _project_status(self, repo: Data, command: Any, context: Data) -> Data:
        project = context["project"]
        visible = []
        grants = await self.collaboration.scm.repositories(context["receipt"]["provider"])
        for run in await self.store.runs():
            if run.get("project_id") != project["id"]:
                continue
            component = run.get("component_snapshot") or {}
            if not isinstance(run.get("component_id"), str) or not isinstance(run.get("repository_id"), str):
                continue
            try:
                graph = await self.projects.snapshot_definition(
                    component,
                    project_id=run["project_id"],
                    component_id=run["component_id"],
                    repository_id=run["repository_id"],
                )
            except (KeyError, ValueError):
                continue
            try:
                await self._members(
                    repo, command, context["receipt"], [*context["graphs"], graph], configure=False, grants=grants
                )
            except ValueError:
                continue
            visible.append(
                {key: run.get(key) for key in ["id", "repository_id", "component_id", "status", "branch", "pr_url"]}
            )
        return {
            "project_id": project["id"],
            "revision": project["revision"],
            "digest": project["digest"],
            "definition": project["definition"],
            "runs": visible,
        }

    async def _repositories(self, repo: Data, command: Any, receipt: Data) -> Data:
        grants = await self.collaboration.scm.repositories(receipt["provider"])
        candidates = sorted(await self.store.repositories(), key=lambda item: item["id"])
        visible = []
        for candidate in candidates:
            if command.cursor and candidate["id"] <= command.cursor:
                continue
            try:
                await self._members(
                    repo,
                    command,
                    receipt,
                    [{"components": [{"repository_id": repo["id"]}, {"repository_id": candidate["id"]}]}],
                    configure=True,
                    grants=grants,
                )
            except ValueError:
                continue
            if any(
                grant["id"] == candidate.get("remote_repository_id")
                and grant["url"] == candidate["url"]
                and grant["name"] == candidate["name"]
                for grant in grants
            ):
                visible.append({key: candidate[key] for key in ["id", "name", "url"]})
            if len(visible) == 21:
                break
        page = visible[:20]
        return {"repositories": page, "next_cursor": page[-1]["id"] if len(visible) > 20 else None}

    @staticmethod
    def _analysis_summary(report: Data) -> Data:
        return {
            "analysis_id": report["id"],
            "project_id": report["project_id"],
            "status": report["status"],
            "digest": report.get("digest"),
            "reviewed": report["reviewed"],
            "functional_definition": report.get("functional_definition"),
            "next_steps": report.get("next_steps", []),
        }

    @staticmethod
    def _conversation_summary(conversation: Data) -> Data:
        return {
            "conversation_id": conversation["id"],
            "project_id": conversation["project_id"],
            "journal": conversation.get("journal"),
            "turns": [
                {
                    "turn_id": turn["id"],
                    "status": turn["status"],
                    "reply": (turn.get("reply") or {}).get("message"),
                    "questions": (turn.get("reply") or {}).get("questions", []),
                    "proposal_reviewed": False,
                    "detail": turn.get("detail"),
                }
                for turn in conversation.get("turns", [])[-5:]
            ],
        }

    async def _proposal_reference(
        self, repo: Data, command: Any, context: Data, turn: Data, receipt_id: str | None
    ) -> Data:
        from firefly_forge.channel_artifacts import ReferencePublisher

        assert receipt_id is not None
        payload = {
            "version": 1,
            "conversation_id": command.conversation_id,
            "turn_id": turn["id"],
            "status": "unreviewed",
            "reply": turn["reply"],
            "project_id": command.project_id,
            "frozen_project_digest": (context.get("conversation", {}).get("project_snapshot") or {}).get("digest"),
        }
        sources = {
            f"docs/product/proposals/{turn['id']}.json": json.dumps(payload, sort_keys=True, indent=2),
            f"docs/product/proposals/{turn['id']}.md": "# Unreviewed product proposal\n\n"
            + turn["reply"]["message"]
            + "\n\nThis model proposal has not approved functional, architecture or design review.\n\n"
            + "```json\n"
            + json.dumps(turn["reply"].get("proposal"), indent=2)
            + "\n```\n",
        }
        await self.store.admit_credentials(sources)
        digest = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()

        async def intent(value: Data) -> None:
            await self.collaboration.reference_intent(receipt_id, value)

        async def guard() -> None:
            await self.authorize(repo, command, receipt_id)
            await self.collaboration.reference_guard(receipt_id)

        return await ReferencePublisher(self.collaboration.scm).publish(
            repo, digest, "proposal", sources, intent=intent, guard=guard
        )
