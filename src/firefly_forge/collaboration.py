# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Signed repository inbox, customer contracts, and write-ahead SCM collaboration."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.kernel.lifecycle import CONSUMER_PHASE

from firefly_forge.collaboration_persistence import (
    ForgeCollaborationSettingsEntity,
    ForgeCollaborationSettingsRepository,
    ForgeProductDefinitionEntity,
    ForgeProductDefinitionRepository,
    ForgeWebhookInboxEntity,
    ForgeWebhookInboxRepository,
)
from firefly_forge.functional_features import FunctionalFeature, feature_contract, validate_features
from firefly_forge.models import Backend, Command, Frontend, RunInput
from firefly_forge.operations import DeploymentManager
from firefly_forge.product_architecture import ProductArchitecture
from firefly_forge.product_design import DesignReview, ProductDesign
from firefly_forge.product_lifecycle import IntegrationContract, validate_integrations
from firefly_forge.product_provenance import AIOrigin, validate_origin
from firefly_forge.prompts import prompt_text
from firefly_forge.scm_connections import SCMConnections
from firefly_forge.store import Store, identifier, now
from firefly_forge.visual_design import ProcessWorkflow, VisualDesign, validate_process_references

if TYPE_CHECKING:
    from firefly_forge.channel_actions import ChannelCommand

Data = dict[str, Any]
Provider = Literal["github", "gitlab", "bitbucket"]
Action = Literal["run", "test", "maintain", "status", "deploy"]


@dataclass(frozen=True)
class _AdmittedSCMEvent:
    provider: str
    payload_digest: str
    event_digest: str


class ProductDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer: str = Field(min_length=1, max_length=4000)
    problem: str = Field(min_length=1, max_length=8000)
    journey: list[str] = Field(min_length=1, max_length=50)
    user_stories: list[str] = Field(min_length=1, max_length=100)
    acceptance: list[str] = Field(min_length=1, max_length=100)
    nonfunctional_requirements: list[str] = Field(default_factory=list, max_length=50)
    security_requirements: list[str] = Field(default_factory=list, max_length=50)
    owners: list[str] = Field(min_length=1, max_length=30)
    architecture: ProductArchitecture | None = None
    design_review: DesignReview | None = None
    visual_design: VisualDesign = Field(default_factory=VisualDesign)
    processes: list[ProcessWorkflow] = Field(default_factory=list, max_length=20)
    features: list[FunctionalFeature] = Field(default_factory=list, max_length=100)
    integrations: list[IntegrationContract] = Field(default_factory=list, max_length=50)
    functional_reviewed: bool = False
    ai_origin: AIOrigin | None = None

    @field_validator(
        "journey", "user_stories", "acceptance", "nonfunctional_requirements", "security_requirements", "owners"
    )
    @classmethod
    def bounded_lines(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 4000 for value in values):
            raise ValueError("Definition entries must contain between 1 and 4000 characters")
        return [value.strip() for value in values]

    @model_validator(mode="after")
    def bounded_definition(self) -> ProductDefinition:
        validate_process_references(self.processes, self.journey, len(self.acceptance))
        validate_features(self.features, self.journey, len(self.acceptance))
        validate_integrations(self.integrations, self.features, self.journey)
        if self.functional_reviewed and feature_contract(self)["status"] != "ready":
            raise ValueError("Map expected behavior and acceptance criteria for every journey before functional review")
        if len(self.model_dump_json()) > 80000:
            raise ValueError("Product definition exceeds 80 KB")
        return self


class CollaborationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    trusted_actors: list[str] = Field(default_factory=list, max_length=100)
    configuration_actors: list[str] = Field(default_factory=list, max_length=100)
    channel_commands: list[str] = Field(default_factory=list, max_length=15)
    remote_artifacts: bool = False
    webhook_secret: SecretStr | None = Field(default=None, min_length=8, max_length=500)
    backend: Backend = "pyfly"
    frontend: Frontend = "react"
    agent: Literal["codex", "claude", "bridge", "agentic", "api"] = "codex"
    assistant_agent: Literal["bridge", "agentic"] = "bridge"
    publish: bool = False
    allowed_commands: list[Action] = Field(
        default_factory=lambda: cast(list[Action], ["run", "test", "maintain", "status"]), max_length=5
    )
    test_commands: list[Command] = Field(default_factory=list, max_length=20)
    deploy_enabled: bool = False
    deploy_targets: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("trusted_actors", "configuration_actors", "deploy_targets")
    @classmethod
    def identifiers(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z0-9{}_.-]{1,200}", value) for value in values):
            raise ValueError("Use exact stable actor identifiers or configured target IDs")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def channel_permissions(self) -> CollaborationSettings:
        from firefly_forge.channel_actions import CHANNEL_ACTIONS

        if not set(self.configuration_actors).issubset(self.trusted_actors):
            raise ValueError("Configuration actors must be trusted actors")
        if set(self.channel_commands) - set(CHANNEL_ACTIONS):
            raise ValueError("Unknown repository channel command")
        return self


class RepositoryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    brief: str | None = Field(default=None, min_length=1, max_length=40000)
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    target_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    product_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def meaningful_options(self) -> RepositoryCommand:
        if self.action in {"run", "maintain"} and (not self.brief or not self.brief.strip()):
            raise ValueError("run and maintain require a brief")
        if self.action == "deploy" and (not self.run_id or not self.target_id):
            raise ValueError("deploy requires a verified run_id and configured target_id")
        if self.action != "deploy" and self.target_id:
            raise ValueError("target_id is only valid for deploy")
        if self.action in {"run", "test", "maintain"} and self.run_id:
            raise ValueError("A new run cannot reuse a run_id")
        return self

    @classmethod
    def parse(cls, text: str) -> RepositoryCommand | ChannelCommand:
        match = re.fullmatch(r"/forge (run|test|maintain|status|deploy)(?: ([^\r\n]+))?", text.strip())
        if match is None:
            from firefly_forge.channel_actions import ChannelCommand

            return ChannelCommand.parse(text)
        options = json.loads(match[2]) if match[2] else {}
        if not isinstance(options, dict) or "action" in options:
            raise ValueError("Command options must be a JSON object without action overrides")
        return cls.model_validate({"action": match[1], **options})


def customer_instruction(snapshot: Data | None) -> str:
    if not snapshot:
        return ""
    definition = ProductDefinition.model_validate(snapshot["definition"])
    api_only = bool(definition.architecture and definition.architecture.frontend == "none")
    return prompt_text(
        "implementation/customer",
        revision=str(snapshot["revision"]),
        definition=definition.model_dump_json(indent=2),
        surface=(
            prompt_text("implementation/customer-api") if api_only else prompt_text("implementation/customer-frontend")
        ),
    )


