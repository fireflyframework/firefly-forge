# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import ValidationError

from firefly_forge.collaboration import (
    Collaboration,
    CollaborationSettings,
    ProductDefinition,
    RepositoryCommand,
    customer_instruction,
    verify_webhook,
)
from firefly_forge.models import RepositoryInput


def product():
    return ProductDefinition(
        customer="Support teams",
        problem="Tickets lose context",
        journey=["Receive ticket", "Resolve ticket"],
        user_stories=["As an agent I can see prior replies"],
        acceptance=["Prior replies appear in chronological order"],
        nonfunctional_requirements=["Respond within 300 ms"],
        security_requirements=["Only assigned teams can read tickets"],
        owners=["support-owner"],
    )


@pytest.mark.parametrize("provider,header", [("github", "x-hub-signature-256"), ("bitbucket", "x-hub-signature")])
def test_signed_raw_bytes_cannot_be_reserialized_or_changed(provider, header):
    raw = b'{ "message": "customer" }'
    headers = {header: "sha256=" + hmac.new(b"webhook-only-secret", raw, hashlib.sha256).hexdigest()}
    assert verify_webhook(provider, raw, headers, "webhook-only-secret")
    assert not verify_webhook(provider, b'{"message":"customer"}', headers, "webhook-only-secret")
    assert not verify_webhook(provider, raw, {header: headers[header].replace("sha256", "sha1")}, "webhook-only-secret")


def test_gitlab_secret_is_constant_time_compared_and_never_empty():
    assert verify_webhook("gitlab", b"{}", {"x-gitlab-token": "hook"}, "hook")
    assert not verify_webhook("gitlab", b"{}", {"x-gitlab-token": ""}, "")


def test_comment_command_schema_rejects_shell_argv_and_multiple_commands():
    assert RepositoryCommand.parse('/forge run {"brief":"Build ticket search"}').action == "run"
    for text in ['/forge run {"argv":["rm","file"]}', "/forge run --publish", "/forge status\n/forge run {}"]:
        with pytest.raises(ValueError):
            RepositoryCommand.parse(text)
    with pytest.raises(ValidationError):
        ProductDefinition(customer="", problem="missing")
    assert "Only assigned teams" in customer_instruction({"revision": 1, "definition": product().model_dump()})


async def configured(store, *, provider="github", remote="42", actor="7", scm=None):
    repo = await store.add_repository(
        RepositoryInput(
            name="team/product",
            url=f"https://{provider}.com/team/product.git",
            provider=provider,
            connection_provider=provider,
            remote_repository_id=remote,
        )
    )
    service = Collaboration(store, scm)
    await service.configure(
        repo["id"],
        CollaborationSettings(
            enabled=True,
            trusted_actors=[actor],
            webhook_secret="webhook-only-secret",
            frontend="none",
        ),
    )
    await service.save_product(repo["id"], product())
    return service, repo


def delivery(repo, *, actor=7, remote=42, comment=91, brief="Build ticket search", delivery_id="delivery-1"):
    body = json.dumps(
        {
            "action": "created",
            "repository": {"id": remote, "full_name": "team/product"},
            "sender": {"id": actor},
            "issue": {"number": 12, "title": "Search"},
            "comment": {"id": comment, "body": "/forge run " + json.dumps({"brief": brief})},
        }
    ).encode()
    headers = {
        "X-GitHub-Event": "issue_comment",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"webhook-only-secret", body, hashlib.sha256).hexdigest(),
    }
    return body, headers


async def test_delivery_actor_repo_signature_denials_are_recorded_without_execution(store):
    service, repo = await configured(store)
    for kwargs, reason in [({"actor": 8}, "untrusted_actor"), ({"remote": 43}, "repository_mismatch")]:
        raw, headers = delivery(repo, **kwargs, delivery_id=reason)
        receipt = await service.receive("github", repo["id"], raw, headers)
        assert receipt["status"] == "denied" and receipt["reason"] == reason
    raw, headers = delivery(repo, delivery_id="bad-signature")
    receipt = await service.receive("github", repo["id"], raw + b" ", headers)
    assert receipt["reason"] == "invalid_signature"
    assert await store.runs() == []
    assert "webhook-only-secret" not in json.dumps(await service.settings(repo["id"]))


