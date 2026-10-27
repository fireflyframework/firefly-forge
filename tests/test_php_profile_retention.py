# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PHP observations are local, receipt-bound journal data, never command replay permission."""

import copy
import hashlib
import uuid

import pytest

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal, digest
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.php_diagnostics import PHPProfileRequest, build_php_profile_argv, php_profile_observation
from firefly_forge.process import ProcessResult
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.workspace_tools import tool_catalog

IMAGE = "php@sha256:" + "a" * 64


async def completed_php(store, tmp_path, *, cleanup=True, output=None, actor=None, exit_code=7):
    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess

    await store.acquire_ownership()
    project = {}
    if actor is not None:
        from test_project_access import seed

        repo, projects = await seed(store, tmp_path)
        project = {"project_id": projects[0]["id"], "component_id": "service"}
        await ProjectAccess(store).set_membership(project["project_id"], actor, "developer")
    else:
        repo = await store.add_repository(RepositoryInput(name="Owned PHP", url="https://github.com/test/owned.git"))
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        run = await store.create_run(
            RunInput(
                **project,
                repository_id=repo["id"],
                brief="Observe a fictional PHP fixture",
                agent="agentic",
                sandbox=SandboxInput(mode="docker", image=IMAGE, context="owned-test", workspace_transport="archive"),
                roles=ProjectRoles(allowed_tools={"implementer": ["execute_command", "profile_php"]}),
            ),
            prepared_tools=tool_catalog({"execute_command": True, "profile_php": True}),
        )
    finally:
        CURRENT_PRINCIPAL.reset(context)
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "implement")
    request = PHPProfileRequest(script="workload.php")
    source = "<?php exit(7);"
    workspace = tmp_path / "owned-source"
    workspace.mkdir()
    (workspace / request.script).write_text(source)
    sha = hashlib.sha256(source.encode()).hexdigest()
    argv = build_php_profile_argv(request, sha)
    command = {"argv": argv, "configuration": run["sandbox_snapshot"]["configuration"], "workspace_digest": "c" * 64}
    execution = uuid.uuid5(uuid.NAMESPACE_URL, f"forge:sandbox:{run['id']}:implement:1:1").hex
    payload = {
        "parent_attempt": 1,
        "ordinal": 1,
        "container_name": "forge-sandbox-" + execution,
        "snapshot_digest": run["sandbox_snapshot"]["digest"],
        "request_digest": digest(command),
        "request": command,
    }
    result = ProcessResult(
        argv, exit_code, output or 'before exit\nFORGE_PHP_PROFILE_V1\n{"complete":true,"workload_exit_code":7}\n'
    )
    for effect, evidence in [
        ("create", {"container_id": "owned"}),
        ("start", {"exit_code": exit_code, "output": result.output}),
        *(([("cleanup", {"container_absent": True})]) if cleanup else []),
    ]:
        await journal.sandbox_effect_intent(run["id"], "implement", execution, effect=effect, payload=payload)
        await journal.sandbox_effect_receipt(run["id"], "implement", execution, effect=effect, evidence=evidence)
    binding = {
        "execution_id": execution,
        "request_digest": payload["request_digest"],
        "snapshot_digest": payload["snapshot_digest"],
        "workspace_digest": "c" * 64,
        "start_key": execution + ":start",
        "cleanup_key": execution + ":cleanup",
    }
    observation = php_profile_observation(request, sha, IMAGE, result, binding)
    return run, journal, request, observation, workspace, result, binding