def verify_webhook(provider: str, raw: bytes, headers: Mapping[str, str], secret: str) -> bool:
    if not secret:
        return False
    normalized = {key.lower(): value for key, value in headers.items()}
    if provider == "gitlab":
        return hmac.compare_digest(normalized.get("x-gitlab-token", "").encode(), secret.encode())
    header = {"github": "x-hub-signature-256", "bitbucket": "x-hub-signature"}.get(provider)
    if header is None:
        return False
    actual = normalized.get(header, "")
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(actual.encode(), expected.encode())


def normalized_event(provider: str, body: Data, headers: Mapping[str, str]) -> Data:
    event = headers.get(
        {"github": "x-github-event", "gitlab": "x-gitlab-event", "bitbucket": "x-event-key"}[provider], ""
    )
    if provider == "github":
        if event != "issue_comment" or body.get("action") != "created":
            raise ValueError("unsupported_event")
        issue, comment = body["issue"], body["comment"]
        number = int(issue["number"])
        return {
            "remote_id": str(body["repository"]["id"]),
            "actor": str(body["sender"]["id"]),
            "source_key": "comment:" + str(comment["id"]),
            "text": str(comment["body"]),
            "issue": {"kind": "pull_request" if "pull_request" in issue else "issue", "number": number},
        }
    if provider == "gitlab":
        attrs = body["object_attributes"]
        if event != "Note Hook" or body.get("object_kind") != "note" or attrs.get("action", "create") != "create":
            raise ValueError("unsupported_event")
        kind = attrs["noteable_type"]
        if kind not in {"Issue", "MergeRequest"}:
            raise ValueError("unsupported_event")
        issue = body["issue"] if kind == "Issue" else body["merge_request"]
        return {
            "remote_id": str(body["project"]["id"]),
            "actor": str(body["user"]["id"]),
            "source_key": "comment:" + str(attrs["id"]),
            "text": str(attrs["note"]),
            "issue": {"kind": "issue" if kind == "Issue" else "merge_request", "number": int(issue["iid"])},
        }
    if event != "pullrequest:comment_created":
        raise ValueError("unsupported_event")
    issue = body["pullrequest"]
    return {
        "remote_id": str(body["repository"]["uuid"]),
        "actor": str(body["actor"]["uuid"]),
        "source_key": "pullrequest:" + str(issue["id"]) + ":comment:" + str(body["comment"]["id"]),
        "text": str(body["comment"]["content"]["raw"]),
        "issue": {"kind": "pull_request", "number": int(issue["id"])},
    }


