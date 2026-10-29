# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared native DI/PG fixture with a recorded App transport, never live GitHub."""

import copy
import json
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from communication_model_fixture import recorded_communication_models
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
from test_identity import fixture as oidc_fixture
from test_identity import request as identity_request
from worker_reply_fixtures import authenticated_operator

from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest
from firefly_forge.api import create_application
from firefly_forge.collaboration import Collaboration, CollaborationSettings
from firefly_forge.journal import Journal, digest
from firefly_forge.models import RunInput
from firefly_forge.project_access import ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.scm_poll_controllers import SCMPollController
from firefly_forge.scm_polling import SCMPolling
from firefly_forge.store import Store


@pytest.fixture
def native_poll(tmp_path, postgres_schema, monkeypatch, request):
    token = "owned-native-poll-bootstrap-token"
    state = {"clock": time.time(), "comments": [], "posts": [], "lost_reply": False, "permission": "write"}
    implementation = getattr(request, "param", None) == "implementation"
    monkeypatch.setattr("firefly_forge.scm_polling.time", lambda: state["clock"])

    def comment(number, body, issue=3):
        return dict(
            id=number,
            body=body,
            user={"id": 7, "login": "alice", "type": "User"},
            issue_url=f"https://api.github.com/repos/team/product/issues/{issue}",
            created_at="2026-10-16T10:00:00Z",
            updated_at="2026-10-16T10:00:00Z",
        )

    def provider(request):
        from pyfly.data.transaction import current_unit_of_work

        assert current_unit_of_work() is None, "Provider I/O must remain outside native units"
        path = request.url.path
        if request.method == "POST" and path == "/app/installations/34/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": "owned-native-poll-provider-fixture-token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        if request.method == "GET" and path == "/installation/repositories":
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
        if request.method == "GET" and path == "/repos/team/product/issues":
            return httpx.Response(200, json=[{"number": 3}, {"number": 4, "pull_request": {}}])
        if request.method == "GET" and path == "/repos/team/product/issues/comments":
            return httpx.Response(200, json=copy.deepcopy(state["comments"]))
        if request.method == "GET" and path.startswith("/repos/team/product/issues/comments/"):
            number = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=copy.deepcopy(next(c for c in state["comments"] if c["id"] == number)))
        if request.method == "GET" and path == "/repos/team/product/issues/3":
            return httpx.Response(200, json={"number": 3})
        if request.method == "GET" and path == "/repos/team/product/issues/4":
            return httpx.Response(
                200, json={"number": 4, **({} if state.get("pr_kind_changed") else {"pull_request": {}})}
            )
        if request.method == "GET" and path == "/repos/team/product/collaborators/alice/permission":
            return httpx.Response(200, json={"permission": state["permission"], "user": {"id": 7}})
        if request.method == "POST" and path in {
            "/repos/team/product/issues/3/comments",
            "/repos/team/product/issues/4/comments",
        }:
            state["posts"].append(json.loads(request.content))
            if state["lost_reply"]:
                raise httpx.ReadError("Owned post-admission response loss", request=request)
            number = len(state["posts"]) + 100
            return httpx.Response(
                201,
                json={
                    "id": number,
                    "html_url": f"https://github.com/team/product/issues/{path.split('/')[-2]}#issuecomment-{number}",
                },
            )
        raise AssertionError("Unexpected recorded App route: " + request.method + " " + path)

    with (
        recorded_communication_models() as install_communication,
        TestClient(
            create_application(tmp_path / "api", token, role="api"),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
            client=("127.0.0.1", 52602),
        ) as http,
        ExitStack() as resources,
    ):
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        context = http.app.state.runtime.context
        service = context.get_bean(SCMPolling)
        collaboration = context.get_bean(Collaboration)
        store = context.get_bean(Store)
        journal = context.get_bean(Journal)
        assert service.store is store is http.app.state.store
        assert context.get_bean(SCMPollController).polling is service
        assert collaboration.polling is service and store.scm_polling is service
        assert collaboration.deployments.engine.journal is journal
        install_communication(collaboration, http.app.state.engine)
        assert service.collaboration is collaboration and service.poller is None
        client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        service.scm._client = client
        resources.callback(http.portal.call, client.aclose)

        async def seed():
            if implementation:
                from native_client_knowledge_fixtures import activate_native_client_target

                await activate_native_client_target(store, tmp_path / "framework-prerequisite")
            key = (
                rsa.generate_private_key(public_exponent=65537, key_size=2048)
                .private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
                .decode()
            )
            await service.scm.configure(
                "github", {"values": {"app_id": "12", "installation_id": "34", "private_key": key}}
            )
            repo = await service.scm.connect("github", "42")
            identity, _, _, _, issue = await oidc_fixture(store)
            try:
                mapped = await identity.authenticate(
                    identity_request(bearer="Bearer " + issue("forge-api", sub="poll-mapped", roles=["forge-user"]))
                )
            finally:
                await identity.client.aclose()
            assert mapped is not None and not ProjectAccess.is_admin(mapped)
            async with authenticated_operator(store) as owner:
                project = await Projects(store).configure(
                    ProjectDefinition(
                        name="Recorded local issue conversation",
                        description="Native polling, consent and clarification without model execution",
                        topology="monorepo",
                        owners=["Owner"],
                        components=[
                            dict(id="api", name="API", repository_id=repo["id"], responsibility="Serve requests")
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
                        brief="Check the explicitly selected existing result",
                    )
                )
                await journal.ensure_plan(run["id"])
                if implementation:
                    await journal.intent(run["id"], "implement", 1)
                await journal.worker_feedback(
                    run["id"],
                    WorkerFeedback(
                        id="poll-native-feedback",
                        role_id="implementer" if implementation else "qa",
                        task_id="implement" if implementation else "verify",
                        attempt=1,
                        disposition="clarification",
                        summary="Choose the expected result",
                        request=WorkerRequest(
                            id="poll-native-question",
                            kind="product_definition",
                            question="Keep the existing result? Owned final feedback credential 471f",
                            context="A native operator must answer before any explicit continuation.",
                        ),
                    ),
                )
            # The API lifecycle has no consumer. Explicit test-driven steps use the real
            # Store execution fence, without starting Engine, scheduler or a worker loop.
            await store.acquire_ownership()
            return repo, project, run, mapped, owner

        repo, project, run, mapped, owner = http.portal.call(seed)
        settings = CollaborationSettings(
            enabled=True, trusted_actors=["7"], allowed_commands=["status"], channel_commands=["respond"]
        )
        configured = http.put(f"/api/v1/repositories/{repo['id']}/collaboration", json=settings.model_dump(mode="json"))
        assert configured.status_code == 200, configured.text
        path = f"/api/v1/projects/{project['id']}/repositories/{repo['id']}/scm-polling"
        state["comments"] = [comment(10, "Fictional prior discussion")]

        def configuration_body(project_id=project["id"]):
            response = http.get(path.replace(project["id"], project_id) + "/setup")
            assert response.status_code == 200, response.text
            setup = response.json()
            return dict(
                version=1,
                request_id=uuid4().hex,
                expected_previous_digest=setup["expected_previous_digest"],
                expected_project_binding_digest=setup["project_binding_digest"],
                expected_connection_revision=setup["connection_revision"],
                enabled=True,
            )

        def ready():
            body = configuration_body()
            response = http.post(path + "/configuration", json=body)
            assert response.status_code == 201, response.text
            exact = http.get(path + "/configuration/requests/" + body["request_id"])
            assert exact.status_code == 200 and exact.json() == response.json()
            for _ in range(2):
                http.portal.call(service.step, repo["id"])
            status = http.get(path + "/status")
            assert status.status_code == 200 and status.json()["ready"], status.text
            return response.json()

        def consent():
            response = http.get(path + "/candidates")
            assert response.status_code == 200, response.text
            page = response.json()
            observation = next(item for item in page["items"] if item["verification"]["resource_id"] == "10")
            body = dict(
                version=2,
                request_id=uuid4().hex,
                expected_previous_digest=None,
                expected_project_binding_digest=page["project_binding_digest"],
                observed_receipt_id=observation["id"],
                expected_reference_digest=digest(observation["verification"]),
                native_user_id=mapped.id.hex,
                component_ids=["api"],
                allowed_actions=["respond", "status"],
                active=True,
                acknowledge_api_observed_identity=True,
            )
            response = http.post(path + "/bindings", json=body)
            assert response.status_code == 201, response.text
            exact = http.get(path + "/bindings/requests/" + body["request_id"])
            assert exact.status_code == 200 and exact.json() == response.json()
            return response.json()

        async def retained():
            async with store._unit():
                row, retained_state = await service._state(repo["id"])
                rows = await service.inbox.find_latest(UUID(repo["id"]))
                return copy.deepcopy(row.data), copy.deepcopy(retained_state), [copy.deepcopy(r.data) for r in rows]

        def observe(action="respond", *, number=None, answer="Keep the existing result", issue=3):
            options = {"run_id": run["id"]}
            if action == "respond":
                options.update(request_id="poll-native-question", answer=answer)
            number = number or 20 + len(state["comments"])
            state["comments"].append(comment(number, "/forge " + action + " " + json.dumps(options), issue))
            state["clock"] += 3600
            for _ in range(2):
                http.portal.call(service.step, repo["id"])
            return next(
                row for row in http.portal.call(retained)[2] if row["verification"]["resource_id"] == str(number)
            )

        def process(delivery):
            response = http.post(f"/api/v1/collaboration/receipts/{delivery['id']}/process")
            assert response.status_code == 200, response.text
            return response.json()

        yield SimpleNamespace(
            http=http,
            store=store,
            service=service,
            journal=journal,
            collaboration=collaboration,
            scm=service.scm,
            repo=repo,
            project=project,
            run=run,
            owner=owner,
            mapped=mapped,
            path=path,
            token=token,
            state=state,
            comment=comment,
            ready=ready,
            consent=consent,
            observe=observe,
            process=process,
            retained=retained,
            configuration_body=configuration_body,
        )
