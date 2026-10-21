# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Git, Journal and admission orchestration; identity repositories are offline fixtures."""

import copy
import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_workspace_dependency_reconciliation import git, legacy_workspace  # noqa: F401
from test_workspace_reconciliation_journal import environment_journal  # noqa: F401

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.provider_recoveries import ProviderRecoveries
from firefly_forge.sandbox_models import capture_sandbox
from firefly_forge.tool_broker import tool_policy_snapshot
from firefly_forge.verification_models import verification_provider, verification_scope, verification_source
from firefly_forge.workspace_reconciliation_models import SOURCE_REFUSAL, WorkspaceReconciliationInput


@pytest.fixture
async def reconciliation_service(environment_journal, legacy_workspace, monkeypatch, tmp_path):  # noqa: F811
    journal, store, task, actor, _, counts = environment_journal
    root, baseline, metadata, external = legacy_workspace
    store.configuration = ConfigurationStore(tmp_path / "private-native-fixture")
    store.run.data.update(
        active_task="implement",
        error=SOURCE_REFUSAL,
        git_metadata_digest=metadata,
        base_revision=baseline,
        agent="codex",
        backend="pyfly",
        commands=[{"argv": ["approved-check"], "timeout": 10}],
        sandbox_snapshot=capture_sandbox({"mode": "host", "command_limit": 16}),
        verification_handoff={"id": "handoff", "status": "completed"},
        worker_response={"verification_handoff_id": "handoff"},
        worker_request={"id": "request", "status": "responded"},
        **store.configuration.snapshot("codex"),
    )
    for kind, payload in (
        (
            "verification_handoff_intent",
            {
                "id": "handoff",
                "continuation": 1,
                "max_continuations": 1,
                "request_id": "request",
                "source_digest": verification_source(root),
                "scope_digest": verification_scope(store.run.data),
                "commands_digest": digest({"commands": store.run.data["commands"]}),
                "policy_digest": tool_policy_snapshot(store.configuration)["digest"],
                "provider_digest": verification_provider(store, store.run.data),
                "workspace": str(root),
            },
        ),
        (
            "worker_response",
            {
                "responding_actor": {
                    "version": 4,
                    "authentication_method": "factory_verification",
                    "handoff_id": "handoff",
                }
            },
        ),
        ("verification_handoff_outcome", {"id": "handoff", "status": "completed"}),
        ("resume", {"automatic": True, "verification_handoff_id": "handoff"}),
    ):
        await journal._append(root.name, "implement", "retained-" + kind, kind, payload)

    async def sandbox_rows(*args):
        return []

    journal.sandboxes = SimpleNamespace(for_run=sandbox_rows)
    store.event_records = SimpleNamespace(provider_budget_events=sandbox_rows)
    identity = SimpleNamespace(enabled=True, issuer=actor.issuer, subject=actor.subject)

    async def find_identity(*args, **kwargs):
        return identity

    async def actor_permission(*args):
        if not identity.enabled:
            raise AccessDenied("Fixture identity was revoked")
        return True

    async def campaign_current(*args):
        return None

    async def credential_admission(value):
        return None

    @asynccontextmanager
    async def unit(**kwargs):
        yield None

    store._unit = unit
    store.assert_campaign_current = campaign_current
    store.reject_credentials = lambda value: None
    store.admit_credentials = credential_admission
    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", find_identity)
    monkeypatch.setattr(ProjectAccess, "authorize_actor", actor_permission)
    engine = SimpleNamespace(workspaces=root.parent, active={}, tool_broker=None)
    recoveries = ProviderRecoveries(store, journal, engine)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        yield SimpleNamespace(
            recoveries=recoveries,
            store=store,
            journal=journal,
            task=task,
            root=root,
            identity=identity,
            external=external,
            counts=counts,
        )
    finally:
        CURRENT_PRINCIPAL.reset(token)


def request(candidate):
    return WorkspaceReconciliationInput(
        request_id="d" * 32,
        candidate_digest=candidate["digest"],
        acknowledge_environment_only=True,
        acknowledge_resume_existing_task=True,
    )


