# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared PG/ASGI intake controls; only the external GitHub transport is recorded."""

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
from sqlalchemy import func, select, text
from starlette.testclient import TestClient

from firefly_forge.access_persistence import ForgeAccessEventEntity, ForgeMembershipRepository
from firefly_forge.api import create_application
from firefly_forge.collaboration import Collaboration
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.project_persistence import ForgeProjectEntity
from firefly_forge.scm_product_intake import EVENT_KIND, SCMProductIntake
from firefly_forge.scm_product_intake_controllers import SCMProductIntakeController
from firefly_forge.scm_product_intake_models import SCMProductIntakeCommandV1
from firefly_forge.store import Store


@pytest.fixture
def native_intake(tmp_path, postgres_schema):
    token, secret = "owned-intake-bootstrap-token", "owned-intake-webhook-fixture"
    observed = {"comments": [], "granted": True, "after_get": None}

    async def provider(request):
        from pyfly.data.transaction import current_unit_of_work

        assert current_unit_of_work() is None, "No native transaction may span outbound GitHub I/O"
        if request.method == "POST" and request.url.path == "/app/installations/34/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": "owned-intake-provider-token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        if request.method == "POST" and request.url.path == "/repos/team/stock/issues/3/comments":
            observed["comments"].append(json.loads(request.content))
            number = len(observed["comments"]) + 100
            return httpx.Response(
                201, json={"id": number, "html_url": f"https://github.com/team/stock/issues/3#issuecomment-{number}"}
            )
        assert request.method == "GET" and request.url.path == "/installation/repositories"
        if observed["after_get"]:
            await observed["after_get"]()
        repositories = [
            {
                "id": 42,
                "full_name": "team/stock",
                "clone_url": "https://github.com/team/stock.git",
                "default_branch": "main",
            }
        ]
        return httpx.Response(200, json={"repositories": repositories if observed["granted"] else []})

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
        service = context.get_bean(SCMProductIntake)
        store = context.get_bean(Store)
        collaboration = context.get_bean(Collaboration)
        assert service.store is store and collaboration.product_intake_authority is service
        assert context.get_bean(SCMProductIntakeController).intake is service
        transport = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        service.scm._client = transport
        resources.callback(http.portal.call, transport.aclose)

        async def connect():
            key = (
                rsa.generate_private_key(public_exponent=65537, key_size=2048)
                .private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
                )
                .decode()
            )
            await service.scm.configure(
                "github", {"values": {"app_id": "12", "installation_id": "34", "private_key": key}}
            )
            return await service.scm.connect("github", "42")

        repo = http.portal.call(connect)
        response = http.put(
            f"/api/v1/repositories/{repo['id']}/collaboration",
            json={
                "enabled": True,
                "trusted_actors": ["7"],
                "configuration_actors": ["7"],
                "channel_commands": ["project"],
                "webhook_secret": secret,
            },
        )
        assert response.status_code == 200, response.text
        assert "intake" not in response.json()["channel_commands"]
        comment = 0

        def receive(command):
            nonlocal comment
            comment += 1
            raw = json.dumps(
                {
                    "action": "created",
                    "installation": {"id": 34},
                    "repository": {"id": 42},
                    "sender": {"id": 7},
                    "issue": {"number": 3},
                    "comment": {"id": comment, "body": command},
                }
            ).encode()
            response = http.post(
                f"/api/v1/webhooks/github/{repo['id']}",
                content=raw,
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "native-intake-" + str(comment),
                    "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
                },
            )
            assert response.status_code == 202, response.text
            return response.json()

        ping = receive("/forge project")
        assert ping["status"] == "received" and ping["verification"]["external_actor_id"] == "7"
        path = f"/api/v1/repositories/{repo['id']}/scm-product-intake"
        consent_body = dict(
            version=1,
            request_id=uuid4().hex,
            expected_previous_digest=None,
            verified_receipt_id=ping["id"],
            expected_verified_reference_digest=digest(ping["verification"]),
            active=True,
            acknowledge_product_intake=True,
        )

        def consent():
            response = http.post(path + "/consents", json=consent_body)
            assert response.status_code == 201, response.text
            assert http.get(path + "/consents/" + consent_body["request_id"]).json() == response.json()
            return response.json()

        def command(grant):
            return dict(
                version=1,
                request_id=uuid4().hex,
                expected_consent_request_id=consent_body["request_id"],
                expected_consent_digest=grant["digest"],
                name="Stock",
                description="Track stock movements",
                component={
                    "id": "app",
                    "name": "Stock API",
                    "responsibility": "Track stock",
                    "backend": "pyfly",
                    "frontend": "none",
                },
                definition={
                    "customer": "Warehouse operator",
                    "problem": "Stock counts drift",
                    "journey": ["Count stock"],
                    "user_stories": ["As an operator, I count stock"],
                    "acceptance": ["The count is retained"],
                    "owners": ["Warehouse team"],
                },
            )

        def delivery(body):
            return receive("/forge intake " + json.dumps(body))

        def process(receipt):
            response = http.post(f"/api/v1/collaboration/receipts/{receipt['id']}/process")
            assert response.status_code == 200, response.text
            return response.json()

        async def projects():
            async with store._unit() as session:
                return await session.scalar(select(func.count()).select_from(ForgeProjectEntity))

        yield SimpleNamespace(
            http=http,
            store=store,
            service=service,
            collaboration=collaboration,
            repo=repo,
            consent=consent,
            consent_body=consent_body,
            path=path,
            command=command,
            delivery=delivery,
            process=process,
            observed=observed,
            projects=projects,
        )


