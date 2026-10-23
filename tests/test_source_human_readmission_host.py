# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A fresh native human admission advances the same source-only task without an environment effect."""

# ruff: noqa: F401, F811
import copy
from uuid import UUID, uuid4

import pytest
from test_source_edit_continuations_host import controlled_metadata, source_environment, source_request
from test_workspace_dependency_reconciliation import legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge.agent_roles import WorkerFeedback, WorkerResponse
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.source_edit_continuations import source_edit_receipt


async def answered_source(env, tmp_path, monkeypatch, *, observed=False, sanitized=False):
    env = await source_environment(env, tmp_path, monkeypatch)

    async def campaign(*args, **kwargs):
        pass

    env.store.assert_campaign_current = campaign
    from firefly_forge import source_edit_model_catalog

    catalog = tmp_path / "package/resources/native/codex-0.163.0/source-only-model.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_bytes(source_edit_model_catalog.CATALOG_PATH.read_bytes())
    monkeypatch.setattr(source_edit_model_catalog, "CATALOG_PATH", catalog)
    env.store.run.data["repair_budget"] = 0
    env.store.run.repository_id = UUID(int=9)
    env.store.run.data["repository_id"] = UUID(int=9).hex
    env.store.run.data["execution_actor"]["global_admin"] = True
    # The environment fixture retains this exact repository in its immutable verification cut.
    from firefly_forge.verification_models import verification_scope

    old = [copy.deepcopy(r.data) for r in env.journal.records.rows]
    old[0]["payload"]["scope_digest"] = verification_scope(env.store.run.data)
    env.journal.records.rows.clear()
    for entry in old:
        await env.journal._append(
            env.root.name, entry["task_id"], entry["idempotency_key"], entry["kind"], entry["payload"]
        )
    initial = source_request(await env.service.source_candidate(env.root.name))
    if observed:
        from test_source_edit_observed_reconciliation_host import request as observed_request

        from firefly_forge import workspace_reconciliations
        from firefly_forge.source_edit_observed_reconciliation import ObservedSourceReconciliation

        apply = workspace_reconciliations.apply_dependency_plan

        def completed_then_lost(*args):
            apply(*args)
            raise OSError("Known fixture lost response after environment effect")

        monkeypatch.setattr(workspace_reconciliations, "apply_dependency_plan", completed_then_lost)
        with pytest.raises(OSError):
            await env.service.activate_source_edit(env.root.name, initial)

        def never_replay(*args):
            raise AssertionError("The original unknown effect must never replay")

        monkeypatch.setattr(workspace_reconciliations, "apply_dependency_plan", never_replay)
        observer = ObservedSourceReconciliation(env.service)
        if sanitized:
            from test_source_index_sanitation_native import CACHE, _request
            from test_workspace_dependency_reconciliation import git

            cache = env.root / CACHE
            cache.parent.mkdir(parents=True)
            cache.write_text('{"public":"controlled-generated-cache"}\n')
            git(env.root, "add", "--", CACHE)
            sanitation = await observer.index_sanitation_candidate(env.root.name, initial.request_id, str(catalog))
            await observer.sanitize_index(env.root.name, initial.request_id, _request(sanitation, catalog))
        review = await observer.candidate(env.root.name, initial.request_id, str(catalog))
        parent = await observer.admit(env.root.name, initial.request_id, observed_request(review, catalog))
    else:
        parent = await env.service.activate_source_edit(env.root.name, initial)
    env.store.run.status = "developing"
    env.store.run.data.update(status="developing", implementation_rounds=2)
    env.task.status = "running"
    from uuid import NAMESPACE_URL, uuid5

    key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:2").hex
    env.task.data["idempotency_key"] = key
    await env.journal._append(env.root.name, "implement", key, "intent", {"attempt": 2})
    await env.journal.worker_feedback(
        env.root.name,
        WorkerFeedback(
            id="diagnostic-feedback",
            role_id="implementer",
            task_id="implement",
            attempt=2,
            disposition="clarification",
            summary="Existing retained error context is required",
            request={
                "id": "existing-diagnostics",
                "kind": "other",
                "question": "What did the existing check report?",
                "context": "Read retained output only; do not execute it again",
            },
        ),
    )
    await env.journal.worker_response(
        env.root.name,
        "existing-diagnostics",
        WorkerResponse(
            id="diagnostic-answer",
            actor="Native operator",
            answer="The retained check reports a missing normalized_name column.",
        ),
    )
    return env, parent