async def test_replay_and_concurrent_process_create_one_run_with_frozen_product(store):
    service, repo = await configured(store)
    raw, headers = delivery(repo)
    receipts = await asyncio.gather(*(service.receive("github", repo["id"], raw, headers) for _ in range(2)))
    assert receipts[0]["id"] == receipts[1]["id"]
    results = await asyncio.gather(*(service.process(receipts[0]["id"]) for _ in range(2)))
    assert results[0]["run_id"] == results[1]["run_id"]
    run = await store.get_run(results[0]["run_id"])
    assert run["product_definition"]["revision"] == 1
    assert run["issue_context"]["number"] == 12
    assert run["commands"] == [] and not run["publish"]
    updated = product().model_copy(update={"problem": "A revised problem"})
    await service.save_product(repo["id"], updated)
    assert (await store.get_run(run["id"]))["product_definition"]["definition"]["problem"] == "Tickets lose context"
    assert len(await store.runs()) == 1


async def test_same_delivery_with_different_bytes_and_same_comment_new_delivery_never_reexecute(store):
    service, repo = await configured(store)
    raw, headers = delivery(repo)
    receipt = await service.receive("github", repo["id"], raw, headers)
    other, other_headers = delivery(repo, brief="Different task")
    with pytest.raises(ValueError, match="delivery"):
        await service.receive("github", repo["id"], other, other_headers)
    await service.process(receipt["id"])
    other, other_headers = delivery(repo, delivery_id="redelivered-same-comment")
    duplicate = await service.receive("github", repo["id"], other, other_headers)
    assert duplicate["id"] == receipt["id"]
    assert len(await store.runs()) == 1


@pytest.mark.parametrize(
    "provider,remote,actor,event,payload",
    [
        (
            "gitlab",
            "42",
            "7",
            "Note Hook",
            {
                "object_kind": "note",
                "project": {"id": 42},
                "user": {"id": 7},
                "object_attributes": {
                    "id": 92,
                    "noteable_type": "MergeRequest",
                    "note": '/forge maintain {"brief":"Fix search"}',
                },
                "merge_request": {"iid": 13},
            },
        ),
        (
            "bitbucket",
            "{repo-42}",
            "{actor-7}",
            "pullrequest:comment_created",
            {
                "repository": {"uuid": "{repo-42}", "full_name": "team/product"},
                "actor": {"uuid": "{actor-7}"},
                "pullrequest": {"id": 13},
                "comment": {"id": 93, "content": {"raw": '/forge maintain {"brief":"Fix search"}'}},
            },
        ),
    ],
)
async def test_provider_specific_comment_normalization(store, provider, remote, actor, event, payload):
    service, repo = await configured(store, provider=provider, remote=remote, actor=actor)
    raw = json.dumps(payload).encode()
    headers = (
        {"x-gitlab-event": event, "webhook-id": "g-1", "x-gitlab-token": "webhook-only-secret"}
        if provider == "gitlab"
        else {
            "x-event-key": event,
            "x-request-uuid": "b-1",
            "x-hub-signature": "sha256=" + hmac.new(b"webhook-only-secret", raw, hashlib.sha256).hexdigest(),
        }
    )
    receipt = await service.receive(provider, repo["id"], raw, headers)
    result = await service.process(receipt["id"])
    assert (await store.get_run(result["run_id"]))["operation"] == "maintain"


async def test_feedback_unknown_write_is_not_replayed_and_does_not_send_operator_token(store, monkeypatch):
    from firefly_forge.scm_connections import SCMConnections

    count = 0

    def handle(request):
        nonlocal count
        count += 1
        assert request.headers["authorization"] == "Bearer provider-grant"
        assert request.url.path == "/repos/team/product/issues/12/comments"
        raise httpx.ReadTimeout("provider outcome unknown", request=request)

    scm = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    async def auth(repository, **kwargs):
        return "provider-grant", "https://api.github.com"

    monkeypatch.setattr(scm, "issue_authentication", auth)
    service, repo = await configured(store, scm=scm)
    raw, headers = delivery(repo)
    receipt = await service.receive("github", repo["id"], raw, headers)
    result = await service.process(receipt["id"])
    assert result["feedback_status"] == "needs_reconciliation"
    await service.process(receipt["id"])
    await service.sync_feedback()
    assert count == 1 and len(await store.runs()) == 1


