# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Signed application-wide SCM onboarding and bounded repository action contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pyfly.container import service
from sqlalchemy import text

from firefly_forge.collaboration import (
    Collaboration,
    CollaborationSettings,
    ProductDefinition,
    normalized_event,
    verify_webhook,
)
from firefly_forge.collaboration_persistence import ForgeChannelInboxEntity, ForgeChannelInboxRepository
from firefly_forge.models import RepositoryInput
from firefly_forge.product_branding import BrandInput, ProductBranding
from firefly_forge.product_design import ProductDesign
from firefly_forge.scm_connections import SCMConnections
from firefly_forge.store import Store, now

Data = dict[str, Any]
CHANNEL_ACTIONS = (
    "project",
    "product",
    "brand",
    "design",
    "knowledge",
    "applications",
    "maintenance",
    "resume",
    "handoff",
    "cancel",
    "publish",
    "respond",
    "analysis",
    "assistant",
    "roles",
)
ChannelAction = Literal[
    "project",
    "product",
    "brand",
    "design",
    "knowledge",
    "applications",
    "maintenance",
    "resume",
    "handoff",
    "cancel",
    "publish",
    "respond",
    "analysis",
    "assistant",
    "roles",
]


class BootstrapSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    auto_register: bool = False
    trusted_actors: list[str] = Field(default_factory=list, max_length=100)
    configuration_actors: list[str] = Field(default_factory=list, max_length=100)
    webhook_secret: SecretStr | None = Field(default=None, min_length=8, max_length=500)
    defaults: CollaborationSettings = Field(default_factory=lambda: CollaborationSettings(channel_commands=["project"]))

    @model_validator(mode="after")
    def permissions(self) -> BootstrapSettings:
        for value in [*self.trusted_actors, *self.configuration_actors]:
            if not re.fullmatch(r"[A-Za-z0-9{}_.-]{1,200}", value):
                raise ValueError("Use exact stable actor identifiers")
        if not set(self.configuration_actors).issubset(self.trusted_actors):
            raise ValueError("Configuration actors must be trusted actors")
        if self.defaults.webhook_secret is not None:
            raise ValueError("Configure the global secret outside repository defaults")
        return self


class ChannelCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ChannelAction
    definition: dict[str, Any] | None = None
    brand: dict[str, Any] | None = None
    operation: (
        Literal[
            "preview", "status", "search", "index", "remember", "configure", "repositories", "start", "turn", "review"
        ]
        | None
    ) = None
    digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    query: str | None = Field(default=None, min_length=1, max_length=4000)
    paths: list[str] | None = Field(default=None, min_length=1, max_length=8)
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    incident_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    brief: str | None = Field(default=None, min_length=1, max_length=20000)
    request_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    answer: str | None = Field(default=None, min_length=1, max_length=8000)
    agent: Literal["codex", "claude", "bridge", "agentic"] | None = None
    project_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    project_definition: dict[str, Any] | None = None
    cursor: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    analysis_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    component_ids: list[str] | None = Field(default=None, max_length=50)
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    message: str | None = Field(default=None, min_length=1, max_length=12000)
    roles: dict[str, Any] | None = None

    @model_validator(mode="after")
    def shape(self) -> ChannelCommand:
        permitted = {
            "project": {"operation", "project_id", "project_definition", "cursor"},
            "analysis": {"operation", "project_id", "analysis_id", "component_ids", "digest"},
            "assistant": {"operation", "project_id", "conversation_id", "request_id", "message"},
            "roles": {"operation", "project_id", "roles"},
            "product": {"definition"},
            "brand": {"brand"},
            "design": {"operation", "definition", "digest"},
            "knowledge": {"operation", "query", "run_id", "paths"},
            "applications": set(),
            "maintenance": {"incident_id", "brief"},
            "resume": {"run_id"},
            "handoff": {"run_id", "agent"},
            "cancel": {"run_id"},
            "publish": {"run_id"},
            "respond": {"run_id", "request_id", "answer"},
        }[self.action]
        if self.model_fields_set - permitted - {"action"}:
            raise ValueError("Unsupported options for this repository action")
        if self.action in {"project", "analysis", "assistant", "roles"}:
            self.product_shape()
        elif self.action == "product":
            ProductDefinition.model_validate(self.definition)
        elif self.action == "brand":
            BrandInput.model_validate(self.brand)
        elif self.action == "design":
            if self.operation not in {"preview", "status"} or (self.operation == "status" and not self.digest):
                raise ValueError("Design requires preview, or status with a saved digest")
            if self.definition is not None:
                ProductDefinition.model_validate(self.definition)
            if (
                self.operation == "preview"
                and self.digest
                or self.operation == "status"
                and self.definition is not None
            ):
                raise ValueError("Design preview uses a definition; status uses a saved digest")
        elif self.action == "knowledge":
            required = {
                "search": {"operation", "query"},
                "index": {"operation", "run_id", "paths"},
                "remember": {"operation", "run_id"},
            }
            if self.operation not in required or self.model_fields_set - {"action"} != required[self.operation]:
                raise ValueError("Choose knowledge search/query, index/run_id/paths, or remember/run_id")
        elif self.action == "respond" and (not self.run_id or not self.request_id or not self.answer):
            raise ValueError("Respond requires a run_id, request_id and answer")
        elif self.action in {"resume", "handoff", "cancel", "publish"} and not self.run_id:
            raise ValueError("This action requires a run_id")
        elif self.action == "handoff" and not self.agent:
            raise ValueError("Handoff requires a configured coding provider")
        elif self.action == "maintenance" and (not self.incident_id or not self.brief):
            raise ValueError("Maintenance requires an incident_id and brief")
        return self

    def product_shape(self) -> None:
        from firefly_forge.agent_roles import ProjectRoles
        from firefly_forge.product_assistant import AssistantTurnInput
        from firefly_forge.project_models import ProjectDefinition

        provided = self.model_fields_set - {"action"}
        shapes: dict[str, dict[str | None, tuple[set[str], set[str]]]] = {
            "project": {
                None: (set(), set()),
                "repositories": ({"operation"}, {"cursor"}),
                "status": ({"operation", "project_id"}, set()),
                "configure": ({"operation", "project_definition"}, {"project_id"}),
            },
            "analysis": {
                "start": ({"operation", "project_id"}, {"component_ids"}),
                "status": ({"operation", "project_id"}, {"analysis_id"}),
                "review": ({"operation", "project_id", "analysis_id", "digest"}, set()),
            },
            "assistant": {
                "start": ({"operation"}, {"project_id"}),
                "status": ({"operation"}, {"project_id", "conversation_id"}),
                "turn": ({"operation", "conversation_id", "request_id", "message"}, {"project_id"}),
            },
            "roles": {
                "status": ({"operation"}, {"project_id"}),
                "configure": ({"operation", "roles"}, {"project_id"}),
            },
        }
        if self.operation not in shapes[self.action]:
            raise ValueError("Choose a supported product action operation")
        required, optional = shapes[self.action][self.operation]
        if not required <= provided or provided - required - optional:
            raise ValueError("Use the exact structured fields for this product action")
        if any(getattr(self, field) is None for field in provided):
            raise ValueError("Product action fields cannot be null")
        if self.project_definition is not None:
            definition = ProjectDefinition.model_validate(self.project_definition)
            if any(component.verification for component in definition.components):
                raise ValueError("Verification commands are operator-owned")
        if self.roles is not None:
            ProjectRoles.model_validate(self.roles)
        if self.action == "assistant" and self.operation == "turn":
            AssistantTurnInput.model_validate({"request_id": self.request_id, "message": self.message})

    @classmethod
    def parse(cls, value: str) -> ChannelCommand:
        match = re.fullmatch(r"/forge ([a-z]+)(?: ([^\r\n]+))?", value.strip())
        if match is None or match[1] not in CHANNEL_ACTIONS:
            raise ValueError("Use a supported repository action with an optional JSON object")
        options = json.loads(match[2]) if match[2] else {}
        if not isinstance(options, dict) or "action" in options:
            raise ValueError("Options require a JSON object without action overrides")
        return cls.model_validate({"action": match[1], **options})


