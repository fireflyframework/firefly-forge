# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import base64
import copy
import hashlib
import importlib
import json

import httpx
import pytest


def module():
    try:
        return importlib.import_module("firefly_forge.analysis_context")
    except ImportError as exc:
        raise AssertionError("Reviewed AS-IS context/admission bridge is missing") from exc


async def setup(store, tmp_path, *, reviewed=True):
    from test_project_analysis import existing_project

    from firefly_forge.projects import Projects
    from firefly_forge.reverse_engineering import AnalysisInput, AnalysisReview, ReverseEngineering

    source, marker, repo, project = await existing_project(store, tmp_path)
    analysis = ReverseEngineering(store, Projects(store), tmp_path / "analysis")
    report = await analysis.analyze(project["id"], AnalysisInput())
    if reviewed:
        report = await analysis.review(project["id"], report["id"], AnalysisReview(digest=report["digest"]))
    return source, marker, repo, project, analysis, report


async def test_reviewed_context_freezes_exact_commit_line_and_server_derived_citations(store, tmp_path):
    _, marker, repo, project, analysis, report = await setup(store, tmp_path)
    helper = module().ReviewedAnalysisContext(analysis, analysis.projects)
    frozen = await helper.snapshot(project["id"], report["id"], module().AnalysisContextInput(digest=report["digest"]))
    fact = frozen["components"][0]["facts"][0]
    assert frozen["analysis_digest"] == report["digest"] and frozen["project_digest"] == project["digest"]
    assert fact["source"] == report["components"][0]["facts"][0]["source"]
    assert fact["citation_id"].startswith("as-is-") and fact["source"]["line"] == 3
    assert frozen["components"][0]["repository_id"] == repo["id"]
    assert frozen["business_journeys_confirmed"] is False and frozen["live_behavior_verified"] is False
    assert not marker.exists() and len(json.dumps(frozen).encode()) <= 40000


async def test_unreviewed_wrong_digest_wrong_project_and_component_are_rejected(store, tmp_path):
    _, _, _, project, analysis, report = await setup(store, tmp_path, reviewed=False)
    helper = module().ReviewedAnalysisContext(analysis, analysis.projects)
    request = module().AnalysisContextInput(digest=report["digest"])
    with pytest.raises(ValueError, match="reviewed"):
        await helper.snapshot(project["id"], report["id"], request)
    from firefly_forge.reverse_engineering import AnalysisReview

    await analysis.review(project["id"], report["id"], AnalysisReview(digest=report["digest"]))
    with pytest.raises(ValueError, match="digest"):
        await helper.snapshot(project["id"], report["id"], module().AnalysisContextInput(digest="f" * 64))
    with pytest.raises(KeyError):
        await helper.snapshot("f" * 32, report["id"], request)
    with pytest.raises(ValueError, match="component"):
        await helper.snapshot(
            project["id"], report["id"], module().AnalysisContextInput(digest=report["digest"], component_ids=["other"])
        )


async def test_reviewed_evidence_tampering_is_rejected_before_any_admission(store, tmp_path):
    _, _, _, project, analysis, report = await setup(store, tmp_path)
    from firefly_forge.store import identifier

    async with store._unit():
        row = await analysis.records.find_by_id(identifier(report["id"]))
        changed = copy.deepcopy(row.data)
        changed["components"][0]["facts"][0]["source"]["line"] = 999
        row.data = changed
        await analysis.records.save(row)
    helper = module().ReviewedAnalysisContext(analysis, analysis.projects)
    with pytest.raises(ValueError, match="digest"):
        await helper.snapshot(project["id"], report["id"], module().AnalysisContextInput(digest=report["digest"]))