async def test_ordinary_resume_cannot_reuse_old_source_activation_for_next_attempt(
    reconciliation_service, tmp_path, monkeypatch
):
    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="fresh native source-only human"):
        await env.journal.resume(env.root.name)
    assert env.task.attempt == 2 and env.task.status == "paused"
    assert env.store.run.status == "paused"


async def test_fresh_human_admission_keeps_original_reserve_and_satisfies_unchanged_source_receipt(
    reconciliation_service, tmp_path, monkeypatch
):
    from firefly_forge.source_human_readmission import SourceHumanReadmissionInput, SourceHumanReadmissions

    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch)
    from firefly_forge.source_edit_model_catalog import model_catalog_path

    service = SourceHumanReadmissions(env.service)
    path = str(model_catalog_path("fixture-model"))
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path)
    body = SourceHumanReadmissionInput(
        version=1,
        request_id=uuid4().hex,
        candidate_digest=review["digest"],
        admission_contract_digest=review["admission_contract_digest"],
        previous_catalog_path=path,
        valid_for_seconds=900,
        acknowledge_same_answer_and_next_attempt=True,
        acknowledge_original_budgets=True,
        acknowledge_no_environment_replay=True,
        acknowledge_new_finite_activation=True,
    )
    before = copy.deepcopy(env.store.run.data)
    entries = copy.deepcopy([r.data for r in env.journal.records.rows])
    index = (env.root / ".git/index").read_bytes()
    receipt = await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert receipt == await service.get(env.root.name, parent["request"]["request_id"], body.request_id)
    assert receipt == await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert env.task.status == "queued" and env.task.attempt == 3 and env.store.run.status == "queued"
    assert receipt["candidate"]["previous_task"]["attempt"] == 3
    assert receipt["candidate"]["source_edit"]["remaining_commands"] == 7
    assert receipt["candidate"]["source_edit"]["command_limit"] == 35
    assert (env.root / ".git/index").read_bytes() == index
    assert [r.data for r in env.journal.records.rows][: len(entries)] == entries
    for key in ("repair_budget", "implementation_rounds", "commands", "sandbox_snapshot", "execution_actor"):
        assert env.store.run.data[key] == before[key]
    assert "scm_source_activation" not in receipt
    assert await source_edit_receipt(env.recoveries, await env.store.get_run(env.root.name), qualify=True) == receipt
    assert await env.journal.human_clarification_continuation_allowance(env.root.name) == 1


def human_request(review, path, **changes):
    from firefly_forge.source_human_readmission import SourceHumanReadmissionInput

    values = dict(
        version=1,
        request_id=uuid4().hex,
        candidate_digest=review["digest"],
        admission_contract_digest=review["admission_contract_digest"],
        previous_catalog_path=path,
        valid_for_seconds=900,
        acknowledge_same_answer_and_next_attempt=True,
        acknowledge_original_budgets=True,
        acknowledge_no_environment_replay=True,
        acknowledge_new_finite_activation=True,
    )
    return SourceHumanReadmissionInput(**{**values, **changes})


