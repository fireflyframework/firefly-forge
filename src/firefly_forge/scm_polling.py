# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native, opt-in GitHub API observations; no fabricated webhook authentication."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from time import time
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from pyfly.kernel.lifecycle import CONSUMER_PHASE
from sqlalchemy import Text, cast, func, select, text

from firefly_forge.access_persistence import (
    ForgeAccessEventEntity,
    ForgeAccessEventRepository,
    ForgeMembershipRepository,
)
from firefly_forge.collaboration_persistence import (
    ForgeCollaborationSettingsRepository,
    ForgeWebhookInboxEntity,
    ForgeWebhookInboxRepository,
)
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, Permission, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_connections import SCMConnections
from firefly_forge.scm_poll_models import (
    GitHubPollReferenceV1,
    PollConfigurationV1,
    PollState,
    SCMPollActorBindingInputV2,
    SCMPollActorBindingReceiptV2,
)
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]
STATE_KEY = "_github_poll_v1"
CONFIG_KIND = "github_poll_configuration_v1"
BINDING_KIND = "scm_poll_actor_binding_v2"
TRANSPORT = "github_rest_poll_v1"
MAX_BYTES = 16 * 1024 * 1024


def state_of(data: Data) -> Data:
    state = copy.deepcopy(
        data.get(
            STATE_KEY,
            {
                "version": 1,
                "configuration": None,
                "config_requests": {},
                "heads": {},
                "requests": {},
                "cursor": None,
            },
        )
    )
    try:
        if len(json.dumps(state, ensure_ascii=False).encode()) > MAX_BYTES:
            raise ValueError
        return PollState.model_validate(state).model_dump(mode="json")
    except (TypeError, ValueError):
        raise ValueError("The polling index or cursor is unavailable; preserve it for review") from None


def fresh_cursor(*, ready: bool = False, sequence: int = 0) -> Data:
    return dict(
        epoch=uuid4().hex,
        phase="issues",
        page=1,
        pages=0,
        bytes=0,
        issues={},
        ready=ready,
        sequence=sequence,
        conflicts=0,
        retry_at=None,
        next_due_at=None,
        last_success_at=None,
        last_error=None,
    )


def sealed(data: Data) -> Data:
    return {**data, "digest": digest(data)}


def poll_key(project: str, reference: Data) -> str:
    return digest(
        {
            "project_id": project,
            "transport": TRANSPORT,
            **{
                k: reference[k]
                for k in (
                    "provider",
                    "api_origin",
                    "installation_id",
                    "remote_repository_id",
                    "external_actor_id",
                )
            },
        }
    )


