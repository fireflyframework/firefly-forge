# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import ValidationError

from firefly_forge.channel_actions import BootstrapSettings, ChannelCommand, RepositoryChannels
from firefly_forge.collaboration import Collaboration
from firefly_forge.scm_connections import SCMConnections


def signed(text="/forge project", *, actor=7, remote=42, delivery="channel-1", installation=None):
    body = {
        "action": "created",
        "repository": {"id": remote},
        "sender": {"id": actor},
        "issue": {"number": 12},
        "comment": {"id": 91, "body": text},
    }
    if installation:
        body = {
            "action": "created",
            "installation": {"id": installation},
            "repositories": [{"id": remote}],
            "sender": {"id": actor},
        }
    raw = json.dumps(body).encode()
    return raw, {
        "X-GitHub-Event": "installation" if installation else "issue_comment",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"global-webhook-secret", raw, hashlib.sha256).hexdigest(),
    }


async def channel(store, monkeypatch):
    scm = SCMConnections(store)
    grants = [
        {"id": "42", "name": "team/product", "url": "https://github.com/team/product.git", "default_branch": "main"}
    ]

    async def repositories(provider):
        return grants

    monkeypatch.setattr(scm, "repositories", repositories)
    collaboration = Collaboration(store, scm)
    service = RepositoryChannels(store, collaboration, scm)
    await service.configure(
        "github",
        BootstrapSettings(
            enabled=True,
            auto_register=True,
            trusted_actors=["7"],
            configuration_actors=["7"],
            webhook_secret="global-webhook-secret",
        ),
    )
    return service, collaboration, grants


async def test_global_signature_actor_and_grant_checks_before_registration(store, monkeypatch):
    service, collaboration, grants = await channel(store, monkeypatch)
    for options, reason in [({"actor": 8}, "untrusted_actor"), ({"remote": 43}, "repository_not_granted")]:
        raw, headers = signed(**options, delivery=reason)
        receipt = await service.receive("github", raw, headers)
        assert receipt["status"] == "denied" and receipt["reason"] == reason
    raw, headers = signed(delivery="bad-signature")
    assert (await service.receive("github", raw + b" ", headers))["reason"] == "invalid_signature"
    assert await store.repositories() == [] and await store.runs() == []
    assert "global-webhook-secret" not in json.dumps(service.settings("github"))


async def test_global_delivery_registers_granted_project_once_and_routes_comment(store, monkeypatch):
    service, collaboration, grants = await channel(store, monkeypatch)
    raw, headers = signed()
    first = await service.receive("github", raw, headers)
    assert first["status"] == "processed" and first["repository_ids"]
    assert (await service.receive("github", raw, headers))["id"] == first["id"]
    assert len(await store.repositories()) == 1
    routed = await collaboration.receipt(first["repository_receipt_id"])
    assert routed["command"]["action"] == "project"
    assert not await store.runs()
    raw2, _ = signed('/forge project {"unexpected":1}')
    with pytest.raises(ValueError, match="different bytes"):
        await service.receive(
            "github",
            raw2,
            {
                **headers,
                "X-Hub-Signature-256": "sha256=" + hmac.new(b"global-webhook-secret", raw2, hashlib.sha256).hexdigest(),
            },
        )


def test_channel_json_registry_never_accepts_raw_argv_credentials_or_actor_escalation():
    assert ChannelCommand.parse('/forge design {"operation":"preview"}').action == "design"
    for value in [
        '/forge product {"argv":["echo"]}',
        '/forge project {"webhook_secret":"new"}',
        '/forge handoff {"run_id":"' + ("a" * 32) + '","agent":"shell"}',
    ]:
        with pytest.raises(ValueError):
            ChannelCommand.parse(value)
    with pytest.raises(ValidationError):
        BootstrapSettings(trusted_actors=["7"], configuration_actors=["8"])