@pytest.mark.parametrize(
    "change", ["question", "answer", "source", "index", "config", "rounds", "repairs", "scope", "actor", "unknown"]
)
async def test_drift_or_unknown_outcome_refuses_before_new_intent(
    reconciliation_service, tmp_path, monkeypatch, change
):
    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.source_edit_model_catalog import model_catalog_path
    from firefly_forge.source_human_readmission import SourceHumanReadmissions

    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch)
    service = SourceHumanReadmissions(env.service)
    path = str(model_catalog_path("fixture-model"))
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path)
    body = human_request(review, path)
    if change == "question":
        env.store.run.data["worker_request"]["context"] = "Different context"
    elif change == "answer":
        env.store.run.data["worker_response"]["answer"] = "Different answer"
    elif change == "source":
        (env.root / "frontend/app.js").write_text("unreviewed source\n")
    elif change == "index":
        from test_workspace_dependency_reconciliation import git

        (env.root / "new.py").write_text("NEW=1\n")
        git(env.root, "add", "new.py")
    elif change == "config":
        env.store.configuration.update("codex", ProviderConfiguration(values={"model": "different-model"}))
    elif change == "rounds":
        env.store.run.data["implementation_rounds"] = 3
    elif change == "repairs":
        env.store.run.data["repair_budget"] += 1
    elif change == "scope":
        env.store.run.data["execution_actor"]["global_admin"] = False
    elif change == "actor":
        env.identity.enabled = False
    else:
        await env.journal._append(
            env.root.name, "implement", "f" * 32, "workspace_reconciliation_intent", {"unknown": True}
        )
    with pytest.raises((ValueError, AccessDenied)):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert env.task.attempt == 2 and env.store.run.status == "paused"
    assert not any(r.data["idempotency_key"] == body.request_id for r in env.journal.records.rows)


@pytest.mark.parametrize("change", ["revoked", "source", "question", "actor-context"])
async def test_credential_wait_cannot_replace_the_reviewed_answer_source_or_actor(
    reconciliation_service, tmp_path, monkeypatch, change
):
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.source_edit_model_catalog import model_catalog_path
    from firefly_forge.source_human_readmission import SourceHumanReadmissions
    from firefly_forge.source_patch_admission import SourcePatch
    from firefly_forge.store import Store

    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch)
    service = SourceHumanReadmissions(env.service)
    path = str(model_catalog_path("fixture-model"))
    body = human_request(await service.candidate(env.root.name, parent["request"]["request_id"], path), path)

    async def change_after_scan(self, patch):
        assert isinstance(patch, SourcePatch)
        if change == "revoked":
            env.identity.enabled = False
        elif change == "source":
            (env.root / "frontend/app.js").write_text("unreviewed source\n")
        elif change == "question":
            env.store.run.data["worker_request"]["context"] = "Different context"
        else:
            CURRENT_PRINCIPAL.set(None)

    monkeypatch.setattr(Store, "admit_source_patch", change_after_scan)
    token = CURRENT_PRINCIPAL.set(CURRENT_PRINCIPAL.get())
    try:
        with pytest.raises((ValueError, AccessDenied)):
            await service.admit(env.root.name, parent["request"]["request_id"], body)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert env.task.attempt == 2 and env.store.run.status == "paused"
    assert not any(r.data["idempotency_key"] == body.request_id for r in env.journal.records.rows)


async def test_uncommitted_human_intent_is_never_replayed(reconciliation_service, tmp_path, monkeypatch):
    from firefly_forge.source_edit_model_catalog import model_catalog_path
    from firefly_forge.source_human_readmission import SourceHumanReadmissions

    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch)
    service = SourceHumanReadmissions(env.service)
    path = str(model_catalog_path("fixture-model"))
    body = human_request(await service.candidate(env.root.name, parent["request"]["request_id"], path), path)

    async def fail(*args, **kwargs):
        raise OSError("Controlled unknown queue outcome")

    monkeypatch.setattr(env.journal, "resume", fail)
    with pytest.raises(OSError):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    with pytest.raises(ValueError, match="unknown"):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert env.task.attempt == 2
    assert len([r for r in env.journal.records.rows if r.data["idempotency_key"] == body.request_id]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("valid_for_seconds", True),
        ("valid_for_seconds", 1801),
        ("acknowledge_original_budgets", 1),
        ("acknowledge_no_environment_replay", False),
    ],
)
def test_human_consent_is_strict_and_finite(field, value):
    with pytest.raises(ValueError):
        human_request({"digest": "a" * 64, "admission_contract_digest": "b" * 64}, "/public/catalog", **{field: value})


