# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host contracts for retained evidence, without execution or a database."""

import copy
import importlib.util
import io
import json
import zipfile

import pytest

from firefly_forge.journal import ZERO_HASH, digest

RUN, REPO = "a" * 32, "b" * 32


def module():
    assert importlib.util.find_spec("firefly_forge.run_evidence") is not None, (
        "A bounded native run-evidence reader is required"
    )
    from firefly_forge import run_evidence

    return run_evidence


def sample():
    plan = {
        "version": 1,
        "revision": 1,
        "run_id": RUN,
        "repository_id": REPO,
        "project_id": None,
        "component_id": None,
        "component_snapshot": None,
        "tasks": [{"id": "prepare", "kind": "local"}],
    }
    plan = {**plan, "digest": digest(plan)}
    entry = {
        "run_id": RUN,
        "sequence": 1,
        "task_id": "plan",
        "idempotency_key": "c" * 32,
        "kind": "plan",
        "payload": {"revision": 1, "digest": plan["digest"]},
        "created_at": "2026-10-15T00:00:00+00:00",
        "previous_hash": ZERO_HASH,
    }
    entry["digest"] = digest(entry)
    run = {
        "id": RUN,
        "repository_id": REPO,
        "project_id": None,
        "component_id": None,
        "status": "paused",
        "pause": {"kind": "provider_outcome_unknown"},
        "artifacts": [{"name": "changes.zip", "size": 20, "url": f"/api/v1/runs/{RUN}/artifacts/changes.zip"}],
    }
    return {
        "run": run,
        "plan": plan,
        "journal": [entry],
        "tasks": [],
        "tools": [],
        "activity": [
            {"id": "d" * 32, "sequence": 1, "data": {"id": "d" * 32, "stage": "paused", "message": "Résumé retained"}}
        ],
    }


def test_complete_bundle_is_repeatable_and_does_not_certify_old_source_zip():
    m = module()
    data = sample()
    before = copy.deepcopy(data)
    snap = m.build_evidence(**data)
    query = m.EvidenceQuery()
    page = m.evidence_page(snap, query)
    assert page["manifest"]["journal"]["valid"] is True
    assert page["manifest"]["run"]["pause"]["kind"] == "provider_outcome_unknown"
    assert page["manifest"]["artifacts"][0]["integrity"] == "not_historically_bound"
    content = m.evidence_zip(snap)
    assert content == m.evidence_zip(m.build_evidence(**data))
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.comment == b""
        for info in archive.infolist():
            assert info.create_system == 3
            assert info.external_attr >> 16 == 0o100600
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.extra == info.comment == b"" and not info.is_dir()
        assert set(archive.namelist()) == {
            "manifest.json",
            "run.json",
            "plan.json",
            "tasks.json",
            "journal.json",
            "tools.json",
            "activity.json",
            "artifacts.json",
        }
        manifest = json.loads(archive.read("manifest.json"))
        import hashlib

        for member in manifest["members"]:
            assert hashlib.sha256(archive.read(member["name"])).hexdigest() == member["sha256"]
        assert json.loads(archive.read("journal.json")) == data["journal"]
    assert data == before


@pytest.mark.parametrize("change", ["journal_hash", "plan_hash", "run_scope", "invented_repository"])
def test_unverified_historical_bindings_fail_closed(change):
    m = module()
    data = sample()
    if change == "journal_hash":
        data["journal"][0]["payload"]["revision"] = 2
    if change == "plan_hash":
        data["plan"]["repository_id"] = "f" * 32
    if change == "run_scope":
        data["run"]["repository_id"] = "f" * 32
    if change == "invented_repository":
        data["run"]["component_snapshot"] = {"repository_id": "f" * 32}
    with pytest.raises(m.EvidenceUnavailable):
        m.build_evidence(**data)


