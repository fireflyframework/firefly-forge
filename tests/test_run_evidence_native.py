# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared operator-owned native PG/DI cases; no external model or provider."""

import io
import json
import zipfile
from uuid import uuid4

import pytest
from pyfly.data.transaction import is_transaction_active
from test_campaign_reports import seeded
from test_tool_broker import invoke, seed

from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.run_evidence import EvidenceCutChanged, EvidenceQuery, EvidenceUnavailable, RunEvidence


async def test_native_retained_tool_result_exports_without_replay_or_wal_writes(store, tmp_path):
    actor = Principal(uuid4(), "local", "evidence-owner", authentication_method="local_bootstrap")
    async with store._unit() as session:
        session.add(ForgeIdentityUserEntity(id=actor.id, issuer=actor.issuer, subject=actor.subject, enabled=True))
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        _, broker, run, source = await seed(store, tmp_path)
        calls = []

        async def callback():
            calls.append(True)
            return {"output": "Retained fictional result"}

        await invoke(broker, run, source, "e" * 32, callback)
        before = await broker.journal.entries(run["id"])
        service = RunEvidence(store)
        page = await service.query(run["id"], EvidenceQuery(record_id="tool:" + "e" * 32))
        content = await service.export(run["id"], page["manifest"]["digest"])
        assert content == await service.export(run["id"], page["manifest"]["digest"])
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            assert json.loads(archive.read("journal.json")) == before
            assert Journal.verify_chain(json.loads(archive.read("journal.json")))
            assert json.loads(archive.read("tools.json"))[0]["data"]["result"]["output"] == "Retained fictional result"
        assert await broker.journal.entries(run["id"]) == before and calls == [True]
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_native_current_reader_revocation_after_admission_returns_no_private_cut(store, tmp_path):
    project, _, journal, campaign, context = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await journal.ensure_plan(run_id)
        viewer = Principal(uuid4(), "https://identity.invalid", "evidence-reader")
        await ProjectAccess(store).set_membership(project["id"], viewer, "viewer")
        reader = CURRENT_PRINCIPAL.set(viewer)
        try:

            async def revoke(value):
                assert not is_transaction_active()
                await ProjectAccess(store).set_membership(project["id"], viewer, None)

            store.vault_credential_admission = revoke
            with pytest.raises(AccessDenied):
                await RunEvidence(store).query(run_id, EvidenceQuery())
        finally:
            CURRENT_PRINCIPAL.reset(reader)
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_native_newly_registered_credentials_withhold_original_sealed_records(store, tmp_path):
    _, _, journal, campaign, context = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await journal.ensure_plan(run_id)
        key = (await journal.intent(run_id, "prepare"))["idempotency_key"]
        secret = "fictional-retained-output-" + uuid4().hex
        await journal.receipt(run_id, key, {"output": secret})
        before = await journal.entries(run_id)
        service = RunEvidence(store)
        old = await service.query(run_id, EvidenceQuery())
        store.credential_registry = lambda: {"NEW_CREDENTIAL": secret}
        with pytest.raises(EvidenceUnavailable, match="credential_admission"):
            await service.export(run_id, old["manifest"]["digest"])
        store.credential_registry = dict
        assert await journal.entries(run_id) == before
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_native_concurrent_new_activity_invalidates_the_old_cut(store, store_factory, tmp_path):
    _, _, journal, campaign, context = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await journal.ensure_plan(run_id)
        replica = await store_factory(store.path)
        before = await journal.entries(run_id)

        async def advance(value):
            assert not is_transaction_active()
            await replica.event(run_id, "review", "A later native observation")

        store.vault_credential_admission = advance
        with pytest.raises(EvidenceCutChanged):
            await RunEvidence(store).query(run_id, EvidenceQuery())
        assert await journal.entries(run_id) == before
    finally:
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("first_query", [{"limit": 1}, {}, {"tool_name": "execute_command"}])
async def test_native_optin_agent_queries_only_its_original_run_without_effects(store, tmp_path, first_query):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.agent_roles import ProjectRoles
    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.workspace_tools import ToolRuntimeContext, tool_catalog

    actor = Principal(uuid4(), "local", "native-evidence-reader", authentication_method="local_bootstrap")
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        async with store._unit():
            await ForgeIdentityUserRepository().save(
                ForgeIdentityUserEntity(id=actor.id, issuer=actor.issuer, subject=actor.subject, enabled=True)
            )
        repo = await store.add_repository(RepositoryInput(name="Fictional evidence", url=str(tmp_path)))
        run = await store.create_run(
            RunInput(
                repository_id=repo["id"],
                brief="Read retained source evidence",
                agent="agentic",
                roles=ProjectRoles(allowed_tools={"implementer": ["read_run_evidence"]}),
            ),
            prepared_tools=tool_catalog({"read_run_evidence": True}),
        )
        journal = Journal(store)
        await journal.ensure_plan(run["id"])
        before = await journal.entries(run["id"])
        calls = []

        async def read(query):
            calls.append(query)
            return await RunEvidence(store).for_worker(
                run["id"], EvidenceQuery.model_validate(query), task_id="implement"
            )

        model_queries = []

        def model(messages, info):
            if not calls:
                assert [tool.name for tool in info.function_tools] == ["read_run_evidence"]
                query = first_query if not model_queries else {"limit": 1}
                model_queries.append(query)
                return ModelResponse(parts=[ToolCallPart("read_run_evidence", {"query": query})])
            report = {
                "summary": "Evidence read; awaiting owner input",
                "worker_feedback": {
                    "id": "evidence-review",
                    "role_id": "implementer",
                    "task_id": "implement",
                    "attempt": 1,
                    "disposition": "clarification",
                    "summary": "Review retained facts",
                    "request": {
                        "id": "owner-review",
                        "kind": "product_definition",
                        "question": "Proceed with this evidence?",
                        "context": "Fictional fixture",
                    },
                },
            }
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, report)])

        async def emit(*args):
            pass

        await execute_agentic(
            tmp_path,
            "Inspect retained facts",
            "pyfly",
            "none",
            emit,
            model=FunctionModel(model),
            runtime_context=ToolRuntimeContext(
                tmp_path, tmp_path, allowed_tools=frozenset({"read_run_evidence"}), read_run_evidence=read
            ),
        )
        assert calls == [{"limit": 10 if first_query == {} else 1}]
        assert await journal.entries(run["id"]) == before
        with pytest.raises(ValueError):
            await read({"run_id": uuid4().hex})
    finally:
        CURRENT_PRINCIPAL.reset(context)


def test_native_api_di_sdk_reads_and_exports_through_current_authenticated_routes(
    tmp_path, postgres_schema, monkeypatch
):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.client import ForgeClient
    from firefly_forge.models import RepositoryInput, RunInput

    monkeypatch.setenv("FORGE_AUTH_MODE", "local_bootstrap")
    token = "fictional-native-evidence-operator-token"
    app = create_application(tmp_path / "api", token, role="api")

    class Client(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with Client(
        app,
        base_url="http://127.0.0.1/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        client = ForgeClient("http://127.0.0.1", token)
        client.http.close()
        client.http = http
        repo = client.add_repository(RepositoryInput(name="Native retained evidence", url=str(tmp_path)))
        run = client.create_run(
            RunInput(repository_id=repo["id"], brief="Inspect native retained facts", operation="test")
        )
        http.portal.call(app.state.runtime.context.get_bean(Journal).ensure_plan, run["id"])
        result = client.run_evidence(run["id"], {"limit": 1})
        assert result["manifest"]["journal"]["valid"] is True
        assert client.export_run_evidence(run["id"], result["manifest"]["digest"]).startswith(b"PK")
        assert http.get(f"runs/{run['id']}/evidence", headers={"Authorization": "Bearer incorrect"}).status_code == 401