async def test_configuration_actions_require_roles_and_product_revision_is_once(store, monkeypatch):
    from firefly_forge.channel_actions import ChannelActions
    from firefly_forge.collaboration import CollaborationSettings
    from firefly_forge.product_branding import ProductBranding

    service, collaboration, grants = await channel(store, monkeypatch)
    await service.configure(
        "github",
        BootstrapSettings(
            enabled=True,
            auto_register=True,
            trusted_actors=["7"],
            webhook_secret="global-webhook-secret",
            defaults=CollaborationSettings(channel_commands=["project", "product", "brand", "design"]),
        ),
    )
    definition = {
        "customer": "Support agents",
        "problem": "Tickets lose context",
        "journey": ["Find ticket"],
        "user_stories": ["As an agent I see prior replies"],
        "acceptance": ["Account access is checked"],
        "owners": ["support"],
    }
    raw, headers = signed("/forge product " + json.dumps({"definition": definition}))
    global_receipt = await service.receive("github", raw, headers)
    receipt_id = global_receipt["repository_receipt_id"]
    denied = await collaboration.process(receipt_id)
    assert denied["reason"] == "configuration_actor_required"
    repository_id = global_receipt["repository_ids"][0]
    assert await collaboration.product(repository_id) is None
    await collaboration.configure(
        repository_id,
        CollaborationSettings(
            enabled=True,
            trusted_actors=["7"],
            configuration_actors=["7"],
            channel_commands=["product", "brand", "design"],
        ),
    )
    await service.configure(
        "github", BootstrapSettings(enabled=True, auto_register=True, trusted_actors=["7"], configuration_actors=["7"])
    )
    raw, headers = signed("/forge product " + json.dumps({"definition": definition}), delivery="product-save")
    body = json.loads(raw)
    body["comment"]["id"] = 92
    raw = json.dumps(body).encode()
    headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(b"global-webhook-secret", raw, hashlib.sha256).hexdigest()
    received = await service.receive("github", raw, headers)
    result = await collaboration.process(received["repository_receipt_id"])
    assert result["action_result"]["revision"] == 1
    await collaboration.process(received["repository_receipt_id"])
    assert (await collaboration.product(repository_id))["revision"] == 1
    repo = await store.repository(repository_id)
    actions = ChannelActions(store, collaboration)
    saved = await actions.execute(
        repo,
        ChannelCommand.parse('/forge brand {"brand":{"name":"Support"}}'),
        await collaboration.settings(repository_id),
    )
    assert saved["digest"] == (await ProductBranding(store).current(repository_id))["digest"]
    preview = await actions.execute(
        repo, ChannelCommand.parse('/forge design {"operation":"preview"}'), await collaboration.settings(repository_id)
    )
    assert preview["coverage"]["journeys"] == 1 and preview["method"] == "wireframe"


async def test_installation_registration_requires_exact_app_installation_and_fresh_grants(store, monkeypatch):
    service, collaboration, grants = await channel(store, monkeypatch)
    monkeypatch.setattr(service.scm, "_values", lambda provider: {"installation_id": "81"})
    raw, headers = signed(installation=82)
    assert (await service.receive("github", raw, headers))["reason"] == "installation_mismatch"
    assert not await store.repositories()
    raw, headers = signed(installation=81, remote=999, delivery="installation-good")
    result = await service.receive("github", raw, headers)
    assert result["status"] == "processed"
    repos = await store.repositories()
    assert len(repos) == 1 and repos[0]["remote_repository_id"] == "42"
    assert not await store.runs()


async def test_unknown_channel_effect_is_retained_and_never_replayed(store, monkeypatch):
    from firefly_forge.channel_actions import ChannelActions
    from firefly_forge.collaboration import CollaborationSettings

    service, collaboration, grants = await channel(store, monkeypatch)
    await service.configure(
        "github",
        BootstrapSettings(
            enabled=True,
            auto_register=True,
            trusted_actors=["7"],
            defaults=CollaborationSettings(channel_commands=["knowledge"]),
            webhook_secret="global-webhook-secret",
        ),
    )
    raw, headers = signed('/forge knowledge {"operation":"search","query":"account access"}')
    receipt = await service.receive("github", raw, headers)
    calls = []

    async def unknown(self, *args, **kwargs):
        calls.append(1)
        raise httpx.ReadTimeout("unknown remote outcome")

    monkeypatch.setattr(ChannelActions, "execute", unknown)
    result = await collaboration.process(receipt["repository_receipt_id"])
    assert result["status"] == "needs_reconciliation" and result["reason"] == "channel_action_outcome_unknown"
    await collaboration.process(receipt["repository_receipt_id"])
    await collaboration.sync_feedback()
    assert calls == [1]