@pytest.mark.parametrize("change", [None, "revision", "definition"])
def test_repository_authority_comes_from_the_verified_native_project_cut(change):
    import hashlib

    from firefly_forge.project_models import ProjectDefinition

    m = module()
    data = sample()
    definition = ProjectDefinition.model_validate(
        {
            "name": "Fictional retained product",
            "description": "Two separately registered components",
            "topology": "multi_repo",
            "owners": ["owner"],
            "components": [
                {"id": "api", "name": "API", "repository_id": REPO, "responsibility": "Service"},
                {"id": "jobs", "name": "Jobs", "repository_id": "e" * 32, "responsibility": "Background work"},
            ],
        }
    ).model_dump(mode="json")
    project_digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
    component = {"project_revision": 2, "project_digest": project_digest, "project_definition": definition}
    scope = {"project_id": "f" * 32, "component_id": "api", "component_snapshot": component}
    data["run"].update(scope)
    data["plan"].update(scope)
    data["plan"]["digest"] = digest({k: v for k, v in data["plan"].items() if k != "digest"})
    data["journal"][0]["payload"]["digest"] = data["plan"]["digest"]
    data["journal"][0]["digest"] = digest({k: v for k, v in data["journal"][0].items() if k != "digest"})
    data["project_revision"] = {
        "id": "f" * 32,
        "revision": 2,
        "digest": project_digest,
        "definition": copy.deepcopy(definition),
    }
    data["activity"][0]["data"]["repository_id"] = "1" * 32
    if change == "revision":
        data["project_revision"]["revision"] = 3
    elif change == "definition":
        data["project_revision"]["definition"]["components"][1]["repository_id"] = "1" * 32
    if change is not None:
        with pytest.raises(m.EvidenceUnavailable):
            m.build_evidence(**data)
    else:
        assert m.build_evidence(**data).repositories == frozenset({REPO, "e" * 32})


def test_page_is_bound_to_cut_and_filters_and_never_restarts_silently():
    m = module()
    snap = m.build_evidence(**sample())
    first = m.evidence_page(snap, m.EvidenceQuery(limit=1))
    assert first["next_after"] == 1
    with pytest.raises(ValueError):
        m.EvidenceQuery(after=1)
    second = m.evidence_page(
        snap,
        m.EvidenceQuery(
            after=1, expected_cut=first["manifest"]["digest"], expected_filter=first["filter_digest"], limit=1
        ),
    )
    assert second["items"][0]["id"] != first["items"][0]["id"]
    with pytest.raises(m.EvidenceCutChanged):
        m.evidence_page(snap, m.EvidenceQuery(expected_cut="f" * 64))
    with pytest.raises(m.EvidenceCutChanged):
        m.evidence_page(
            snap, m.EvidenceQuery(after=1, expected_cut=first["manifest"]["digest"], expected_filter="f" * 64)
        )


@pytest.mark.parametrize("value", [True, -1, 51, 1.0, "1"])
def test_query_limits_are_strict(value):
    with pytest.raises(ValueError):
        module().EvidenceQuery(limit=value)


async def test_authenticated_mcp_exposes_strict_read_only_query_contract():
    from firefly_forge.mcp_server import create_server

    tools = {tool.name: tool for tool in await create_server().list_tools()}
    assert "query_run_evidence" in tools, "Native agent operators need the bounded evidence reader"
    schema = tools["query_run_evidence"].input_schema
    assert schema["properties"]["query"]["additionalProperties"] is False
    assert schema["properties"]["query"]["properties"]["limit"]["maximum"] == 50


def test_sdk_query_and_explicit_export_use_existing_auth_and_no_post():
    import httpx

    from firefly_forge.client import ForgeClient

    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer fictional"
        return httpx.Response(200, content=b"archive" if request.url.path.endswith(".zip") else b'{"items":[]}')

    with ForgeClient("http://factory.invalid", "fictional") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="http://factory.invalid/api/v1/",
            headers={"Authorization": "Bearer fictional"},
            transport=httpx.MockTransport(handle),
        )
        assert hasattr(client, "run_evidence"), "SDK must expose the bounded query"
        assert client.run_evidence(RUN, {"limit": 1}) == {"items": []}
        assert client.export_run_evidence(RUN, "f" * 64) == b"archive"
        assert [c.method for c in calls] == ["GET", "GET"]
        assert calls[0].url.params["limit"] == "1"
        assert calls[1].url.params["expected_cut"] == "f" * 64
        with pytest.raises(ValueError):
            client.run_evidence(RUN, {"after": 1})
        assert len(calls) == 2


def add_entry(data, kind, key, payload, task="prepare"):
    previous = data["journal"][-1]
    entry = {
        "run_id": RUN,
        "sequence": previous["sequence"] + 1,
        "task_id": task,
        "idempotency_key": key,
        "kind": kind,
        "payload": payload,
        "created_at": "2026-10-15T00:00:00+00:00",
        "previous_hash": previous["digest"],
    }
    entry["digest"] = digest(entry)
    data["journal"].append(entry)