def test_native_signed_intake_atomically_registers_owner_definition_and_immutable_original_receipt(native_intake):
    case = native_intake
    grant = case.consent()
    body = case.command(grant)
    delivery = case.delivery(body)
    result = case.process(delivery)
    assert result["status"] == "processed" and result["action_result"]["status"] == "registered"
    receipt = result["action_result"]
    assert receipt["verified_receipt_id"] == delivery["id"]
    assert receipt["request"] == SCMProductIntakeCommandV1.model_validate({"action": "intake", **body}).model_dump(
        mode="json"
    )
    assert case.http.get(case.path + "/requests/" + body["request_id"]).json() == receipt
    assert case.process(delivery) == result and len(case.observed["comments"]) == 1
    repeated = case.process(case.delivery(body))
    assert repeated["action_result"] == receipt and case.http.portal.call(case.projects) == 1
    product = case.http.portal.call(case.collaboration.product, case.repo["id"])
    assert product["revision"] == 1 and product["origin"] == {"kind": "manual"}
    assert product["definition"]["functional_reviewed"] is False and product["definition"]["ai_origin"] is None

    async def inspect():
        async with case.store._unit() as session:
            members = await ForgeMembershipRepository().for_project(UUID(receipt["project_id"]))
            assert [(member.user_id.hex, member.role) for member in members] == [(receipt["native_user_id"], "owner")]
            events = list(
                await session.scalars(
                    select(ForgeAccessEventEntity).where(
                        ForgeAccessEventEntity.project_id == UUID(receipt["project_id"])
                    )
                )
            )
            assert len([row for row in events if row.data["kind"] == EVENT_KIND]) == 1
            assert await session.scalar(text("SELECT count(*) FROM forge_runs")) == 0

    case.http.portal.call(inspect)

    async def mutate():
        async with case.store._unit() as session:
            await session.execute(
                text("UPDATE forge_access_events SET data = '{}'::jsonb WHERE id = :id"),
                {"id": UUID(receipt["event_id"])},
            )

    with pytest.raises(Exception, match="immutable"):
        case.http.portal.call(mutate)
    assert case.http.get(case.path + "/requests/" + body["request_id"]).json() == receipt
    other = case.process(case.delivery({**body, "request_id": uuid4().hex}))
    assert other["status"] == "denied" and case.http.portal.call(case.projects) == 1


def test_native_intake_requires_separate_consent_and_current_grant(native_intake):
    case = native_intake
    body = case.command({"digest": "f" * 64})
    result = case.process(case.delivery(body))
    assert result["status"] == "denied" and case.http.portal.call(case.projects) == 0
    grant = case.consent()
    case.observed["granted"] = False
    result = case.process(case.delivery(case.command(grant)))
    assert result["status"] == "denied" and case.http.portal.call(case.projects) == 0
    assert case.observed["comments"] == []


def test_native_identity_revoked_after_application_readback_never_commits_intake(native_intake):
    case = native_intake
    grant = case.consent()
    delivery = case.delivery(case.command(grant))

    async def revoke():
        async with case.store._unit():
            user = await ForgeIdentityUserRepository().find_by_id(UUID(grant["native_actor"]["user_id"]))
            assert user is not None
            user.enabled = False
            await ForgeIdentityUserRepository().save(user)

    case.observed["after_get"] = revoke
    result = case.http.portal.call(case.collaboration.process, delivery["id"])
    assert result["status"] == "denied" and case.http.portal.call(case.projects) == 0
    assert case.observed["comments"] == []


def test_native_concurrent_original_intake_deliveries_commit_one_product(native_intake):
    case = native_intake
    body = case.command(case.consent())
    deliveries = [case.delivery(body) for _ in range(2)]

    async def race():
        return await asyncio.wait_for(asyncio.gather(*(case.service.process(item["id"]) for item in deliveries)), 10)

    results = case.http.portal.call(race)
    assert results[0]["action_result"] == results[1]["action_result"]
    assert case.http.portal.call(case.projects) == 1 and case.observed["comments"] == []
    assert all(item["action_status"] == "completed" for item in results)