async def test_disable_or_remove_actor_after_receipt_prevents_dispatch(store):
    service, repo = await configured(store)
    raw, headers = delivery(repo)
    receipt = await service.receive("github", repo["id"], raw, headers)
    await service.configure(repo["id"], CollaborationSettings(enabled=False, trusted_actors=["7"]))
    result = await service.process(receipt["id"])
    assert result["status"] == "denied" and result["reason"] == "collaboration_disabled"
    assert not await store.runs()


async def test_chunked_webhook_is_bounded_before_receiving_or_persisting():
    from starlette.requests import Request

    from firefly_forge.collaboration_controllers import CollaborationController

    chunks = iter([b"x" * 60000, b"y" * 60000])

    async def receive():
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)
    controller = CollaborationController(None)
    response = await controller.webhook("github", "repo", request)
    assert response.status_code == 413


async def test_issue_authoring_idempotency_and_terminal_feedback(store, monkeypatch):
    from firefly_forge.scm_connections import SCMConnections

    writes = []

    def handle(request):
        writes.append((request.url.path, json.loads(request.content)))
        assert request.headers["authorization"] == "Bearer provider-grant"
        return httpx.Response(
            201, json={"id": 900, "number": 19, "html_url": "https://github.com/team/product/issues/19"}
        )

    scm = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    async def auth(repository, **kwargs):
        return "provider-grant", "https://api.github.com"

    monkeypatch.setattr(scm, "issue_authentication", auth)
    service, repo = await configured(store, scm=scm)
    first, second = await asyncio.gather(
        *(
            service.create_issue(
                repo["id"], "Customer search", "Accept search by account", idempotency_key="issue-request-1"
            )
            for _ in range(2)
        )
    )
    assert first["id"] == second["id"] and len(writes) == 1
    with pytest.raises(ValueError, match="idempotency"):
        await service.create_issue(repo["id"], "Different", "Different", idempotency_key="issue-request-1")
    raw, headers = delivery(repo)
    receipt = await service.receive("github", repo["id"], raw, headers)
    processed = await service.process(receipt["id"])
    assert processed["feedback_status"] == "sent"
    await store.finish(processed["run_id"], "succeeded")
    await service.sync_feedback()
    await service.sync_feedback()
    assert len(writes) == 3
    assert "succeeded" in writes[-1][1]["body"]
    assert (await service.receipt(receipt["id"]))["feedback_stage"] == "succeeded"


async def test_bitbucket_pullrequest_comments_use_workspace_and_slug(store, monkeypatch):
    from firefly_forge.scm_connections import SCMConnections

    paths = []

    def handle(request):
        paths.append(request.url.path)
        assert "### forge" in json.loads(request.content)["content"]["raw"]
        return httpx.Response(201, json={"id": 19})

    scm = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    async def auth(repository, **kwargs):
        return "provider-grant", "https://api.bitbucket.org/2.0"

    monkeypatch.setattr(scm, "issue_authentication", auth)
    service, repo = await configured(store, provider="bitbucket", remote="{repo-42}", actor="{actor-7}", scm=scm)
    body = {
        "repository": {"uuid": "{repo-42}"},
        "actor": {"uuid": "{actor-7}"},
        "pullrequest": {"id": 13},
        "comment": {"id": 93, "content": {"raw": '/forge maintain {"brief":"Fix search"}'}},
    }
    raw = json.dumps(body).encode()
    receipt = await service.receive(
        "bitbucket",
        repo["id"],
        raw,
        {
            "x-event-key": "pullrequest:comment_created",
            "x-request-uuid": "b-1",
            "x-hub-signature": "sha256=" + hmac.new(b"webhook-only-secret", raw, hashlib.sha256).hexdigest(),
        },
    )
    result = await service.process(receipt["id"])
    assert result["feedback_status"] == "sent"
    assert paths == ["/2.0/repositories/team/product/pullrequests/13/comments"]