async def test_completed_php_result_is_retained_as_direct_journal_observation(store, tmp_path, monkeypatch):
    run, journal, request, observation, workspace, result, binding = await completed_php(store, tmp_path)
    engine = Engine(store, tmp_path / "jobs", journal=journal)
    engine.sandbox = object()
    calls = []

    async def command(*args, **kwargs):
        calls.append(args)
        kwargs["_receipt_sink"](binding)
        return result

    monkeypatch.setattr(engine, "_project_command", command)
    actual = await engine._profile_php(run["id"], run, workspace, request)
    entries = await journal.entries(run["id"])
    retained = [entry for entry in entries if entry["kind"] == "diagnostic_observation"]
    assert len(retained) == 1
    assert retained[0]["payload"] == observation and retained[0]["task_id"] == "implement"
    assert retained[0]["idempotency_key"] == binding["execution_id"] + ":php_profile"
    assert actual["retention"] == {
        "status": "recorded",
        "sequence": retained[0]["sequence"],
        "digest": retained[0]["digest"],
    }
    assert Journal.verify_chain(entries) and len(calls) == 1


async def test_retention_exact_replay_is_inert_and_conflicting_payload_refuses(store, tmp_path):
    run, journal, request, observation, *_ = await completed_php(store, tmp_path)
    first = await journal.php_diagnostic(run["id"], request, observation)
    before = await journal.entries(run["id"])
    assert await journal.php_diagnostic(run["id"], request, observation) == first
    changed = {**observation, "output": "different"}
    with pytest.raises(ValueError):
        await journal.php_diagnostic(run["id"], request, changed)
    assert await journal.entries(run["id"]) == before


@pytest.mark.parametrize("field", ["exit_code", "output", "source_sha256", "image", "receipt", "argv"])
async def test_diagnostic_cannot_rebind_a_completed_command(store, tmp_path, field):
    run, journal, request, observation, *_ = await completed_php(store, tmp_path)
    changed = copy.deepcopy(observation)
    changed[field] = {
        "exit_code": 0,
        "output": "forged",
        "source_sha256": "f" * 64,
        "image": "php@sha256:" + "f" * 64,
        "receipt": None,
        "argv": ["php", "different.php"],
    }[field]
    before = await journal.entries(run["id"])
    with pytest.raises(ValueError):
        await journal.php_diagnostic(run["id"], request, changed)
    assert await journal.entries(run["id"]) == before


async def test_retention_requires_completed_cleanup(store, tmp_path):
    run, journal, request, observation, *_ = await completed_php(store, tmp_path, cleanup=False)
    with pytest.raises(ValueError):
        await journal.php_diagnostic(run["id"], request, observation)


@pytest.mark.parametrize("binding_present", [True, False])
async def test_retention_failure_preserves_known_exit_without_replaying_command(
    store, tmp_path, monkeypatch, binding_present
):
    run, journal, request, _, workspace, result, binding = await completed_php(store, tmp_path)
    engine = Engine(store, tmp_path / "jobs", journal=journal)
    engine.sandbox = object()
    calls = []

    async def command(*args, **kwargs):
        calls.append(args)
        if binding_present:
            kwargs["_receipt_sink"](binding)
        return result

    async def refused(*args):
        raise RuntimeError("private backend detail must not be returned")

    monkeypatch.setattr(engine, "_project_command", command)
    monkeypatch.setattr(journal, "php_diagnostic", refused)
    actual = await engine._profile_php(run["id"], run, workspace, request)
    assert actual["exit_code"] == 7 and actual["output"] == result.output and len(calls) == 1
    assert actual["retention"] == {"status": "unknown" if binding_present else "not_retained"}
    assert not any(entry["kind"] == "diagnostic_observation" for entry in await journal.entries(run["id"]))


@pytest.mark.parametrize("field,value", [("exit_code", True), ("output_truncated", 1), ("report_marker_observed", 1)])
async def test_raw_diagnostic_types_are_not_coerced_into_command_equality(store, tmp_path, field, value):
    run, journal, request, observation, *_ = await completed_php(store, tmp_path, exit_code=1, output="x" * 16001)
    with pytest.raises(ValueError):
        await journal.php_diagnostic(run["id"], request, {**observation, field: value})