@service
class SCMPolling:
    phase = CONSUMER_PHASE + 3

    def __init__(
        self,
        store: Store,
        scm: SCMConnections,
        settings: ForgeCollaborationSettingsRepository | None = None,
        inbox: ForgeWebhookInboxRepository | None = None,
        events: ForgeAccessEventRepository | None = None,
    ) -> None:
        self.store, self.scm = store, scm
        self.settings = settings or ForgeCollaborationSettingsRepository()
        self.inbox = inbox or ForgeWebhookInboxRepository()
        self.events = events or ForgeAccessEventRepository()
        self.users, self.memberships = ForgeIdentityUserRepository(), ForgeMembershipRepository()
        self.authority = SCMActorAuthority(store, scm)
        self.access = ProjectAccess(store)
        self.collaboration: Any = None
        self.poller: asyncio.Task[None] | None = None

    async def _state(self, repository: str) -> tuple[Any, Data]:
        row = await self.settings.find_for_repository(identifier(repository))
        if row is None:
            raise AccessDenied("Configure native repository collaboration before polling")
        return row, state_of(row.data)

    async def _event(self, pointer: Data, kind: str, project: str, repository: str) -> Data:
        async with self.store._unit() as session:
            size = await session.scalar(
                select(func.octet_length(cast(ForgeAccessEventEntity.data, Text))).where(
                    ForgeAccessEventEntity.id == identifier(pointer["event_id"])
                )
            )
            if size is None or int(size) > 65536:
                raise ValueError("The polling receipt is unavailable or exceeds 64 KiB")
            row = await self.events.find_by_id(identifier(pointer["event_id"]))
            if (
                row is None
                or row.project_id.hex != project
                or set(row.data) != {"kind", "receipt"}
                or row.data["kind"] != kind
            ):
                raise ValueError("The immutable polling receipt scope changed")
            value = copy.deepcopy(row.data["receipt"])
        if (
            value.get("event_id") != pointer["event_id"]
            or value.get("digest") != pointer["digest"]
            or value.get("project_id") != project
            or value.get("repository_id") != repository
            or digest({k: v for k, v in value.items() if k != "digest"}) != value.get("digest")
        ):
            raise ValueError("The polling receipt binding changed")
        if kind == BINDING_KIND:
            SCMPollActorBindingReceiptV2.model_validate(value)
            if (
                digest(value["request"]) != value["request_digest"]
                or value["previous_digest"] != value["request"]["expected_previous_digest"]
                or value["project_binding_digest"] != value["request"]["expected_project_binding_digest"]
                or value["binding_key"] != poll_key(project, value["verified_reference"])
                or digest(value["verified_reference"]) != value["request"]["expected_reference_digest"]
            ):
                raise ValueError("The polling consent request digest or scope changed")
            observation = await self._observation(project, repository, value["request"]["observed_receipt_id"])
            if observation["verification"] != value["verified_reference"]:
                raise ValueError("The historical polling consent observation changed")
        return dict(value)

    async def _save_event(self, kind: str, receipt: Data) -> Data:
        envelope = {"kind": kind, "receipt": receipt}
        self.store.reject_credentials(envelope)
        if len(json.dumps(envelope, ensure_ascii=False).encode()) > 65536:
            raise ValueError("The polling receipt exceeds 64 KiB")
        async with self.store._unit() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(func.octet_length(cast(ForgeAccessEventEntity.data, Text))), 0)).where(
                    ForgeAccessEventEntity.data["kind"].as_string().in_([CONFIG_KIND, BINDING_KIND]),
                    ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == receipt["repository_id"],
                )
            )
            if int(total or 0) + len(json.dumps(envelope, ensure_ascii=False).encode()) > MAX_BYTES:
                raise ValueError("Polling receipt retention is full")
            await self.events.save(
                ForgeAccessEventEntity(
                    id=identifier(receipt["event_id"]), project_id=identifier(receipt["project_id"]), data=envelope
                )
            )
        return {"event_id": receipt["event_id"], "digest": receipt["digest"]}

    async def _latest(self, project: str, repository: str, kind: str, key: str | None = None) -> str | None:
        event = ForgeAccessEventEntity
        query = select(event.id).where(
            event.data["kind"].as_string() == kind,
            event.data["receipt"]["repository_id"].as_string() == repository,
        )
        if kind != CONFIG_KIND:
            query = query.where(event.project_id == identifier(project))
        if key is not None:
            query = query.where(event.data["receipt"]["binding_key"].as_string() == key)
        async with self.store._unit() as session:
            latest = await session.scalar(
                query.order_by(event.data["receipt"]["revision"].as_integer().desc()).limit(1)
            )
        return latest.hex if latest is not None else None

    async def _head(self, project: str, repository: str, key: str) -> Data:
        async with self.store._unit():
            _, state = await self._state(repository)
        pointer = state["heads"].get(key)
        if not pointer or await self._latest(project, repository, BINDING_KIND, key) != pointer["event_id"]:
            raise AccessDenied("The polling consent head does not match immutable history")
        value = await self._event(pointer, BINDING_KIND, project, repository)
        if value["binding_key"] != key:
            raise AccessDenied("The polling consent head scope changed")
        return value

    async def _policy(self, repository: str) -> tuple[Data, Data]:
        async with self.store._unit():
            _, state = await self._state(repository)
            pointer = state["configuration"]
        if not pointer:
            raise AccessDenied("GitHub polling is not configured")
        if await self._latest(pointer["project_id"], repository, CONFIG_KIND) != pointer["event_id"]:
            raise AccessDenied("The polling configuration does not match immutable history")
        policy = await self._event(pointer, CONFIG_KIND, pointer["project_id"], repository)
        PollConfigurationV1.model_validate(policy["request"])
        if digest(policy["request"]) != policy["request_digest"]:
            raise ValueError("The polling configuration request changed")
        return policy, state

    async def _scope(self, repository: str, policy: Data) -> Data:
        repo = await self.store.repository(repository)
        scope = self.scm.delegation_scope(repo)
        if scope["provider"] != "github" or scope != policy["scope"]:
            raise AccessDenied("The configured GitHub application or repository changed; review polling setup")
        actor = policy["admin_snapshot"]
        async with self.store._unit():
            user = await self.users.find_by_id(identifier(actor["user_id"]))
            if user is None or not user.enabled:
                raise AccessDenied("The polling configuration administrator is unavailable")
            if not await self.access.authorize_actor(actor, policy["project_id"], "members"):
                raise AccessDenied("Current factory administrator authority is required for polling")
            project = await self.authority._project(policy["project_id"], repository)
            if digest(project) != policy["request"]["expected_project_binding_digest"]:
                raise AccessDenied("The configured project cut changed; review polling setup")
            row, current = await self._state(repository)
            if current["configuration"] != {
                "event_id": policy["event_id"],
                "digest": policy["digest"],
                "project_id": policy["project_id"],
            }:
                raise AccessDenied("The polling configuration changed during authorization")
            if not row.data.get("enabled") or not policy["request"]["enabled"]:
                raise AccessDenied("GitHub polling or repository collaboration is disabled")
        return repo

    @asynccontextmanager
    async def _locked(
        self, project: str, repository: str, user_ids: list[str], *, previous_project: str | None = None
    ) -> AsyncIterator[None]:
        async with self.store._unit() as session:
            for scope in sorted({value for value in (project, previous_project) if value is not None}):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                    {"key": self.store.schema + ":members:" + scope},
                )
            for user_id in sorted(set(user_ids)):
                user = await self.users.find_by_id(identifier(user_id), lock=LockMode.PESSIMISTIC_WRITE)
                if user is None or not user.enabled:
                    raise AccessDenied("The current native identity is unavailable")
            if await ForgeProjectRepository().find_by_id(identifier(project), lock=LockMode.PESSIMISTIC_WRITE) is None:
                raise AccessDenied("The current project is unavailable")
            await self.collaboration._repo_lock(repository)
            yield

    async def _return(self, actor: Any, project: str, repository: str, value: Data, *, admin: bool = False) -> Data:
        await self.store.admit_credentials(value)
        await self.authority._reader(actor, project, repository, admin=admin)
        self.store.reject_credentials(value)
        return copy.deepcopy(value)

    async def setup(self, project: str, repository: str) -> Data:
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=True)
        project_cut = await self.authority._project(project, repository)
        scope = self.scm.delegation_scope(await self.store.repository(repository))
        if scope["provider"] != "github":
            raise AccessDenied("This polling adapter supports GitHub issue comments only")
        async with self.store._unit():
            row, state = await self._state(repository)
            enabled = bool(row.data.get("enabled"))
        previous = await self._policy(repository) if state["configuration"] else None
        result = dict(
            project_id=project,
            repository_id=repository,
            project_binding_digest=digest(project_cut),
            connection_revision=scope["connection_revision"],
            expected_previous_digest=previous[0]["digest"] if previous else None,
            collaboration_enabled=enabled,
            supported_actions=["respond", "status"],
            minimum_interval_seconds=60,
            provider_grant="not_observed",
        )
        result = await self._return(actor, project, repository, result, admin=True)
        if (
            digest(await self.authority._project(project, repository)) != result["project_binding_digest"]
            or self.scm.delegation_scope(await self.store.repository(repository)) != scope
        ):
            raise AccessDenied("The polling setup cut changed before return")
        return result

    async def configure(self, project: str, repository: str, request: PollConfigurationV1) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Configure polling outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=True)
        assert actor is not None
        incoming = request.model_dump(mode="json")
        await self.store.admit_credentials(incoming)
        async with self.store._unit():
            _, state = await self._state(repository)
        if (pointer := state["config_requests"].get(request.request_id)) is not None:
            retained = await self._event(pointer, CONFIG_KIND, project, repository)
            if retained["request"] != incoming or retained["administering_user_id"] != actor.id.hex:
                raise AccessDenied("The configuration request belongs to another actor or input")
            return await self._return(actor, project, repository, retained, admin=True)
        previous = await self._policy(repository) if state["configuration"] else None
        previous_pointer = copy.deepcopy(state["configuration"])
        if (previous_pointer["digest"] if previous_pointer else None) != request.expected_previous_digest:
            raise ValueError("Polling configuration conflict; preserve the request identity")
        project_cut = await self.authority._project(project, repository)
        repo = await self.store.repository(repository)
        scope = self.scm.delegation_scope(repo)
        if (
            scope["provider"] != "github"
            or scope["connection_revision"] != request.expected_connection_revision
            or digest(project_cut) != request.expected_project_binding_digest
        ):
            raise AccessDenied("Select the exact current GitHub connection and project cut")
        if (await self.scm.github_poll_read(repo, "issues")).status_code != 200:
            raise AccessDenied("The current GitHub issue read grant is unavailable")
        async with self._locked(
            project, repository, [actor.id.hex], previous_project=previous[0]["project_id"] if previous else None
        ):
            await self.authority._reader(actor, project, repository, admin=True)
            row, state = await self._state(repository)
            if state["configuration"] != previous_pointer:
                raise ValueError("Polling configuration changed during admission; preserve the request identity")
            if (
                self.scm.delegation_scope(await self.store.repository(repository)) != scope
                or digest(await self.authority._project(project, repository)) != request.expected_project_binding_digest
            ):
                raise AccessDenied("The reviewed polling configuration changed")
            if state["config_requests"].get(request.request_id):
                retained = await self._event(
                    state["config_requests"][request.request_id], CONFIG_KIND, project, repository
                )
                if retained["request"] != incoming or retained["administering_user_id"] != actor.id.hex:
                    raise AccessDenied("The configuration request belongs to another actor or input")
            else:
                if (
                    state["configuration"]["digest"] if state["configuration"] else None
                ) != request.expected_previous_digest:
                    raise ValueError("Polling configuration conflict; preserve the request identity")
                if len(state["config_requests"]) >= 5000:
                    raise ValueError("Polling configuration retention is full")
                retained = sealed(
                    dict(
                        version=1,
                        event_id=uuid4().hex,
                        project_id=project,
                        repository_id=repository,
                        request=incoming,
                        request_digest=digest(incoming),
                        administering_user_id=actor.id.hex,
                        revision=previous[0]["revision"] + 1 if previous else 1,
                        admin_snapshot=ProjectAccess.actor_snapshot(actor),
                        scope=scope,
                        prepared_at=now(),
                    )
                )
                pointer = await self._save_event(CONFIG_KIND, retained)
                state["configuration"] = {**pointer, "project_id": project}
                state["config_requests"][request.request_id] = pointer
                state["cursor"] = fresh_cursor()
                row.data = {**row.data, STATE_KEY: state}
                await self.settings.save(row)
        return await self._return(actor, project, repository, retained, admin=True)

    async def configuration(self, project: str, repository: str, request_id: str) -> Data:
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        async with self.store._unit():
            _, state = await self._state(repository)
        pointer = state["config_requests"].get(request_id)
        if pointer is None:
            raise KeyError("Polling configuration not found; preserve the original request identity")
        return await self._return(
            actor, project, repository, await self._event(pointer, CONFIG_KIND, project, repository)
        )

    async def heads(self, project: str, repository: str, after: str | None = None) -> Data:
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        if after is not None and not re.fullmatch(r"[a-f0-9]{64}", after):
            raise ValueError("Use an exact polling binding key cursor")
        async with self.store._unit():
            _, state = await self._state(repository)
        keys = sorted(k for k, p in state["heads"].items() if p.get("project_id") == project)
        if after is not None and after not in keys:
            raise ValueError("The polling head cursor is unavailable")
        keys = [key for key in keys if after is None or key > after]
        items = []
        for key in keys[:50]:
            receipt = await self._head(project, repository, key)
            items.append(receipt)
        return await self._return(
            actor,
            project,
            repository,
            dict(
                project_id=project,
                repository_id=repository,
                heads_only=True,
                items=items,
                next_after=keys[49] if len(keys) > 50 else None,
            ),
        )

    async def status(self, project: str, repository: str) -> Data:
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        policy, state = await self._policy(repository)
        if policy["project_id"] != project:
            raise AccessDenied("Polling belongs to another project")
        cursor = state["cursor"] or fresh_cursor()
        available = True
        try:
            await self._scope(repository, policy)
        except (AccessDenied, KeyError, ValueError):
            available = False
        result = dict(
            project_id=project,
            repository_id=repository,
            configuration_digest=policy["digest"],
            enabled=policy["request"]["enabled"],
            ready=cursor["ready"] and available and not cursor["last_error"],
            baseline_complete=cursor["ready"],
            availability="available" if available and not cursor["last_error"] else "unavailable",
            next_due_at=cursor["next_due_at"],
            phase=cursor["phase"],
            completed_scans=cursor["sequence"],
            source_conflicts=cursor["conflicts"],
            last_success_at=cursor["last_success_at"],
            retry_at=cursor["retry_at"],
            instruction=(
                "Post a new message or reply in the connected GitHub issue after the baseline completes; "
                "older messages are not dispatched."
            ),
        )
        return await self._return(actor, project, repository, result)

    def _source(self, comment: Data, repository: Data) -> Data:
        from firefly_forge.scm_comment_source import github_comment_source

        return github_comment_source(comment, repository, self.scm._api("github"))

    @staticmethod
    def _next_page(response: Any, page: int, remote_repository_id: str) -> int | None:
        link = response.links.get("next", {}).get("url")
        if link is None:
            return None
        parsed, original = urlsplit(link), urlsplit(str(response.request.url))
        query = parse_qs(parsed.query, keep_blank_values=True)
        expected = parse_qs(original.query, keep_blank_values=True)
        paths = {original.path}
        resource = re.fullmatch(r"(.*)/repos/[^/]+/[^/]+(/issues(?:/comments)?)", original.path)
        if resource and re.fullmatch(r"[1-9][0-9]*", remote_repository_id):
            paths.add(resource[1] + "/repositories/" + remote_repository_id + resource[2])
        if (
            (parsed.scheme, parsed.netloc) != (original.scheme, original.netloc)
            or parsed.path not in paths
            or parsed.username
            or parsed.fragment
            or set(query) != set(expected)
            or query.get("page") != [str(page + 1)]
            or any(query[k] != v for k, v in expected.items() if k != "page")
            or page >= 20
        ):
            raise ValueError("Polling pagination left its exact bounded repository resource")
        return page + 1

    async def step(self, repository: str) -> None:
        if current_unit_of_work() is not None:
            raise ValueError("Read provider observations outside a caller transaction")
        await self.store.assert_ownership()
        policy, state = await self._policy(repository)
        cursor = copy.deepcopy(state["cursor"])
        if any(cursor[k] is not None and cursor[k] > time() for k in ("retry_at", "next_due_at")):
            return
        repo = await self._scope(repository, policy)
        response = await self.scm.github_poll_read(repo, cursor["phase"], page=cursor["page"])
        if response.status_code != 200:
            retry = response.headers.get("retry-after")
            reset = response.headers.get("x-ratelimit-reset")
            delay = int(retry) if retry and retry.isdecimal() else None
            if delay is None and response.headers.get("x-ratelimit-remaining") == "0" and reset and reset.isdecimal():
                delay = max(1, int(reset) - int(time()))
            if response.status_code in {403, 429} and delay is not None and 0 < delay <= 86400:
                async with self._locked(policy["project_id"], repository, [policy["administering_user_id"]]):
                    row, current = await self._state(repository)
                    if current["cursor"] != state["cursor"] or current["configuration"] != state["configuration"]:
                        raise AccessDenied("The polling cursor changed while awaiting the provider")
                    current["cursor"]["retry_at"] = time() + delay
                    current["cursor"]["last_error"] = "provider_backoff"
                    row.data = {**row.data, STATE_KEY: current}
                    await self.settings.save(row)
                return
            raise AccessDenied("The provider polling resource is unavailable; preserve the cursor")
        entries = response.json()
        if not isinstance(entries, list) or len(entries) > 50 or any(not isinstance(item, dict) for item in entries):
            raise ValueError("The provider returned an invalid bounded polling page")
        next_page = self._next_page(response, cursor["page"], repo["remote_repository_id"])
        cursor["pages"] += 1
        cursor["bytes"] += len(response.content)
        if cursor["pages"] > 20 or cursor["bytes"] > MAX_BYTES:
            raise ValueError("Polling backlog exceeds the bounded scan; preserve its cursor")
        staged = []
        cursor["last_error"] = None
        cursor["retry_at"] = None
        if cursor["phase"] == "issues":
            for issue in entries:
                number = issue.get("number")
                if type(number) is not int or number <= 0:
                    raise ValueError("The provider returned an invalid issue number")
                cursor["issues"][str(number)] = {"pull_request": "pull_request" in issue}
        else:
            for comment in entries:
                source = self._source(comment, repo)
                withheld = False
                try:
                    await self.store.admit_credentials(source)
                except ValueError:
                    try:
                        self.store.reject_credentials(source)
                    except ValueError:
                        withheld = True
                    else:
                        raise
                if str(source["issue_number"]) not in cursor["issues"]:
                    issue = await self.scm.github_poll_read(repo, "issue", value=str(source["issue_number"]))
                    if issue.status_code != 200 or not isinstance(issue.json(), dict):
                        raise AccessDenied("The observed issue is unavailable; preserve the page")
                    if issue.json().get("number") != source["issue_number"]:
                        raise AccessDenied("The observed issue identity changed")
                    cursor["issues"][str(source["issue_number"])] = {"pull_request": "pull_request" in issue.json()}
                staged.append((source, digest(source), withheld))
        await self.store.admit_credentials(
            {"sources": [digest(source) if withheld else source for source, _, withheld in staged]}
        )
        scope = self.scm.delegation_scope(repo)
        await self.store.assert_ownership()
        async with self._locked(policy["project_id"], repository, [policy["administering_user_id"]]):
            await self.store.assert_ownership()
            await self._scope(repository, policy)
            row, current = await self._state(repository)
            if current["cursor"] != state["cursor"] or current["configuration"] != state["configuration"]:
                raise AccessDenied("The polling cursor or configuration changed during the provider read")
            for source, source_digest, withheld in staged:
                prior = await self.inbox.find_source(
                    identifier(repository), "github", "comment:" + source["comment_id"]
                )
                if prior is not None:
                    if (
                        (prior.data.get("verification") or {}).get("transport") == TRANSPORT
                        and prior.data.get("digest") != source_digest
                        and not prior.data.get("source_conflict")
                    ):
                        changed = {
                            **prior.data,
                            "source_conflict": True,
                            "conflicting_source_digest": source_digest,
                            "source_conflict_observed_at": now(),
                        }
                        _, retained_bytes = await inbox_size(self.store, repository)
                        if (
                            retained_bytes
                            + len(json.dumps(changed, ensure_ascii=False).encode())
                            - len(json.dumps(prior.data, ensure_ascii=False).encode())
                            > MAX_BYTES
                        ):
                            raise ValueError("Polling inbox retention is full")
                        self.store.reject_credentials(changed)
                        prior.data = changed
                        await self.inbox.save(prior)
                        cursor["conflicts"] += 1
                    continue
                reason = "baseline_existing" if not cursor["ready"] else None
                issue = cursor["issues"].get(str(source["issue_number"]))
                if issue is None or issue["pull_request"]:
                    reason = "unsupported_poll_issue"
                if withheld:
                    reason = "sensitive_content_withheld"
                if source["author_type"] != "User":
                    reason = "unsupported_poll_author"
                command = None
                if reason is None or reason == "unsupported_poll_issue" and issue is not None and issue["pull_request"]:
                    from firefly_forge.collaboration import RepositoryCommand

                    try:
                        parsed = RepositoryCommand.parse(
                            source["body"],
                            bot_addresses=self.scm.bot_addresses("github"),
                            functional_dialogue=False,
                        )
                        version = getattr(parsed, "version", None)
                        natural_status = getattr(parsed, "adapter", None) == "addressed_status_v1"
                        if issue is not None and issue["pull_request"] and not natural_status:
                            raise ValueError("Only the read-only addressed adapter supports polling pull requests")
                        if not (
                            parsed.action in {"respond", "status"}
                            and version is None
                            or natural_status
                            or parsed.action == "assistant"
                            and getattr(parsed, "operation", None) in {"start", "status", "turn"}
                            or parsed.action == "resume"
                            and version == 2
                            or parsed.action == "source_edit"
                            and version == 1
                        ):
                            raise ValueError("Unsupported polling action")
                        command = parsed.model_dump(exclude_none=True)
                        reason = "baseline_existing" if not cursor["ready"] else None
                    except (ValueError, TypeError, AttributeError):
                        try:
                            reply = await self._natural_reply_source(
                                repository, source, scope, bool(issue and issue["pull_request"])
                            )
                            command = reply.model_dump(exclude_none=True) if reply is not None else None
                        except (AccessDenied, ValueError, KeyError, TypeError):
                            command = None
                        reason = None if command else "not_an_enabled_poll_command"
                proof = GitHubPollReferenceV1.model_validate(
                    {
                        **scope,
                        "version": 1,
                        "transport": TRANSPORT,
                        "external_actor_id": source["author_id"],
                        "resource_id": source["comment_id"],
                        "issue_number": source["issue_number"],
                        "resource_updated_at": source["updated_at"],
                        "payload_digest": source_digest,
                        "response_sha256": hashlib.sha256(response.content).hexdigest(),
                        "observed_at": now(),
                    }
                ).model_dump(mode="json")
                value = dict(
                    id=uuid4().hex,
                    repository_id=repository,
                    provider="github",
                    remote_id=scope["remote_repository_id"],
                    delivery_id="poll:" + source["comment_id"] + ":" + source_digest,
                    digest=source_digest,
                    actor=source["author_id"],
                    issue={
                        "kind": "pull_request"
                        if command and command.get("adapter") and issue and issue["pull_request"]
                        else "issue",
                        "number": source["issue_number"],
                    },
                    command=command,
                    source_snapshot=None if withheld else source,
                    verification=proof,
                    status="ignored" if reason else "received",
                    reason=reason,
                    created_at=now(),
                    updated_at=now(),
                    run_id=None,
                    feedback_status="not_requested",
                    global_ingress=False,
                )
                self.store.reject_credentials(value)
                count, retained_bytes = await inbox_size(self.store, repository)
                if count >= 5000 or retained_bytes + len(json.dumps(value, ensure_ascii=False).encode()) > MAX_BYTES:
                    raise ValueError("Polling inbox retention is full")
                await self.inbox.save(
                    ForgeWebhookInboxEntity(
                        id=identifier(value["id"]),
                        repository_id=identifier(repository),
                        provider="github",
                        delivery_id=value["delivery_id"],
                        source_key="comment:" + source["comment_id"],
                        status=value["status"],
                        data=value,
                    )
                )
            if next_page is not None:
                cursor["page"] = next_page
            elif cursor["phase"] == "issues":
                cursor["phase"], cursor["page"] = "comments", 1
            else:
                conflicts = cursor["conflicts"]
                cursor = fresh_cursor(ready=True, sequence=cursor["sequence"] + 1)
                cursor["conflicts"] = conflicts
                cursor["last_success_at"] = now()
                cursor["next_due_at"] = time() + policy["request"]["interval_seconds"]
            current["cursor"] = cursor
            row.data = {**row.data, STATE_KEY: current}
            self.store.reject_credentials(row.data)
            await self.settings.save(row)

    async def candidates(self, project: str, repository: str) -> Data:
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=True)
        async with self.store._unit():
            rows = await self.inbox.find_latest(identifier(repository))
        items = [
            {**copy.deepcopy(r.data), "reference_digest": digest(r.data["verification"])}
            for r in rows
            if (r.data.get("verification") or {}).get("transport") == TRANSPORT
        ]
        return await self._return(
            actor,
            project,
            repository,
            {
                "project_id": project,
                "repository_id": repository,
                "project_binding_digest": digest(await self.authority._project(project, repository)),
                "items": items[:50],
            },
            admin=True,
        )

    async def _fresh(self, delivery: Data, repository: Data) -> None:
        source = delivery["source_snapshot"]
        if source["author_type"] != "User":
            raise AccessDenied("Only an API-observed user account can receive polling authority")
        response = await self.scm.github_poll_read(repository, "comment", value=source["comment_id"])
        if response.status_code != 200 or self._source(response.json(), repository) != source:
            raise AccessDenied("The exact GitHub comment or author changed; publish a new comment")
        issue = await self.scm.github_poll_read(repository, "issue", value=str(source["issue_number"]))
        addressed = (delivery.get("command") or {}).get("adapter") in {
            "addressed_status_v1",
            "retained_question_reply_v1",
        }
        resource = issue.json() if issue.status_code == 200 else {}
        if (
            issue.status_code != 200
            or not isinstance(resource, dict)
            or "pull_request" in resource
            and not addressed
            or addressed
            and ("pull_request" in resource) != (delivery["issue"]["kind"] == "pull_request")
            or resource.get("number") != source["issue_number"]
        ):
            raise AccessDenied("The current GitHub ticket is unavailable or its qualified resource kind changed")
        permissions = await self.scm.github_poll_read(repository, "permission", value=source["author_login"])
        if permissions.status_code != 200:
            raise AccessDenied("Current GitHub account permission is unavailable")
        data = permissions.json()
        if (
            type(data.get("user", {}).get("id")) is not int
            or str(data["user"]["id"]) != source["author_id"]
            or data.get("permission") not in {"read", "write", "admin"}
        ):
            raise AccessDenied("The GitHub author no longer has current repository access")
        if (delivery.get("command") or {}).get("adapter") == "retained_question_reply_v1":
            for reference in delivery["command"]["question_refs"]:
                response = await self.scm.github_poll_read(
                    repository, "comment", value=reference["publication_comment_id"]
                )
                current = self._source(response.json(), repository) if response.status_code == 200 else None
                if (
                    current is None
                    or current["issue_number"] != source["issue_number"]
                    or hashlib.sha256(current["body"].encode()).hexdigest() != reference["publication_body_sha256"]
                    or datetime.fromisoformat(current["created_at"].replace("Z", "+00:00"))
                    >= datetime.fromisoformat(source["created_at"].replace("Z", "+00:00"))
                ):
                    raise AccessDenied("The exact published question changed or did not precede this human reply")

    async def _natural_reply_source(
        self,
        repository: str,
        source: Data,
        reference: Data,
        pull_request: bool,
        receipt_id: str | None = None,
        expected: Any = None,
    ) -> Any:
        from firefly_forge.scm_natural_tasks import SCMNaturalTasks

        if self.collaboration is None or source["author_type"] != "User":
            return None
        policy, state = await self._policy(repository)
        project = policy["project_id"]
        await self._scope(repository, policy)
        binding = await self._head(
            project, repository, poll_key(project, {**reference, "external_actor_id": source["author_id"]})
        )
        request = binding["request"]
        project_cut = await self.authority._project(project, repository, request["component_ids"])
        async with self.store._unit():
            row, current = await self._state(repository)
            user = await self.users.find_by_id(identifier(request["native_user_id"]))
            memberships = await self.memberships.for_user(user.id) if user is not None else []
            if (
                user is None
                or not user.enabled
                or not request["active"]
                or "status" not in request["allowed_actions"]
                or binding["project_binding_digest"] != digest(project_cut)
                or current["configuration"] != state["configuration"]
                or current["heads"] != state["heads"]
                or not row.data.get("enabled")
                or source["author_id"] not in row.data.get("trusted_actors", [])
                or "status" not in row.data.get("allowed_commands", [])
                or not any(
                    m.project_id.hex == project and m.active and ProjectAccess.permits(m.role, "read")
                    for m in memberships
                )
            ):
                raise AccessDenied("A retained reply requires the current exact native read binding")
        delivery = dict(
            id=receipt_id or "0" * 32,
            repository_id=repository,
            provider="github",
            actor=source["author_id"],
            issue=dict(kind="pull_request" if pull_request else "issue", number=source["issue_number"]),
        )
        scopes = [
            dict(
                project_id=project,
                repository_id=repository,
                native_user_id=user.id.hex,
                binding_digest=binding["digest"],
                component_ids=request["component_ids"],
            )
        ]
        if expected is not None:
            historical_bindings = {ref.binding_digest for ref in expected.question_refs}
            if len(historical_bindings) != 1 or any(
                ref.native_user_id != user.id.hex for ref in expected.question_refs
            ):
                raise AccessDenied("The retained question's original owner mapping changed")
            # Preserve source provenance under renewal; current read/effect authority is checked separately.
            scopes[0]["binding_digest"] = next(iter(historical_bindings))
        return await SCMNaturalTasks(self.collaboration).reply_source(
            delivery, scopes, source["body"], expected=expected
        )

    async def _observation(self, project: str, repository: str, receipt_id: str) -> Data:
        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id))
            if row is None or row.repository_id.hex != repository:
                raise AccessDenied("Select an API observation from this exact repository")
            delivery = copy.deepcopy(row.data)
        if not isinstance(delivery.get("source_snapshot"), dict):
            raise AccessDenied("The observation payload was withheld; no authority can be granted")
        proof = GitHubPollReferenceV1.model_validate(delivery.get("verification")).model_dump(mode="json")
        if (
            proof["payload_digest"] != delivery.get("digest")
            or digest(delivery["source_snapshot"]) != delivery["digest"]
            or proof["external_actor_id"] != delivery.get("actor")
            or proof["resource_id"] != delivery["source_snapshot"]["comment_id"]
            or proof["issue_number"] != delivery["source_snapshot"]["issue_number"]
            or proof["resource_updated_at"] != delivery["source_snapshot"]["updated_at"]
        ):
            raise AccessDenied("The retained API observation binding changed")
        if delivery.get("natural_parent") is not None:
            from firefly_forge.scm_natural_effects import authorize_projection

            await authorize_projection(self, delivery)
        if delivery.get("natural_product_parent") is not None:
            from firefly_forge.scm_natural_product import validate_product_projection

            parent = await self.collaboration.receipt(delivery["natural_product_parent"]["receipt_id"])
            validate_product_projection(delivery, parent)
        return delivery

    async def configure_binding(self, project: str, repository: str, request: SCMPollActorBindingInputV2) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Configure polling consent outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=True)
        assert actor is not None
        incoming = request.model_dump(mode="json")
        await self.store.admit_credentials(incoming)
        async with self.store._unit():
            _, state = await self._state(repository)
        if pointer := state["requests"].get(project + ":" + request.request_id):
            receipt = await self._event(pointer, BINDING_KIND, project, repository)
            if receipt["request"] != incoming or receipt["administering_user_id"] != actor.id.hex:
                raise AccessDenied("The polling consent request belongs to another actor or input")
            return await self._return(actor, project, repository, receipt, admin=True)
        policy, state = await self._policy(repository)
        if policy["project_id"] != project:
            raise AccessDenied("Configure GitHub polling for this exact project before granting consent")
        repo = await self._scope(repository, policy)
        if not state["cursor"]["ready"]:
            raise AccessDenied("Wait for the complete polling baseline before granting authority")
        delivery = await self._observation(project, repository, request.observed_receipt_id)
        reference = delivery["verification"]
        if delivery.get("source_conflict"):
            raise AccessDenied("The source was edited after observation; select a new comment")
        if digest(reference) != request.expected_reference_digest or any(
            reference[k] != v for k, v in policy["scope"].items()
        ):
            raise AccessDenied("The observed GitHub connection cut changed")
        await self._fresh(delivery, repo)
        project_cut = await self.authority._project(project, repository, request.component_ids)
        if digest(project_cut) != request.expected_project_binding_digest:
            raise AccessDenied("The reviewed project component cut changed")
        await self.store.admit_credentials({"request": incoming, "reference": reference})
        key = poll_key(project, reference)
        async with self._locked(project, repository, [actor.id.hex, request.native_user_id]):
            await self.authority._reader(actor, project, repository, admin=True)
            await self._scope(repository, policy)
            if (
                digest(await self.authority._project(project, repository, request.component_ids))
                != request.expected_project_binding_digest
            ):
                raise AccessDenied("The reviewed component cut changed during admission")
            if await self._observation(project, repository, request.observed_receipt_id) != delivery:
                raise AccessDenied("The reviewed API observation changed")
            memberships = await self.memberships.for_user(identifier(request.native_user_id))
            for action in request.allowed_actions:
                permission: Permission = "read" if action == "status" else "execute"
                if not any(
                    r.project_id.hex == project and r.active and ProjectAccess.permits(r.role, permission)
                    for r in memberships
                ):
                    raise AccessDenied("The selected native user lacks current project permission")
            row, current = await self._state(repository)
            if current["configuration"] != state["configuration"]:
                raise AccessDenied("The polling configuration changed during consent")
            old_pointer = current["heads"].get(key)
            old = await self._head(project, repository, key) if old_pointer else None
            replay = current["requests"].get(project + ":" + request.request_id)
            if replay:
                receipt = await self._event(replay, BINDING_KIND, project, repository)
                if receipt["request"] != incoming or receipt["administering_user_id"] != actor.id.hex:
                    raise AccessDenied("The consent request identity changed")
            else:
                if (old["digest"] if old else None) != request.expected_previous_digest:
                    raise ValueError("Polling consent conflict; preserve this request identity")
                if len(current["requests"]) >= 5000 or key not in current["heads"] and len(current["heads"]) >= 100:
                    raise ValueError("Polling consent retention is full")
                revision = old["revision"] + 1 if old else 1
                receipt = sealed(
                    dict(
                        version=2,
                        event_id=uuid4().hex,
                        project_id=project,
                        repository_id=repository,
                        project_binding_digest=digest(project_cut),
                        binding_key=key,
                        revision=revision,
                        previous_digest=request.expected_previous_digest,
                        prepared_at=now(),
                        administering_user_id=actor.id.hex,
                        request=incoming,
                        request_digest=digest(incoming),
                        verified_reference=reference,
                    )
                )
                SCMPollActorBindingReceiptV2.model_validate(receipt)
                pointer = await self._save_event(BINDING_KIND, receipt)
                current["requests"][project + ":" + request.request_id] = pointer
                current["heads"][key] = {**pointer, "project_id": project}
                row.data = {**row.data, STATE_KEY: current}
                await self.settings.save(row)
        return await self._return(actor, project, repository, receipt, admin=True)

    async def binding(self, project: str, repository: str, request_id: str) -> Data:
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        async with self.store._unit():
            _, state = await self._state(repository)
        pointer = state["requests"].get(project + ":" + request_id)
        if not pointer:
            raise KeyError("Polling consent not found; absence does not permit another write")
        return await self._return(
            actor, project, repository, await self._event(pointer, BINDING_KIND, project, repository)
        )

    async def conversation_scope(self, delivery: Data, *, remote: bool = True) -> Data:
        """Authorize an observed status address without inventing a run or execution principal."""
        from firefly_forge.scm_conversation_models import AddressedStatusV1, ConversationReadScopeV1, addressed_status
        from firefly_forge.scm_natural_replies import RetainedQuestionReplyV1

        retained = (delivery.get("command") or {}).get("adapter") == "retained_question_reply_v1"
        message = (
            RetainedQuestionReplyV1.model_validate(delivery["command"])
            if retained
            else AddressedStatusV1.model_validate(delivery.get("command"))
        )
        repository = delivery["repository_id"]
        policy, state = await self._policy(repository)
        project = policy["project_id"]
        repo = await self._scope(repository, policy)
        original = await self._observation(project, repository, delivery["id"])
        if original != delivery or delivery.get("source_conflict") or not state["cursor"]["ready"]:
            raise AccessDenied("The retained status observation or polling baseline changed")
        parsed = (
            await self._natural_reply_source(
                repository,
                delivery["source_snapshot"],
                delivery["verification"],
                delivery["issue"]["kind"] == "pull_request",
                delivery["id"],
                expected=message,
            )
            if retained
            else addressed_status(delivery["source_snapshot"]["body"], self.scm.bot_addresses("github"))
        )
        if parsed != message:
            raise AccessDenied("The status address no longer binds its exact original human comment")
        reference = delivery["verification"]
        key = poll_key(project, reference)
        if key not in state["heads"]:
            raise AccessDenied("Current API-observed status consent is required")
        receipt = await self._head(project, repository, key)
        request = receipt["request"]
        current_project = await self.authority._project(project, repository, request["component_ids"])
        if (
            not request["active"]
            or "status" not in request["allowed_actions"]
            or receipt["project_binding_digest"] != digest(current_project)
            or any(
                receipt["verified_reference"][k] != reference[k]
                for k in (
                    "provider",
                    "api_origin",
                    "installation_id",
                    "remote_repository_id",
                    "external_actor_id",
                    "connection_revision",
                    "connection_scope_digest",
                )
            )
        ):
            raise AccessDenied("The status consent is revoked or outside the current native project scope")
        if remote:
            await self._fresh(delivery, repo)
        await self._scope(repository, policy)
        if await self._observation(project, repository, delivery["id"]) != original:
            raise AccessDenied("The original status observation changed during authorization")
        async with self.store._unit():
            user = await self.users.find_by_id(identifier(request["native_user_id"]))
            if user is None or not user.enabled:
                raise AccessDenied("The mapped native status reader is unavailable")
            memberships = await self.memberships.for_user(user.id)
            if not any(
                r.project_id.hex == project and r.active and ProjectAccess.permits(r.role, "read") for r in memberships
            ):
                raise AccessDenied("The mapped user lacks current project read permission")
            row, current = await self._state(repository)
            if (
                current["configuration"] != state["configuration"]
                or current["heads"].get(key) != state["heads"].get(key)
                or not row.data.get("enabled")
                or delivery["actor"] not in row.data.get("trusted_actors", [])
                or "status" not in row.data.get("allowed_commands", [])
            ):
                raise AccessDenied("The current status consent, actor or command changed")
        return ConversationReadScopeV1(
            transport="github_rest_poll_v1",
            project_id=project,
            repository_id=repository,
            native_user_id=user.id.hex,
            component_ids=sorted(request["component_ids"]),
            binding_key=key,
            binding_digest=receipt["digest"],
            binding_request_id=request["request_id"],
            verified_receipt_id=delivery["id"],
            verified_reference_digest=digest(reference),
            project_binding_digest=digest(current_project),
            observation_digest=digest(
                {
                    "source": delivery["source_snapshot"],
                    "issue": delivery["issue"],
                    "command": message.model_dump(mode="json"),
                }
            ),
        ).model_dump(mode="json")

    async def ticket_reference(self, delivery: Data, run: Data) -> None:
        """Validate a historical authenticated ticket link; this confers no current authority."""
        project, repository = run["project_id"], run["repository_id"]
        if await self._observation(project, repository, delivery["id"]) != delivery:
            raise AccessDenied("The retained ticket context receipt changed")
        actor = delivery.get("scm_execution_actor") or {}
        context = actor.get("scm_delegation") or {}
        command = delivery.get("command") or {}
        if (
            delivery.get("status") != "processed"
            or delivery.get("source_conflict")
            or delivery.get("action_status") != "completed"
            or context.get("transport") != TRANSPORT
            or context.get("project_id") != project
            or context.get("repository_id") != repository
            or context.get("component_id") != run["component_id"]
            or context.get("run_id") != run["id"]
            or context.get("verified_receipt_id") != delivery["id"]
            or context.get("verified_reference") != delivery["verification"]
            or command.get("run_id") != run["id"]
            or command.get("action") != context.get("action")
            or context.get("action") not in {"respond", "status"}
        ):
            raise AccessDenied("The historical ticket context does not bind this native run")
        async with self.store._unit():
            _, state = await self._state(repository)
        pointer = state["requests"].get(project + ":" + context["binding_request_id"])
        if pointer is None:
            raise AccessDenied("The historical ticket consent receipt is unavailable")
        receipt = await self._event(pointer, BINDING_KIND, project, repository)
        request = receipt["request"]
        if (
            receipt["digest"] != context["binding_digest"]
            or receipt["binding_key"] != context["binding_key"]
            or poll_key(project, delivery["verification"]) != context["binding_key"]
            or request["native_user_id"] != actor.get("user_id")
            or not request["active"]
            or run["component_id"] not in request["component_ids"]
            or context["action"] not in request["allowed_actions"]
        ):
            raise AccessDenied("The historical ticket link lacks its exact native consent")

    async def resolve(self, delivery: Data) -> Principal:
        command = delivery.get("command") or {}
        if command.get("action") not in {"respond", "status"} or not command.get("run_id"):
            raise AccessDenied("Polling only permits scoped answer and status actions")
        run = await self.store.get_run(command["run_id"])
        project, repository = run.get("project_id"), delivery["repository_id"]
        if not project or run["repository_id"] != repository or not run.get("component_id"):
            raise AccessDenied("Polling requires the existing run's explicit project and component")
        policy, state = await self._policy(repository)
        repo = await self._scope(repository, policy)
        if policy["project_id"] != project or not state["cursor"]["ready"]:
            raise AccessDenied("The polling baseline or project scope is unavailable")
        original = await self._observation(project, repository, delivery["id"])
        if original != delivery or original.get("source_conflict"):
            raise AccessDenied("The retained polling delivery changed")
        pointer = state["heads"].get(poll_key(project, delivery["verification"]))
        if pointer is None:
            raise AccessDenied("A fresh API-observed polling consent is required; V1 is not upgraded")
        receipt = await self._head(project, repository, poll_key(project, delivery["verification"]))
        await self._fresh(delivery, repo)
        async with self.store._unit():
            user = await self.users.find_by_id(identifier(receipt["request"]["native_user_id"]))
        if user is None or not user.enabled:
            raise AccessDenied("The mapped native user is unavailable")
        context = dict(
            transport=TRANSPORT,
            project_id=project,
            repository_id=repository,
            component_id=run["component_id"],
            run_id=run["id"],
            action=command["action"],
            binding_key=receipt["binding_key"],
            binding_digest=receipt["digest"],
            binding_request_id=receipt["request"]["request_id"],
            verified_receipt_id=delivery["id"],
            verified_reference=delivery["verification"],
        )
        actor = Principal(
            user.id, user.issuer, user.subject, authentication_method="scm_mapping", scm_delegation=context
        )
        await self.store.admit_credentials({"actor": ProjectAccess.actor_snapshot(actor), "receipt": receipt})
        await self.authorize_snapshot(
            ProjectAccess.actor_snapshot(actor), project, "read" if command["action"] == "status" else "execute"
        )
        if run.get("publish") and command["action"] == "respond":
            await self.authorize_snapshot(ProjectAccess.actor_snapshot(actor), project, "release")
        return actor

    async def authorize_snapshot(self, actor: Data, project: str | None, permission: Permission) -> None:
        context = actor.get("scm_delegation") or {}
        if (
            not isinstance(project, str)
            or context.get("transport") != TRANSPORT
            or context.get("project_id") != project
            or context.get("action") not in {"respond", "status"}
            or permission not in ({"read"} if context["action"] == "status" else {"read", "execute", "release"})
        ):
            raise AccessDenied("The polling principal cannot exercise this authority")
        repository = context["repository_id"]
        policy, state = await self._policy(repository)
        if policy["project_id"] != project:
            raise AccessDenied("The current polling configuration belongs to another exact project")
        await self._scope(repository, policy)
        pointer = state["heads"].get(context["binding_key"])
        if not state["cursor"]["ready"] or pointer is None or pointer["digest"] != context["binding_digest"]:
            raise AccessDenied("Polling consent is unavailable or revoked")
        receipt = await self._head(project, repository, context["binding_key"])
        request = receipt["request"]
        current_project = await self.authority._project(project, repository)
        delivery = await self._observation(project, repository, context["verified_receipt_id"])
        reference = delivery["verification"]
        identity_fields = [
            "provider",
            "api_origin",
            "installation_id",
            "remote_repository_id",
            "external_actor_id",
            "connection_revision",
            "connection_scope_digest",
        ]
        if (
            delivery.get("source_conflict")
            or not request["active"]
            or request["native_user_id"] != actor["user_id"]
            or request["request_id"] != context["binding_request_id"]
            or receipt["project_binding_digest"] != digest(current_project)
            or context["component_id"] not in request["component_ids"]
            or context["action"] not in request["allowed_actions"]
            or context["verified_reference"] != reference
            or poll_key(project, reference) != context["binding_key"]
            or any(receipt["verified_reference"][k] != reference[k] for k in identity_fields)
            or (delivery.get("command") or {}).get("action") != context["action"]
            or (delivery.get("command") or {}).get("run_id") != context["run_id"]
        ):
            raise AccessDenied("Polling authority is revoked, changed or outside its exact reviewed scope")
        async with self.store._unit():
            user = await self.users.find_by_id(identifier(actor["user_id"]))
            if user is None or not user.enabled:
                raise AccessDenied("The delegated native user is unavailable")
            memberships = await self.memberships.for_user(user.id)
            if not any(
                r.project_id.hex == project and r.active and ProjectAccess.permits(r.role, permission)
                for r in memberships
            ):
                raise AccessDenied("The native user's current project permission is insufficient")
            row, current = await self._state(repository)
            if current["configuration"] != {
                "event_id": policy["event_id"],
                "digest": policy["digest"],
                "project_id": project,
            }:
                raise AccessDenied("The polling configuration changed before authorization completed")
            allowed = [*row.data.get("allowed_commands", []), *row.data.get("channel_commands", [])]
            if (
                not row.data.get("enabled")
                or delivery["actor"] not in row.data.get("trusted_actors", [])
                or context["action"] not in allowed
            ):
                raise AccessDenied("The current repository actor or action is not enabled")
            run = await self.store.get_run(context["run_id"])
            if (run.get("project_id"), run["repository_id"], run.get("component_id")) != (
                project,
                repository,
                context["component_id"],
            ):
                raise AccessDenied("The delegated run scope changed")
        self.store.reject_credentials({"actor": actor, "receipt": receipt})

    async def authorize_feedback_local(self, delivery: Data) -> None:
        actor = delivery.get("scm_execution_actor")
        if not actor:
            raise AccessDenied("The original polling action authority is unavailable")
        context = actor["scm_delegation"]
        async with self._locked(context["project_id"], delivery["repository_id"], [actor["user_id"]]):
            await self.authorize_snapshot(
                actor, context["project_id"], "read" if context["action"] == "status" else "execute"
            )
            if context["action"] == "respond" and (await self.store.get_run(context["run_id"])).get("publish"):
                await self.authorize_snapshot(actor, context["project_id"], "release")

    async def authorize_feedback(self, delivery: Data) -> None:
        await self.authorize_feedback_local(delivery)
        await self._fresh(delivery, await self.store.repository(delivery["repository_id"]))
        await self.authorize_feedback_local(delivery)

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
            async with self.store._unit() as session:
                from firefly_forge.collaboration_persistence import ForgeCollaborationSettingsEntity

                rows = list(
                    await session.scalars(
                        select(ForgeCollaborationSettingsEntity)
                        .where(ForgeCollaborationSettingsEntity.data[STATE_KEY].is_not(None))
                        .limit(101)
                    )
                )
            if len(rows) > 100:
                raise ValueError("Too many polling repositories for this bounded worker")
            for row in rows:
                try:
                    await self.step(row.repository_id.hex)
                except (ValueError, AccessDenied, KeyError):
                    async with self.store._unit():
                        await self.collaboration._repo_lock(row.repository_id.hex)
                        current_row, current = await self._state(row.repository_id.hex)
                        if current["cursor"] is not None:
                            current["cursor"]["last_error"] = "poll_unavailable"
                            current_row.data = {**current_row.data, STATE_KEY: current}
                            await self.settings.save(current_row)
            await asyncio.sleep(60)


async def inbox_size(store: Store, repository: str) -> tuple[int, int]:
    async with store._unit() as session:
        clause = ForgeWebhookInboxEntity.repository_id == identifier(repository)
        count = await session.scalar(select(func.count(ForgeWebhookInboxEntity.id)).where(clause))
        size = await session.scalar(
            select(func.coalesce(func.sum(func.octet_length(cast(ForgeWebhookInboxEntity.data, Text))), 0)).where(
                clause
            )
        )
        return int(count or 0), int(size or 0)
