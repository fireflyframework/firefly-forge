# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A retained source starts a distinct maintain unit without copying authority."""

import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_checkpoint_test_source_host import (
    PROJECT,
    REFERENCE,
    REPO,
    request,
    source_service,
)
from test_checkpoint_test_source_host import (
    source_case as source_case,
)
from test_checkpoint_test_trace_host import lineage as lineage
from test_run_admission_prepared_host import admission as admission

from firefly_forge.checkpoint_test_models import ResolvedCheckpointV1, frozen_checkpoint_source
from firefly_forge.git_deliveries import delivery_digest
from firefly_forge.models import RunInput
from firefly_forge.run_creation_models import RunCreationReceipt


def maintain_input(**changes):
    value = request()
    value.update(operation="maintain", **changes)
    return RunInput.model_validate(value)


async def test_maintain_mode_is_explicitly_hashed_while_default_test_snapshot_is_byte_compatible(source_case):
    service = await source_service(source_case)
    test = await service.prepare(RunInput.model_validate(request()))
    maintain = await service.prepare(maintain_input())
    assert "operation" not in test
    assert ResolvedCheckpointV1.model_validate({**test, "operation": "test"}).model_dump(mode="json") == test
    assert maintain["operation"] == "maintain"
    assert maintain["digest"] == delivery_digest({k: v for k, v in maintain.items() if k != "digest"})
    assert maintain["digest"] != test["digest"]
    assert {k: v for k, v in maintain.items() if k not in {"operation", "digest"}} == {
        k: v for k, v in test.items() if k != "digest"
    }


@pytest.mark.parametrize(
    "change", [{"publish": True}, {"operation": "build"}, {"project_id": None, "component_id": None}]
)
def test_checkpoint_maintain_never_allows_publication_build_or_unscoped_selection(change):
    value = request()
    value.update(operation="maintain")
    value.update(change)
    with pytest.raises(ValidationError):
        RunInput.model_validate(value)


async def test_runtime_clone_source_and_creation_receipt_refuse_operation_drift(source_case):
    body = maintain_input()
    snapshot = await (await source_service(source_case)).prepare(body)
    run = {**body.model_dump(mode="json"), "source_checkpoint_snapshot": snapshot}
    repository = {"id": REPO, "default_branch": "main"}
    _, revision, tree = frozen_checkpoint_source(run, repository)
    assert revision == snapshot["revision"] and tree == snapshot["source_tree"]
    receipt = dict(
        version=1,
        request_id="7" * 32,
        original_actor={"user_id": "e" * 32, "authentication_method": "oidc_bearer"},
        request_digest="8" * 64,
        input=body.model_dump(mode="json", exclude_unset=True),
        repository_id=REPO,
        project_id=PROJECT,
        component_id="api",
        run_id="9" * 32,
        admitted_at="2026-10-24T00:00:00Z",
        admission_digest="a" * 64,
        digest="b" * 64,
        source_checkpoint=snapshot,
    )
    assert RunCreationReceipt.model_validate(receipt).source_checkpoint.operation == "maintain"
    with pytest.raises(ValueError):
        frozen_checkpoint_source({**run, "operation": "test"}, repository)
    with pytest.raises(ValidationError):
        RunCreationReceipt.model_validate({**receipt, "input": {**receipt["input"], "operation": "test"}})
    damaged = {k: v for k, v in snapshot.items() if k not in {"operation", "digest"}}
    damaged["digest"] = delivery_digest(damaged)
    with pytest.raises(ValueError):
        frozen_checkpoint_source({**run, "source_checkpoint_snapshot": damaged}, repository)


async def test_locked_source_recheck_binds_mode_and_current_authority(source_case, monkeypatch):
    import firefly_forge.checkpoint_test_sources as module

    c = source_case
    service = await source_service(c)
    body = maintain_input()
    snapshot = await service.prepare(body)

    async def row(*args, **kwargs):
        return SimpleNamespace(data=copy.deepcopy(c.run))

    c.store.run_records = SimpleNamespace(find_by_id=row)
    monkeypatch.setattr(module, "current_unit_of_work", lambda: object())
    await service.revalidate_locked(body, snapshot)
    with pytest.raises(ValueError):
        await service.revalidate_locked(RunInput.model_validate(request()), snapshot)
    c.state["allowed"] = False
    with pytest.raises(PermissionError):
        await service.revalidate_locked(body, snapshot)


async def test_maintain_source_does_not_inherit_failed_parent_budget_session_or_answer(source_case):
    c = source_case
    c.run.update(
        status="failed",
        implementation_rounds=6,
        provider_recovery={"status": "consumed", "request_id": "5" * 32},
        source_budget_allowance={"shared_calls": 30, "read_file_calls": 20},
        worker_response={"answer": "historical only"},
    )
    before = copy.deepcopy(c.run)
    value = await (await source_service(c)).prepare(maintain_input())
    assert value["operation"] == "maintain" and value["reference"] == REFERENCE
    assert (
        not {"provider_recovery", "source_budget_allowance", "worker_response", "implementation_rounds"} & value.keys()
    )
    assert c.run == before


async def test_maintain_prepare_refuses_revocation_during_remote_ref_observation(source_case):
    c = source_case
    c.state["mutation"] = lambda: c.state.update(allowed=False)
    with pytest.raises(PermissionError):
        await (await source_service(c)).prepare(maintain_input())


async def test_child_lineage_labels_maintain_and_remains_read_only(lineage):
    c = lineage
    snapshot = {**c.child["source_checkpoint_snapshot"], "operation": "maintain"}
    snapshot["digest"] = delivery_digest({k: v for k, v in snapshot.items() if k != "digest"})
    c.child.update(operation="maintain", source_checkpoint_snapshot=snapshot)
    c.receipt["source_checkpoint"] = copy.deepcopy(snapshot)
    references, _ = await c.service.references(c.delivery, [c.scope])
    child = next(row for row in references if row["run_id"] == c.child["id"])
    assert child["kind"] == "native_checkpoint_maintain"
    assert child["receipt_id"] != child["parent_receipt_id"]


async def test_legacy_maintain_selector_never_creates_a_run_without_its_own_receipt(admission):
    store, state, _, _ = admission
    with pytest.raises(ValueError, match="run-creation-requests"):
        await store.create_run(maintain_input())
    assert state["rows"] == {} and state["writes"] == []


@pytest.mark.parametrize("intent", ["answer", "resume", "checkpoint"])
def test_read_only_maintain_lineage_grants_no_new_execution_or_answer(intent):
    from firefly_forge.scm_natural_models import NaturalDecision, resolve_task

    scope = dict(
        repository_id=REPO,
        project_id=PROJECT,
        issue={"kind": "issue", "number": 4},
        actor="17",
        binding_digest="a" * 64,
        native_user_id="e" * 32,
    )
    child = dict(
        **scope,
        run_id="9" * 32,
        run_status="paused",
        origin="native_checkpoint_maintain",
        read_only=True,
        operation="maintain",
        question_id="not-an-execution-grant",
        question_digest="b" * 64,
        question_status="pending" if intent == "answer" else "answered",
        response_digest="c" * 64,
        consumed=False,
    )
    result = resolve_task(
        scope, [child], NaturalDecision(intent=intent, message="Read-only source lineage"), answer="Go"
    )
    assert result.state == "waiting" and result.run_id is None