async def test_full_command_marker_beyond_excerpt_keeps_exact_prefix_and_truncation(store, tmp_path):
    output = "x" * 16010 + "\nFORGE_PHP_PROFILE_V1\n{}\n"
    run, journal, request, observation, *_ = await completed_php(store, tmp_path, output=output)
    entry = await journal.php_diagnostic(run["id"], request, observation)
    assert entry["payload"]["output"] == "x" * 16000
    assert entry["payload"]["output_truncated"] is True
    assert entry["payload"]["report_marker_observed"] is True
    with pytest.raises(ValueError):
        await journal.php_diagnostic(run["id"], request, {**observation, "report_marker_observed": False})


async def test_diagnostic_receipt_from_another_run_cannot_be_retained(store, tmp_path):
    run, journal, request, observation, *_ = await completed_php(store, tmp_path)
    repo = await store.add_repository(RepositoryInput(name="Other", url="https://github.com/test/other.git"))
    other = await store.create_run(RunInput(repository_id=repo["id"], brief="Other run"))
    await journal.ensure_plan(other["id"])
    with pytest.raises(ValueError):
        await journal.php_diagnostic(other["id"], request, observation)
    assert not any(e["kind"] == "diagnostic_observation" for e in await journal.entries(run["id"]))


@pytest.mark.parametrize("change", ["revoke", "cancel", "credential"])
async def test_diagnostic_retention_rechecks_current_admission_after_credential_await(store, tmp_path, change):
    from test_project_access import principal

    from firefly_forge.project_access import AccessDenied, ProjectAccess

    actor = principal()
    run, journal, request, observation, *_ = await completed_php(store, tmp_path, actor=actor)
    before = await journal.entries(run["id"])

    async def admission(value):
        if change == "revoke":
            await ProjectAccess(store).set_membership(run["project_id"], actor, None)
        elif change == "cancel":
            await store.update(run["id"], cancel_request={"status": "requested"})
        else:
            store.credential_registry = lambda: {"FIXTURE_API_TOKEN": "before exit"}

    store.vault_credential_admission = admission
    with pytest.raises((AccessDenied, ValueError)):
        await journal.php_diagnostic(run["id"], request, observation)
    store.vault_credential_admission = None
    store.credential_registry = dict
    assert await journal.entries(run["id"]) == before


@pytest.mark.parametrize("change", ["revoke", "credential"])
async def test_authenticated_diagnostic_reader_is_rechecked_after_credential_await(store, tmp_path, change):
    from test_project_access import principal

    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

    actor = principal()
    run, journal, request, observation, *_ = await completed_php(store, tmp_path, actor=actor)
    await journal.php_diagnostic(run["id"], request, observation)
    reader = principal()
    access = ProjectAccess(store)
    await access.set_membership(run["project_id"], reader, "viewer")

    async def admission(value):
        if change == "revoke":
            await access.set_membership(run["project_id"], reader, None)
        else:
            store.credential_registry = lambda: {"FIXTURE_API_TOKEN": "before exit"}

    store.vault_credential_admission = admission
    token = CURRENT_PRINCIPAL.set(reader)
    try:
        with pytest.raises((AccessDenied, ValueError)):
            await journal.entries(run["id"])
    finally:
        CURRENT_PRINCIPAL.reset(token)
        store.vault_credential_admission = None
        store.credential_registry = dict
    assert Journal.verify_chain(await journal.entries(run["id"]))


@pytest.mark.parametrize("replacement", ["none", "another_reader"])
async def test_authenticated_reader_context_cannot_disappear_or_switch_during_admission(store, tmp_path, replacement):
    from test_project_access import principal

    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

    actor = principal()
    run, journal, request, observation, *_ = await completed_php(store, tmp_path, actor=actor)
    await journal.php_diagnostic(run["id"], request, observation)
    other = principal()
    await ProjectAccess(store).set_membership(run["project_id"], other, "viewer")

    async def admission(value):
        CURRENT_PRINCIPAL.set(None if replacement == "none" else other)

    token = CURRENT_PRINCIPAL.set(actor)
    store.vault_credential_admission = admission
    try:
        with pytest.raises(AccessDenied):
            await journal.entries(run["id"])
    finally:
        CURRENT_PRINCIPAL.reset(token)
        store.vault_credential_admission = None


