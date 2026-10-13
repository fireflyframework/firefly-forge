# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The lifecycle describes implemented participants without changing execution snapshots."""

import copy

from starlette.testclient import TestClient

from firefly_forge.agent_lifecycle import lifecycle_catalog
from firefly_forge.agent_roles import ProjectRoles, RoleRegistry, role_digest
from firefly_forge.api import create_application

TOKEN = "test-only-lifecycle-operator-token-32"


def test_lifecycle_covers_product_delivery_and_operations_without_inventing_agents(tmp_path):
    registry = RoleRegistry(tmp_path)
    frozen = registry.snapshot()
    saved = copy.deepcopy(frozen)
    result = lifecycle_catalog(
        registry, knowledge={"enabled": True, "api_key": "do-not-return", "readiness": {"state": "connected"}}
    )
    assert [phase["id"] for phase in result["phases"]] == [
        "product_definition",
        "design",
        "architecture",
        "prepare",
        "implement",
        "verify",
        "checkpoint",
        "assurance",
        "publication",
        "deploy",
        "observe",
        "maintain",
    ]
    assistants = result["phases"][0]["participants"]
    assert {item["task"] for item in assistants} == {"product", "functions"}
    assert all(item["execution_kind"] == "configured_model" and item["tools"] == [] for item in assistants)
    assert all({s["id"] for s in item["skills"]} == {"thinking", "evidence-first"} for item in assistants)
    assert result["knowledge"]["status"] == "configured_unverified"
    assert "do-not-return" not in str(result)
    implement = next(p for p in result["phases"] if p["id"] == "implement")
    assert implement["participants"][0]["execution_kind"] == "run_provider"
    assert implement["participants"][0]["skills"][0]["application"] == "injected_instruction"
    gates = next(p for p in result["phases"] if p["id"] == "verify")
    assert all(
        p["execution_kind"] == "deterministic" and p["knowledge_scope"] == "frozen_evidence"
        for p in gates["participants"]
    )
    assert registry.snapshot() == frozen == saved
    assert frozen["digest"] == role_digest({k: v for k, v in frozen.items() if k != "digest"})


def test_current_override_projection_changes_but_frozen_snapshot_does_not(tmp_path):
    registry = RoleRegistry(tmp_path)
    frozen = registry.snapshot()
    result = lifecycle_catalog(
        registry, overrides=ProjectRoles(enabled=["docs"], skills={"implementer": ["humanizer"]})
    )
    implement = next(p for p in result["phases"] if p["id"] == "implement")
    worker = next(p for p in implement["participants"] if p["id"] == "implementer")
    assert [s["id"] for s in worker["skills"]] == ["humanizer"]
    assert worker["skills"][0]["provenance"]["license"] == "MIT"
    assert next(p for p in implement["participants"] if p["id"] == "docs")["enabled"]
    assert all(t["availability"] == "not_configured" for t in result["tools"] if t["id"] == "execute_command")
    changed = registry.current()["document"]
    changed["roles"][3]["instructions"] = "Retain existing reservation behavior."
    registry.save(changed)
    newer = lifecycle_catalog(registry)
    assert newer["registry_digest"] != result["registry_digest"]
    assert frozen["roles"][3]["instructions"] != changed["roles"][3]["instructions"]


def test_api_catalog_requires_auth_and_projects_have_independent_overrides(tmp_path, postgres_schema):
    with TestClient(create_application(tmp_path, TOKEN, role="api"), client=("127.0.0.1", 50000)) as client:
        headers = {"Authorization": "Bearer " + TOKEN}
        assert client.get("/api/v1/agent-roles/lifecycle").status_code == 401
        global_catalog = client.get("/api/v1/agent-roles/lifecycle", headers=headers)
        assert global_catalog.status_code == 200, global_catalog.text
        repo = client.post(
            "/api/v1/repositories", headers=headers, json={"name": "Product", "url": str(tmp_path)}
        ).json()
        scoped = f"/api/v1/repositories/{repo['id']}/agent-roles/lifecycle"
        assert client.get(scoped).status_code == 401
        before = client.get(scoped, headers=headers).json()
        override = {"enabled": ["docs"], "skills": {"implementer": ["humanizer"]}}
        assert client.put(scoped.removesuffix("/lifecycle"), headers=headers, json=override).status_code == 200
        after = client.get(scoped, headers=headers).json()
        assert before["effective_digest"] != after["effective_digest"]
        assert after["scope"]["repository_id"] == repo["id"]
        assert after["permissions"] == {"edit_registry": True, "edit_project": True}
        assert (
            client.get("/api/v1/repositories/" + "f" * 32 + "/agent-roles/lifecycle", headers=headers).status_code
            == 404
        )


