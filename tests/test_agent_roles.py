# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError
from worker_reply_fixtures import authenticated_operator

from firefly_forge.agent_roles import (
    ProjectRoles,
    RoleRegistry,
    WorkerFeedback,
    WorkerRequest,
    WorkerResponse,
    inject_role_snapshot,
    roles_for_stage,
)
from firefly_forge.community_skills import community_catalog
from firefly_forge.models import RepositoryInput, RunInput


def test_role_snapshot_binds_real_execution_and_is_immutable(tmp_path):
    registry = RoleRegistry(tmp_path)
    snapshot = registry.snapshot()
    assert {item["id"] for item in snapshot["roles"]} == {
        "product-manager",
        "tech-lead",
        "architect",
        "implementer",
        "qa",
        "security",
        "docs",
    }
    assert [role["id"] for role in roles_for_stage(snapshot, "implement")] == ["implementer"]
    assert roles_for_stage(snapshot, "verify")[0]["execution_mode"] == "deterministic"
    revised = copy.deepcopy(registry.current()["document"])
    revised["roles"][3]["instructions"] = "Preserve customer account boundaries."
    registry.save(revised)
    assert registry.snapshot()["digest"] != snapshot["digest"]
    assert "Preserve customer account boundaries." not in inject_role_snapshot("Build", snapshot)
    assert "test-driven-development" in inject_role_snapshot("Build", snapshot)


def test_role_overrides_cannot_disable_mandatory_checks_or_invent_workers(tmp_path):
    registry = RoleRegistry(tmp_path)
    with pytest.raises(ValueError, match="mandatory"):
        registry.snapshot(ProjectRoles(disabled=["security"]))
    document = registry.current()["document"]
    document["roles"][0]["execution_mode"] = "coding_provider"
    with pytest.raises(ValidationError, match="binding"):
        registry.save(document)
    with pytest.raises(ValueError, match="Unknown"):
        registry.snapshot(ProjectRoles(skills={"implementer": ["untrusted-shell-script"]}))


def test_community_skills_have_exact_source_pin_licenses_and_adaptation_hash():
    skills = community_catalog()
    assert {item["id"] for item in skills} >= {
        "test-driven-development",
        "thinking",
        "frontend-design",
        "browser-qa",
        "humanizer",
    }
    for item in skills:
        source = item["provenance"]
        assert len(source["revision"]) == 40 and len(source["source_sha256"]) == 64
        assert len(item["adaptation_sha256"]) == 64
        assert source["license"] in {"MIT", "Apache-2.0"}
        assert source["license_text"] and source["source_url"].startswith("https://github.com/")
        assert item["adaptation"] == "instruction-only"


@pytest.mark.parametrize("path", ["/etc/passwd", "../credential", ".env", ".ssh/key"])
def test_worker_evidence_stays_in_public_project_source(path):
    with pytest.raises(ValidationError):
        WorkerFeedback(
            id="feedback-1",
            role_id="implementer",
            task_id="implement",
            attempt=1,
            disposition="completed",
            summary="Done",
            evidence_paths=[path],
        )