def test_native_role_api_returns_the_actual_direct_diagnostic_and_valid_full_chain(tmp_path, postgres_schema):
    from native_client_knowledge_fixtures import activate_native_client_target
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.controllers import JournalController

    token = "owned-php-journal-api-token-32chars"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 52000),
    ) as http:
        store = http.app.state.store
        http.portal.call(activate_native_client_target, store, tmp_path / "framework-prerequisite")
        context = http.app.state.runtime.context
        assert context.get_bean(JournalController).journal.store is store
        run, journal, request, observation, *_ = http.portal.call(completed_php, store, tmp_path)
        saved = http.portal.call(journal.php_diagnostic, run["id"], request, observation)
        response = http.get(f"/api/v1/runs/{run['id']}/journal")
        assert response.status_code == 200
        actual = response.json()
        assert actual["valid"] is True and Journal.verify_chain(actual["entries"])
        assert actual["entries"][-1] == saved
        assert saved["payload"] == observation and saved["kind"] == "diagnostic_observation"
        assert len([entry for entry in actual["entries"] if entry["kind"] == "sandbox_receipt"]) == 3
        assert (
            http.get(f"/api/v1/runs/{run['id']}/journal", headers={"Authorization": "Bearer invalid"}).status_code
            == 401
        )


async def test_retention_serializes_actual_membership_revocation_through_commit(
    store, store_factory, tmp_path, monkeypatch
):
    import asyncio
    import contextvars

    from sqlalchemy import text
    from test_project_access import principal

    from firefly_forge.project_access import AccessDenied, ProjectAccess

    actor = principal()
    run, journal, request, observation, *_ = await completed_php(store, tmp_path, actor=actor)
    other = await store_factory(tmp_path / "other-state" / "factory.db")
    entered = asyncio.Event()
    binding = journal._php_diagnostic_binding
    revocation = None
    pid = None

    async def revoke():
        nonlocal pid
        async with other._unit() as connection:
            pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            entered.set()
            await ProjectAccess(other).set_membership(run["project_id"], actor, None)

    async def observe_fence(*args):
        nonlocal revocation
        result = await binding(*args)
        revocation = asyncio.create_task(revoke(), context=contextvars.Context())
        await asyncio.wait_for(entered.wait(), 3)
        async with store._unit() as connection:
            waiting = None
            for _ in range(100):
                waiting = await connection.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}
                )
                if waiting == "Lock" or revocation.done():
                    break
                await asyncio.sleep(0.01)
            assert waiting == "Lock" and not revocation.done()
        return result

    monkeypatch.setattr(journal, "_php_diagnostic_binding", observe_fence)
    try:
        retained = await journal.php_diagnostic(run["id"], request, observation)
        await asyncio.wait_for(revocation, 3)
        assert retained["payload"] == observation
        with pytest.raises(AccessDenied):
            await ProjectAccess(store).authorize_execution(await store.get_run(run["id"]))
        assert (await journal.entries(run["id"]))[-1] == retained
    finally:
        if revocation is not None and not revocation.done():
            revocation.cancel()
            await asyncio.gather(revocation, return_exceptions=True)