async def test_removed_bitbucket_cloud_issue_authoring_is_explicitly_unsupported(store):
    service, repo = await configured(store, provider="bitbucket", remote="{repo-42}", actor="{actor-7}")
    with pytest.raises(ValueError, match="removed"):
        await service.create_issue(repo["id"], "Customer search", "Customer acceptance")
    settings = await service.settings(repo["id"])
    assert not settings["capabilities"]["issue_authoring"]
    assert settings["capabilities"]["pull_request_comments"]


async def test_gitlab_delivery_identity_uses_retry_stable_message_id_not_hook_id(store):
    service, repo = await configured(store, provider="gitlab", remote="42", actor="7")
    for number in [92, 93]:
        body = {
            "object_kind": "note",
            "project": {"id": 42},
            "user": {"id": 7},
            "object_attributes": {"id": number, "noteable_type": "Issue", "note": '/forge run {"brief":"Search"}'},
            "issue": {"iid": 12},
        }
        receipt = await service.receive(
            "gitlab",
            repo["id"],
            json.dumps(body).encode(),
            {
                "x-gitlab-event": "Note Hook",
                "webhook-id": "message-" + str(number),
                "x-gitlab-webhook-uuid": "same-hook",
                "x-gitlab-token": "webhook-only-secret",
            },
        )
        assert receipt["status"] == "received"
    assert len(await service.receipts(repo["id"])) == 2


async def test_unverified_signature_cannot_preempt_a_valid_delivery_identifier(store):
    service, repo = await configured(store)
    raw, headers = delivery(repo)
    bad = await service.receive("github", repo["id"], raw + b" ", headers)
    good = await service.receive("github", repo["id"], raw, headers)
    assert bad["status"] == "denied" and good["status"] == "received"
    assert bad["id"] != good["id"]


def test_run_input_rejects_unbounded_or_mismatched_product_snapshot():
    from firefly_forge.models import RunInput

    with pytest.raises(ValidationError):
        RunInput(
            repository_id="repo-one",
            brief="Build",
            product_definition={"repository_id": "repo-two", "revision": 1, "definition": product().model_dump()},
        )


async def test_terminal_feedback_does_not_starve_new_inbox_work(store):
    import uuid

    from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity
    from firefly_forge.store import identifier

    service, repo = await configured(store)
    async with store._unit():
        for index in range(201):
            await service.inbox.save(
                ForgeWebhookInboxEntity(
                    id=uuid.uuid4(),
                    repository_id=identifier(repo["id"]),
                    provider="github",
                    delivery_id="old-" + str(index),
                    source_key="old-" + str(index),
                    status="processed",
                    data={
                        "id": uuid.uuid4().hex,
                        "status": "processed",
                        "feedback_status": "sent",
                        "feedback_stage": "succeeded",
                    },
                )
            )
    raw, headers = delivery(repo)
    receipt = await service.receive("github", repo["id"], raw, headers)
    async with store._unit():
        pending = await service.inbox.find_pending()
        assert [row.data["id"] for row in pending] == [receipt["id"]]


async def test_customer_digest_rejects_mutation_after_product_save(store):
    from firefly_forge.models import RunInput

    service, repo = await configured(store)
    snapshot = await service.product(repo["id"])
    snapshot["definition"]["customer"] = "Changed without a new revision"
    with pytest.raises(ValidationError, match="digest"):
        RunInput(repository_id=repo["id"], brief="Build", product_definition=snapshot)


async def test_explicit_deploy_comment_is_denied_until_operator_enablement(store):
    service, repo = await configured(store)
    await service.configure(
        repo["id"], CollaborationSettings(enabled=True, trusted_actors=["7"], allowed_commands=["deploy"])
    )
    raw, headers = delivery(repo)
    body = json.loads(raw)
    body["comment"]["body"] = "/forge deploy " + json.dumps({"run_id": "a" * 32, "target_id": "production"})
    raw = json.dumps(body).encode()
    headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(b"webhook-only-secret", raw, hashlib.sha256).hexdigest()
    receipt = await service.receive("github", repo["id"], raw, headers)
    result = await service.process(receipt["id"])
    assert result["reason"] == "deployment_not_enabled" and result["status"] == "denied"
    assert not await store.runs()