def implementation(env):
    assert importlib.util.find_spec("firefly_forge.workspace_reconciliations") is not None
    env.module = importlib.import_module("firefly_forge.workspace_reconciliations")
    env.service = env.module.WorkspaceReconciliations(env.recoveries)
    return env


async def test_native_effect_has_prior_intent_exact_receipt_and_no_budget_or_source_change(reconciliation_service):
    env = implementation(reconciliation_service)
    old = copy.deepcopy(env.store.run.data)
    candidate = await env.service.candidate(env.root.name)
    assert candidate["budget"]["final_checks"] == 1 and candidate["budget"]["attempt"] == 2
    assert candidate["plan"]["aliases"] == ["frontend/node_modules"]
    body = request(candidate)
    result = await env.service.admit(env.root.name, body)
    assert result == await env.service.get(env.root.name, body.request_id)
    assert await env.service.admit(env.root.name, body) == result
    assert env.store.run.status == "queued" and env.task.attempt == 2
    for field in (
        "attempt",
        "implementation_rounds",
        "repair_budget",
        "sandbox_snapshot",
        "commands",
        "execution_actor",
    ):
        assert env.store.run.data[field] == old[field]
    assert env.store.run.data["git_metadata_digest"] != old["git_metadata_digest"]
    assert (env.root / "frontend/app.js").read_text() == "export const value = 1;\n"
    assert (env.root / "frontend/node_modules").is_symlink()
    assert git(env.root, "ls-files", "--", "frontend/node_modules") == ""
    entries = [row.data for row in env.journal.records.rows]
    assert [e["kind"] for e in entries[-3:]] == [
        "workspace_reconciliation_intent",
        "workspace_reconciliation_receipt",
        "workspace_reconciliation_queued",
    ]
    assert result["intent_digest"] == entries[-3]["digest"]
    assert result["candidate"]["previous_run_digest"] == digest(old)
    assert env.journal.verify_chain(entries)


async def test_credential_wait_revocation_refuses_before_intent_or_native_effect(reconciliation_service):
    env = implementation(reconciliation_service)
    body = request(await env.service.candidate(env.root.name))
    before = (env.root / ".git/index").read_bytes()

    async def revoke(value):
        env.identity.enabled = False

    env.store.admit_credentials = revoke
    with pytest.raises(AccessDenied):
        await env.service.admit(env.root.name, body)
    assert (env.root / ".git/index").read_bytes() == before
    assert not any(e.kind.startswith("workspace_reconciliation") for e in env.journal.records.rows)


async def test_completed_handoff_source_drift_refuses_review_before_environment_intent(reconciliation_service):
    env = implementation(reconciliation_service)
    (env.root / "frontend/app.js").write_text("export const value = 2;\n")
    with pytest.raises(ValueError, match="Automatic verification source changed"):
        await env.service.candidate(env.root.name)
    assert not any(e.kind.startswith("workspace_reconciliation") for e in env.journal.records.rows)


async def test_lost_effect_result_is_retained_as_unknown_and_never_reexecuted(reconciliation_service, monkeypatch):
    env = implementation(reconciliation_service)
    body = request(await env.service.candidate(env.root.name))
    apply = env.module.apply_dependency_plan

    def lost_result(root, plan):
        assert env.journal.records.rows[-1].kind == "workspace_reconciliation_intent"
        apply(root, plan)
        raise OSError("Fixture interrupted after the owned metadata effect")

    monkeypatch.setattr(env.module, "apply_dependency_plan", lost_result)
    with pytest.raises(OSError):
        await env.service.admit(env.root.name, body)
    assert env.store.run.status == "failed" and env.task.attempt == 2
    observed = await env.service.get(env.root.name, body.request_id)
    assert observed["status"] == "needs_reconciliation"
    assert observed["request"] == body.model_dump()
    after = (env.root / ".git/index").read_bytes()
    with pytest.raises(ValueError, match="unknown|reconciliation"):
        await env.service.admit(env.root.name, body)
    assert (env.root / ".git/index").read_bytes() == after
    assert len([e for e in env.journal.records.rows if e.kind == "workspace_reconciliation_intent"]) == 1