@pytest.mark.parametrize("boundary", ["binding", "lookup", "save", "exact_replay"])
async def test_late_registered_credential_rolls_back_diagnostic_append(store, tmp_path, monkeypatch, boundary):
    run, journal, request, observation, *_ = await completed_php(store, tmp_path)
    if boundary == "exact_replay":
        await journal.php_diagnostic(run["id"], request, observation)
    before = await journal.entries(run["id"])

    def register():
        store.credential_registry = lambda: {"FIXTURE_API_TOKEN": "before exit"}

    if boundary == "binding":
        original = journal._php_diagnostic_binding

        async def binding(*args):
            result = await original(*args)
            register()
            return result

        monkeypatch.setattr(journal, "_php_diagnostic_binding", binding)
    elif boundary in {"lookup", "exact_replay"}:
        original_find = journal.records.find_for_run
        calls = 0

        async def find(*args):
            nonlocal calls
            result = await original_find(*args)
            calls += 1
            if calls == 2:
                register()
            return result

        monkeypatch.setattr(journal.records, "find_for_run", find)
    else:
        original_append = journal._append

        async def append(*args):
            result = await original_append(*args)
            register()
            return result

        monkeypatch.setattr(journal, "_append", append)
    with pytest.raises(ValueError):
        await journal.php_diagnostic(run["id"], request, observation)
    store.credential_registry = dict
    assert await journal.entries(run["id"]) == before


async def test_mutable_command_projection_cannot_replace_immutable_source_bound_intent(store, tmp_path):
    run, journal, request, observation, *_ = await completed_php(store, tmp_path)
    before = await journal.entries(run["id"])
    changed = copy.deepcopy(observation)
    changed["source_sha256"] = "f" * 64
    changed["argv"] = build_php_profile_argv(request, changed["source_sha256"])
    async with store._unit():
        row = await journal.sandboxes.find_by_id(uuid.UUID(observation["receipt"]["execution_id"]))
        projection = copy.deepcopy(row.data)
        projection["request"]["request"]["argv"] = changed["argv"]
        projection["request"]["request_digest"] = digest(projection["request"]["request"])
        changed["receipt"]["request_digest"] = projection["request"]["request_digest"]
        row.data = projection
        await journal.sandboxes.save(row)
    with pytest.raises(ValueError, match="immutable command intent"):
        await journal.php_diagnostic(run["id"], request, changed)
    assert await journal.entries(run["id"]) == before


async def test_engine_never_returns_late_unadmitted_diagnostic_text_but_keeps_known_exit(store, tmp_path, monkeypatch):
    import json

    run, journal, request, _, workspace, result, binding = await completed_php(store, tmp_path)
    engine = Engine(store, tmp_path / "jobs", journal=journal)
    engine.sandbox = object()
    calls = []
    original_binding = journal._php_diagnostic_binding

    async def command(*args, **kwargs):
        calls.append(args)
        kwargs["_receipt_sink"](binding)
        return result

    async def registered(*args):
        execution = await original_binding(*args)
        store.credential_registry = lambda: {"FIXTURE_API_TOKEN": "before exit"}
        return execution

    monkeypatch.setattr(engine, "_project_command", command)
    monkeypatch.setattr(journal, "_php_diagnostic_binding", registered)
    actual = await engine._profile_php(run["id"], run, workspace, request)
    assert actual["exit_code"] == 7 and len(calls) == 1
    assert "before exit" not in json.dumps(actual)
    assert actual["diagnostic_unavailable"] is True
    assert not set(actual).intersection({"argv", "output", "script", "source_sha256", "receipt", "image"})
    store.credential_registry = dict
    assert not any(e["kind"] == "diagnostic_observation" for e in await journal.entries(run["id"]))


@pytest.mark.parametrize("boundary", ["retention", "final_admission", "final_cancel"])
async def test_security_refusal_after_completed_command_never_becomes_a_raw_result(
    store, tmp_path, monkeypatch, boundary
):
    import asyncio

    from test_project_access import principal

    from firefly_forge.project_access import AccessDenied, ProjectAccess

    actor = principal()
    run, journal, request, _, workspace, result, binding = await completed_php(store, tmp_path, actor=actor)
    engine = Engine(store, tmp_path / "jobs", journal=journal)
    engine.sandbox = object()
    calls = []

    async def command(*args, **kwargs):
        calls.append(args)
        kwargs["_receipt_sink"](binding)
        return result

    async def refused(*args):
        raise AccessDenied("Current execution permission was revoked")

    async def admission(value):
        if "retention" in value:
            if boundary == "final_cancel":
                await store.update(run["id"], cancel_request={"status": "requested"})
            else:
                await ProjectAccess(store).set_membership(run["project_id"], actor, None)

    monkeypatch.setattr(engine, "_project_command", command)
    if boundary == "retention":
        monkeypatch.setattr(journal, "php_diagnostic", refused)
    else:
        store.vault_credential_admission = admission
    with pytest.raises(asyncio.CancelledError if boundary == "final_cancel" else AccessDenied):
        await engine._profile_php(run["id"], run, workspace, request)
    store.vault_credential_admission = None
    assert len(calls) == 1
    executions = await journal.sandbox_executions(run["id"])
    assert len(executions) == 1 and executions[0]["effects"]["start"]["evidence"]["exit_code"] == 7


