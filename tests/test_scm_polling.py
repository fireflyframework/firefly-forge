# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled host persistence and signed App-token HTTP transport, never live GitHub."""

import asyncio
import copy
import importlib
import importlib.util
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from firefly_forge.collaboration import Collaboration
from firefly_forge.identity import Principal
from firefly_forge.journal import Journal, digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.scm_connections import SCMConnections


def implementation():
    assert importlib.util.find_spec("firefly_forge.scm_polling") is not None, "Native authenticated polling is missing"
    return importlib.import_module("firefly_forge.scm_polling")


@pytest.fixture
def poll_case(tmp_path, monkeypatch):
    @asynccontextmanager
    async def create():
        module = implementation()
        models = importlib.import_module("firefly_forge.scm_poll_models")
        project, repository, run_id, owner_id, user_id = [uuid4().hex for _ in range(5)]
        db = {
            name: {}
            for name in [
                "settings",
                "inbox",
                "events",
                "users",
                "projects",
                "runs",
                "journal",
                "memberships",
                "repositories",
            ]
        }
        state = {
            "depth": 0,
            "fail_save": False,
            "external_permission": "write",
            "edited": False,
            "lost_reply": False,
            "posts": [],
            "requests": [],
            "comments": [],
            "credential": None,
            "clock": time.time(),
        }

        monkeypatch.setattr(module, "time", lambda: state["clock"])

        class Records:
            def __init__(self, kind):
                self.kind = kind

            async def find_by_id(self, value, **kwargs):
                if kwargs.get("lock") is not None:
                    state.setdefault("lock_trace", []).append((self.kind, value.hex))
                return db[self.kind].get(value.hex)

            async def save(self, row):
                if self.kind == "settings" and state["fail_save"]:
                    raise ValueError("Controlled cursor persistence failure")
                if row.id is None:
                    row.id = uuid4()
                db[self.kind][row.id.hex] = row
                return row

            async def find_for_repository(self, value):
                return next((r for r in db[self.kind].values() if r.repository_id == value), None)

            async def find_source(self, repo, provider, source):
                return next(
                    (
                        r
                        for r in db[self.kind].values()
                        if r.repository_id == repo and r.provider == provider and r.source_key == source
                    ),
                    None,
                )

            async def find_delivery(self, repo, provider, delivery):
                return next(
                    (
                        r
                        for r in db[self.kind].values()
                        if r.repository_id == repo and r.provider == provider and r.delivery_id == delivery
                    ),
                    None,
                )

            async def find_latest(self, value=None):
                return list(reversed([r for r in db[self.kind].values() if value is None or r.repository_id == value]))[
                    :200
                ]

            async def find_pending(self):
                return [
                    r
                    for r in db[self.kind].values()
                    if r.status == "received"
                    or r.data.get("feedback_status") in {"pending", "writing"}
                    or r.status == "processed"
                ][:200]

            async def for_user(self, value):
                return [r for r in db[self.kind].values() if r.user_id == value and r.active]

            async def find_for_run(self, value):
                return [r for r in db[self.kind].values() if r.run_id == value]

        records = {name: Records(name) for name in db}

        class Session:
            async def execute(self, *args, **kwargs):
                if len(args) > 1 and "pg_advisory_xact_lock" in str(args[0]):
                    key = args[1]["key"]
                    state.setdefault("lock_trace", []).append(("members", key))
                    if state.get("held_members") == key:
                        state["members_blocked"].set()
                        await state["release_members"].wait()

            async def scalar(self, statement, *args, **kwargs):
                params = list(statement.compile().params.values())
                if "ORDER BY" in str(statement) and "forge_access_events" in str(statement):
                    rows = [r for r in db["events"].values() if r.data["kind"] in params]
                    if "binding_key" in params:
                        rows = [r for r in rows if r.data["receipt"]["binding_key"] in params]
                    return max(rows, key=lambda r: r.data["receipt"]["revision"]).id if rows else None
                for value in params:
                    if isinstance(value, UUID) and value.hex in db["events"]:
                        return len(json.dumps(db["events"][value.hex].data).encode())
                return 0

            async def scalars(self, *args):
                return list(db["settings"].values())

        @asynccontextmanager
        async def unit(**kwargs):
            outer = state["depth"] == 0
            before = copy.deepcopy(db) if outer else None
            state["depth"] += 1
            try:
                yield Session()
            except BaseException:
                if outer:
                    db.clear()
                    db.update(before)
                raise
            finally:
                state["depth"] -= 1

        async def repo(value):
            assert value == repository
            return copy.deepcopy(repo_value)

        async def run(value):
            return copy.deepcopy(db["runs"][value].data)

        async def admit(value):
            assert state["depth"] == 0, "No credential I/O under a native transaction"
            hook = state.get("admit_hook")
            if hook:
                hook(value)
            reject(value)

        def reject(value):
            if state["credential"] and state["credential"] in json.dumps(value):
                raise ValueError("Configured credential withheld")

        async def ownership():
            if state.get("ownership_lost"):
                raise RuntimeError("Controlled ownership loss")

        store = SimpleNamespace(
            _unit=unit,
            repository=repo,
            get_run=run,
            admit_credentials=admit,
            reject_credentials=reject,
            assert_ownership=ownership,
            schema="host_poll",
            path=tmp_path / "factory.db",
            run_records=records["runs"],
            runtime_roles=None,
            scm_polling=None,
            repository_records=records["repositories"],
        )
        repo_value = dict(
            id=repository,
            name="team/product",
            provider="github",
            connection_provider="github",
            remote_repository_id="42",
            url="https://github.com/team/product.git",
            default_branch="main",
        )
        db["repositories"][repository] = SimpleNamespace(id=UUID(repository), data=repo_value)
        definition = {"id": project, "definition": {"components": [{"id": "api", "repository_id": repository}]}}
        db["projects"][project] = SimpleNamespace(id=UUID(project), data=definition)
        owner = Principal(UUID(owner_id), "local:bootstrap", "operator", authentication_method="local_bootstrap")
        for key, issuer, subject in [
            (owner_id, owner.issuer, owner.subject),
            (user_id, "https://identity.invalid", "mapped"),
        ]:
            db["users"][key] = SimpleNamespace(id=UUID(key), issuer=issuer, subject=subject, enabled=True)
        db["memberships"]["member"] = SimpleNamespace(
            user_id=UUID(user_id), project_id=UUID(project), role="developer", active=True
        )
        db["settings"][repository] = SimpleNamespace(
            id=UUID(repository),
            repository_id=UUID(repository),
            data={
                "enabled": True,
                "trusted_actors": ["7"],
                "configuration_actors": [],
                "channel_commands": ["respond"],
                "allowed_commands": ["status"],
                "publish": False,
            },
        )
        db["runs"][run_id] = SimpleNamespace(
            id=UUID(run_id),
            repository_id=UUID(repository),
            status="paused",
            data={
                "id": run_id,
                "repository_id": repository,
                "project_id": project,
                "component_id": "api",
                "status": "paused",
                "publish": False,
                "worker_request": {
                    "id": "question1",
                    "status": "pending",
                    "task_id": "implement",
                    "question": "Keep existing contacts?",
                    "choices": ["Yes", "No"],
                },
            },
        )

        for modname in [
            "firefly_forge.scm_actor_authority",
            "firefly_forge.project_access",
            "firefly_forge.journal",
            "firefly_forge.identity_persistence",
            module.__name__,
        ]:
            target = importlib.import_module(modname)
            for name, kind in [
                ("ForgeIdentityUserRepository", "users"),
                ("ForgeProjectRepository", "projects"),
                ("ForgeMembershipRepository", "memberships"),
            ]:
                if hasattr(target, name):
                    monkeypatch.setattr(target, name, lambda k=kind: records[k])
        import pyfly.data.transaction

        monkeypatch.setattr(
            pyfly.data.transaction, "current_unit_of_work", lambda: object() if state["depth"] else None
        )
        if hasattr(module, "current_unit_of_work"):
            monkeypatch.setattr(module, "current_unit_of_work", lambda: object() if state["depth"] else None)

        def comment(number=10, body="Existing ordinary discussion"):
            return dict(
                id=number,
                body=body,
                user={"id": 7, "login": "alice", "type": "User"},
                issue_url="https://api.github.com/repos/team/product/issues/3",
                created_at="2026-10-15T10:00:00Z",
                updated_at="2026-10-15T10:00:00Z",
            )

        state["comments"] = [comment()]

        def provider(request):
            assert state["depth"] == 0, "Provider requests must not hold database locks"
            state["requests"].append((request.method, request.url.path))
            path = request.url.path
            if request.method == "POST" and path.endswith("/access_tokens"):
                state.setdefault("token_scopes", []).append(json.loads(request.content))
                return httpx.Response(
                    201,
                    json={
                        "token": "fictional-poll-token",
                        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    },
                )
            if path == "/installation/repositories":
                return httpx.Response(
                    200,
                    json={
                        "repositories": [
                            {
                                "id": 42,
                                "full_name": "team/product",
                                "clone_url": repo_value["url"],
                                "default_branch": "main",
                            }
                        ]
                    },
                )
            if path == "/repos/team/product/issues" and state.get("read_status"):
                return httpx.Response(state["read_status"], headers=state.get("read_headers", {}))
            if path == "/repos/team/product/issues":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 100,
                            "number": 3,
                            "title": "Fictional acceptance",
                            "body": "Context",
                            **({"pull_request": {}} if state.get("pull_request") else {}),
                        }
                    ],
                )
            if path == "/repos/team/product/issues/comments":
                return httpx.Response(
                    200, json=copy.deepcopy(state["comments"]), headers=state.get("comment_headers", {})
                )
            if path.startswith("/repos/team/product/issues/comments/"):
                item = copy.deepcopy(next(c for c in state["comments"] if str(c["id"]) == path.rsplit("/", 1)[1]))
                if state["edited"]:
                    item["body"] += " changed"
                return httpx.Response(200, json=item)
            if path == "/repos/team/product/collaborators/alice/permission":
                return httpx.Response(
                    200,
                    json={
                        "permission": state["external_permission"],
                        "user": {"id": state.get("permission_actor", 7), "login": "alice"},
                    },
                )
            if path == "/repos/team/product/issues/3":
                return httpx.Response(200, json={"number": 3, "id": 100})
            if path == "/repos/team/product/issues/3/comments" and request.method == "POST":
                state["posts"].append(json.loads(request.content))
                if state["lost_reply"]:
                    raise httpx.ReadError("Controlled lost reply", request=request)
                return httpx.Response(
                    201, json={"id": 101, "html_url": "https://github.com/team/product/issues/3#issuecomment-101"}
                )
            raise AssertionError("Unexpected provider request " + str(request.url))

        client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        scm = SCMConnections(store, tmp_path, client=client)
        store.configuration = scm.private
        key = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            .decode()
        )
        await scm.configure("github", {"values": {"app_id": "12", "installation_id": "34", "private_key": key}})
        service = module.SCMPolling(store, scm, records["settings"], records["inbox"], records["events"])
        service.authority.settings = records["settings"]
        service.authority.inbox = records["inbox"]
        service.authority.events = records["events"]
        collaboration = Collaboration(store, scm, settings_records=records["settings"], inbox=records["inbox"])
        collaboration.polling = service
        service.collaboration = collaboration
        store.scm_polling = service
        journal = Journal(store, records=records["journal"])
        collaboration.deployments = SimpleNamespace(engine=SimpleNamespace(journal=journal, poller=None))
        token = CURRENT_PRINCIPAL.set(owner)

        async def configure():
            return await service.configure(
                project,
                repository,
                models.PollConfigurationV1(
                    version=1,
                    request_id=uuid4().hex,
                    expected_previous_digest=None,
                    expected_project_binding_digest=digest(definition),
                    expected_connection_revision=scm.delegation_scope(repo_value)["connection_revision"],
                    enabled=True,
                ),
            )

        async def ready():
            await configure()
            for _ in range(3):
                await service.step(repository)
                if (await service.status(project, repository))["ready"]:
                    return
            raise AssertionError("Baseline did not finish")

        async def consent():
            candidates = await service.candidates(project, repository)
            observation = candidates["items"][0]
            request = models.SCMPollActorBindingInputV2(
                version=2,
                request_id=uuid4().hex,
                expected_previous_digest=None,
                expected_project_binding_digest=digest(definition),
                observed_receipt_id=observation["id"],
                expected_reference_digest=digest(observation["verification"]),
                native_user_id=user_id,
                component_ids=["api"],
                allowed_actions=["respond", "status"],
                active=True,
                acknowledge_api_observed_identity=True,
            )
            state["consent_request"] = request
            return await service.configure_binding(project, repository, request)

        async def new_command(action="respond", number=20):
            options = {"run_id": run_id}
            if action == "respond":
                options.update(request_id="question1", answer="Yes, preserve contacts")
            state["clock"] += 3600
            state["comments"].append(comment(number, "/forge " + action + " " + json.dumps(options)))
            for _ in range(3):
                await service.step(repository)
            return next(copy.deepcopy(r.data) for r in db["inbox"].values() if r.source_key == "comment:" + str(number))

        try:
            yield SimpleNamespace(
                service=service,
                scm=scm,
                store=store,
                collaboration=collaboration,
                db=db,
                state=state,
                project=project,
                repository=repository,
                run_id=run_id,
                user_id=user_id,
                configure=configure,
                ready=ready,
                consent=consent,
                new_command=new_command,
                models=models,
                comment=comment,
                definition=definition,
                repo_value=repo_value,
                owner=owner,
            )
        finally:
            CURRENT_PRINCIPAL.reset(token)
            await client.aclose()

    return create