async def test_global_chunked_ingress_enforces_bound_before_registration():
    from starlette.requests import Request

    from firefly_forge.channel_controllers import ChannelController

    chunks = iter([b"x" * 60000, b"y" * 60000])

    async def receive():
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)
    response = await ChannelController(None).webhook("github", request)
    assert response.status_code == 413


async def test_global_actor_and_repository_grant_revocation_prevents_queued_dispatch(store, monkeypatch):
    service, collaboration, grants = await channel(store, monkeypatch)
    raw, headers = signed('/forge run {"brief":"Build support search"}')
    received = await service.receive("github", raw, headers)
    await service.configure("github", BootstrapSettings(enabled=True, auto_register=True, trusted_actors=[]))
    denied = await collaboration.process(received["repository_receipt_id"])
    assert denied["reason"] == "global_actor_revoked" and not await store.runs()
    await service.configure("github", BootstrapSettings(enabled=True, auto_register=True, trusted_actors=["7"]))
    body, headers = signed('/forge run {"brief":"Build support search"}', delivery="grant-revocation")
    payload = json.loads(body)
    payload["comment"]["id"] = 93
    body = json.dumps(payload).encode()
    headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(b"global-webhook-secret", body, hashlib.sha256).hexdigest()
    received = await service.receive("github", body, headers)
    grants.clear()
    denied = await collaboration.process(received["repository_receipt_id"])
    assert denied["reason"] == "repository_grant_revoked" and not await store.runs()


async def test_reference_intent_is_durable_before_unknown_write_and_manual_reconciliation_sends_nothing(
    store, monkeypatch
):
    from firefly_forge.channel_artifacts import ReferencePublisher
    from firefly_forge.collaboration import CollaborationSettings

    service, collaboration, grants = await channel(store, monkeypatch)
    await service.configure(
        "github",
        BootstrapSettings(
            enabled=True,
            auto_register=True,
            trusted_actors=["7"],
            configuration_actors=["7"],
            defaults=CollaborationSettings(channel_commands=["brand"], remote_artifacts=True),
        ),
    )
    raw, headers = signed('/forge brand {"brand":{"name":"Support"}}')
    global_receipt = await service.receive("github", raw, headers)
    receipt_id = global_receipt["repository_receipt_id"]
    effects = []

    async def unknown(self, repo, digest, kind, sources, *, intent, guard, readonly):
        from firefly_forge.channel_artifacts import reference_manifest

        manifest, files = reference_manifest(repo["id"], digest, kind, sources)
        await intent({"branch": "forge/references/" + digest, "manifest": manifest})
        assert (await collaboration.receipt(receipt_id))["reference_intent"]["manifest"] == manifest
        await guard()
        effects.append(1)
        raise httpx.ReadTimeout("unknown remote outcome")

    monkeypatch.setattr(ReferencePublisher, "publish", unknown)
    result = await collaboration.process(receipt_id)
    assert result["status"] == "needs_reconciliation" and result["reference_intent"]["manifest"]["files"]
    await collaboration.process(receipt_id)
    await collaboration.sync_feedback()
    assert effects == [1]
    reconciled = await collaboration.reconcile(
        receipt_id, "completed", "Checked branch and manifest at provider; all files match"
    )
    assert reconciled["action_status"] == "reconciled_completed" and effects == [1]