@service
class Collaboration:
    phase = CONSUMER_PHASE + 2

    def __init__(
        self,
        store: Store,
        scm: SCMConnections | None = None,
        deployments: DeploymentManager | None = None,
        settings_records: ForgeCollaborationSettingsRepository | None = None,
        inbox: ForgeWebhookInboxRepository | None = None,
        products: ForgeProductDefinitionRepository | None = None,
    ) -> None:
        self.store = store
        self.scm = scm or SCMConnections(store)
        self.deployments = deployments
        self.settings_records = settings_records or ForgeCollaborationSettingsRepository()
        self.inbox = inbox or ForgeWebhookInboxRepository()
        self.products = products or ForgeProductDefinitionRepository()
        self.secrets_path = store.configuration.directory / "collaboration-secrets.json"
        self.poller: asyncio.Task[None] | None = None
        self._active_writes: set[str] = set()
        self.analysis_manager: Any = None
        self.product_assistant: Any = None

    def _secret(self, repository_id: str) -> str:
        value = self.store.configuration._read(self.secrets_path).get(repository_id, "")
        return str(value.get("secret", "")) if isinstance(value, dict) else str(value)

    def _secret_revision(self, repository_id: str) -> str | None:
        value = self.store.configuration._read(self.secrets_path).get(repository_id)
        return str(value["revision"]) if isinstance(value, dict) and value.get("revision") else None

    def redaction_settings(self) -> dict[str, str]:
        return {
            "WEBHOOK_SECRET_" + key: str(value.get("secret", "")) if isinstance(value, dict) else str(value)
            for key, value in self.store.configuration._read(self.secrets_path).items()
        }

    async def _repo_lock(self, repository_id: str) -> None:
        row = await self.store.repository_records.find_by_id(identifier(repository_id), lock=LockMode.PESSIMISTIC_WRITE)
        if row is None:
            raise KeyError("Repository not found")

    async def settings(self, repository_id: str) -> Data:
        repo = await self.store.repository(repository_id)
        async with self.store._unit():
            row = await self.settings_records.find_for_repository(identifier(repository_id))
            data = copy.deepcopy(row.data) if row else CollaborationSettings().model_dump(exclude={"webhook_secret"})
            data.pop("_scm_authority_v1", None)
            return {
                **data,
                "repository_id": repository_id,
                "webhook_secret_configured": bool(self._secret(repository_id)),
                "capabilities": {
                    "issue_authoring": repo.get("connection_provider") in {"github", "gitlab"},
                    "issue_comments": repo.get("connection_provider") in {"github", "gitlab"},
                    "pull_request_comments": repo.get("connection_provider") in {"github", "gitlab", "bitbucket"},
                },
            }

    async def configure(self, repository_id: str, request: CollaborationSettings) -> Data:
        repo = await self.store.repository(repository_id)
        if repo.get("connection_provider") not in {"github", "gitlab", "bitbucket"} or not repo.get(
            "remote_repository_id"
        ):
            raise ValueError("Select a repository from an application connection before enabling collaboration")
        for command in request.test_commands:
            from firefly_forge.configuration import redact_secrets
            from firefly_forge.secrets import redact_environment

            content = " ".join(command.argv)
            if (
                redact_environment(content) != content
                or redact_secrets(content, self.scm.redaction_settings()) != content
            ):
                raise ValueError("Test commands must use server credential configuration, not inline secrets")
        async with self.store._unit():
            await self._repo_lock(repository_id)
            row = await self.settings_records.find_for_repository(identifier(repository_id))
            data = request.model_dump(exclude={"webhook_secret"})
            if row is not None and "_scm_authority_v1" in row.data:
                data["_scm_authority_v1"] = copy.deepcopy(row.data["_scm_authority_v1"])
            if row is None:
                row = ForgeCollaborationSettingsEntity(
                    id=uuid.uuid4(), repository_id=identifier(repository_id), data=data
                )
            else:
                row.data = data
            await self.settings_records.save(row)
            if request.webhook_secret is not None:
                with self.store.configuration.lock:
                    secrets = self.store.configuration._read(self.secrets_path)
                    secrets[repository_id] = {
                        "secret": request.webhook_secret.get_secret_value(),
                        "revision": uuid.uuid4().hex,
                    }
                    self.store.configuration._write(self.secrets_path, secrets)
        return await self.settings(repository_id)

    async def product(self, repository_id: str) -> Data | None:
        await self.store.repository(repository_id)
        async with self.store._unit():
            row = await self.products.find_current(identifier(repository_id))
            return copy.deepcopy(row.data) if row else None

    async def save_product(self, repository_id: str, request: ProductDefinition) -> Data:
        await self.store.admit_credentials(request.model_dump())
        if request.architecture is not None and not request.architecture.reviewed:
            raise ValueError("Review the architecture proposal before saving the product definition")
        await ProductDesign(self.store).validate_review(repository_id, request)
        async with self.store._unit():
            await self._repo_lock(repository_id)
            previous = await self.products.find_current(identifier(repository_id))
            origin = await validate_origin(self.store, repository_id, request)
            data = {
                "repository_id": repository_id,
                "revision": previous.revision + 1 if previous else 1,
                "definition": request.model_dump(),
                "created_at": now(),
                "origin": origin,
            }
            data["digest"] = hashlib.sha256(
                json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            await self.products.save(
                ForgeProductDefinitionEntity(
                    id=uuid.uuid4(),
                    repository_id=identifier(repository_id),
                    revision=data["revision"],
                    digest=data["digest"],
                    data=data,
                )
            )
            return copy.deepcopy(data)

    async def receipts(self, repository_id: str | None = None) -> list[Data]:
        async with self.store._unit():
            return [
                copy.deepcopy(row.data)
                for row in await self.inbox.find_latest(identifier(repository_id) if repository_id else None)
            ]

    async def receipt(self, receipt_id: str) -> Data:
        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id))
            if row is None:
                raise KeyError("Delivery receipt not found")
            return copy.deepcopy(row.data)

    def _allowed(
        self, repo: Data, settings: Data, event: Data, provider: str, *, channel_snapshot: Data | None = None
    ) -> str | None:
        if event.get("global_ingress"):
            if channel_snapshot is None:
                with self.store.configuration.lock:
                    channel = self.store.configuration._read(
                        self.store.configuration.directory / "channel-bootstrap.json"
                    ).get(provider, {})
            else:
                channel = channel_snapshot
            if not channel.get("enabled"):
                return "channel_disabled"
            if event["actor"] not in channel.get("trusted_actors", []):
                return "global_actor_revoked"
        if not settings["enabled"]:
            return "collaboration_disabled"
        if repo.get("connection_provider") != provider or str(repo.get("remote_repository_id")) != event["remote_id"]:
            return "repository_mismatch"
        if event["actor"] not in settings["trusted_actors"]:
            return "untrusted_actor"
        return None

    async def receive(
        self,
        provider: str,
        repository_id: str,
        raw: bytes,
        headers: Mapping[str, str],
        *,
        signature_secret: str | None = None,
        signature_revision: str | None = None,
        global_ingress: bool = False,
        _admitted_event: _AdmittedSCMEvent | None = None,
    ) -> Data:
        prepared = await self._prepare_receive(
            provider,
            repository_id,
            raw,
            headers,
            signature_secret=signature_secret,
            signature_revision=signature_revision,
            global_ingress=global_ingress,
            _admitted_event=_admitted_event,
        )
        return await self._retain_receive(prepared)

    async def _prepare_receive(
        self,
        provider: str,
        repository_id: str,
        raw: bytes,
        headers: Mapping[str, str],
        *,
        signature_secret: str | None = None,
        signature_revision: str | None = None,
        global_ingress: bool = False,
        _admitted_event: _AdmittedSCMEvent | None = None,
    ) -> Data:
        from pyfly.data.transaction import current_unit_of_work

        if current_unit_of_work() is not None and _admitted_event is None:
            raise ValueError("Prepare signed ingress outside an active caller transaction")
        if provider not in {"github", "gitlab", "bitbucket"}:
            raise ValueError("Unsupported webhook provider")
        if len(raw) > 100000:
            raise ValueError("Webhook body exceeds 100 KB")
        headers = {key.lower(): value for key, value in headers.items()}
        delivery_id = (
            headers.get("webhook-id") or headers.get("idempotency-key") or headers.get("x-gitlab-event-uuid", "")
            if provider == "gitlab"
            else headers.get("x-github-delivery" if provider == "github" else "x-request-uuid", "")
        )
        if not re.fullmatch(r"[A-Za-z0-9{}_.:-]{1,200}", delivery_id):
            raise ValueError("A bounded provider delivery identifier is required")
        repo, settings = await self.store.repository(repository_id), await self.settings(repository_id)
        digest = hashlib.sha256(raw).hexdigest()
        reason: str | None = None
        event: Data = {}
        secret_record = self.store.configuration._read(self.secrets_path).get(repository_id, "")
        secret = (
            signature_secret
            if signature_secret is not None
            else (str(secret_record.get("secret", "")) if isinstance(secret_record, dict) else str(secret_record))
        )
        key_revision = (
            signature_revision
            if signature_secret is not None
            else (secret_record.get("revision") if isinstance(secret_record, dict) else None)
        )
        if signature_secret is not None and signature_revision is not None:
            configured_key = self.store.configuration._read(
                self.store.configuration.directory / "channel-webhook-secrets.json"
            ).get(provider)
            if not isinstance(configured_key, dict) or configured_key != {
                "secret": signature_secret,
                "revision": signature_revision,
            }:
                key_revision = None
        from firefly_forge.scm_webhooks import verify_gitlab_signing_token

        signing_mode = provider == "gitlab" and secret.startswith("whsec_")
        signature_valid = (
            verify_gitlab_signing_token(raw, headers, secret, current_time=time.time())
            if signing_mode
            else verify_webhook(provider, raw, headers, secret)
        )
        verified_reference: Data | None = None
        delivery_key = delivery_id if signature_valid else "invalid:" + digest
        if not signature_valid:
            reason = "invalid_signature"
        else:
            try:
                body = json.loads(raw)
                event = normalized_event(provider, body, headers)
                if _admitted_event is not None:
                    from firefly_forge.journal import digest as event_digest

                    if _admitted_event != _AdmittedSCMEvent(provider, digest, event_digest(event)):
                        raise ValueError("The admitted SCM event does not match the signed payload")
                if key_revision and (provider != "gitlab" or signing_mode):
                    from firefly_forge.scm_authority_models import VerifiedSCMReferenceV1

                    try:
                        scope = self.scm.delegation_scope(repo)
                        installation = body.get("installation", {}).get("id") if provider == "github" else None
                        if provider == "github" and type(installation) is not int:
                            raise ValueError("A GitHub App installation identifier is required")
                        verified_reference = VerifiedSCMReferenceV1.model_validate(
                            {
                                **scope,
                                "version": 1,
                                "installation_id": str(installation) if installation is not None else None,
                                "external_actor_id": event["actor"],
                                "webhook_key_revision": key_revision,
                                "verification_method": "gitlab_signing_hmac_sha256"
                                if signing_mode
                                else "raw_body_hmac_sha256",
                                "payload_digest": digest,
                            }
                        ).model_dump(mode="json")
                        if verified_reference["installation_id"] != scope["installation_id"]:
                            raise ValueError("The delivery belongs to a different GitHub App installation")
                    except (ValueError, KeyError, TypeError):
                        verified_reference = None
                event["global_ingress"] = global_ingress
                event["issue"]["url"] = self._ticket_url(repo, event["issue"])
                if event["issue"]["number"] <= 0:
                    raise ValueError("invalid_issue")
                if _admitted_event is None:
                    await self.store.admit_credentials(event)
                else:
                    self.store.reject_credentials(event)
                reason = self._allowed(repo, settings, event, provider)
                if not reason:
                    command = RepositoryCommand.parse(event.pop("text"))
                    event["command"] = command.model_dump(exclude_none=True)
                    if command.action not in [*settings["allowed_commands"], *settings.get("channel_commands", [])]:
                        reason = "command_disabled"
            except (ValueError, KeyError, TypeError):
                event.pop("text", None)
                event.pop("command", None)
                event["issue"] = {
                    "number": event.get("issue", {}).get("number", 0),
                    "kind": event.get("issue", {}).get("kind", "issue"),
                }
                reason = "unsupported_or_invalid_command"
        if _admitted_event is None:
            await self.store.admit_credentials({"event": event, "verification": verified_reference})
        else:
            self.store.reject_credentials({"event": event, "verification": verified_reference})
        return {
            "provider": provider,
            "repository_id": repository_id,
            "repository": repo,
            "settings": settings,
            "digest": digest,
            "delivery_id": delivery_id,
            "delivery_key": delivery_key,
            "event": event,
            "reason": reason,
            "global_ingress": global_ingress,
            "verified_reference": verified_reference,
        }

    async def _admit_signed_event(self, provider: str, raw: bytes, headers: Mapping[str, str]) -> _AdmittedSCMEvent:
        from pyfly.data.transaction import current_unit_of_work

        from firefly_forge.journal import digest as event_digest

        if current_unit_of_work() is not None:
            raise ValueError("Admit signed ingress credentials outside a caller transaction")
        event = normalized_event(provider, json.loads(raw), headers)
        await self.store.admit_credentials(event)
        return _AdmittedSCMEvent(provider, hashlib.sha256(raw).hexdigest(), event_digest(event))

    async def _retain_receive(self, prepared: Data) -> Data:
        """Commit an admitted ingress cut without provider or vault I/O."""
        provider, repository_id = prepared["provider"], prepared["repository_id"]
        digest, delivery_id, delivery_key = prepared["digest"], prepared["delivery_id"], prepared["delivery_key"]
        event, reason = prepared["event"], prepared["reason"]
        global_ingress, verified_reference = prepared["global_ingress"], prepared["verified_reference"]
        async with self.store._unit():
            await self._repo_lock(repository_id)
            current_repository, current_settings = (
                await self.store.repository(repository_id),
                await self.settings(repository_id),
            )
            if current_repository != prepared["repository"] or current_settings != prepared["settings"]:
                reason = "collaboration_scope_changed"
                verified_reference = None
            if verified_reference is not None:
                from firefly_forge.scm_actor_authority import SCMActorAuthority

                authority = SCMActorAuthority(self.store, self.scm)
                current_scope = self.scm.delegation_scope(current_repository)
                if current_scope != {
                    key: verified_reference[key] for key in current_scope
                } or not authority._key_current(repository_id, verified_reference):
                    reason = "verification_scope_changed"
                    verified_reference = None
            previous = await self.inbox.find_delivery(identifier(repository_id), provider, delivery_key)
            if previous:
                if previous.data["digest"] != digest:
                    raise ValueError("Provider delivery identifier was reused with different bytes")
                return copy.deepcopy(previous.data)
            source_key = (
                str(event["source_key"])
                if reason is None
                else "denied:" + hashlib.sha256(delivery_key.encode()).hexdigest()
            )
            previous = await self.inbox.find_source(identifier(repository_id), provider, source_key)
            if previous:
                return copy.deepcopy(previous.data)
            data = {
                "id": uuid.uuid4().hex,
                "repository_id": repository_id,
                "provider": provider,
                "delivery_id": delivery_id,
                "digest": digest,
                "created_at": now(),
                "updated_at": now(),
                "status": "denied" if reason else "received",
                "reason": reason,
                "actor": event.get("actor"),
                "issue": event.get("issue"),
                "command": event.get("command"),
                "run_id": None,
                "feedback_status": "not_requested",
                "global_ingress": global_ingress,
            }
            if not reason:
                data["remote_id"] = event["remote_id"]
            if verified_reference is not None:
                data["verification"] = verified_reference
            self.store.reject_credentials(data)
            await self.inbox.save(
                ForgeWebhookInboxEntity(
                    id=identifier(str(data["id"])),
                    repository_id=identifier(repository_id),
                    provider=provider,
                    delivery_id=delivery_key,
                    source_key=source_key,
                    status=data["status"],
                    data=data,
                )
            )
            self.store.reject_credentials(data)
            return copy.deepcopy(data)

    async def _write_row(self, row: ForgeWebhookInboxEntity, **changes: Any) -> None:
        row.data = {**row.data, **changes, "updated_at": now()}
        row.status = row.data["status"]
        await self.inbox.save(row)

    async def process(self, receipt_id: str) -> Data:
        initial = await self.receipt(receipt_id)
        if initial["status"] != "received":
            return initial
        from firefly_forge.scm_actor_authority import authority_state

        async with self.store._unit():
            configured = await self.settings_records.find_for_repository(identifier(initial["repository_id"]))
            mapped_repository = bool(authority_state(configured.data if configured else {})["heads"])
        if mapped_repository or (initial.get("command") or {}).get("action") == "respond":
            await self._process_delegated(receipt_id)
            await self.feedback(receipt_id)
            return await self.receipt(receipt_id)
        if initial.get("global_ingress"):
            repo = await self.store.repository(initial["repository_id"])
            settings = await self.settings(repo["id"])
            reason = self._allowed(repo, settings, initial, initial["provider"])
            if not reason:
                try:
                    grants = await self.scm.repositories(initial["provider"])
                    if not any(
                        grant["id"] == repo["remote_repository_id"] and grant["url"] == repo["url"] for grant in grants
                    ):
                        reason = "repository_grant_revoked"
                except Exception:
                    reason = "repository_grant_unavailable"
            if reason:
                async with self.store._unit():
                    row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                    assert row is not None
                    if row.status == "received":
                        await self._write_row(row, status="denied", reason=reason)
                    return copy.deepcopy(row.data)
        if (initial.get("command") or {}).get("action") not in {"run", "test", "maintain", "status", "deploy"}:
            return await self._process_channel(receipt_id)
        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Delivery receipt not found")
            if row.status != "received":
                return copy.deepcopy(row.data)
            repo = await self.store.repository(row.data["repository_id"])
            settings = await self.settings(row.data["repository_id"])
            reason = self._allowed(repo, settings, row.data, row.provider)
            command = RepositoryCommand.model_validate(row.data["command"])
            if command.action not in settings["allowed_commands"]:
                reason = "command_disabled"
            if reason:
                await self._write_row(row, status="denied", reason=reason)
                return copy.deepcopy(row.data)
            if command.action in {"run", "test", "maintain"}:
                snapshot = await self.product(repo["id"])
                if command.product_revision and (not snapshot or snapshot["revision"] != command.product_revision):
                    await self._write_row(row, status="denied", reason="product_revision_mismatch")
                    return copy.deepcopy(row.data)
                architecture = (
                    ProductDefinition.model_validate(snapshot["definition"]).architecture if snapshot else None
                )
                run = await self.store.create_run(
                    RunInput(
                        repository_id=repo["id"],
                        brief=command.brief or prompt_text("implementation/verification-request"),
                        backend=architecture.backend if architecture else settings["backend"],
                        frontend=architecture.frontend if architecture else settings["frontend"],
                        agent=settings["agent"],
                        publish=settings["publish"],
                        operation=cast(
                            Literal["build", "test", "maintain"], "build" if command.action == "run" else command.action
                        ),
                        commands=settings["test_commands"],
                        product_definition=snapshot,
                        issue_context={
                            "provider": row.provider,
                            "receipt_id": receipt_id,
                            "actor": row.data["actor"],
                            **row.data["issue"],
                        },
                    )
                )
                await self._write_row(
                    row, status="processed", run_id=run["id"], feedback_status="pending", feedback_stage="queued"
                )
                await self.store.event(
                    run["id"], "repository_command", "Trusted repository command accepted: " + receipt_id
                )
            elif command.action == "status":
                candidates = await self.store.runs()
                status_run = next(
                    (
                        run
                        for run in candidates
                        if run["repository_id"] == repo["id"]
                        and (
                            run["id"] == command.run_id
                            if command.run_id
                            else (run.get("issue_context") or {}).get("number") == row.data["issue"]["number"]
                        )
                    ),
                    None,
                )
                if status_run is None:
                    await self._write_row(row, status="denied", reason="issue_run_not_found")
                    return copy.deepcopy(row.data)
                await self._write_row(
                    row,
                    status="processed",
                    run_id=status_run["id"],
                    feedback_status="pending",
                    feedback_stage=status_run["status"],
                )
            else:
                if (
                    not settings["deploy_enabled"]
                    or command.target_id not in settings["deploy_targets"]
                    or self.deployments is None
                ):
                    await self._write_row(row, status="denied", reason="deployment_not_enabled")
                    return copy.deepcopy(row.data)
                assert command.run_id and command.target_id
                run = await self.store.get_run(command.run_id)
                if run["repository_id"] != repo["id"]:
                    await self._write_row(row, status="denied", reason="deployment_repository_mismatch")
                    return copy.deepcopy(row.data)
                job = await self.deployments.create(command.run_id, command.target_id)
                await self._write_row(
                    row,
                    status="processed",
                    run_id=run["id"],
                    deployment_id=job["id"],
                    feedback_status="pending",
                    feedback_stage="deployment_queued",
                )
        await self.feedback(receipt_id)
        return await self.receipt(receipt_id)

    async def _process_delegated(self, receipt_id: str) -> Data:
        from sqlalchemy import text

        from firefly_forge.agent_roles import WorkerResponse
        from firefly_forge.identity_persistence import ForgeIdentityUserRepository
        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, Permission, ProjectAccess
        from firefly_forge.scm_actor_authority import SCMActorAuthority

        authority = SCMActorAuthority(self.store, self.scm)
        initial = await self.receipt(receipt_id)
        command = initial.get("command") or {}
        try:
            actor = await authority.resolve(initial)
            if command["action"] == "cancel" and self.store.execution_requests is None:
                raise AccessDenied("SCM cancellation requires the admitted execution-request runtime")
        except (AccessDenied, ValueError, KeyError):
            async with self.store._unit():
                row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                assert row is not None
                if row.status == "received":
                    await self._write_row(
                        row, status="denied", reason="scm_authority_setup_required", action_status="not_dispatched"
                    )
                return copy.deepcopy(row.data)
        token = CURRENT_PRINCIPAL.set(actor)
        try:
            context = actor.scm_delegation
            assert context is not None
            try:
                async with self.store._unit() as session:
                    if command["action"] == "cancel":
                        await session.execute(
                            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                            {
                                "key": "forge:execution-request-admission:" + self.store.schema,
                            },
                        )
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {
                            "key": self.store.schema + ":members:" + context["project_id"],
                        },
                    )
                    await ForgeIdentityUserRepository().find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
                    await self._repo_lock(initial["repository_id"])
                    locked = await self.store.run_records.find_by_id(
                        identifier(context["run_id"]), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if locked is None:
                        raise AccessDenied("The delegated run is unavailable")
                    permission: Permission = "read" if command["action"] == "status" else "execute"
                    await ProjectAccess(self.store).authorize_actor(
                        ProjectAccess.actor_snapshot(actor), context["project_id"], permission
                    )
                    if locked.data.get("publish") and command["action"] != "status":
                        await ProjectAccess(self.store).authorize_actor(
                            ProjectAccess.actor_snapshot(actor), context["project_id"], "release"
                        )
                    row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                    assert row is not None
                    if row.status != "received":
                        return copy.deepcopy(row.data)
                    settings = await self.settings(initial["repository_id"])
                    repository = await self.store.repository(initial["repository_id"])
                    reason = self._allowed(
                        repository,
                        settings,
                        row.data,
                        row.provider,
                        channel_snapshot=self.store.configuration._read(
                            self.store.configuration.directory / "channel-bootstrap.json"
                        ).get(row.provider, {}),
                    )
                    allowed = [*settings["allowed_commands"], *settings.get("channel_commands", [])]
                    if reason or command["action"] not in allowed:
                        await self._write_row(
                            row, status="denied", reason=reason or "command_disabled", action_status="not_dispatched"
                        )
                        return copy.deepcopy(row.data)
                    if CURRENT_PRINCIPAL.get() != actor:
                        raise AccessDenied("The delegated action context changed before admission")
                    self.store.reject_credentials(row.data)
                    await self._write_row(
                        row,
                        status="needs_reconciliation",
                        reason="channel_action_pending",
                        action_status="writing",
                        scm_execution_actor=ProjectAccess.actor_snapshot(actor),
                    )
                    if command["action"] == "cancel":
                        assert self.store.execution_requests is not None
                        result = await self.store.execution_requests.request_cancel(context["run_id"])
                        await self._write_row(
                            row,
                            status="processed",
                            reason=None,
                            action_status="completed",
                            action_result={"request_id": result["id"], "status": result["status"]},
                            run_id=context["run_id"],
                            feedback_status="pending",
                            feedback_stage=locked.data["status"],
                        )
                        return copy.deepcopy(row.data)
            except AccessDenied:
                async with self.store._unit():
                    row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                    assert row is not None
                    if row.status == "received":
                        await self._write_row(
                            row, status="denied", reason="scm_authority_setup_required", action_status="not_dispatched"
                        )
                    return copy.deepcopy(row.data)
            try:
                if command["action"] == "respond":
                    from firefly_forge.journal import Journal

                    journal = self.deployments.engine.journal if self.deployments else Journal(self.store)
                    await journal.worker_response(
                        context["run_id"],
                        command["request_id"],
                        WorkerResponse(
                            id=receipt_id,
                            actor=initial["actor"],
                            answer=command["answer"],
                        ),
                    )
                run = await self.store.get_run(context["run_id"])
                await ProjectAccess(self.store).authorize_actor(
                    ProjectAccess.actor_snapshot(actor), context["project_id"], permission
                )
                result = {"run_id": run["id"], "status": run["status"]}
                await self.store.admit_credentials(result)
                await ProjectAccess(self.store).authorize_actor(
                    ProjectAccess.actor_snapshot(actor), context["project_id"], permission
                )
                if CURRENT_PRINCIPAL.get() != actor:
                    raise AccessDenied("The delegated action context changed before return")
                self.store.reject_credentials(result)
            except Exception:
                async with self.store._unit():
                    row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                    assert row is not None
                    await self._write_row(
                        row, reason="channel_action_outcome_unknown", action_status="needs_reconciliation"
                    )
            else:
                async with self.store._unit():
                    row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                    assert row is not None
                    await self._write_row(
                        row,
                        status="processed",
                        reason=None,
                        action_status="completed",
                        action_result=result,
                        run_id=context["run_id"],
                        feedback_status="pending",
                        feedback_stage=result["status"],
                    )
        finally:
            CURRENT_PRINCIPAL.reset(token)
        return await self.receipt(receipt_id)

    async def _process_channel(self, receipt_id: str) -> Data:
        from firefly_forge.channel_actions import ChannelActions, ChannelCommand
        from firefly_forge.channel_products import ChannelProducts, configuration_action

        initial = await self.receipt(receipt_id)
        command = ChannelCommand.model_validate(initial["command"])
        if initial["status"] == "received" and (
            command.action in {"analysis", "assistant", "roles"}
            or (command.action == "project" and command.operation is not None)
        ):
            try:
                await ChannelProducts(self.store, self).authorize(
                    await self.store.repository(initial["repository_id"]), command, receipt_id
                )
            except (ValueError, KeyError):
                async with self.store._unit():
                    row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                    assert row is not None
                    if row.status == "received":
                        await self._write_row(row, status="denied", reason="member_product_authorization_failed")
                    return copy.deepcopy(row.data)

        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            assert row is not None
            if row.status != "received":
                return copy.deepcopy(row.data)
            repo = await self.store.repository(row.data["repository_id"])
            settings = await self.settings(repo["id"])
            reason = self._allowed(repo, settings, row.data, row.provider)
            command = ChannelCommand.model_validate(row.data["command"])
            if command.action not in settings.get("channel_commands", []):
                reason = "command_disabled"
            configuration = configuration_action(command)
            if configuration and row.data["actor"] not in settings.get("configuration_actors", []):
                reason = "configuration_actor_required"
            if configuration and row.data.get("global_ingress"):
                with self.store.configuration.lock:
                    channel = self.store.configuration._read(
                        self.store.configuration.directory / "channel-bootstrap.json"
                    ).get(row.provider, {})
                if row.data["actor"] not in channel.get("configuration_actors", []):
                    reason = "configuration_actor_required"
            if reason:
                await self._write_row(row, status="denied", reason=reason)
                return copy.deepcopy(row.data)
            await self._write_row(
                row, status="needs_reconciliation", reason="channel_action_pending", action_status="writing"
            )
        try:
            result = await ChannelActions(self.store, self).execute(repo, command, settings, receipt_id=receipt_id)
        except Exception:
            async with self.store._unit():
                row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                assert row is not None
                await self._write_row(
                    row, reason="channel_action_outcome_unknown", action_status="needs_reconciliation"
                )
        else:
            async with self.store._unit():
                row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                assert row is not None
                await self._write_row(
                    row,
                    status="processed",
                    reason=None,
                    action_status="completed",
                    action_result=result,
                    run_id=result.get("run_id"),
                    feedback_status="pending",
                    feedback_stage=result.get("status", "queued") if result.get("run_id") else "action_completed",
                )
            await self.feedback(receipt_id)
        return await self.receipt(receipt_id)

    async def reference_guard(self, receipt_id: str) -> None:
        data = await self.receipt(receipt_id)
        repo = await self.store.repository(data["repository_id"])
        settings = await self.settings(repo["id"])
        if (
            self._allowed(repo, settings, data, data["provider"])
            or not settings.get("remote_artifacts")
            or data["actor"] not in settings.get("configuration_actors", [])
            or data["command"]["action"] not in settings.get("channel_commands", [])
        ):
            raise ValueError("Remote reference publication is no longer authorized")
        if data.get("global_ingress"):
            with self.store.configuration.lock:
                channel = self.store.configuration._read(
                    self.store.configuration.directory / "channel-bootstrap.json"
                ).get(data["provider"], {})
            if data["actor"] not in channel.get("configuration_actors", []):
                raise ValueError("Global configuration role was revoked")
        if self.deployments is not None and self.deployments.engine.poller is not None:
            await self.store.assert_ownership()

    async def reference_intent(self, receipt_id: str, value: Data) -> None:
        await self.reference_guard(receipt_id)
        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            assert row is not None
            if row.status != "needs_reconciliation" or row.data.get("action_status") != "writing":
                raise ValueError("Remote references require a pending action intent")
            await self._write_row(row, reference_intent=value)

    def _ticket_url(self, repo: Data, issue: Data) -> str:
        provider = str(repo["connection_provider"])
        name = str(repo["name"])
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in name.split("/")):
            raise ValueError("Invalid connected repository path")
        host = self.scm._host(provider)
        kind = issue["kind"]
        suffix = (
            ("/pull/" if kind == "pull_request" else "/issues/")
            if provider == "github"
            else ("/-/merge_requests/" if kind == "merge_request" else "/-/issues/")
            if provider == "gitlab"
            else "/pull-requests/"
        )
        return host + "/" + name + suffix + str(issue["number"])

    def _issue_path(self, provider: str, repo: Data, issue: Data | None = None) -> str:
        if provider == "github":
            name = str(repo["name"])
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
                raise ValueError("Invalid connected GitHub repository name")
            return "/repos/" + name + "/issues" + ("/" + str(issue["number"]) + "/comments" if issue else "")
        if provider == "gitlab":
            base = "/projects/" + quote(str(repo["remote_repository_id"]), safe="")
            if not issue:
                return base + "/issues"
            kind = "merge_requests" if issue["kind"] == "merge_request" else "issues"
            return base + "/" + kind + "/" + str(issue["number"]) + "/notes"
        name = str(repo["name"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
            raise ValueError("Invalid connected Bitbucket workspace/repository name")
        base = "/repositories/" + name
        if not issue:
            return base + "/issues"
        kind = "pullrequests" if issue["kind"] == "pull_request" else "issues"
        return base + "/" + kind + "/" + str(issue["number"]) + "/comments"

    async def _remote_write(self, repo: Data, payload: Data, issue: Data | None = None) -> Data:
        provider = str(repo["connection_provider"])
        token, api = await self.scm.issue_authentication(
            repo, pull_request=bool(issue and issue["kind"] == "pull_request")
        )
        if self.deployments is not None and self.deployments.engine.poller is not None:
            await self.store.assert_ownership()
        response = await self.scm._request(
            "POST",
            api + self._issue_path(provider, repo, issue),
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            json=payload,
        )
        result: Data = response.json()
        return {
            "id": result.get("id"),
            "number": result.get("number", result.get("iid", result.get("id"))),
            "url": result.get("html_url", result.get("web_url", result.get("links", {}).get("html", {}).get("href"))),
        }

    async def feedback(self, receipt_id: str) -> None:
        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.data.get("feedback_status") != "pending":
                return
            data = copy.deepcopy(row.data)
            await self._write_row(row, feedback_status="writing")
            self._active_writes.add(receipt_id)
        try:
            repo = await self.store.repository(data["repository_id"])
            run = await self.store.get_run(data["run_id"]) if data.get("run_id") else {}
            message = f"Forge run {data['run_id']}: {data['feedback_stage']}. Receipt {receipt_id}."
            if data.get("action_result"):
                from firefly_forge.configuration import redact_secrets
                from firefly_forge.secrets import redact_environment

                encoded = json.dumps(data["action_result"], ensure_ascii=True)
                summary = redact_environment(redact_secrets(encoded, self.scm.redaction_settings()))
                message = (
                    "Forge "
                    + data["command"]["action"]
                    + ": "
                    + str(data["action_result"].get("status", "completed"))
                    + ". Receipt "
                    + receipt_id
                    + ".\n"
                )
                if data["command"]["action"] == "assistant":
                    message += (
                        "Any product proposal remains UNREVIEWED; no run, approval or source change was requested.\n"
                    )
                message += summary[:5000]
                if run:
                    message += "\nCurrent run status: " + run["status"] + "."
                if len(summary) > 5000:
                    message += "\nSummary shortened; the receipt retains the full action result."
                reference = data["action_result"].get("remote_reference") or {}
                if reference.get("published") and reference.get("url"):
                    message += "\nReference files: " + reference["url"]
                message = message[:6000]
            worker_request = run.get("worker_request", {})
            if worker_request.get("status") == "pending":
                from firefly_forge.configuration import redact_secrets
                from firefly_forge.secrets import redact_environment

                question = redact_environment(
                    redact_secrets(str(worker_request["question"]), self.scm.redaction_settings())
                )
                message += "\nWorker request " + worker_request["id"] + ": " + question[:4000]
                if worker_request.get("choices"):
                    choices = redact_environment(
                        redact_secrets(json.dumps(worker_request["choices"]), self.scm.redaction_settings())
                    )
                    message += "\nChoices: " + choices[:2000]
                message += "\nA configuration actor can answer with /forge respond using run_id, request_id and answer."
            checks = run.get("final_verification", [])
            if checks:
                passed = sum(check.get("exit_code") == 0 for check in checks)
                message += f" Verification: {passed}/{len(checks)} checks passed."
            branch = run.get("branch", "")
            if re.fullmatch(r"forge/[a-f0-9]+", branch):
                message += " Branch: " + branch + "."
            if run.get("pr_url"):
                url = urlsplit(run["pr_url"])
                host = urlsplit(self.scm._host(data["provider"]))
                if url.scheme == "https" and url.netloc == host.netloc and not url.username and not url.query:
                    message += " Review: " + run["pr_url"]
            if data.get("deployment_id"):
                message += " Deployment " + data["deployment_id"] + "."
            payload = {"content": {"raw": message}} if data["provider"] == "bitbucket" else {"body": message}
            remote = await self._remote_write(repo, payload, data["issue"])
        except Exception:
            async with self.store._unit():
                row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                assert row is not None
                await self._write_row(
                    row, feedback_status="needs_reconciliation", reason="remote_feedback_outcome_unknown"
                )
        else:
            async with self.store._unit():
                row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                assert row is not None
                await self._write_row(
                    row,
                    feedback_status="sent",
                    feedback_remote=remote,
                    feedback_request_id=run.get("worker_request", {}).get("id"),
                )
        finally:
            self._active_writes.discard(receipt_id)

    async def reconcile(
        self, receipt_id: str, outcome: Literal["posted", "not_posted", "completed", "not_completed"], note: str
    ) -> Data:
        if (
            not note.strip()
            or len(note) > 2000
            or outcome not in {"posted", "not_posted", "completed", "not_completed"}
        ):
            raise ValueError("Record a bounded note describing the checked provider outcome")
        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Delivery receipt not found")
            if outcome in {"completed", "not_completed"}:
                if row.status != "needs_reconciliation" or row.data.get("action_status") not in {
                    "writing",
                    "needs_reconciliation",
                }:
                    raise ValueError("Only an uncertain channel action can be reconciled")
                await self._write_row(
                    row,
                    status="processed" if outcome == "completed" else "denied",
                    reason=None if outcome == "completed" else "operator_confirmed_not_completed",
                    action_status="reconciled_" + outcome,
                    feedback_status="not_requested",
                    reconciliation={"outcome": outcome, "note": note.strip(), "operator": True, "created_at": now()},
                )
                return copy.deepcopy(row.data)
            if row.data.get("feedback_status") != "needs_reconciliation":
                raise ValueError("Only an uncertain remote write can be reconciled")
            await self._write_row(
                row,
                status="processed",
                reason=None,
                feedback_status="sent" if outcome == "posted" else "not_sent",
                reconciliation={"outcome": outcome, "note": note.strip(), "operator": True, "created_at": now()},
            )
            return copy.deepcopy(row.data)

    async def create_issue(
        self, repository_id: str, title: str, body: str, *, idempotency_key: str | None = None
    ) -> Data:
        if not title.strip() or len(title) > 250 or len(body) > 50000:
            raise ValueError("Issue title or body is outside its size limit")
        repo = await self.store.repository(repository_id)
        if repo.get("connection_provider") not in {"github", "gitlab", "bitbucket"}:
            raise ValueError("Issue authoring requires an application-connected repository")
        if repo.get("connection_provider") == "bitbucket":
            raise ValueError("Bitbucket Cloud removed its native issue tracker; use pull request comments instead")
        request_digest = hashlib.sha256(json.dumps([title, body], separators=(",", ":")).encode()).hexdigest()
        key = idempotency_key or request_digest
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", key):
            raise ValueError("Invalid issue idempotency key")
        data = {
            "id": uuid.uuid4().hex,
            "repository_id": repository_id,
            "provider": repo["connection_provider"],
            "delivery_id": "outbound:" + key,
            "digest": request_digest,
            "status": "needs_reconciliation",
            "reason": "issue_write_pending",
            "created_at": now(),
            "updated_at": now(),
            "actor": "operator",
            "issue": None,
            "command": {"action": "author_issue"},
            "run_id": None,
            "feedback_status": "writing",
        }
        async with self.store._unit():
            await self._repo_lock(repository_id)
            existing = await self.inbox.find_delivery(identifier(repository_id), data["provider"], data["delivery_id"])
            if existing is not None:
                if existing.data["digest"] != request_digest:
                    raise ValueError("Issue idempotency key was reused with different content")
                return copy.deepcopy(existing.data)
            await self.inbox.save(
                ForgeWebhookInboxEntity(
                    id=identifier(str(data["id"])),
                    repository_id=identifier(repository_id),
                    provider=data["provider"],
                    delivery_id=data["delivery_id"],
                    source_key=data["delivery_id"],
                    status=data["status"],
                    data=data,
                )
            )
        payload = (
            {"title": title, "content": {"raw": body}}
            if data["provider"] == "bitbucket"
            else {"title": title, "description" if data["provider"] == "gitlab" else "body": body}
        )
        self._active_writes.add(str(data["id"]))
        try:
            remote = await self._remote_write(repo, payload)
        except Exception:
            async with self.store._unit():
                row = await self.inbox.find_by_id(identifier(str(data["id"])), lock=LockMode.PESSIMISTIC_WRITE)
                assert row is not None
                await self._write_row(row, reason="issue_write_outcome_unknown", feedback_status="needs_reconciliation")
        else:
            async with self.store._unit():
                row = await self.inbox.find_by_id(identifier(str(data["id"])), lock=LockMode.PESSIMISTIC_WRITE)
                assert row is not None
                await self._write_row(row, status="processed", reason=None, issue=remote, feedback_status="sent")
        finally:
            self._active_writes.discard(str(data["id"]))
        return await self.receipt(data["id"])

    async def sync_feedback(self) -> None:
        async with self.store._unit():
            rows = [copy.deepcopy(row.data) for row in await self.inbox.find_pending()]
        for data in rows:
            if data["status"] == "received":
                await self.process(data["id"])
            elif data.get("feedback_status") == "pending":
                await self.feedback(data["id"])
            elif data.get("feedback_status") == "writing" and data["id"] not in self._active_writes:
                async with self.store._unit():
                    row = await self.inbox.find_by_id(identifier(str(data["id"])), lock=LockMode.PESSIMISTIC_WRITE)
                    assert row is not None
                    await self._write_row(
                        row, feedback_status="needs_reconciliation", reason="remote_feedback_interrupted"
                    )
            elif data.get("run_id") and data.get("feedback_status") == "sent":
                run = await self.store.get_run(data["run_id"])
                if run["status"] in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "interrupted",
                    "paused",
                    "needs_reconciliation",
                } and (
                    run["status"] != data.get("feedback_stage")
                    or run.get("worker_request", {}).get("id") != data.get("feedback_request_id")
                ):
                    async with self.store._unit():
                        row = await self.inbox.find_by_id(identifier(str(data["id"])), lock=LockMode.PESSIMISTIC_WRITE)
                        assert row is not None
                        await self._write_row(row, feedback_status="pending", feedback_stage=run["status"])
                    await self.feedback(data["id"])

    async def start(self) -> None:
        if self.store.runtime_roles and not self.store.runtime_roles.executes_jobs:
            return
        if self.poller is None:
            self.poller = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self.poller:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)
            self.poller = None

    async def _poll(self) -> None:
        while True:
            await self.store.assert_ownership()
            await self.sync_feedback()
            await asyncio.sleep(1)