async def test_poll_baseline_records_api_provenance_without_dispatch_or_webhook_claim(poll_case):
    async with poll_case() as c:
        c.state["comments"][0]["body"] = "/forge respond " + json.dumps(
            {"run_id": c.run_id, "request_id": "question1", "answer": "Old command"}
        )
        await c.ready()
        rows = list(c.db["inbox"].values())
        assert len(rows) == 1 and rows[0].status == "ignored"
        proof = rows[0].data["verification"]
        assert proof["transport"] == "github_rest_poll_v1" and proof["external_actor_id"] == "7"
        assert "webhook_key_revision" not in proof and "verification_method" not in proof
        assert not c.db["journal"] and not c.state["posts"]
        assert any(
            v["permissions"] == {"issues": "read"} and v["repository_ids"] == [42] for v in c.state["token_scopes"]
        )


async def test_poll_requires_new_consent_and_does_not_upgrade_v1_binding(poll_case):
    async with poll_case() as c:
        await c.ready()
        row = c.db["settings"][c.repository]
        row.data["_scm_authority_v1"] = {
            "version": 1,
            "heads": {"old": {"event_id": "f" * 32, "digest": "f" * 64}},
            "requests": {},
        }
        delivery = await c.new_command()
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "denied" and not c.db["journal"]
        await c.consent()
        newer = await c.new_command(number=21)
        assert (await c.collaboration.process(newer["id"]))["status"] == "processed"