async def test_signed_configuration_actor_can_answer_worker_request_without_automatic_resume(store, monkeypatch):
    from types import SimpleNamespace

    from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest
    from firefly_forge.collaboration import CollaborationSettings
    from firefly_forge.journal import Journal
    from firefly_forge.models import RunInput

    service, collaboration, _ = await channel(store, monkeypatch)
    raw, headers = signed()
    received = await service.receive("github", raw, headers)
    repository_id = received["repository_ids"][0]
    await collaboration.configure(
        repository_id,
        CollaborationSettings(
            enabled=True, trusted_actors=["7"], configuration_actors=["7"], channel_commands=["respond"]
        ),
    )
    run = await store.create_run(RunInput(repository_id=repository_id, brief="Build", backend="pyfly", frontend="none"))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.worker_feedback(
        run["id"],
        WorkerFeedback(
            id="feedback-1",
            role_id="implementer",
            task_id="implement",
            attempt=1,
            disposition="clarification",
            summary="Customer needed",
            request=WorkerRequest(
                id="request-1", kind="product_definition", question="Which customer?", context="No owner provided"
            ),
        ),
    )
    collaboration.deployments = SimpleNamespace(engine=SimpleNamespace(poller=None, journal=journal))
    posted = []

    async def comment(repo, payload, issue):
        posted.append(payload)
        return {"id": "comment-1"}

    monkeypatch.setattr(collaboration, "_remote_write", comment)
    command = "/forge respond " + json.dumps({"run_id": run["id"], "request_id": "request-1", "answer": "Support team"})
    raw, headers = signed(command, delivery="worker-answer")
    body = json.loads(raw)
    body["comment"]["id"] = 92
    raw = json.dumps(body).encode()
    headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(b"global-webhook-secret", raw, hashlib.sha256).hexdigest()
    receipt = await service.receive("github", raw, headers)
    result = await collaboration.process(receipt["repository_receipt_id"])
    assert result["status"] == "processed", result.get("reason")
    messages = await journal.worker_messages(run["id"])
    assert messages[-1]["payload"]["actor"] == "7"
    assert messages[-1]["payload"]["answer"] == "Support team"
    assert (await store.get_run(run["id"]))["status"] == "paused"
    await collaboration.process(receipt["repository_receipt_id"])
    assert len(await journal.worker_messages(run["id"])) == len(messages)


async def test_project_channel_configuration_requires_every_member_actor_and_grant(store, monkeypatch):
    from firefly_forge.collaboration import CollaborationSettings
    from firefly_forge.models import RepositoryInput

    service, collaboration, grants = await channel(store, monkeypatch)
    raw, headers = signed()
    first = await service.receive("github", raw, headers)
    origin = first["repository_ids"][0]
    grants.append({"id": "43", "name": "team/web", "url": "https://github.com/team/web.git", "default_branch": "main"})
    second = await store.add_repository(
        RepositoryInput(
            name="team/web",
            url=grants[1]["url"],
            provider="github",
            connection_provider="github",
            remote_repository_id="43",
        )
    )
    await collaboration.configure(
        origin,
        CollaborationSettings(
            enabled=True,
            trusted_actors=["7"],
            configuration_actors=["7"],
            channel_commands=["project", "analysis", "assistant", "roles"],
        ),
    )
    await collaboration.configure(
        second["id"],
        CollaborationSettings(
            enabled=True,
            trusted_actors=["8"],
            configuration_actors=["8"],
            channel_commands=["project", "analysis", "assistant", "roles"],
        ),
    )
    definition = {
        "name": "Support",
        "description": "Support product",
        "topology": "multi_repo",
        "owners": ["owner"],
        "product_repository_id": second["id"],
        "components": [
            {
                "id": "service",
                "name": "Support API",
                "repository_id": origin,
                "path": ".",
                "responsibility": "Resolve tickets",
            },
            {
                "id": "web",
                "name": "Support web",
                "repository_id": second["id"],
                "path": ".",
                "kind": "web",
                "backend": "none",
                "frontend": "react",
                "responsibility": "Customer ticket view",
            },
        ],
    }

    async def receive(definition, delivery, number):
        raw, headers = signed(
            "/forge project " + json.dumps({"operation": "configure", "project_definition": definition}),
            delivery=delivery,
        )
        body = json.loads(raw)
        body["comment"]["id"] = number
        raw = json.dumps(body).encode()
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(b"global-webhook-secret", raw, hashlib.sha256).hexdigest()
        receipt = await service.receive("github", raw, headers)
        return (
            await collaboration.process(receipt["repository_receipt_id"])
            if receipt["repository_receipt_id"]
            else receipt
        )

    denied = await receive(definition, "graph-outsider", 92)
    assert denied["status"] == "denied" and "member" in denied["reason"]
    from firefly_forge.projects import Projects

    assert await Projects(store).projects() == []
    await collaboration.configure(
        second["id"],
        CollaborationSettings(
            enabled=True,
            trusted_actors=["7"],
            configuration_actors=["7"],
            channel_commands=["project", "analysis", "assistant", "roles"],
        ),
    )

    async def write(repo, payload, issue):
        return {"id": "comment-1"}

    monkeypatch.setattr(collaboration, "_remote_write", write)
    accepted = await receive(definition, "graph-configured", 93)
    assert accepted["status"] == "processed"
    project = await Projects(store).project(accepted["action_result"]["project_id"])
    assert project["definition"]["product_repository_id"] == second["id"]
    assert project["definition"]["components"][0]["verification"] == []
    definition["components"][0]["verification"] = [{"argv": ["arbitrary-comment-command"]}]
    bad = await receive(definition, "graph-raw-command", 94)
    assert bad["status"] == "denied"
    assert len(await Projects(store).projects()) == 1


