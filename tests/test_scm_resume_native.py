# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared native PG/ASGI controls; only the external SCM HTTP transport is recorded."""

import asyncio
import hashlib
import hmac
import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
from test_identity import fixture as oidc_fixture
from test_identity import request as identity_request
from worker_reply_fixtures import authenticated_operator

from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest
from firefly_forge.api import create_application
from firefly_forge.collaboration import Collaboration, CollaborationSettings
from firefly_forge.collaboration_controllers import CollaborationController
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal, digest
from firefly_forge.models import RunInput
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_resume_controllers import SCMResumeAuthorityController
from firefly_forge.scm_resume_cut import FROZEN_FIELDS
from firefly_forge.scm_resume_models import SCMResumeGrantInputV2
from firefly_forge.scm_resume_service import SCMResumeAuthority
from firefly_forge.store import Store


@pytest.fixture
def native_resume(tmp_path, postgres_schema, request):
    """Actual API-role DI, persisted identities/settings, signed ingress and Journal."""
    token = "owned-native-resume-bootstrap-token"
    secret = "owned-native-resume-webhook-fixture"
    comments = []
    implementation = getattr(request, "param", None) == "implementation"

    def provider(request):
        from pyfly.data.transaction import current_unit_of_work

        assert current_unit_of_work() is None, "Provider preflight must precede transaction locks"
        if request.method == "POST" and request.url.path == "/repos/team/product/issues/3/comments":
            comments.append(json.loads(request.content))
            number = len(comments) + 100
            return httpx.Response(
                201,
                json={"id": number, "html_url": f"https://github.com/team/product/issues/3#issuecomment-{number}"},
            )
        if request.method == "POST" and request.url.path == "/app/installations/34/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": "owned-native-resume-provider-fixture-token",
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

    with (
        TestClient(
            create_application(tmp_path / "api", token, role="api"),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
            client=("127.0.0.1", 52601),
        ) as http,
        ExitStack() as resources,
    ):
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        context = http.app.state.runtime.context
        service = context.get_bean(SCMResumeAuthority)
        collaboration = context.get_bean(Collaboration)
        store = context.get_bean(Store)
        journal = context.get_bean(Journal)
        assert service.store is store is http.app.state.store
        assert service.journal is journal and service.authority is context.get_bean(SCMActorAuthority)
        assert context.get_bean(SCMResumeAuthorityController).authority is service
        assert context.get_bean(CollaborationController).collaboration is collaboration
        assert collaboration.resume_authority is service
        provider_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        service.authority.scm._client = provider_client
        resources.callback(http.portal.call, provider_client.aclose)

        async def seed():
            scm = service.authority.scm
            key = (
                rsa.generate_private_key(public_exponent=65537, key_size=2048)
                .private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
                .decode()
            )
            await scm.configure("github", {"values": {"app_id": "12", "installation_id": "34", "private_key": key}})
            repo = await scm.connect("github", "42")
            identity, _, _, _, issue = await oidc_fixture(store)
            try:
                mapped = await identity.authenticate(
                    identity_request(bearer="Bearer " + issue("forge-api", sub="mapped-user", roles=["forge-user"]))
                )
            finally:
                await identity.client.aclose()
            assert mapped is not None and not ProjectAccess.is_admin(mapped)
            async with authenticated_operator(store) as owner:
                project = await Projects(store).configure(
                    ProjectDefinition(
                        name="Same-run consent",
                        description="Explicit clarification continuation without model execution",
                        topology="monorepo",
                        owners=["Owner"],
                        components=[
                            dict(id="api", name="API", repository_id=repo["id"], responsibility="Verify requests")
                        ],
                    )
                )
                await ProjectAccess(store).set_membership(project["id"], owner, "owner")
                await ProjectAccess(store).set_membership(project["id"], mapped, "developer")
                run = await store.create_run(
                    RunInput(
                        repository_id=repo["id"],
                        project_id=project["id"],
                        component_id="api",
                        operation="build" if implementation else "test",
                        agent="bridge",
                        frontend="none",
                        brief="Verify the owner-selected empty result",
                    )
                )
                await journal.ensure_plan(run["id"])
                if implementation:
                    await journal.intent(run["id"], "implement", 1)
                await journal.worker_feedback(
                    run["id"],
                    WorkerFeedback(
                        id="native-feedback",
                        role_id="implementer" if implementation else "qa",
                        task_id="implement" if implementation else "verify",
                        attempt=1,
                        disposition="clarification",
                        summary="Choose the expected result",
                        request=WorkerRequest(
                            id="native-question",
                            kind="product_definition",
                            question="Which result is expected?",
                            context="Verification needs an explicit choice.",
                        ),
                    ),
                )
            return repo, project, run, mapped, owner

        repo, project, run, mapped, owner = http.portal.call(seed)
        settings = CollaborationSettings(
            enabled=True,
            trusted_actors=["7"],
            allowed_commands=["status"],
            channel_commands=["respond"],
            webhook_secret=secret,
        )
        configured = http.put(
            f"/api/v1/repositories/{repo['id']}/collaboration",
            json={**settings.model_dump(mode="json", exclude={"webhook_secret"}), "webhook_secret": secret},
        )
        assert configured.status_code == 200
        assert "resume" not in configured.json()["channel_commands"]
        next_comment = 0

        def receive(action, body):
            nonlocal next_comment
            next_comment += 1
            raw = json.dumps(
                {
                    "action": "created",
                    "installation": {"id": 34},
                    "repository": {"id": 42},
                    "sender": {"id": 7},
                    "issue": {"number": 3},
                    "comment": {"id": next_comment, "body": "/forge " + action + " " + json.dumps(body)},
                }
            ).encode()
            response = http.post(
                f"/api/v1/webhooks/github/{repo['id']}",
                content=raw,
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "native-resume-" + str(next_comment),
                    "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
                },
            )
            assert response.status_code == 202, response.text
            return response.json()

        def process(delivery):
            response = http.post(f"/api/v1/collaboration/receipts/{delivery['id']}/process")
            assert response.status_code == 200, response.text
            return response.json()

        answer = receive(
            "respond", {"run_id": run["id"], "request_id": "native-question", "answer": "The empty result is expected"}
        )
        assert answer["status"] == "received" and answer["verification"]["external_actor_id"] == "7"
        binding_path = f"/api/v1/projects/{project['id']}/repositories/{repo['id']}/scm-actor-bindings"
        binding_body = {
            "version": 1,
            "request_id": uuid4().hex,
            "expected_previous_digest": None,
            "expected_project_binding_digest": digest(http.portal.call(Projects(store).project, project["id"])),
            "verified_receipt_id": answer["id"],
            "expected_verified_reference_digest": digest(answer["verification"]),
            "native_user_id": mapped.id.hex,
            "component_ids": ["api"],
            "allowed_actions": ["respond", "status", "cancel"],
            "active": True,
            "acknowledge_webhook_asserted_identity": True,
        }
        bound = http.post(binding_path, json=binding_body)
        assert bound.status_code == 201, bound.text
        binding = bound.json()
        assert process(answer)["status"] == "processed"
        current = http.portal.call(store.get_run, run["id"])
        assert current["status"] == "paused" and current["worker_request"]["status"] == "answered"
        grant_path = f"/api/v1/projects/{project['id']}/repositories/{repo['id']}/scm-resume-grants"

        def grant_body():
            return SCMResumeGrantInputV2(
                version=2,
                request_id=uuid4().hex,
                expected_previous_digest=None,
                expected_project_binding_digest=binding["project_binding_digest"],
                expected_binding_request_id=binding_body["request_id"],
                expected_binding_digest=binding["digest"],
                component_ids=["api"],
                allowed_actions=["resume"],
                active=True,
                acknowledge_same_run_resume=True,
            ).model_dump(mode="json")

        def consent():
            response = http.post(grant_path, json=grant_body())
            assert response.status_code == 201, response.text
            grant = response.json()
            exact = http.get(grant_path + "/requests/" + grant["request"]["request_id"])
            assert exact.status_code == 200 and exact.json() == grant
            current = http.get(grant_path + "/current/" + binding_body["request_id"])
            assert current.status_code == 200 and current.json() == grant
            return grant

        def review(grant):
            command = {"version": 2, "run_id": run["id"], "expected_grant_digest": grant["digest"]}
            delivery = receive("status", command)
            assert delivery["status"] == "received"
            result = process(delivery)
            assert result["status"] == "processed" and result["action_result"]["status"] == "paused"
            cut = result["action_result"]["review"]
            assert cut["digest"] == digest({key: value for key, value in cut.items() if key != "digest"})
            return {
                **command,
                "expected_question_id": cut["question_id"],
                "expected_response_digest": cut["response_digest"],
                "expected_cut_digest": cut["digest"],
            }

        yield SimpleNamespace(
            http=http,
            store=store,
            journal=journal,
            service=service,
            collaboration=collaboration,
            run=run,
            owner=owner,
            mapped=mapped,
            project=project,
            repo=repo,
            binding=binding,
            binding_body=binding_body,
            binding_path=binding_path,
            grant_path=grant_path,
            grant_body=grant_body,
            consent=consent,
            review=review,
            receive=receive,
            process=process,
            comments=comments,
        )