@service
class ChannelActions:
    def __init__(self, store: Store, collaboration: Collaboration) -> None:
        self.store, self.collaboration = store, collaboration

    async def execute(
        self, repo: Data, command: ChannelCommand, settings: Data, *, receipt_id: str | None = None
    ) -> Data:
        repository_id = repo["id"]
        engine = self.collaboration.deployments.engine if self.collaboration.deployments else None
        if engine is not None and engine.poller is not None:
            await self.store.assert_ownership()
        if settings.get("remote_artifacts") and command.action in {"brand", "design"} and receipt_id is None:
            raise ValueError("Remote references require an accepted repository delivery receipt")
        if command.action in {"analysis", "assistant", "roles"} or (
            command.action == "project" and command.operation is not None
        ):
            from firefly_forge.channel_products import ChannelProducts

            return await ChannelProducts(self.store, self.collaboration).execute(repo, command, settings, receipt_id)
        if command.action == "project":
            snapshot = await self.collaboration.product(repository_id)
            return {
                "repository_id": repository_id,
                "name": repo["name"],
                "url": repo["url"],
                "product_revision": snapshot["revision"] if snapshot else None,
            }
        if command.action == "product":
            saved = await self.collaboration.save_product(
                repository_id, ProductDefinition.model_validate(command.definition)
            )
            return {key: saved[key] for key in ("repository_id", "revision", "digest")}
        if command.action == "brand":
            saved = await ProductBranding(self.store).configure(repository_id, BrandInput.model_validate(command.brand))
            result = {key: saved[key] for key in ("repository_id", "name", "revision", "digest")}
            if settings.get("remote_artifacts"):
                result["remote_reference"] = await self._references(repo, saved, "brand", receipt_id)
            return result
        if command.action == "design":
            designs = ProductDesign(self.store)
            if command.operation == "status":
                assert command.digest
                saved = await designs.load(repository_id, command.digest)
            else:
                snapshot = await self.collaboration.product(repository_id)
                definition = command.definition or (snapshot["definition"] if snapshot else None)
                if definition is None:
                    raise ValueError("Save a product definition before previewing its journeys")
                saved = await designs.preview(repository_id, ProductDefinition.model_validate(definition))
            result = {
                key: saved[key]
                for key in (
                    "repository_id",
                    "digest",
                    "definition_digest",
                    "brand_digest",
                    "method",
                    "coverage",
                    "artifact_paths",
                )
            }
            if settings.get("remote_artifacts"):
                result["remote_reference"] = await self._references(
                    repo, saved, "design", receipt_id, readonly=command.operation == "status"
                )
            return result
        if command.action == "knowledge":
            from firefly_forge.knowledge import KnowledgeManager

            knowledge = engine.knowledge if engine is not None else KnowledgeManager(self.store)
            if command.operation == "search":
                assert command.query
                return await knowledge.search(repository_id, command.query)
            assert command.run_id
            run = await self.store.get_run(command.run_id)
            if run["repository_id"] != repository_id:
                raise ValueError("Run belongs to a different repository")
            if command.operation == "index":
                assert command.paths
                return await knowledge.index(command.run_id, command.paths)
            return await knowledge.remember(run)
        if engine is None:
            raise ValueError("Factory runtime is not available for this action")
        if command.action in {"applications", "maintenance"}:
            from firefly_forge.application_operations import ApplicationOperations, MaintenanceRequest

            assert self.collaboration.deployments is not None
            operations = ApplicationOperations(self.store, engine, self.collaboration.deployments)
            if command.action == "applications":
                return {
                    "applications": [
                        app for app in await operations.applications() if app["repository_id"] == repository_id
                    ]
                }
            assert command.incident_id and command.brief
            incident = next(
                (
                    item
                    for item in await operations.incidents()
                    if item["id"] == command.incident_id and item["repository_id"] == repository_id
                ),
                None,
            )
            if incident is None:
                raise ValueError("Incident belongs to a different repository or does not exist")
            if not settings["test_commands"]:
                raise ValueError("Operator verification checks are required for maintenance")
            result = await operations.maintenance(
                command.incident_id,
                MaintenanceRequest(brief=command.brief, agent=settings["agent"], commands=settings["test_commands"]),
            )
            return {"run_id": result["id"], "status": result["status"]}
        assert command.run_id
        run = await self.store.get_run(command.run_id)
        if run["repository_id"] != repository_id:
            raise ValueError("Run belongs to a different repository")
        if command.action == "respond":
            from firefly_forge.agent_roles import WorkerResponse

            if receipt_id is None:
                raise ValueError("Worker response requires an accepted repository delivery receipt")
            receipt = await self.collaboration.receipt(receipt_id)
            if receipt["repository_id"] != repository_id or receipt["actor"] not in settings["configuration_actors"]:
                raise ValueError("A configuration actor must answer the worker request")
            assert command.request_id and command.answer
            await engine.journal.worker_response(
                command.run_id,
                command.request_id,
                WorkerResponse(id=receipt_id, actor=receipt["actor"], answer=command.answer),
            )
            result = await self.store.get_run(command.run_id)
        elif command.action in {"resume", "handoff"}:
            result = await engine.journal.resume(command.run_id, command.agent if command.action == "handoff" else None)
        elif command.action == "cancel":
            await engine.cancel(command.run_id)
            result = await self.store.get_run(command.run_id)
        else:
            if not settings["publish"]:
                raise ValueError("Operator publication is not enabled for repository commands")
            await engine.publish(command.run_id)
            result = await self.store.get_run(command.run_id)
        return {"run_id": result["id"], "status": result["status"], "pr_url": result.get("pr_url")}

    async def _references(
        self, repo: Data, saved: Data, kind: str, receipt_id: str | None, *, readonly: bool = False
    ) -> Data:
        from firefly_forge.channel_artifacts import ReferencePublisher
        from firefly_forge.product_design import design_files

        assert receipt_id is not None

        async def intent(value: Data) -> None:
            await self.collaboration.reference_intent(receipt_id, value)

        async def guard() -> None:
            await self.collaboration.reference_guard(receipt_id)

        return await ReferencePublisher(self.collaboration.scm).publish(
            repo,
            saved["digest"],
            kind,
            saved["files"] if kind == "brand" else design_files(saved),
            intent=intent,
            guard=guard,
            readonly=readonly,
        )