async def test_github_issue_grant_is_repo_scoped_and_does_not_request_contents_permission(store):
    from datetime import UTC, datetime, timedelta

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from firefly_forge.scm_connections import SCMConnections

    requests = []

    def handle(request):
        if request.url.path == "/installation/repositories":
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
        payload = json.loads(request.content)
        requests.append(payload)
        assert "contents" not in payload["permissions"]
        return httpx.Response(
            201,
            json={"token": "provider-only-grant", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        )

    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )
    scm = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    await scm.configure("github", {"values": {"app_id": "12", "installation_id": "34", "private_key": key}})
    repo = await store.add_repository(
        RepositoryInput(
            name="team/product",
            url="https://github.com/team/product.git",
            provider="github",
            connection_provider="github",
            remote_repository_id="42",
        )
    )
    token, api = await scm.issue_authentication(repo)
    assert token == "provider-only-grant" and api == "https://api.github.com"
    assert requests[-1] == {"repository_ids": [42], "permissions": {"issues": "write"}}
    await scm.issue_authentication(repo, pull_request=True)
    assert requests[-1] == {"repository_ids": [42], "permissions": {"pull_requests": "write"}}
    with pytest.raises(ValueError, match="grant|identity"):
        await scm.issue_authentication({**repo, "remote_repository_id": "43"})


def test_architecture_proposal_uses_profiles_and_requires_review_before_freezing():
    from firefly_forge.product_architecture import ProductArchitectureService

    proposer = ProductArchitectureService()
    proposal = proposer.propose(product().model_dump())
    assert proposal["method"] == "rule_based"
    architecture = proposal["architecture"]
    assert architecture["backend"] == "pyfly" and architecture["frontend"] == "react"
    assert {component["layer"] for component in architecture["components"]} == {
        "domain",
        "application",
        "data",
        "web",
        "frontend",
        "operations",
    }
    assert not architecture["reviewed"]
    reviewed = ProductDefinition.model_validate(
        {**product().model_dump(), "architecture": {**architecture, "reviewed": True}}
    )
    assert "atomic design" in customer_instruction({"revision": 1, "definition": reviewed.model_dump()}).lower()
    alternate = proposer.propose(product().model_dump(), backend="larafly", frontend="vue")
    assert alternate["architecture"]["backend"] == "larafly" and alternate["architecture"]["frontend"] == "vue"
    assert "Laravel" in alternate["recommendation"]["rationale"]


async def test_unreviewed_architecture_cannot_be_saved_as_product_revision(store):
    from firefly_forge.product_architecture import ProductArchitectureService

    service, repo = await configured(store)
    architecture = ProductArchitectureService().propose(product().model_dump())["architecture"]
    definition = ProductDefinition.model_validate({**product().model_dump(), "architecture": architecture})
    with pytest.raises(ValueError, match="Review"):
        await service.save_product(repo["id"], definition)


async def test_reviewed_architecture_selects_default_stack_and_rejects_explicit_conflict(store):
    from firefly_forge.models import RunInput
    from firefly_forge.product_architecture import ProductArchitectureService

    service, repo = await configured(store)
    architecture = ProductArchitectureService().propose(product().model_dump(), backend="larafly", frontend="vue")[
        "architecture"
    ]
    architecture["reviewed"] = True
    snapshot = await service.save_product(
        repo["id"], ProductDefinition.model_validate({**product().model_dump(), "architecture": architecture})
    )
    request = RunInput(repository_id=repo["id"], brief="Build", product_definition=snapshot)
    assert request.backend == "larafly" and request.frontend == "vue"
    with pytest.raises(ValidationError, match="architecture"):
        RunInput(repository_id=repo["id"], brief="Build", backend="pyfly", product_definition=snapshot)
    raw, headers = delivery(repo)
    receipt = await service.receive("github", repo["id"], raw, headers)
    result = await service.process(receipt["id"])
    run = await store.get_run(result["run_id"])
    assert run["backend"] == "larafly" and run["frontend"] == "vue"