@pytest.mark.parametrize("change", ["answer", "unknown"])
async def test_candidate_cannot_reseal_tampered_answer_or_an_unknown_environment_intent(
    reconciliation_service, tmp_path, monkeypatch, change
):
    from firefly_forge.source_edit_model_catalog import model_catalog_path
    from firefly_forge.source_human_readmission import SourceHumanReadmissions

    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch)
    if change == "answer":
        env.store.run.data["worker_response"]["answer"] = "Different from immutable human answer"
    else:
        await env.journal._append(
            env.root.name, "implement", "f" * 32, "workspace_reconciliation_intent", {"unknown": True}
        )
    with pytest.raises(ValueError):
        await SourceHumanReadmissions(env.service).candidate(
            env.root.name, parent["request"]["request_id"], str(model_catalog_path("fixture-model"))
        )


async def test_request_identity_alone_never_authorizes_human_resume(reconciliation_service, tmp_path, monkeypatch):
    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="fresh native source-only human"):
        await env.journal.resume(env.root.name, source_human_readmission=uuid4().hex)
    assert env.task.attempt == 2 and env.task.status == "paused"


async def test_expiry_during_queue_holds_before_the_attempt_changes(reconciliation_service, tmp_path, monkeypatch):
    from firefly_forge import source_human_readmission
    from firefly_forge.source_edit_model_catalog import model_catalog_path

    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch)
    service = source_human_readmission.SourceHumanReadmissions(env.service)
    path = str(model_catalog_path("fixture-model"))
    body = human_request(await service.candidate(env.root.name, parent["request"]["request_id"], path), path)
    resume = env.journal.resume

    async def expire(*args, **kwargs):
        clock = source_human_readmission.clock_ms()
        monkeypatch.setattr(source_human_readmission, "clock_ms", lambda: clock + 901000)
        return await resume(*args, **kwargs)

    monkeypatch.setattr(env.journal, "resume", expire)
    with pytest.raises(ValueError, match="fresh native source-only human"):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert env.task.attempt == 2 and env.task.status == "paused"


async def test_original_activation_can_expire_without_rewriting_it(reconciliation_service, tmp_path, monkeypatch):
    from firefly_forge import source_human_readmission
    from firefly_forge.source_edit_model_catalog import model_catalog_path

    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch)
    old = copy.deepcopy(parent)
    clock = parent["expires_at_ms"] + 1
    monkeypatch.setattr(source_human_readmission, "clock_ms", lambda: clock)
    service = source_human_readmission.SourceHumanReadmissions(env.service)
    path = str(model_catalog_path("fixture-model"))
    body = human_request(await service.candidate(env.root.name, parent["request"]["request_id"], path), path)
    receipt = await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert receipt["expires_at_ms"] == clock + 900000
    assert parent == old
    assert (
        next(r.data["payload"] for r in env.journal.records.rows if r.kind == "workspace_reconciliation_receipt") == old
    )


async def answer_successor(env, attempt):
    from uuid import NAMESPACE_URL, uuid5

    env.store.run.status = "developing"
    env.store.run.data.update(status="developing", implementation_rounds=attempt)
    env.task.status = "running"
    key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:{attempt}").hex
    env.task.data["idempotency_key"] = key
    await env.journal._append(env.root.name, "implement", key, "intent", {"attempt": attempt})
    await env.journal.worker_feedback(
        env.root.name,
        WorkerFeedback(
            id=f"feedback-{attempt}",
            role_id="implementer",
            task_id="implement",
            attempt=attempt,
            disposition="clarification",
            summary="Another immutable product decision is required",
            request={
                "id": f"question-{attempt}",
                "kind": "other",
                "question": "Which retained answer applies?",
                "context": "No new commands",
            },
        ),
    )
    await env.journal.worker_response(
        env.root.name,
        f"question-{attempt}",
        WorkerResponse(
            id=f"answer-{attempt}", actor="Native operator", answer="Keep the original bounded product scope."
        ),
    )