async def test_webhook_registry_values_are_treated_as_credentials(store, monkeypatch):
    from firefly_forge.configuration import redact_secrets
    from firefly_forge.secrets import contains_known_credential

    service, collaboration, _ = await channel(store, monkeypatch)
    values = {**collaboration.redaction_settings(), **service.redaction_settings()}
    assert contains_known_credential("global-webhook-secret", values)
    assert redact_secrets("global-webhook-secret", values) == "[redacted]"


async def product_channel(store, monkeypatch):
    from firefly_forge.collaboration import CollaborationSettings
    from firefly_forge.models import RepositoryInput
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    service, collaboration, grants = await channel(store, monkeypatch)
    first = await service.receive("github", *signed())
    origin = await store.repository(first["repository_ids"][0])
    grants.append({"id": "43", "name": "team/web", "url": "https://github.com/team/web.git", "default_branch": "main"})
    second = await store.add_repository(
        RepositoryInput(
            name="team/web",
            url=grants[1]["url"],
            provider="github",
            connection_provider="github",
            remote_repository_id="43",
        )
    )
    settings = CollaborationSettings(
        enabled=True,
        trusted_actors=["7"],
        configuration_actors=["7"],
        channel_commands=["project", "analysis", "assistant", "roles"],
    )
    for repo in [origin, second]:
        await collaboration.configure(repo["id"], settings)
    project = await Projects(store).configure(
        ProjectDefinition.model_validate(
            {
                "name": "Support",
                "description": "Customer support product",
                "topology": "multi_repo",
                "owners": ["owner"],
                "product_repository_id": second["id"],
                "components": [
                    {
                        "id": "service",
                        "name": "Support API",
                        "repository_id": origin["id"],
                        "responsibility": "Resolve tickets",
                    },
                    {
                        "id": "web",
                        "name": "Support web",
                        "repository_id": second["id"],
                        "kind": "web",
                        "backend": "none",
                        "frontend": "react",
                        "responsibility": "Customer ticket view",
                    },
                ],
            }
        )
    )
    posted = []

    async def write(repo, payload, issue):
        posted.append(payload)
        return {"id": "comment-1"}

    monkeypatch.setattr(collaboration, "_remote_write", write)

    async def execute(action, options, number):
        raw, headers = signed("/forge " + action + " " + json.dumps(options), delivery="product-action-" + str(number))
        body = json.loads(raw)
        body["comment"]["id"] = number
        raw = json.dumps(body).encode()
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(b"global-webhook-secret", raw, hashlib.sha256).hexdigest()
        receipt = await service.receive("github", raw, headers)
        if receipt["repository_receipt_id"]:
            return await collaboration.process(receipt["repository_receipt_id"])
        return receipt

    return service, collaboration, grants, origin, second, project, execute, posted