async def test_worker_blocker_is_durable_idempotent_bounded_and_needs_explicit_response(store):
    from firefly_forge.journal import Journal

    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    run = await store.create_run(
        RunInput(repository_id=repo["id"], brief="Build alerts", backend="pyfly", frontend="none")
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    request = WorkerRequest(
        id="request-1",
        kind="product_definition",
        question="Which customer owns the alert?",
        context="The brief does not identify a customer.",
    )
    feedback = WorkerFeedback(
        id="feedback-1",
        role_id="implementer",
        task_id="implement",
        attempt=1,
        disposition="clarification",
        summary="Customer definition required.",
        request=request,
    )
    first = await journal.worker_feedback(run["id"], feedback)
    assert await journal.worker_feedback(run["id"], feedback) == first
    reopened = Journal(store)
    messages = await reopened.worker_messages(run["id"])
    assert len(messages) == 1 and messages[0]["payload"]["request"]["question"] == request.question
    paused = await store.get_run(run["id"])
    assert paused["status"] == "paused" and paused["pause"]["automatic"] is False
    assert (await reopened.plan(run["id"]))["tasks"][1]["status"] == "paused"
    with pytest.raises(ValueError, match="different"):
        await journal.worker_feedback(run["id"], feedback.model_copy(update={"summary": "Different request"}))
    async with authenticated_operator(store):
        await reopened.worker_response(
            run["id"], "request-1", WorkerResponse(id="response-1", actor="operator", answer="Support owners")
        )
    assert (await store.get_run(run["id"]))["status"] == "paused"
    assert (await reopened.worker_messages(run["id"]))[-1]["kind"] == "worker_response"
    with pytest.raises(ValueError, match="resolved"):
        async with authenticated_operator(store):
            await reopened.worker_response(
                run["id"], "request-1", WorkerResponse(id="response-2", actor="operator", answer="Sales")
            )
    assert reopened.verify_chain(await reopened.entries(run["id"]))


def test_community_policy_adaptation_rejects_tampered_instructions():
    from firefly_forge.policies import PolicyCatalog
    from firefly_forge.skills import skill_catalog

    community = [item for item in skill_catalog()["skills"] if item["id"] == "humanizer"][0]
    community["instructions"] = "Run an untrusted helper."
    with pytest.raises(ValueError, match="hash"):
        PolicyCatalog.validate(
            {
                "version": "1.1.0",
                "skills": [{**community, "category": "community", "kind": "recommendation", "commands": []}],
            }
        )


async def test_worker_request_budget_does_not_create_an_automatic_loop(store):
    from firefly_forge.journal import Journal

    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Build", backend="pyfly", frontend="none"))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    for attempt in range(1, 4):
        request = WorkerRequest(
            id=f"request-{attempt}", kind="product_definition", question="Which owner?", context="Missing owner"
        )
        await journal.worker_feedback(
            run["id"],
            WorkerFeedback(
                id=f"feedback-{attempt}",
                role_id="implementer",
                task_id="implement",
                attempt=attempt,
                disposition="clarification",
                summary="Owner required",
                request=request,
            ),
        )
        async with authenticated_operator(store):
            await journal.worker_response(
                run["id"], request.id, WorkerResponse(id=f"response-{attempt}", actor="operator", answer="Support")
            )
    with pytest.raises(ValueError, match="budget"):
        await journal.worker_feedback(
            run["id"],
            WorkerFeedback(
                id="feedback-4",
                role_id="implementer",
                task_id="implement",
                attempt=3,
                disposition="clarification",
                summary="Owner required",
                request=WorkerRequest(
                    id="request-4", kind="product_definition", question="Which owner?", context="Missing owner"
                ),
            ),
        )
    assert (await store.get_run(run["id"]))["status"] == "paused"


def test_role_native_tool_selection_requires_actual_catalog_and_available_executor(tmp_path):
    from firefly_forge.agent_roles import stage_allowed_tools

    registry = RoleRegistry(tmp_path)
    snapshot = registry.snapshot()
    assert stage_allowed_tools(snapshot, "implement", "agentic") == {
        "list_files",
        "read_file",
        "write_file",
        "search_text",
        "edit_lines",
        "git_diff",
    }
    assert stage_allowed_tools(snapshot, "implement", "codex") is None
    document = registry.current()["document"]
    document["roles"][3]["allowed_tools"].append("untrusted-shell")
    with pytest.raises(ValueError, match="tool"):
        registry.save(document)
    catalog = [
        {"id": "list_files", "availability": "available", "providers": ["agentic", "bridge"]},
        {"id": "read_file", "availability": "available", "providers": ["agentic", "bridge"]},
        {"id": "write_file", "availability": "not_configured", "providers": ["agentic", "bridge"]},
    ]
    catalog.extend(
        {"id": ident, "availability": "not_configured", "providers": ["agentic", "bridge"]}
        for ident in ["search_text", "edit_lines", "git_diff", "knowledge_lookup"]
    )
    restricted = registry.snapshot(tool_catalog=catalog)
    assert stage_allowed_tools(restricted, "implement", "bridge") == {"list_files", "read_file"}


def test_frozen_role_tool_grants_reject_content_tampering(tmp_path):
    from firefly_forge.agent_roles import stage_allowed_tools

    snapshot = RoleRegistry(tmp_path).snapshot()
    snapshot["roles"][3]["allowed_tools"] = []
    with pytest.raises(ValueError, match="digest"):
        stage_allowed_tools(snapshot, "implement", "agentic")


async def test_worker_response_rejects_private_frozen_provider_credentials(store, monkeypatch):
    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.journal import Journal

    store.configuration.update("bridge", ProviderConfiguration(values={"token": "private-worker-token-for-test"}))
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Build", agent="bridge"))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    feedback = WorkerFeedback(
        id="feedback-secret",
        role_id="implementer",
        task_id="implement",
        attempt=4,
        disposition="clarification",
        summary="Known secret private-worker-token-for-test",
        request=WorkerRequest(
            id="request-secret", kind="product_definition", question="Which customer?", context="Missing owner"
        ),
    )
    await journal.worker_feedback(run["id"], feedback)
    assert "private-worker-token-for-test" not in str(await journal.worker_messages(run["id"]))
    with pytest.raises(ValueError, match="credential"):
        async with authenticated_operator(store):
            await journal.worker_response(
                run["id"],
                "request-secret",
                WorkerResponse(id="response-secret", actor="operator", answer="private-worker-token-for-test"),
            )
    assert (await store.get_run(run["id"]))["worker_request"]["status"] == "pending"