@pytest.mark.parametrize("observed", [False, True])
async def test_repeated_human_source_clarifications_derive_three_to_four_and_preserve_four_attempt_cap(
    reconciliation_service, tmp_path, monkeypatch, observed
):
    from firefly_forge.source_edit_model_catalog import model_catalog_path
    from firefly_forge.source_human_readmission import SourceHumanReadmissions

    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch, observed=observed)
    service = SourceHumanReadmissions(env.service)
    path = str(model_catalog_path("fixture-model"))
    first = human_request(await service.candidate(env.root.name, parent["request"]["request_id"], path), path)
    first_receipt = await service.admit(env.root.name, parent["request"]["request_id"], first)
    assert env.task.attempt == 3
    await answer_successor(env, 3)
    second = human_request(await service.candidate(env.root.name, first.request_id, path), path)
    receipt = await service.admit(env.root.name, first.request_id, second)
    assert receipt["candidate"]["previous_task"]["attempt"] == 4 and env.task.attempt == 4
    assert receipt["candidate"]["source_edit"]["remaining_commands"] == 7
    assert receipt["candidate"]["source_edit"]["command_limit"] == 35
    assert env.store.run.data["repair_budget"] == 0 and env.store.run.data["implementation_rounds"] == 3
    assert await env.journal.human_clarification_continuation_allowance(env.root.name) == 2
    assert await source_edit_receipt(env.recoveries, await env.store.get_run(env.root.name), qualify=True) == receipt
    assert first_receipt == await service.get(env.root.name, parent["request"]["request_id"], first.request_id)
    await answer_successor(env, 4)
    with pytest.raises(ValueError, match="finite ordinary successor"):
        await service.candidate(env.root.name, second.request_id, path)
    assert env.task.attempt == 4


async def test_question_attempt_must_match_the_completed_parent_activation(
    reconciliation_service, tmp_path, monkeypatch
):
    from firefly_forge.source_edit_model_catalog import model_catalog_path
    from firefly_forge.source_human_readmission import SourceHumanReadmissions

    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch)
    await answer_successor(env, 3)
    with pytest.raises(ValueError):
        await SourceHumanReadmissions(env.service).candidate(
            env.root.name, parent["request"]["request_id"], str(model_catalog_path("fixture-model"))
        )


async def test_observed_ancestry_never_authorizes_another_unknown_intent(reconciliation_service, tmp_path, monkeypatch):
    from firefly_forge.source_edit_model_catalog import model_catalog_path
    from firefly_forge.source_human_readmission import SourceHumanReadmissions

    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch, observed=True)
    service = SourceHumanReadmissions(env.service)
    path = str(model_catalog_path("fixture-model"))
    first = human_request(await service.candidate(env.root.name, parent["request"]["request_id"], path), path)
    await service.admit(env.root.name, parent["request"]["request_id"], first)
    await answer_successor(env, 3)
    await env.journal._append(
        env.root.name, "implement", "f" * 32, "workspace_reconciliation_intent", {"unknown": True}
    )
    with pytest.raises(ValueError, match="unknown native environment"):
        await service.candidate(env.root.name, first.request_id, path)
    assert env.task.attempt == 3 and env.task.status == "paused"


async def test_mid_edit_source_is_held_until_a_separate_proven_pause_checkpoint_exists(
    reconciliation_service, tmp_path, monkeypatch
):
    from firefly_forge.source_edit_model_catalog import model_catalog_path
    from firefly_forge.source_human_readmission import SourceHumanReadmissions

    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch)
    (env.root / "frontend/app.js").write_text("Known worker output cannot be inferred from a mutable path\n")
    with pytest.raises(ValueError, match="completed activation source"):
        await SourceHumanReadmissions(env.service).candidate(
            env.root.name, parent["request"]["request_id"], str(model_catalog_path("fixture-model"))
        )
    assert env.task.attempt == 2 and env.task.status == "paused"