def tool_sample():
    data = sample()
    key = "e" * 32
    admission = {
        "run_id": RUN,
        "repository_id": REPO,
        "project_id": None,
        "component_id": None,
        "task_id": "prepare",
        "correlation_id": key,
        "tool": "internet_search",
        "arguments": {"query": "fictional"},
    }
    add_entry(data, "tool_intent", key, admission)
    result = {"output": "Original command-like tool output"}
    add_entry(
        data,
        "tool_receipt",
        key,
        {"correlation_id": key, "tool": "internet_search", "status": "succeeded", "result": result},
    )
    data["tools"] = [
        {
            "id": "f" * 32,
            "run_id": RUN,
            "task_id": "prepare",
            "correlation_id": key,
            "tool": "internet_search",
            "status": "succeeded",
            "data": {"admission": admission, "request_digest": digest(admission), "result": result},
        }
    ]
    return data


@pytest.mark.parametrize("change", ["pending_after_receipt", "changed_result", "no_intent"])
def test_mutable_tool_rows_cannot_rewrite_or_hide_their_wal_outcome(change):
    m = module()
    data = tool_sample()
    if change == "pending_after_receipt":
        data["tools"][0]["status"] = "pending"
    if change == "changed_result":
        data["tools"][0]["data"]["result"] = {"output": "Changed"}
    if change == "no_intent":
        del data["journal"][1]
        data["journal"][1]["sequence"] = 2
        data["journal"][1]["previous_hash"] = data["journal"][0]["digest"]
        data["journal"][1]["digest"] = digest({k: v for k, v in data["journal"][1].items() if k != "digest"})
    with pytest.raises(m.EvidenceUnavailable):
        m.build_evidence(**data)


def test_exact_tool_record_and_full_bundle_preserve_original_output():
    m = module()
    data = tool_sample()
    snap = m.build_evidence(**data)
    result = m.evidence_page(snap, m.EvidenceQuery(record_id="tool:" + "e" * 32))
    assert result["items"][0]["payload"]["data"]["result"] == {"output": "Original command-like tool output"}
    assert result["items"][0]["omitted_bytes"] == 0


@pytest.mark.parametrize("change", [None, "decision", "task", "terminal_row"])
def test_explicit_abandonment_is_bound_to_the_actual_broker_decision(change):
    m = module()
    data = tool_sample()
    row = data["tools"][0]
    data["journal"][-1]["kind"] = "tool_unknown"
    data["journal"][-1]["payload"]["status"] = "unknown"
    data["journal"][-1]["digest"] = digest({k: v for k, v in data["journal"][-1].items() if k != "digest"})
    row["status"] = "abandoned"
    row["data"].update(reconciliation_id="7" * 32, decision_actor={"user_id": "8" * 32})
    add_entry(
        data,
        "tool_reconciled",
        "7" * 32,
        {
            "correlation_id": row["correlation_id"],
            "decision": "abandoned_without_replay",
            "actor": row["data"]["decision_actor"],
        },
    )
    if change == "decision":
        data["journal"][-1]["payload"]["decision"] = "succeeded"
    elif change == "task":
        data["journal"][-1]["task_id"] = "other"
    elif change == "terminal_row":
        row["status"] = "succeeded"
    data["journal"][-1]["digest"] = digest({k: v for k, v in data["journal"][-1].items() if k != "digest"})
    if change is None:
        assert m.build_evidence(**data).manifest["journal"]["valid"]
    else:
        with pytest.raises(m.EvidenceUnavailable):
            m.build_evidence(**data)