async def test_assistant_turn_freezes_and_applies_task_instructions_without_replaying_old_requests(store, tmp_path):
    from firefly_forge.models import RepositoryInput
    from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant
    from firefly_forge.product_assistant_runtime import AssistantReply

    repo = await store.add_repository(RepositoryInput(name="Product", url=str(tmp_path)))
    calls = []

    async def designer(prompt, settings):
        calls.append(prompt)
        return AssistantReply(message="Retained proposal for owner review")

    assistant = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    session = await assistant.create(repo["id"], AssistantSessionInput(task="design"))
    request = AssistantTurnInput(request_id="d" * 32, message="Design the reservation journey")
    first = await assistant.turn(repo["id"], session["id"], request)
    frozen = copy.deepcopy(first["context"]["instruction_snapshot"])
    assert {s["id"] for s in frozen["skills"]} == {"frontend-design", "accessibility", "thinking"}
    assert all(s["instructions"] in calls[0] for s in frozen["skills"])
    assert frozen["digest"] == role_digest({k: v for k, v in frozen.items() if k != "digest"})
    current = store.policies.current()
    document = {"version": "1.2.0", "skills": current["skills"]}
    next(s for s in document["skills"] if s["id"] == "accessibility")["instructions"] = (
        "Check focus order in the reservation form."
    )
    store.policies.private._write(
        store.policies.path, {"revision": "changed-guidance", "source": {"kind": "builtin"}, "document": document}
    )
    assert await assistant.turn(repo["id"], session["id"], request) == first
    assert len(calls) == 1
    second = await assistant.turn(repo["id"], session["id"], request.model_copy(update={"request_id": "e" * 32}))
    assert second["context"]["instruction_snapshot"]["digest"] != frozen["digest"]
    assert "Check focus order in the reservation form." in calls[1]
    retained = await assistant.session(repo["id"], session["id"])
    assert retained["turns"][0]["context"]["instruction_snapshot"] == frozen


async def test_scoped_lifecycle_respects_current_membership_and_cannot_edit_registry(
    tmp_path, postgres_schema, monkeypatch
):
    from httpx import ASGITransport, AsyncClient
    from test_project_access import principal, seed

    from firefly_forge.identity import IdentityService
    from firefly_forge.project_access import ProjectAccess

    user = principal()

    async def authenticate(self, request):
        return user

    monkeypatch.setattr(IdentityService, "authenticate", authenticate)
    app = create_application(tmp_path / "server", TOKEN, role="api")
    async with app.router.lifespan_context(app):
        repo, projects = await seed(app.state.store, tmp_path)
        access = app.state.runtime.context.get_bean(ProjectAccess)
        path = f"/api/v1/repositories/{repo['id']}/agent-roles/lifecycle"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            assert (await client.get("/api/v1/agent-roles/lifecycle")).status_code == 200
            assert (await client.get(path)).status_code == 403
            await access.set_membership(projects[0]["id"], user, "viewer")
            scoped = await client.get(path)
            assert scoped.status_code == 200
            assert scoped.json()["permissions"] == {"edit_registry": False, "edit_project": False}
            assert (
                next(t for t in scoped.json()["tools"] if t["id"] == "execute_command")["availability"]
                == "worker_unverified"
            )
            assert (await client.put(path.removesuffix("/lifecycle"), json={"enabled": ["docs"]})).status_code == 403
            assert (await client.put("/api/v1/agent-roles", json={})).status_code == 403
            await access.set_membership(projects[0]["id"], user, "owner")
            assert (await client.put(path.removesuffix("/lifecycle"), json={"enabled": ["docs"]})).status_code == 200
            await access.set_membership(projects[0]["id"], principal(), "owner")
            await access.set_membership(projects[0]["id"], user, None)
            assert (await client.get(path)).status_code == 403


async def test_every_assistant_task_has_actual_frozen_prompt_guidance(store, tmp_path):
    from firefly_forge.agent_lifecycle import ASSISTANT_SKILLS
    from firefly_forge.models import RepositoryInput
    from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant
    from firefly_forge.product_assistant_runtime import AssistantReply

    repo = await store.add_repository(RepositoryInput(name="Product", url=str(tmp_path)))
    prompts = []

    async def designer(prompt, settings):
        prompts.append(prompt)
        return AssistantReply(message="Proposal requires owner review")

    assistant = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    for index, task in enumerate(ASSISTANT_SKILLS):
        session = await assistant.create(repo["id"], AssistantSessionInput(task=task))
        result = await assistant.turn(
            repo["id"], session["id"], AssistantTurnInput(request_id=str(index) * 32, message="Clarify the product")
        )
        snapshot = result["context"]["instruction_snapshot"]
        assert [skill["id"] for skill in snapshot["skills"]] == ASSISTANT_SKILLS[task]
        assert all(skill["instructions"] in prompts[-1] for skill in snapshot["skills"])
        assert "tool-free dialogue" in prompts[-1]
        assert (await assistant.session(repo["id"], session["id"]))["journal"]["verified"]
    assert len(prompts) == 4


def test_profile_and_knowledge_projection_returns_metadata_without_credentials_or_access_claims(tmp_path):
    result = lifecycle_catalog(
        RoleRegistry(tmp_path),
        routing={"bindings": {"design": "design-profile"}},
        profiles=[
            {
                "id": "design-profile",
                "provider": "bridge",
                "config": {"token": "private-profile-token", "endpoint": "https://private.invalid"},
                "generation_verified": True,
            }
        ],
        knowledge={
            "enabled": True,
            "framework_workspace": "framework",
            "agent_token": "private-knowledge-token",
            "readiness": {"state": "connected"},
        },
    )
    designer = next(p for p in result["phases"] if p["id"] == "design")["participants"][0]
    assert designer["selection"] == {
        "status": "configured_unverified",
        "profile_id": "design-profile",
        "provider": "bridge",
        "task": "design",
    }
    assert result["knowledge"]["status"] == "configured_unverified"
    assert "private-profile-token" not in str(result) and "private-knowledge-token" not in str(result)
    assert "private.invalid" not in str(result) and "generation_verified" not in str(result)