async def test_signed_assistant_targets_canonical_product_replays_once_and_rejects_revoked_members(store, monkeypatch):
    from firefly_forge.product_assistant import ProductAssistant
    from firefly_forge.product_assistant_runtime import AssistantReply

    service, collaboration, grants, origin, second, project, execute, posted = await product_channel(store, monkeypatch)
    calls = []

    async def designer(prompt, settings):
        calls.append(prompt)
        return AssistantReply(message="Which customer owns the support account?", questions=["Who owns this account?"])

    manager = ProductAssistant(store, store.path.parent / "assistant-channel", designer=designer)
    collaboration.product_assistant = manager
    started = await execute("assistant", {"operation": "start", "project_id": project["id"]}, 100)
    assert started["status"] == "processed", started["reason"]
    result = started["action_result"]
    assert result["repository_id"] == second["id"] and result["model_invoked"] is False
    conversation = await manager.session(second["id"], result["conversation_id"])
    assert conversation["project_snapshot"]["digest"] == project["digest"]
    options = {
        "operation": "turn",
        "project_id": project["id"],
        "conversation_id": conversation["id"],
        "request_id": "a" * 32,
        "message": "Define the support customer",
    }
    first = await execute("assistant", options, 101)
    replay = await execute("assistant", options, 102)
    assert first["status"] == replay["status"] == "processed"
    assert first["action_result"]["turn_id"] == replay["action_result"]["turn_id"] and len(calls) == 1
    assert first["action_result"]["proposal_reviewed"] is False and not await store.runs()
    grants.pop()
    revoked = await execute("assistant", {**options, "request_id": "b" * 32}, 103)
    assert revoked["status"] == "denied" and revoked.get("action_status") is None and len(calls) == 1
    assert len(posted) == 3


async def test_channel_checks_frozen_analysis_and_current_graph_and_filters_discovery(store, monkeypatch):
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.project_persistence import ForgeAnalysisEntity
    from firefly_forge.projects import Projects
    from firefly_forge.reverse_engineering import ReverseEngineering
    from firefly_forge.store import identifier

    service, collaboration, grants, origin, second, project, execute, posted = await product_channel(store, monkeypatch)
    manager = ReverseEngineering(store, Projects(store), store.path.parent / "source-channel")
    collaboration.analysis_manager = manager
    analysis_id = "c" * 32
    async with store._unit():
        await manager.records.save(
            ForgeAnalysisEntity(
                id=identifier(analysis_id),
                project_id=identifier(project["id"]),
                status="ready_for_review",
                data={
                    "id": analysis_id,
                    "project_id": project["id"],
                    "project_snapshot": project,
                    "status": "ready_for_review",
                    "digest": "d" * 64,
                    "reviewed": False,
                    "history": [],
                },
            )
        )
    current_definition = {
        **project["definition"],
        "product_repository_id": origin["id"],
        "topology": "monorepo",
        "components": [project["definition"]["components"][0]],
    }
    await Projects(store).configure(ProjectDefinition.model_validate(current_definition), project["id"])
    grants.pop()
    denied = await execute(
        "analysis",
        {"operation": "review", "project_id": project["id"], "analysis_id": analysis_id, "digest": "d" * 64},
        110,
    )
    assert denied["status"] == "denied" and denied.get("action_status") is None
    assert (await manager.analysis(project["id"], analysis_id))["reviewed"] is False
    visible = await execute("analysis", {"operation": "status", "project_id": project["id"]}, 111)
    assert visible["action_result"]["analyses"] == []
    discovery = await execute("project", {"operation": "repositories"}, 112)
    assert discovery["action_result"]["repositories"] == [
        {"id": origin["id"], "name": origin["name"], "url": origin["url"]}
    ]
    roles = await execute(
        "roles", {"operation": "configure", "roles": {"skills": {"implementer": ["test-driven-development"]}}}, 113
    )
    assert roles["status"] == "processed", roles.get("reason")
    assert (await store.repository_roles(origin["id"]))["roles"]["skills"]["implementer"] == ["test-driven-development"]


async def test_known_credential_comment_is_rejected_before_registration_and_inbox_payload(store, monkeypatch):
    service, collaboration, grants = await channel(store, monkeypatch)
    store.credential_registry = lambda: {"FORGE_TOKEN": "private-comment-token"}
    raw, headers = signed(
        '/forge assistant {"operation":"turn","conversation_id":"'
        + ("a" * 32)
        + '","request_id":"'
        + ("b" * 32)
        + '","message":"private-comment-token"}'
    )
    result = await service.receive("github", raw, headers)
    assert result["status"] == "denied" and result["repository_receipt_id"] is None
    assert not await store.repositories() and "private-comment-token" not in json.dumps(
        await service.receipts("github")
    )