@service
class RepositoryChannels:
    def __init__(
        self,
        store: Store,
        collaboration: Collaboration,
        scm: SCMConnections,
        inbox: ForgeChannelInboxRepository | None = None,
    ) -> None:
        self.store, self.collaboration, self.scm = store, collaboration, scm
        self.inbox = inbox or ForgeChannelInboxRepository()
        self.path = store.configuration.directory / "channel-bootstrap.json"
        self.secrets_path = store.configuration.directory / "channel-webhook-secrets.json"

    def settings(self, provider: str) -> Data:
        if provider not in {"github", "gitlab", "bitbucket"}:
            raise ValueError("Unsupported webhook provider")
        with self.store.configuration.lock:
            saved = self.store.configuration._read(self.path).get(provider, {})
            secret = self.store.configuration._read(self.secrets_path).get(provider, "")
        return {
            **BootstrapSettings.model_validate(saved).model_dump(exclude={"webhook_secret"}),
            "provider": provider,
            "webhook_secret_configured": bool(secret),
            "ingress_path": "/api/v1/webhooks/" + provider,
        }

    def redaction_settings(self) -> dict[str, str]:
        with self.store.configuration.lock:
            return {
                "CHANNEL_WEBHOOK_SECRET_" + key: value
                for key, value in self.store.configuration._read(self.secrets_path).items()
            }

    async def configure(self, provider: str, request: BootstrapSettings) -> Data:
        self.settings(provider)
        with self.store.configuration.lock:
            settings = self.store.configuration._read(self.path)
            settings[provider] = request.model_dump(exclude={"webhook_secret"})
            self.store.configuration._write(self.path, settings)
            if request.webhook_secret is not None:
                secrets = self.store.configuration._read(self.secrets_path)
                secrets[provider] = request.webhook_secret.get_secret_value()
                self.store.configuration._write(self.secrets_path, secrets)
        return self.settings(provider)

    async def receipts(self, provider: str) -> list[Data]:
        self.settings(provider)
        async with self.store._unit():
            return [copy.deepcopy(row.data) for row in await self.inbox.latest(provider)]

    async def _register(self, provider: str, grant: Data, settings: Data) -> Data:
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "forge:remote:" + provider + ":" + grant["id"]},
            )
            existing = next(
                (
                    repo
                    for repo in await self.store.repositories()
                    if repo.get("connection_provider") == provider and repo.get("remote_repository_id") == grant["id"]
                ),
                None,
            )
            if existing:
                if existing["url"] != grant["url"]:
                    raise ValueError("Connected repository URL changed; review its application grant")
                return existing
            repo = await self.store.add_repository(
                RepositoryInput.model_validate(
                    {
                        "name": grant["name"],
                        "url": grant["url"],
                        "provider": provider,
                        "default_branch": grant["default_branch"],
                        "connection_provider": provider,
                        "remote_repository_id": grant["id"],
                    }
                )
            )
            defaults = CollaborationSettings.model_validate(
                {
                    **settings["defaults"],
                    "enabled": True,
                    "trusted_actors": settings["trusted_actors"],
                    "configuration_actors": settings["configuration_actors"],
                }
            )
            await self.collaboration.configure(repo["id"], defaults)
            return repo

    async def receive(self, provider: str, raw: bytes, headers: Mapping[str, str]) -> Data:
        settings = self.settings(provider)
        if len(raw) > 100000:
            raise ValueError("Webhook body exceeds 100 KB")
        headers = {key.lower(): value for key, value in headers.items()}
        delivery = (
            (headers.get("webhook-id") or headers.get("idempotency-key") or headers.get("x-gitlab-event-uuid", ""))
            if provider == "gitlab"
            else headers.get("x-github-delivery" if provider == "github" else "x-request-uuid", "")
        )
        if not re.fullmatch(r"[A-Za-z0-9{}_.:-]{1,200}", delivery):
            raise ValueError("A bounded provider delivery identifier is required")
        with self.store.configuration.lock:
            secret = str(self.store.configuration._read(self.secrets_path).get(provider, ""))
        digest = hashlib.sha256(raw).hexdigest()
        valid = verify_webhook(provider, raw, headers, secret)
        key = delivery if valid else "invalid:" + digest
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "forge:channel:" + provider + ":" + key},
            )
            previous = await self.inbox.find_delivery(provider, key)
            if previous:
                if previous.digest != digest:
                    raise ValueError("Provider delivery identifier was reused with different bytes")
                return copy.deepcopy(previous.data)
            data: Data = {
                "id": uuid.uuid4().hex,
                "provider": provider,
                "delivery_id": delivery,
                "digest": digest,
                "created_at": now(),
                "status": "received",
                "reason": None,
                "repository_ids": [],
                "repository_receipt_id": None,
            }
            reason = None
            if not valid:
                reason = "invalid_signature"
            elif not settings["enabled"]:
                reason = "channel_disabled"
            else:
                try:
                    body = json.loads(raw)
                    installed = provider == "github" and headers.get("x-github-event") in {
                        "installation",
                        "installation_repositories",
                    }
                    if installed:
                        configured = self.scm._values("github").get("installation_id")
                        if not configured or str(body["installation"]["id"]) != configured:
                            reason = "installation_mismatch"
                        elif body.get("action") not in {"created", "added"}:
                            reason = "unsupported_event"
                        elif not settings["auto_register"]:
                            reason = "auto_registration_disabled"
                        else:
                            grants = await self.scm.repositories(provider)
                            for grant in grants:
                                data["repository_ids"].append((await self._register(provider, grant, settings))["id"])
                    else:
                        event = normalized_event(provider, body, headers)
                        self.store.reject_credentials(event["text"])
                        if event["actor"] not in settings["trusted_actors"]:
                            reason = "untrusted_actor"
                        else:
                            grants = await self.scm.repositories(provider)
                            granted = next((item for item in grants if item["id"] == event["remote_id"]), None)
                            if granted is None:
                                reason = "repository_not_granted"
                            else:
                                existing = next(
                                    (
                                        repo
                                        for repo in await self.store.repositories()
                                        if repo.get("connection_provider") == provider
                                        and repo.get("remote_repository_id") == event["remote_id"]
                                    ),
                                    None,
                                )
                                if not existing and not settings["auto_register"]:
                                    reason = "auto_registration_disabled"
                                else:
                                    from firefly_forge.collaboration import RepositoryCommand

                                    RepositoryCommand.parse(event["text"])
                                    repo = await self._register(provider, granted, settings)
                                    data["repository_ids"] = [repo["id"]]
                                    receipt = await self.collaboration.receive(
                                        provider, repo["id"], raw, headers, signature_secret=secret, global_ingress=True
                                    )
                                    data["repository_receipt_id"] = receipt["id"]
                                    data["repository_receipt_status"] = receipt["status"]
                                    data["repository_receipt_reason"] = receipt["reason"]
                except (ValueError, KeyError, TypeError):
                    reason = "unsupported_or_invalid_event"
            data.update(status="denied" if reason else "processed", reason=reason)
            await self.inbox.save(
                ForgeChannelInboxEntity(
                    id=uuid.UUID(hex=data["id"]),
                    provider=provider,
                    delivery_key=key,
                    digest=digest,
                    status=data["status"],
                    data=data,
                )
            )
            return copy.deepcopy(data)