async def test_poll_page_cursor_and_inbox_rollback_then_restart_without_duplicates(poll_case):
    async with poll_case() as c:
        await c.configure()
        await c.service.step(c.repository)
        before = copy.deepcopy(c.db)
        c.state["fail_save"] = True
        with pytest.raises(ValueError, match="cursor persistence"):
            await c.service.step(c.repository)
        assert set(c.db["inbox"]) == set(before["inbox"])
        assert c.db["settings"][c.repository].data == before["settings"][c.repository].data
        assert {k: r.data for k, r in c.db["events"].items()} == {k: r.data for k, r in before["events"].items()}
        c.state["fail_save"] = False
        await c.service.step(c.repository)
        await c.service.step(c.repository)
        assert len(c.db["inbox"]) == 1 and (await c.service.status(c.project, c.repository))["ready"]


async def test_poll_requeries_exact_author_and_current_permission_before_dispatch(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery = await c.new_command()
        c.state["external_permission"] = "none"
        assert (await c.collaboration.process(delivery["id"]))["status"] == "denied"
        assert not c.db["journal"]
        assert ("GET", "/repos/team/product/collaborators/alice/permission") in c.state["requests"]


async def test_poll_answer_once_records_real_journal_actor_and_keeps_run_paused(poll_case):
    async with poll_case() as c:
        await c.ready()
        grant = await c.consent()
        delivery = await c.new_command()
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "processed"
        assert (await c.collaboration.process(delivery["id"])) == result
        events = [r.data for r in c.db["journal"].values()]
        assert len(events) == 1 and events[0]["kind"] == "worker_response"
        actor = events[0]["payload"]["responding_actor"]
        assert actor["version"] == 3 and actor["user_id"] == c.user_id
        assert actor["provenance"]["binding_digest"] == grant["digest"]
        assert c.db["runs"][c.run_id].data["status"] == "paused"
        assert c.db["runs"][c.run_id].data["worker_request"]["status"] == "answered"
        assert len(c.state["posts"]) == 1


async def test_poll_lost_outbound_reply_is_held_and_never_posted_again(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery = await c.new_command("status")
        c.state["lost_reply"] = True
        result = await c.collaboration.process(delivery["id"])
        assert result["feedback_status"] == "needs_reconciliation"
        c.state["lost_reply"] = False
        await c.collaboration.sync_feedback()
        await c.collaboration.sync_feedback()
        assert len(c.state["posts"]) == 1 and not c.db["journal"]


@pytest.mark.parametrize("change", ["comment", "actor", "membership", "identity", "project", "trust"])
async def test_current_dispatch_authority_is_rechecked_without_answer_or_reply(poll_case, change):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery = await c.new_command()
        if change == "comment":
            c.state["edited"] = True
        elif change == "actor":
            c.state["permission_actor"] = 8
        elif change == "membership":
            c.db["memberships"]["member"].active = False
        elif change == "identity":
            c.db["users"][c.user_id].enabled = False
        elif change == "project":
            c.db["projects"][c.project].data["definition"]["changed"] = True
        else:
            c.db["settings"][c.repository].data["trusted_actors"] = []
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "denied"
        assert not c.db["journal"] and not c.state["posts"]
        assert c.db["runs"][c.run_id].data["worker_request"]["status"] == "pending"


async def test_status_only_requires_current_read_but_answer_requires_execute(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        c.db["memberships"]["member"].role = "viewer"
        status = await c.new_command("status")
        assert (await c.collaboration.process(status["id"]))["status"] == "processed"
        answer = await c.new_command(number=21)
        assert (await c.collaboration.process(answer["id"]))["status"] == "denied"
        assert len(c.state["posts"]) == 1 and not c.db["journal"]


async def test_exact_consent_replay_is_inert_and_history_survives_later_revocation(poll_case):
    async with poll_case() as c:
        await c.ready()
        receipt = await c.consent()
        request = c.state["consent_request"]
        count = len(c.db["events"])
        assert await c.service.configure_binding(c.project, c.repository, request) == receipt
        assert len(c.db["events"]) == count
        revoked = c.models.SCMPollActorBindingInputV2.model_validate(
            {
                **request.model_dump(),
                "request_id": uuid4().hex,
                "expected_previous_digest": receipt["digest"],
                "active": False,
            }
        )
        await c.service.configure_binding(c.project, c.repository, revoked)
        delivery = await c.new_command()
        assert (await c.collaboration.process(delivery["id"]))["status"] == "denied"
        assert await c.service.binding(c.project, c.repository, request.request_id) == receipt
        heads = await c.service.heads(c.project, c.repository)
        assert len(heads["items"]) == 1 and not heads["items"][0]["request"]["active"]
        assert not c.db["journal"] and not c.state["posts"]


async def test_cursor_and_inbox_keep_shared_source_identity_and_edits_do_not_create_effect(poll_case):
    async with poll_case() as c:
        await c.ready()
        row = next(iter(c.db["inbox"].values()))
        original = copy.deepcopy(row.data)
        # A source already admitted by another transport remains the original receipt.
        row.delivery_id = "webhook-original"
        row.data["verification"] = {"transport": "existing_signed_webhook"}
        c.state["comments"][0]["body"] = "/forge status {}"
        c.state["clock"] += 3600
        await c.service.step(c.repository)
        await c.service.step(c.repository)
        assert len(c.db["inbox"]) == 1 and row.id.hex == original["id"]
        assert row.data["source_snapshot"] == original["source_snapshot"]
        assert not c.db["journal"] and not c.state["posts"]


@pytest.mark.parametrize("kind", ["pull_request", "bot", "unsupported"])
async def test_shared_issue_endpoints_never_enable_pr_bot_or_other_actions(poll_case, kind):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        if kind == "pull_request":
            c.state["pull_request"] = True
        c.state["clock"] += 3600
        body = "/forge resume {}" if kind == "unsupported" else "/forge status " + json.dumps({"run_id": c.run_id})
        item = c.comment(20, body)
        if kind == "bot":
            item["user"]["type"] = "Bot"
        c.state["comments"].append(item)
        await c.service.step(c.repository)
        await c.service.step(c.repository)
        row = next(r for r in c.db["inbox"].values() if r.source_key == "comment:20")
        assert row.status == "ignored" and row.data["command"] is None
        assert not c.db["journal"] and not c.state["posts"]


async def test_sensitive_comment_is_withheld_before_persistence_but_keeps_source_dedup(poll_case):
    async with poll_case() as c:
        await c.ready()
        c.state["credential"] = "fictional-sensitive-canary"
        c.state["comments"].append(c.comment(20, c.state["credential"]))
        c.state["clock"] += 3600
        await c.service.step(c.repository)
        await c.service.step(c.repository)
        row = next(r for r in c.db["inbox"].values() if r.source_key == "comment:20")
        assert row.status == "ignored" and row.data["reason"] == "sensitive_content_withheld"
        assert row.data["source_snapshot"] is None and row.data["command"] is None
        assert c.state["credential"] not in json.dumps(row.data)
        with pytest.raises(AccessDenied, match="withheld"):
            await c.service._observation(c.project, c.repository, row.id.hex)


async def test_late_credential_registration_rolls_back_new_page_and_cursor(poll_case):
    async with poll_case() as c:
        await c.ready()
        c.state["clock"] += 3600
        await c.service.step(c.repository)
        c.state["comments"].append(c.comment(20, "late-credential-canary"))
        before = copy.deepcopy(c.db["settings"][c.repository].data)

        def register(value):
            if isinstance(value, dict) and "sources" in value:
                c.state["credential"] = "late-credential-canary"

        c.state["admit_hook"] = register
        with pytest.raises(ValueError, match="credential"):
            await c.service.step(c.repository)
        assert c.db["settings"][c.repository].data == before
        assert all(r.source_key != "comment:20" for r in c.db["inbox"].values())
        assert not c.db["journal"]


@pytest.mark.parametrize(
    "headers", [{"Retry-After": "120"}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2000000000"}]
)
async def test_retry_metadata_cannot_advance_scan_or_dispatch(poll_case, headers):
    async with poll_case() as c:
        await c.configure()
        cursor = copy.deepcopy(c.db["settings"][c.repository].data["_github_poll_v1"]["cursor"])
        c.state["clock"] = 2000000000 - 120
        c.state["read_status"], c.state["read_headers"] = 429, headers
        await c.service.step(c.repository)
        retained = c.db["settings"][c.repository].data["_github_poll_v1"]["cursor"]
        assert retained["page"] == cursor["page"] and retained["phase"] == cursor["phase"]
        assert retained["retry_at"] == c.state["clock"] + 120 and not retained["ready"]
        before = len(c.state["requests"])
        await c.service.step(c.repository)
        assert len(c.state["requests"]) == before and not c.db["inbox"]


async def test_foreign_next_link_cannot_commit_partial_observations(poll_case):
    async with poll_case() as c:
        await c.configure()
        await c.service.step(c.repository)
        before = copy.deepcopy(c.db["settings"][c.repository].data)
        c.state["comment_headers"] = {"Link": '<https://foreign.invalid/comments?page=2>; rel="next"'}
        with pytest.raises(ValueError, match="pagination"):
            await c.service.step(c.repository)
        assert not c.db["inbox"] and c.db["settings"][c.repository].data == before


async def test_api_role_never_starts_polling_consumer(poll_case):
    async with poll_case() as c:
        c.store.runtime_roles = SimpleNamespace(executes_jobs=False)
        await c.service.start()
        assert c.service.poller is None and not c.state["requests"]


async def test_consent_rechecks_current_membership_after_credential_await(poll_case):
    async with poll_case() as c:
        await c.ready()
        before = len(c.db["events"])

        def revoke(value):
            if isinstance(value, dict) and "reference" in value:
                c.db["memberships"]["member"].active = False

        c.state["admit_hook"] = revoke
        with pytest.raises(AccessDenied, match="project permission"):
            await c.consent()
        assert len(c.db["events"]) == before
        assert not c.db["settings"][c.repository].data["_github_poll_v1"]["heads"]
        assert not c.db["journal"]


async def test_revoked_head_cannot_be_rolled_back_via_mutable_settings(poll_case):
    async with poll_case() as c:
        await c.ready()
        receipt = await c.consent()
        request = c.state["consent_request"]
        old = copy.deepcopy(c.db["settings"][c.repository].data["_github_poll_v1"]["heads"])
        revoked = c.models.SCMPollActorBindingInputV2.model_validate(
            {
                **request.model_dump(),
                "request_id": uuid4().hex,
                "expected_previous_digest": receipt["digest"],
                "active": False,
            }
        )
        await c.service.configure_binding(c.project, c.repository, revoked)
        c.db["settings"][c.repository].data["_github_poll_v1"]["heads"] = old
        delivery = await c.new_command()
        assert (await c.collaboration.process(delivery["id"]))["status"] == "denied"
        assert not c.db["journal"] and not c.state["posts"]


async def test_poll_configuration_exact_read_is_local_and_replay_cannot_change_input(poll_case):
    async with poll_case() as c:
        receipt = await c.configure()
        before = len(c.state["requests"])
        assert await c.service.configuration(c.project, c.repository, receipt["request"]["request_id"]) == receipt
        model = c.models.PollConfigurationV1.model_validate(receipt["request"])
        assert await c.service.configure(c.project, c.repository, model) == receipt
        assert len(c.state["requests"]) == before and len(c.db["events"]) == 1
        changed = c.models.PollConfigurationV1.model_validate({**receipt["request"], "enabled": False})
        with pytest.raises(AccessDenied, match="another actor or input"):
            await c.service.configure(c.project, c.repository, changed)
        assert len(c.db["events"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2.0),
        ("allowed_actions", ["resume"]),
        ("acknowledge_api_observed_identity", 1),
        ("verified_reference", {}),
    ],
)
async def test_new_consent_does_not_accept_legacy_upgrade_or_trusted_client_proof(poll_case, field, value):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        with pytest.raises(ValueError):
            c.models.SCMPollActorBindingInputV2.model_validate(
                {**c.state["consent_request"].model_dump(), field: value}
            )


async def test_ownership_lost_while_page_is_awaited_cannot_commit_observations(poll_case):
    async with poll_case() as c:
        await c.configure()
        await c.service.step(c.repository)
        before = copy.deepcopy(c.db["settings"][c.repository].data)

        def lose(value):
            if isinstance(value, dict) and "sources" in value:
                c.state["ownership_lost"] = True

        c.state["admit_hook"] = lose
        with pytest.raises(RuntimeError, match="ownership loss"):
            await c.service.step(c.repository)
        assert not c.db["inbox"] and c.db["settings"][c.repository].data == before


async def test_oversized_page_is_not_truncated_or_committed(poll_case):
    async with poll_case() as c:
        await c.configure()
        await c.service.step(c.repository)
        before = copy.deepcopy(c.db["settings"][c.repository].data)
        c.state["comments"] = [c.comment(i) for i in range(1, 52)]
        with pytest.raises(ValueError, match="bounded polling page"):
            await c.service.step(c.repository)
        assert not c.db["inbox"] and c.db["settings"][c.repository].data == before


async def test_setup_derives_project_and_connection_cut_without_claiming_provider_grant(poll_case):
    async with poll_case() as c:
        setup = await c.service.setup(c.project, c.repository)
        assert setup["project_binding_digest"] == digest(c.definition)
        assert setup["connection_revision"] == c.scm.delegation_scope(c.repo_value)["connection_revision"]
        assert setup["expected_previous_digest"] is None and setup["provider_grant"] == "not_observed"
        assert not c.state["requests"] and not c.db["events"]


async def test_status_transition_after_revocation_cannot_read_run_or_send_more_feedback(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery = await c.new_command("status")
        assert (await c.collaboration.process(delivery["id"]))["feedback_status"] == "sent"
        c.db["memberships"]["member"].active = False
        c.db["runs"][c.run_id].data["status"] = "failed"
        reads = []

        async def forbidden(value):
            reads.append(value)
            raise AssertionError("Private run must not be read after polling authority revocation")

        c.store.get_run = forbidden
        await c.collaboration.sync_feedback()
        assert not reads and len(c.state["posts"]) == 1


async def test_read_token_cache_is_distinct_and_does_not_widen_existing_write_defaults(poll_case):
    async with poll_case() as c:
        await c.scm._github_token("42", issues_read=True)
        await c.scm._github_token("42")
        assert {"issues": "read"} in [v["permissions"] for v in c.state["token_scopes"]]
        assert {"contents": "write", "pull_requests": "write"} in [v["permissions"] for v in c.state["token_scopes"]]
        with pytest.raises(ValueError, match="separate"):
            await c.scm._github_token("42", issues_read=True, issues=True)


async def test_observed_edit_is_retained_as_a_hold_even_if_the_body_is_restored(poll_case):
    async with poll_case() as c:
        await c.ready()
        receipt = await c.consent()
        delivery = await c.new_command("status")
        original_body = c.state["comments"][-1]["body"]
        c.state["comments"][-1]["body"] += " changed"
        c.state["clock"] += 3600
        await c.service.step(c.repository)
        await c.service.step(c.repository)
        c.state["comments"][-1]["body"] = original_body
        row = c.db["inbox"][delivery["id"]]
        assert row.data["source_conflict"] and row.data["source_snapshot"] == delivery["source_snapshot"]
        assert (await c.collaboration.process(delivery["id"]))["status"] == "denied"
        assert not c.state["posts"] and not c.db["journal"]
        assert await c.service.binding(c.project, c.repository, receipt["request"]["request_id"]) == receipt


async def test_collaboration_update_preserves_but_does_not_publish_server_polling_state(poll_case):
    from firefly_forge.collaboration import CollaborationSettings

    async with poll_case() as c:
        await c.configure()
        before = copy.deepcopy(c.db["settings"][c.repository].data["_github_poll_v1"])
        result = await c.collaboration.configure(
            c.repository,
            CollaborationSettings(
                enabled=True, trusted_actors=["7"], allowed_commands=["status"], channel_commands=["respond"]
            ),
        )
        assert c.db["settings"][c.repository].data["_github_poll_v1"] == before
        assert "_github_poll_v1" not in result


async def test_other_provider_is_refused_without_http_or_polling_state(poll_case):
    async with poll_case() as c:
        c.repo_value["connection_provider"] = "gitlab"
        with pytest.raises(ValueError, match="Configure a provider application"):
            await c.configure()
        assert not c.state["requests"] and not c.db["events"]
        assert "_github_poll_v1" not in c.db["settings"][c.repository].data


async def test_foreign_comment_origin_cannot_finish_baseline_or_persist_source(poll_case):
    async with poll_case() as c:
        await c.configure()
        await c.service.step(c.repository)
        before = copy.deepcopy(c.db["settings"][c.repository].data)
        c.state["comments"][0]["issue_url"] = "https://foreign.invalid/repos/team/product/issues/3"
        with pytest.raises(ValueError, match="identity or body"):
            await c.service.step(c.repository)
        assert c.db["settings"][c.repository].data == before and not c.db["inbox"]


async def test_publish_enabled_answer_and_later_feedback_keep_current_release_requirement(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        c.db["runs"][c.run_id].data["publish"] = True
        denied = await c.new_command()
        assert (await c.collaboration.process(denied["id"]))["status"] == "denied"
        assert not c.db["journal"] and not c.state["posts"]
        c.db["memberships"]["member"].role = "maintainer"
        accepted = await c.new_command(number=21)
        assert (await c.collaboration.process(accepted["id"]))["status"] == "processed"
        assert len(c.state["posts"]) == 1
        c.db["memberships"]["member"].role = "developer"
        c.db["runs"][c.run_id].data["status"] = "failed"
        await c.collaboration.sync_feedback()
        assert len(c.state["posts"]) == 1


async def test_polling_configuration_cannot_admit_an_orphan_consent_for_another_project(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        other = uuid4().hex
        definition = {**copy.deepcopy(c.definition), "id": other}
        c.db["projects"][other] = SimpleNamespace(id=UUID(other), data=definition)
        c.db["memberships"]["other"] = SimpleNamespace(
            user_id=UUID(c.user_id), project_id=UUID(other), role="developer", active=True
        )
        request = c.models.SCMPollActorBindingInputV2.model_validate(
            {
                **c.state["consent_request"].model_dump(),
                "request_id": uuid4().hex,
                "expected_project_binding_digest": digest(definition),
            }
        )
        before = len(c.db["events"]), len(c.state["requests"])
        with pytest.raises(AccessDenied, match="exact project"):
            await c.service.configure_binding(other, c.repository, request)
        assert (len(c.db["events"]), len(c.state["requests"])) == before
        assert not any(
            k.startswith(other + ":") for k in c.db["settings"][c.repository].data["_github_poll_v1"]["requests"]
        )


async def test_reconfigured_project_cannot_reauthorize_a_retained_previous_project_snapshot(poll_case):
    from firefly_forge.project_access import ProjectAccess

    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery = await c.new_command("status")
        principal = await c.service.resolve(delivery)
        snapshot = ProjectAccess.actor_snapshot(principal)
        other = uuid4().hex
        definition = {**copy.deepcopy(c.definition), "id": other}
        c.db["projects"][other] = SimpleNamespace(id=UUID(other), data=definition)
        setup = await c.service.setup(other, c.repository)
        await c.service.configure(
            other,
            c.repository,
            c.models.PollConfigurationV1(
                version=1,
                request_id=uuid4().hex,
                expected_previous_digest=setup["expected_previous_digest"],
                expected_project_binding_digest=setup["project_binding_digest"],
                expected_connection_revision=setup["connection_revision"],
                enabled=True,
            ),
        )
        await c.service.step(c.repository)
        await c.service.step(c.repository)
        assert (await c.service.status(other, c.repository))["ready"]
        with pytest.raises(AccessDenied, match="project scope"):
            await c.service.resolve(delivery)
        with pytest.raises(AccessDenied, match="exact project"):
            await c.service.authorize_snapshot(snapshot, c.project, "read")
        assert not c.db["journal"] and not c.state["posts"]


async def test_project_switch_waits_for_previous_project_wal_fence_before_native_writes(poll_case):
    async with poll_case() as c:
        await c.ready()
        old = await c.service.setup(c.project, c.repository)
        other = uuid4().hex
        definition = {**copy.deepcopy(c.definition), "id": other}
        c.db["projects"][other] = SimpleNamespace(id=UUID(other), data=definition)
        request = c.models.PollConfigurationV1(
            version=1,
            request_id=uuid4().hex,
            expected_previous_digest=old["expected_previous_digest"],
            expected_project_binding_digest=digest(definition),
            expected_connection_revision=old["connection_revision"],
            enabled=True,
        )
        held_key = c.store.schema + ":members:" + c.project
        c.state.update(
            held_members=held_key, members_blocked=asyncio.Event(), release_members=asyncio.Event(), lock_trace=[]
        )
        operation = asyncio.create_task(c.service.configure(other, c.repository, request))
        blocked = asyncio.create_task(c.state["members_blocked"].wait())
        try:
            done, _ = await asyncio.wait([operation, blocked], timeout=2, return_when=asyncio.FIRST_COMPLETED)
            assert blocked in done and not operation.done(), (
                "Project switch crossed the previous project's held WAL fence"
            )
            assert (
                c.db["settings"][c.repository].data["_github_poll_v1"]["configuration"]["digest"]
                == old["expected_previous_digest"]
            )
            assert all(kind == "members" for kind, _ in c.state["lock_trace"])
        finally:
            c.state["release_members"].set()
            if not blocked.done():
                blocked.cancel()
            await asyncio.gather(operation, blocked, return_exceptions=True)
        assert operation.exception() is None
        assert c.state["lock_trace"][:2] == [
            ("members", c.store.schema + ":members:" + p) for p in sorted([c.project, other])
        ]


async def test_head_change_after_policy_await_cannot_reauthorize_the_old_snapshot(poll_case, monkeypatch):
    from firefly_forge.project_access import ProjectAccess

    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery = await c.new_command("status")
        actor = await c.service.resolve(delivery)
        original = c.service._scope

        async def change_after_scope(repository, policy):
            result = await original(repository, policy)
            state = c.db["settings"][c.repository].data["_github_poll_v1"]
            state["configuration"] = {**state["configuration"], "digest": "f" * 64}
            return result

        monkeypatch.setattr(c.service, "_scope", change_after_scope)
        with pytest.raises(AccessDenied, match="configuration"):
            await c.service.authorize_snapshot(ProjectAccess.actor_snapshot(actor), c.project, "read")
        assert not c.db["journal"] and not c.state["posts"]


async def test_collaboration_flags_wait_for_current_poll_project_wal_fence(poll_case):
    from firefly_forge.collaboration import CollaborationSettings

    async with poll_case() as c:
        await c.ready()
        c.state.update(
            held_members=c.store.schema + ":members:" + c.project,
            members_blocked=asyncio.Event(),
            release_members=asyncio.Event(),
            lock_trace=[],
        )
        operation = asyncio.create_task(c.collaboration.configure(c.repository, CollaborationSettings(enabled=False)))
        blocked = asyncio.create_task(c.state["members_blocked"].wait())
        try:
            done, _ = await asyncio.wait([operation, blocked], timeout=2, return_when=asyncio.FIRST_COMPLETED)
            assert blocked in done and not operation.done(), "Collaboration flags crossed the held polling WAL fence"
            assert c.db["settings"][c.repository].data["enabled"]
        finally:
            c.state["release_members"].set()
            if not blocked.done():
                blocked.cancel()
            await asyncio.gather(operation, blocked, return_exceptions=True)
        assert operation.exception() is None
        assert not c.db["settings"][c.repository].data["enabled"]


@pytest.mark.parametrize("registered_after", ["authentication", "final_guard"])
async def test_current_credentials_guard_feedback_after_the_last_admission_await(
    poll_case, monkeypatch, registered_after
):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        fictional = "owned-late-feedback-credential-57d64"
        c.db["runs"][c.run_id].data["worker_request"]["question"] = "Do not disclose " + fictional
        delivery = await c.new_command("status")
        authentication = c.scm.issue_authentication
        local_guard = c.service.authorize_feedback_local
        reached = []

        async def register_after_authentication(repository, **kwargs):
            result = await authentication(repository, **kwargs)
            reached.append(True)
            if registered_after == "authentication":
                c.state["credential"] = fictional
            return result

        async def register_after_final_guard(value):
            await local_guard(value)
            if reached and registered_after == "final_guard":
                c.state["credential"] = fictional

        monkeypatch.setattr(c.scm, "issue_authentication", register_after_authentication)
        monkeypatch.setattr(c.service, "authorize_feedback_local", register_after_final_guard)
        result = await c.collaboration.process(delivery["id"])
        assert reached == [True] and c.state["posts"] == []
        assert result["action_result"] == {"run_id": c.run_id, "status": "paused"}
        assert result["feedback_status"] == "needs_reconciliation"
        await c.collaboration.process(delivery["id"])
        await c.collaboration.sync_feedback()
        assert reached == [True] and c.state["posts"] == [] and not c.db["journal"]