async def test_worker_does_not_borrow_another_task_frozen_grant(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from firefly_forge.project_access import AccessDenied

    m = module()

    @asynccontextmanager
    async def unit():
        yield None

    service = m.RunEvidence(SimpleNamespace(_unit=unit))

    async def rows(*args):
        return [
            SimpleNamespace(
                data={
                    "agent": "agentic",
                    "execution_actor": {"authentication_method": "local_bootstrap", "user_id": "9" * 32},
                    "role_snapshot": {},
                }
            )
        ]

    async def authorize(run):
        return None

    monkeypatch.setattr(service, "_rows", rows)
    monkeypatch.setattr(service.access, "authorize_execution", authorize)
    from firefly_forge import agent_roles

    monkeypatch.setattr(
        agent_roles,
        "stage_allowed_tools",
        lambda snapshot, stage, provider: {"read_run_evidence"} if stage == "implement" else set(),
    )
    with pytest.raises(AccessDenied, match="frozen role"):
        await service.for_worker(RUN, m.EvidenceQuery(limit=1), task_id="review")


@pytest.mark.parametrize("failure", ["revocation", "credentials", "cut", "reader"])
async def test_service_holds_private_results_after_admission_changes(monkeypatch, failure):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import UUID

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

    m = module()
    data = sample()
    calls = []
    actor = Principal(UUID(int=9), "fictional", "reader", authentication_method="local_bootstrap")
    allowed = True

    class Session:
        async def execute(self, statement):
            return SimpleNamespace(one_or_none=lambda: (UUID(REPO), None))

    class PrivateStore:
        @asynccontextmanager
        async def _unit(self):
            yield Session()

        async def admit_credentials(self, value):
            nonlocal allowed
            calls.append("admission")
            if failure == "credentials":
                raise ValueError("Fictional protected output")
            if failure == "revocation":
                allowed = False
            if failure == "cut":
                data["run"]["status"] = "failed"
            if failure == "reader":
                CURRENT_PRINCIPAL.set(None)

        def reject_credentials(self, value):
            calls.append("local-fence")

    class Access:
        async def current_administrator(self, principal):
            calls.append("authorize")
            if not allowed:
                raise AccessDenied("Revoked")
            return True

        async def require_repository(self, principal, repo, permission):
            assert repo == REPO and permission == "read"

    service = m.RunEvidence(PrivateStore())
    service.access = Access()

    async def capture(run_id):
        return m.build_evidence(**copy.deepcopy(data))

    monkeypatch.setattr(service, "_capture", capture)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises((m.EvidenceUnavailable, m.EvidenceCutChanged, AccessDenied)):
            await service.query(RUN, m.EvidenceQuery())
        assert calls[0] == "authorize" and calls.count("admission") == 1
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_size_is_refused_before_materializing_native_rows():
    from types import SimpleNamespace

    from sqlalchemy import select

    from firefly_forge.persistence import ForgeJournalEntity

    m = module()

    class Session:
        async def execute(self, statement):
            return SimpleNamespace(one=lambda: (2049, 10))

        async def scalars(self, statement):
            pytest.fail("Oversized rows were materialized")

    with pytest.raises(m.EvidenceUnavailable, match="evidence_limit"):
        await m.RunEvidence(None)._rows(Session(), select(ForgeJournalEntity), 2048, [m.MAX_BYTES])


@pytest.mark.parametrize("cut", [None, "", True])
def test_export_requires_an_explicit_valid_reviewed_cut_before_read(cut, monkeypatch):
    m = module()
    service = m.RunEvidence(None)

    async def no_read(*args):
        pytest.fail("Invalid export must not read private evidence")

    monkeypatch.setattr(service, "_read", no_read)
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(service.export(RUN, cut))


def test_artifact_reference_count_is_bounded_before_export():
    m = module()
    data = sample()
    data["run"]["artifacts"] = data["run"]["artifacts"] * 129
    with pytest.raises(m.EvidenceUnavailable, match="evidence_limit"):
        m.build_evidence(**data)


def test_cli_export_keeps_existing_files_and_query_is_read_only(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from click.testing import CliRunner

    from firefly_forge import cli

    calls = []

    class Client:
        def run_evidence(self, run_id, query):
            calls.append(("query", run_id, query))
            return {"items": []}

        def export_run_evidence(self, run_id, cut):
            calls.append(("export", run_id, cut))
            return b"fictional-export"

    monkeypatch.setattr(cli, "client", lambda: nullcontext(Client()))
    runner = CliRunner()
    result = runner.invoke(cli.main, ["evidence", "query", RUN, "--limit", "1"])
    assert result.exit_code == 0 and calls[0][2]["limit"] == 1
    output = tmp_path / "retained.zip"
    output.write_bytes(b"original")
    result = runner.invoke(cli.main, ["evidence", "export", RUN, "--expected-cut", "a" * 64, "--output", str(output)])
    assert result.exit_code != 0 and output.read_bytes() == b"original"


def test_large_record_has_an_honest_preview_but_no_oversized_exact_return():
    m = module()
    data = sample()
    data["activity"][0]["data"]["message"] = "x" * (m.MAX_RECORD_BYTES + 1)
    snapshot = m.build_evidence(**data)
    page = m.evidence_page(snapshot, m.EvidenceQuery(kind="activity"))
    assert page["items"][0]["omitted_bytes"] > 0
    assert len(page["items"][0]["payload"]["preview"].encode()) <= 2048
    with pytest.raises(m.EvidenceUnavailable, match="evidence_record_limit"):
        m.evidence_page(snapshot, m.EvidenceQuery(record_id="activity:" + "d" * 32))


def test_native_reader_is_optional_and_not_a_default_workspace_grant(tmp_path):
    from firefly_forge.agent_roles import RoleRegistry
    from firefly_forge.workspace_tools import tool_catalog

    available = {row["id"]: row for row in tool_catalog()}
    assert available["read_run_evidence"]["availability"] == "not_configured"
    roles = RoleRegistry(tmp_path).current()["document"]["roles"]
    assert all("read_run_evidence" not in role["allowed_tools"] for role in roles)