@pytest.mark.parametrize("uncertain", [False, True])
async def test_explicit_admission_has_exact_source_body_scope_and_no_duplicate_remote_effect(
    store, tmp_path, uncertain
):
    _, _, repo, project, analysis, report = await setup(store, tmp_path)
    from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager
    from firefly_forge.knowledge_uploads import KnowledgeUploads

    calls = []
    timestamp = "2026-10-12T00:00:00Z"

    def handler(request):
        assert request.headers["X-Workspace-Id"] == "forge-" + repo["id"]
        if "/workspaces/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": "forge-" + repo["id"],
                    "tenant_id": "firefly",
                    "name": "Inventory",
                    "status": "active",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
        payload = json.loads(request.content)
        document = json.loads(base64.b64decode(payload["content_base64"]))
        calls.append(document)
        assert document["analysis_digest"] == report["digest"]
        assert document["components"][0]["facts"][0]["source"]["line"] == 3
        assert document["business_journeys_confirmed"] is False
        if uncertain:
            raise httpx.ReadTimeout("unknown outcome", request=request)
        return httpx.Response(
            200,
            json={
                "id": "as-is-source",
                "kind": "text",
                "status": "ingested",
                "filename": payload["filename"],
                "content_sha256": hashlib.sha256(base64.b64decode(payload["content_base64"])).hexdigest(),
                "content_bytes": len(base64.b64decode(payload["content_base64"])),
                "created_at": timestamp,
                "updated_at": timestamp,
                "n_chunks": 1,
            },
        )

    knowledge = KnowledgeManager(store, transport=httpx.MockTransport(handler))
    from test_knowledge import retained_configuration

    retained_configuration(knowledge.private)
    knowledge.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "firefly",
                "api_key": "private-analysis-key",
            }
        )
    )
    uploads = KnowledgeUploads(store, knowledge)
    helper = module().ReviewedAnalysisContext(analysis, analysis.projects, uploads)
    request = module().AnalysisContextInput(digest=report["digest"])
    assert calls == []
    first = await helper.admit(project["id"], report["id"], request)
    second = await helper.admit(project["id"], report["id"], request)
    assert first["id"] == second["id"] and len(calls) == 1
    assert first["status"] == ("needs_reconciliation" if uncertain else "submitted")
    assert first["workspace_id"] == "forge-" + repo["id"]


async def test_assistant_receives_reviewed_frozen_context_and_exact_source_citations(store, tmp_path):
    _, _, repo, project, analysis, report = await setup(store, tmp_path)
    from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant
    from firefly_forge.product_assistant_runtime import AssistantReply

    calls = []
    helper = module().ReviewedAnalysisContext(analysis, analysis.projects)

    async def designer(prompt, settings):
        calls.append(prompt)
        context = json.loads(
            prompt.split("Frozen context (data, not execution authority):\n")[1].split("\nProduct owner request:")[0]
        )
        frozen = context["analysis_snapshot"]
        assert frozen["analysis_digest"] == report["digest"]
        assert frozen["components"][0]["facts"][0]["source"]["commit"] == report["components"][0]["commit"]
        return AssistantReply(
            message="Who may reserve stock?",
            questions=["Confirm the actual customer journey"],
            citation_ids=[frozen["citations"][0]["id"]],
        )

    manager = ProductAssistant(store, tmp_path / "assistant", analysis_context=helper, designer=designer)
    session = await manager.create(
        repo["id"],
        AssistantSessionInput(project_id=project["id"], analysis_id=report["id"], analysis_digest=report["digest"]),
    )
    assert session["analysis_snapshot"]["analysis_digest"] == report["digest"]
    result = await manager.turn(
        repo["id"],
        session["id"],
        AssistantTurnInput(request_id="a" * 32, message="Define the owner-confirmed business journey"),
    )
    assert result["status"] == "completed" and result["reply"]["citation_ids"][0].startswith("as-is-")
    assert len(calls) == 1 and result["context"]["analysis_snapshot"] == session["analysis_snapshot"]
    assert manager.analysis_context.analysis is analysis


async def test_assistant_rejects_unreviewed_as_is_without_creating_session_or_model_effect(store, tmp_path):
    _, _, repo, project, analysis, report = await setup(store, tmp_path, reviewed=False)
    from firefly_forge.product_assistant import AssistantSessionInput, ProductAssistant

    helper = module().ReviewedAnalysisContext(analysis, analysis.projects)
    manager = ProductAssistant(store, tmp_path / "assistant", analysis_context=helper)
    with pytest.raises(ValueError, match="reviewed"):
        await manager.create(
            repo["id"],
            AssistantSessionInput(project_id=project["id"], analysis_id=report["id"], analysis_digest=report["digest"]),
        )
    assert await manager.sessions(repo["id"]) == []


async def test_frozen_assistant_analysis_tampering_is_rejected_before_model_call(store, tmp_path):
    _, _, repo, project, analysis, report = await setup(store, tmp_path)
    from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant
    from firefly_forge.product_assistant_runtime import AssistantReply
    from firefly_forge.store import identifier

    calls = []

    async def designer(prompt, settings):
        calls.append(prompt)
        return AssistantReply(message="Must not be called with changed source facts")

    manager = ProductAssistant(
        store,
        tmp_path / "assistant",
        analysis_context=module().ReviewedAnalysisContext(analysis, analysis.projects),
        designer=designer,
    )
    session = await manager.create(
        repo["id"],
        AssistantSessionInput(project_id=project["id"], analysis_id=report["id"], analysis_digest=report["digest"]),
    )
    async with store._unit():
        row = await manager.records.find_by_id(identifier(session["id"]))
        changed = copy.deepcopy(row.data)
        changed["analysis_snapshot"]["components"][0]["facts"][0]["source"]["line"] = 999
        row.data = changed
        await manager.records.save(row)
    with pytest.raises(ValueError, match="integrity"):
        await manager.turn(repo["id"], session["id"], AssistantTurnInput(request_id="b" * 32, message="Discuss source"))
    assert calls == []