def test_product_channel_dtos_forbid_provider_and_operation_overrides():
    for command in [
        '/forge assistant {"operation":"start","agent":"api"}',
        '/forge assistant {"operation":"turn","conversation_id":"'
        + ("a" * 32)
        + '","request_id":"bad","message":"Hello"}',
        '/forge analysis {"operation":"review","project_id":"' + ("a" * 32) + '","analysis_id":"' + ("b" * 32) + '"}',
        '/forge roles {"operation":"configure","roles":{},"webhook_secret":"escalate"}',
        '/forge project {"operation":"status","project_id":null}',
    ]:
        with pytest.raises(ValueError):
            ChannelCommand.parse(command)


async def test_same_numeric_actor_cannot_authorize_member_from_another_provider(store, monkeypatch):
    from firefly_forge.collaboration import CollaborationSettings
    from firefly_forge.models import RepositoryInput

    service, collaboration, grants, origin, second, project, execute, posted = await product_channel(store, monkeypatch)
    foreign = await store.add_repository(
        RepositoryInput(
            name="team/foreign",
            url="https://gitlab.com/team/foreign.git",
            provider="gitlab",
            connection_provider="gitlab",
            remote_repository_id="44",
        )
    )
    grants.append({"id": "44", "name": foreign["name"], "url": foreign["url"], "default_branch": "main"})
    await collaboration.configure(
        foreign["id"],
        CollaborationSettings(
            enabled=True, trusted_actors=["7"], configuration_actors=["7"], channel_commands=["project"]
        ),
    )
    candidate = {
        **project["definition"],
        "components": [
            project["definition"]["components"][0],
            {**project["definition"]["components"][1], "repository_id": foreign["id"]},
        ],
        "product_repository_id": origin["id"],
    }
    denied = await execute("project", {"operation": "configure", "project_definition": candidate}, 120)
    assert denied["status"] == "denied" and denied.get("action_status") is None and not posted


async def test_signed_action_keeps_full_unreviewed_proposal_and_feedback_prefix(store, monkeypatch):
    from firefly_forge.channel_artifacts import ReferencePublisher
    from firefly_forge.collaboration import CollaborationSettings, ProductDefinition
    from firefly_forge.product_assistant import ProductAssistant
    from firefly_forge.product_assistant_runtime import AssistantReply

    service, collaboration, grants, origin, second, project, execute, posted = await product_channel(store, monkeypatch)
    definition = ProductDefinition(
        customer="Support agents",
        problem="Account owner not visible",
        journey=["Find owner"],
        user_stories=["As an agent I find the account owner"],
        acceptance=["Check account authorization"],
        owners=["support"],
    )

    async def designer(prompt, settings):
        return AssistantReply(message="A" * 11000, proposal=definition)

    collaboration.product_assistant = ProductAssistant(
        store, store.path.parent / "assistant-channel", designer=designer
    )
    await collaboration.configure(
        origin["id"],
        CollaborationSettings(
            enabled=True,
            trusted_actors=["7"],
            configuration_actors=["7"],
            channel_commands=["project", "assistant"],
            remote_artifacts=True,
        ),
    )
    publications = []

    async def publish(self, repo, digest, kind, sources, *, intent, guard, readonly=False):
        await intent({"branch": "forge/references/" + digest, "manifest": {"files": list(sources)}})
        await guard()
        publications.append((repo, digest, kind, sources))
        return {"published": True, "url": "https://github.com/team/product/tree/forge-reference", "digest": digest}

    monkeypatch.setattr(ReferencePublisher, "publish", publish)
    started = await execute("assistant", {"operation": "start", "project_id": project["id"]}, 121)
    conversation_id = started["action_result"]["conversation_id"]
    completed = await execute(
        "assistant",
        {
            "operation": "turn",
            "project_id": project["id"],
            "conversation_id": conversation_id,
            "request_id": "f" * 32,
            "message": "Propose an account ownership journey",
        },
        122,
    )
    assert completed["status"] == "processed", completed.get("reason")
    assert completed["reference_intent"] and publications[0][2] == "proposal"
    sources = publications[0][3]
    encoded = next(value for key, value in sources.items() if key.endswith(".json"))
    assert json.loads(encoded)["reply"]["message"] == "A" * 11000 and json.loads(encoded)["status"] == "unreviewed"
    comment = posted[-1]["body"]
    assert len(comment) <= 6000 and "UNREVIEWED" in comment[:300] and "Reference files:" in comment
    assert not await collaboration.product(second["id"]) and not await store.runs()