def test_native_consent_status_resume_and_same_delivery_have_one_native_resume(native_resume):
    case = native_resume
    before = case.http.portal.call(case.store.get_run, case.run["id"])
    grant = case.consent()
    command = case.review(grant)
    delivery = case.receive("resume", command)
    assert delivery["status"] == "received"
    result = case.process(delivery)
    assert result["status"] == "processed" and result["action_result"]["status"] == "queued"
    assert case.process(delivery) == result
    entries = case.http.portal.call(case.journal.entries, case.run["id"])
    assert Journal.verify_chain(entries)
    assert len([entry for entry in entries if entry["kind"] == "resume"]) == 1
    assert len([entry for entry in entries if entry["kind"] == "scm_resume_receipt"]) == 1
    after = case.http.portal.call(case.store.get_run, case.run["id"])
    assert {key: before.get(key) for key in FROZEN_FIELDS} == {key: after.get(key) for key in FROZEN_FIELDS}
    assert after["execution_actor"]["user_id"] == case.owner.id.hex
    assert len(case.comments) == 3  # Answer, status, resume; replay adds no outbound write.


def test_native_v2_without_consent_and_unversioned_resume_do_not_dispatch(native_resume):
    case = native_resume
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    unversioned = case.receive("resume", {"run_id": case.run["id"]})
    assert unversioned["status"] == "denied" and unversioned["reason"] == "command_disabled"
    versioned = case.receive("status", {"version": 2, "run_id": case.run["id"], "expected_grant_digest": "f" * 64})
    denied = case.process(versioned)
    assert denied["status"] == "denied" and denied["reason"] == "scm_resume_authority_required"
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before


