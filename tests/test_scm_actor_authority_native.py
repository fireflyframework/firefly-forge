# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Genuine native identity, signed ingress and immutable mapping integration."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from worker_reply_fixtures import authenticated_operator

from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest
from firefly_forge.channel_actions import BootstrapSettings, RepositoryChannels
from firefly_forge.collaboration import Collaboration, CollaborationSettings
from firefly_forge.journal import Journal, digest
from firefly_forge.models import RunInput
from firefly_forge.project_access import ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.scm_connections import SCMConnections


async def test_signed_mapped_operator_answers_exact_question_without_resume(store):
    from firefly_forge.scm_actor_authority import SCMActorAuthority
    from firefly_forge.scm_authority_models import SCMActorBindingInputV1

    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )

    def provider(request):
        from pyfly.data.transaction import current_unit_of_work

        assert current_unit_of_work() is None
        if request.url.path == "/app/installations/34/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": "owned-native-scm-fixture-token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        assert request.method == "GET" and request.url.path == "/installation/repositories"
        return httpx.Response(
            200,
            json={
                "repositories": [
                    {
                        "id": 42,
                        "full_name": "team/product",
                        "clone_url": "https://github.com/team/product.git",
                        "default_branch": "main",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    scm = SCMConnections(store, client=client)
    await scm.configure("github", {"values": {"app_id": "12", "installation_id": "34", "private_key": key}})
    repo = await scm.connect("github", "42")
    collaboration = Collaboration(store, scm)
    settings = CollaborationSettings(
        enabled=True,
        trusted_actors=["7"],
        configuration_actors=["7"],
        channel_commands=["respond"],
        webhook_secret="owned-webhook-signing-fixture",
    )
    await collaboration.configure(repo["id"], settings)
    channels = RepositoryChannels(store, collaboration, scm)
    await channels.configure(
        "github",
        BootstrapSettings(
            enabled=True,
            trusted_actors=["7"],
            configuration_actors=["7"],
            webhook_secret="owned-webhook-signing-fixture",
        ),
    )
    project = await Projects(store).configure(
        ProjectDefinition(
            name="SCM clarification",
            description="Explicit delegated question response",
            topology="monorepo",
            owners=["Owner"],
            components=[dict(id="api", name="API", repository_id=repo["id"], responsibility="Serve requests")],
        )
    )
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            project_id=project["id"],
            component_id="api",
            operation="test",
            brief="Verify configured deterministic checks",
        )
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.worker_feedback(
        run["id"],
        WorkerFeedback(
            id="qa-feedback",
            role_id="qa",
            task_id="verify",
            attempt=1,
            disposition="clarification",
            summary="Choose an expected result",
            request=WorkerRequest(
                id="qa-question",
                kind="product_definition",
                question="Which result is expected?",
                context="The deterministic check needs an owner decision.",
            ),
        ),
    )
    command = "/forge respond " + json.dumps(
        {"run_id": run["id"], "request_id": "qa-question", "answer": "The empty result is expected"}
    )
    raw = json.dumps(
        {
            "action": "created",
            "installation": {"id": 34},
            "repository": {"id": 42},
            "sender": {"id": 7},
            "issue": {"number": 3},
            "comment": {"id": 91, "body": command},
        }
    ).encode()
    headers = {
        "x-github-event": "issue_comment",
        "x-github-delivery": "owned-delegation-1",
        "x-hub-signature-256": "sha256=" + hmac.new(b"owned-webhook-signing-fixture", raw, hashlib.sha256).hexdigest(),
    }
    try:
        received = await channels.receive("github", raw, headers)
        assert received["status"] == "processed" and received["repository_ids"] == [repo["id"]]
        delivery = await collaboration.receipt(received["repository_receipt_id"])
        assert delivery["verification"]["installation_id"] == "34"
        async with authenticated_operator(store) as owner:
            await ProjectAccess(store).set_membership(project["id"], owner, "owner")
            authority = SCMActorAuthority(store, scm)
            body = SCMActorBindingInputV1(
                version=1,
                request_id="a" * 32,
                expected_previous_digest=None,
                expected_project_binding_digest=digest(await Projects(store).project(project["id"])),
                verified_receipt_id=delivery["id"],
                expected_verified_reference_digest=digest(delivery["verification"]),
                native_user_id=owner.id.hex,
                component_ids=["api"],
                allowed_actions=["respond", "status", "cancel"],
                active=True,
                acknowledge_webhook_asserted_identity=True,
            )
            candidates = await authority.candidates(project["id"], repo["id"])
            assert candidates["project_binding_digest"] == body.expected_project_binding_digest
            assert candidates["items"][0]["verified_receipt_id"] == delivery["id"]
            first = await authority.configure(project["id"], repo["id"], body)
            assert await authority.configure(project["id"], repo["id"], body) == first
            assert await authority.get(project["id"], repo["id"], body.request_id) == first
            heads = await authority.list(project["id"], repo["id"])
            assert heads["heads_only"] is True and heads["next_cursor"] is None
            assert len(heads["items"]) == 1
            assert heads["items"][0]["digest"] == first["digest"]
            assert heads["items"][0]["head_status"] == "active"
            assert heads["items"][0]["current_head"] is True
            delegated = await authority.resolve(delivery)
            assert delegated.authentication_method == "scm_mapping" and not delegated.api_roles
            assert not ProjectAccess.is_admin(delegated)
        # No ambient native operator: authority comes only from the retained verified mapping.
        result = await collaboration.process(delivery["id"])
        assert result["status"] == "processed"
        current = await store.get_run(run["id"])
        assert current["status"] == "paused" and current["worker_request"]["status"] == "answered"
        messages = await journal.worker_messages(run["id"])
        reply = messages[-1]["payload"]["responding_actor"]
        assert reply["version"] == 2 and reply["authentication_method"] == "scm_mapping"
        assert reply["user_id"] == owner.id.hex
        assert reply["provenance"]["binding_digest"] == first["digest"]
        assert await collaboration.process(delivery["id"]) == result
        assert await journal.worker_messages(run["id"]) == messages
    finally:
        await client.aclose()


async def test_signed_unmapped_label_is_refused_before_any_action_intent(store, monkeypatch):
    from test_channel_actions import channel, signed

    service, collaboration, _ = await channel(store, monkeypatch)
    first_raw, first_headers = signed()
    registered = await service.receive("github", first_raw, first_headers)
    repository_id = registered["repository_ids"][0]
    await collaboration.configure(
        repository_id,
        CollaborationSettings(
            enabled=True,
            trusted_actors=["7"],
            configuration_actors=["7"],
            channel_commands=["respond"],
        ),
    )
    run = await store.create_run(RunInput(repository_id=repository_id, brief="Clarify existing legacy work"))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.worker_feedback(
        run["id"],
        WorkerFeedback(
            id="legacy-feedback",
            role_id="implementer",
            task_id="implement",
            attempt=1,
            disposition="clarification",
            summary="An owner decision is required",
            request=WorkerRequest(
                id="legacy-question",
                kind="product_definition",
                question="Which owner?",
                context="No owner was selected",
            ),
        ),
    )
    before = await journal.worker_messages(run["id"])
    raw, headers = signed(
        "/forge respond "
        + json.dumps(
            {
                "run_id": run["id"],
                "request_id": "legacy-question",
                "answer": "Support owner",
            }
        ),
        delivery="unmapped-answer",
    )
    body = json.loads(raw)
    body["comment"]["id"] = 92
    raw = json.dumps(body).encode()
    headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(b"global-webhook-secret", raw, hashlib.sha256).hexdigest()
    received = await service.receive("github", raw, headers)
    result = await collaboration.process(received["repository_receipt_id"])
    assert result["status"] == "denied" and result["reason"] == "scm_authority_setup_required"
    assert result["action_status"] == "not_dispatched" and "scm_execution_actor" not in result
    assert await journal.worker_messages(run["id"]) == before
    current = await store.get_run(run["id"])
    assert current["status"] == "paused" and current["worker_request"]["status"] == "pending"
    assert await collaboration.process(result["id"]) == result


def test_actual_api_registers_native_mapping_reads_without_provider_io(tmp_path, postgres_schema):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.scm_actor_authority import SCMActorAuthority
    from firefly_forge.scm_actor_authority_controllers import SCMActorAuthorityController

    token = "owned-scm-authority-api-bootstrap-token"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52600),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        context = http.app.state.runtime.context
        authority = context.get_bean(SCMActorAuthority)
        assert authority.store is http.app.state.store
        assert authority.scm is context.get_bean(SCMConnections)
        assert context.get_bean(SCMActorAuthorityController).authority is authority
        repository = http.post("/api/v1/repositories", json={"name": "Unconnected source", "url": str(tmp_path)})
        assert repository.status_code == 201
        repo = repository.json()
        definition = ProjectDefinition(
            name="Explicit setup",
            description="Local route registration without provider traffic",
            topology="monorepo",
            owners=["Owner"],
            components=[dict(id="api", name="API", repository_id=repo["id"], responsibility="Serve requests")],
        )
        configured = http.post("/api/v1/projects", json=definition.model_dump(mode="json"))
        assert configured.status_code == 201
        project = configured.json()
        path = f"/api/v1/projects/{project['id']}/repositories/{repo['id']}/scm-actor-bindings"
        before = http.portal.call(Projects(authority.store).project, project["id"])

        async def forbid_provider(*args, **kwargs):
            raise AssertionError("These explicit reads must not call the provider")

        original = authority.scm.repositories
        authority.scm.repositories = forbid_provider
        try:
            empty = http.get(path)
            assert empty.status_code == 200
            assert empty.json() == dict(
                version=1,
                project_id=project["id"],
                repository_id=repo["id"],
                project_binding_digest=digest(before),
                heads_only=True,
                items=[],
                next_cursor=None,
            )
            # Static /candidates is registered, but an unconnected repository has no eligible cut.
            candidate = http.get(path + "/candidates")
            assert candidate.status_code == 409
            assert http.get(path + "/requests/" + "a" * 32).status_code == 404
            assert http.portal.call(Projects(authority.store).project, project["id"]) == before
        finally:
            authority.scm.repositories = original