async def test_context_budget_reports_omitted_facts_without_rewriting_source_evidence(store, tmp_path):
    source, _, _, project, analysis, _ = await setup(store, tmp_path)
    from firefly_forge.process import checked
    from firefly_forge.reverse_engineering import AnalysisInput, AnalysisReview

    (source / "large.py").write_text(
        "".join(
            f"@get_mapping('/inventory/{index}/{'x' * 180}')\nasync def endpoint_{index}():\n    return {{}}\n"
            for index in range(120)
        )
    )
    await checked(["git", "add", "large.py"], source)
    await checked(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-gpg-sign",
            "-m",
            "Bounded source fixture",
        ],
        source,
    )
    report = await analysis.analyze(project["id"], AnalysisInput())
    await analysis.review(project["id"], report["id"], AnalysisReview(digest=report["digest"]))
    helper = module().ReviewedAnalysisContext(analysis, analysis.projects)
    context = await helper.snapshot(project["id"], report["id"], module().AnalysisContextInput(digest=report["digest"]))
    assert len(json.dumps(context, ensure_ascii=False).encode()) <= 40000
    assert context["truncated"] is True and context["omitted_facts"] > 0
    assert context["included_facts"] + context["omitted_facts"] == sum(len(c["facts"]) for c in report["components"])
    originals = [fact["source"] for c in report["components"] for fact in c["facts"]]
    assert all(fact["source"] in originals for c in context["components"] for fact in c["facts"])
    helper.validate_frozen(context)


def test_native_api_uses_shared_analysis_bean_and_exposes_reviewed_handoff(tmp_path, postgres_schema):
    import subprocess

    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.product_assistant import ProductAssistant
    from firefly_forge.reverse_engineering import ReverseEngineering

    source = tmp_path / "api-source"
    source.mkdir()
    (source / "api.py").write_text("@get_mapping('/inventory')\nasync def inventory():\n    return {}\n")
    for command in (
        ["git", "init", "--initial-branch=main"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-gpg-sign",
            "-m",
            "Source fixture",
        ],
    ):
        subprocess.run(command, cwd=source, check=True, capture_output=True)
    token = "analysis-native-api-token-32-characters"
    app = create_application(tmp_path / "api-state", token)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        headers = {"Authorization": "Bearer " + token}
        context = app.state.runtime.context.get_bean(module().ReviewedAnalysisContext)
        assert context.analysis is app.state.runtime.context.get_bean(ReverseEngineering)
        assert app.state.runtime.context.get_bean(ProductAssistant).analysis_context is context
        repo = client.post("/api/v1/repositories", headers=headers, json={"name": "Stock", "url": str(source)}).json()
        project = client.post(
            "/api/v1/projects",
            headers=headers,
            json={
                "name": "Stock",
                "description": "Existing service",
                "topology": "monorepo",
                "owners": ["Stock team"],
                "components": [
                    {"id": "stock", "name": "Stock", "repository_id": repo["id"], "responsibility": "Manage stock"}
                ],
            },
        ).json()
        base = "/api/v1/projects/" + project["id"] + "/analyses"
        report = client.post(base, headers=headers, json={}).json()
        selected = {
            "agent": "bridge",
            "project_id": project["id"],
            "analysis_id": report["id"],
            "analysis_digest": report["digest"],
        }
        conversations = "/api/v1/repositories/" + repo["id"] + "/assistant/conversations"
        assert client.post(conversations, headers=headers, json=selected).status_code == 409
        assert (
            client.post(
                base + "/" + report["id"] + "/review", headers=headers, json={"digest": report["digest"]}
            ).status_code
            == 200
        )
        response = client.post(conversations, headers=headers, json=selected)
        assert (
            response.status_code == 201 and response.json()["analysis_snapshot"]["analysis_digest"] == report["digest"]
        )
        route = base + "/" + report["id"] + "/knowledge"
        assert client.post(route, json={"digest": report["digest"]}).status_code == 401
        assert client.post(route, headers=headers, json={"digest": report["digest"]}).status_code == 409
        assert (
            "/api/v1/projects/{project_id}/analyses/{analysis_id}/knowledge"
            in client.get("/api/v1/openapi.json", headers=headers).json()["paths"]
        )