def test_native_v1_revocation_invalidates_the_reviewed_v2_resume(native_resume):
    case = native_resume
    command = case.review(case.consent())
    body = {
        **case.binding_body,
        "request_id": uuid4().hex,
        "expected_previous_digest": case.binding["digest"],
        "active": False,
    }
    revoked = case.http.post(case.binding_path, json=body)
    assert revoked.status_code == 201, revoked.text
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    result = case.process(case.receive("resume", command))
    assert result["status"] == "denied"
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before


def test_native_journal_head_change_refuses_the_old_review_cut(native_resume):
    case = native_resume
    command = case.review(case.consent())

    async def record_review_note():
        async with case.store._unit():
            await case.journal._append(case.run["id"], "verify", "native-review-note", "observation", {"fixture": True})

    case.http.portal.call(record_review_note)
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    result = case.process(case.receive("resume", command))
    assert result["status"] == "denied"
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before


def test_native_original_identity_revocation_is_refused_by_the_service_not_a_new_actor(native_resume):
    case = native_resume
    command = case.review(case.consent())
    delivery = case.receive("resume", command)

    async def revoke_and_process():
        async with case.store._unit():
            users = ForgeIdentityUserRepository()
            user = await users.find_by_id(case.owner.id)
            assert user is not None
            user.enabled = False
            await users.save(user)
        # Like the poller: no authenticated caller is substituted for the original actor.
        return await case.collaboration.process(delivery["id"])

    before = case.http.portal.call(case.journal.entries, case.run["id"])
    result = case.http.portal.call(revoke_and_process)
    assert result["status"] == "denied"

    async def retained():
        async with case.store._unit():
            return [row.data for row in await case.journal.records.find_for_run(UUID(case.run["id"]))]

    assert case.http.portal.call(retained) == before


def test_native_concurrent_first_grants_have_one_admission_and_one_cas_refusal(native_resume):
    case = native_resume
    bodies = [SCMResumeGrantInputV2.model_validate(case.grant_body()) for _ in range(2)]

    async def race():
        async with authenticated_operator(case.store):
            return await asyncio.wait_for(
                asyncio.gather(
                    *(case.service.configure(case.project["id"], case.repo["id"], body) for body in bodies),
                    return_exceptions=True,
                ),
                10,
            )

    outcomes = case.http.portal.call(race)
    accepted = [item for item in outcomes if isinstance(item, dict)]
    refused = [item for item in outcomes if isinstance(item, AccessDenied)]
    assert len(accepted) == len(refused) == 1
    assert accepted[0]["revision"] == 1 and accepted[0]["previous_digest"] is None
    current = case.http.get(case.grant_path + "/current/" + case.binding_body["request_id"])
    assert current.status_code == 200 and current.json() == accepted[0]
    assert len(case.comments) == 1