async def test_nested_journal_read_admits_credentials_without_its_callers_transaction(store, tmp_path):
    from cryptography.fernet import Fernet
    from pyfly.data.transaction import current_unit_of_work

    from firefly_forge.connection_oauth import ConnectionOAuth

    run, journal, request, observation, *_ = await completed_php(store, tmp_path)
    await journal.php_diagnostic(run["id"], request, observation)
    seen = []
    vault = ConnectionOAuth(store, encryption_key=Fernet.generate_key().decode(), browser_origin="")

    async def admission(value):
        seen.append(current_unit_of_work())
        assert current_unit_of_work() is None
        await vault.admit_credentials(value)
        assert current_unit_of_work() is None

    store.vault_credential_admission = admission
    try:
        async with store._unit():
            outer = current_unit_of_work()
            assert outer is not None
            await journal._run(run["id"])
            entries = await journal.entries(run["id"])
            assert current_unit_of_work() is outer
            assert await store.get_run(run["id"])
        assert seen == [None] and Journal.verify_chain(entries)
    finally:
        store.vault_credential_admission = None


@pytest.mark.parametrize("outcome", ["refusal", "cancel"])
async def test_nested_read_restores_caller_unit_on_admission_refusal_or_cancellation(store, tmp_path, outcome):
    import asyncio

    from pyfly.data.transaction import current_unit_of_work

    run, journal, request, observation, *_ = await completed_php(store, tmp_path)
    await journal.php_diagnostic(run["id"], request, observation)

    async def admission(value):
        assert current_unit_of_work() is None
        if outcome == "cancel":
            raise asyncio.CancelledError()
        raise ValueError("Fictional credential admission refusal")

    store.vault_credential_admission = admission
    try:
        async with store._unit():
            outer = current_unit_of_work()
            with pytest.raises(asyncio.CancelledError if outcome == "cancel" else ValueError):
                await journal.entries(run["id"])
            assert current_unit_of_work() is outer
            assert await store.get_run(run["id"])
    finally:
        store.vault_credential_admission = None


async def test_outer_repeatable_read_cannot_hide_a_reader_revocation_during_admission(store, tmp_path):
    from pyfly.data.transaction import Isolation, Propagation, TransactionTemplate, current_unit_of_work
    from test_project_access import principal

    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

    actor = principal()
    run, journal, request, observation, *_ = await completed_php(store, tmp_path, actor=actor)
    await journal.php_diagnostic(run["id"], request, observation)
    async with store._unit():
        manager = current_unit_of_work().manager

    async def admission(value):
        assert current_unit_of_work() is None
        await ProjectAccess(store).set_membership(run["project_id"], actor, None)

    token = CURRENT_PRINCIPAL.set(actor)
    store.vault_credential_admission = admission
    try:
        async with TransactionTemplate(
            manager, propagation=Propagation.REQUIRES_NEW, isolation=Isolation.REPEATABLE_READ
        ).transaction():
            outer = current_unit_of_work()
            with pytest.raises(AccessDenied):
                await journal.entries(run["id"])
            assert current_unit_of_work() is outer
    finally:
        CURRENT_PRINCIPAL.reset(token)
        store.vault_credential_admission = None