async def test_reconciliation_records_observed_outcome_without_remote_replay(store, monkeypatch):
    from firefly_forge.scm_connections import SCMConnections

    writes = []

    def handle(request):
        writes.append(request.url.path)
        raise httpx.ReadTimeout("outcome unknown", request=request)

    scm = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    async def auth(repository, **kwargs):
        return "provider-grant", "https://api.github.com"

    monkeypatch.setattr(scm, "issue_authentication", auth)
    service, repo = await configured(store, scm=scm)
    raw, headers = delivery(repo)
    receipt = await service.receive("github", repo["id"], raw, headers)
    await service.process(receipt["id"])
    reconciled = await service.reconcile(
        receipt["id"], "not_posted", "Checked provider conversation; no comment exists"
    )
    assert reconciled["feedback_status"] == "not_sent"
    assert reconciled["reconciliation"]["outcome"] == "not_posted"
    await service.sync_feedback()
    assert len(writes) == 1
    with pytest.raises(ValueError, match="uncertain"):
        await service.reconcile(receipt["id"], "posted", "Second conflicting decision")


async def test_feedback_poll_does_not_reconcile_an_active_same_process_write(store, monkeypatch):
    from firefly_forge.scm_connections import SCMConnections

    entered = asyncio.Event()
    release = asyncio.Event()

    async def handle(request):
        entered.set()
        await release.wait()
        return httpx.Response(201, json={"id": 19})

    scm = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    async def auth(repository, **kwargs):
        return "provider-grant", "https://api.github.com"

    monkeypatch.setattr(scm, "issue_authentication", auth)
    service, repo = await configured(store, scm=scm)
    raw, headers = delivery(repo)
    receipt = await service.receive("github", repo["id"], raw, headers)
    task = asyncio.create_task(service.process(receipt["id"]))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        await service.sync_feedback()
        assert (await service.receipt(receipt["id"]))["feedback_status"] == "writing"
    finally:
        release.set()
        await task


async def test_interrupted_issue_authoring_recovers_as_uncertain_without_reposting(store, monkeypatch):
    from firefly_forge.scm_connections import SCMConnections

    entered = asyncio.Event()
    writes = []

    async def handle(request):
        writes.append(request.url.path)
        entered.set()
        await asyncio.Event().wait()

    scm = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    async def auth(repository, **kwargs):
        return "provider-grant", "https://api.github.com"

    monkeypatch.setattr(scm, "issue_authentication", auth)
    service, repo = await configured(store, scm=scm)
    task = asyncio.create_task(
        service.create_issue(repo["id"], "Search", "Customer acceptance", idempotency_key="cancelled-issue")
    )
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    recovered = Collaboration(store, scm)
    await recovered.sync_feedback()
    receipt = (await recovered.receipts(repo["id"]))[0]
    assert receipt["feedback_status"] == "needs_reconciliation"
    assert receipt["reason"] == "remote_feedback_interrupted"
    duplicate = await recovered.create_issue(
        repo["id"], "Search", "Customer acceptance", idempotency_key="cancelled-issue"
    )
    assert duplicate["id"] == receipt["id"] and len(writes) == 1


async def test_lost_engine_ownership_prevents_remote_feedback_post(store, monkeypatch):
    from types import SimpleNamespace

    from firefly_forge.scm_connections import SCMConnections

    writes = []
    scm = SCMConnections(
        store,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: writes.append(request) or httpx.Response(201, json={}))
        ),
    )

    async def auth(repository, **kwargs):
        return "provider-grant", "https://api.github.com"

    async def lost():
        raise RuntimeError("Queue ownership was lost")

    monkeypatch.setattr(scm, "issue_authentication", auth)
    monkeypatch.setattr(store, "assert_ownership", lost)
    service, repo = await configured(store, scm=scm)
    service.deployments = SimpleNamespace(engine=SimpleNamespace(poller=object()))
    with pytest.raises(RuntimeError, match="ownership"):
        await service._remote_write(repo, {"body": "status"}, {"kind": "issue", "number": 12})
    assert writes == []
