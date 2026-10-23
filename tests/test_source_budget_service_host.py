# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A separate finite admin allowance advances one answered source question without resetting history."""

# ruff: noqa: F401,F811
import copy
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from test_source_completed_output_service_host import completed_source, request
from test_source_edit_continuations_host import controlled_metadata
from test_workspace_dependency_reconciliation import legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge import source_budget_allowances as module
from firefly_forge.agent_roles import WorkerFeedback, WorkerResponse
from firefly_forge.source_result_readmission import SourceCompletedOutputReadmissions


async def answered_four(env, tmp_path, monkeypatch, *, consumed=False):
    # Explicit memory-only persistence seam; native SQL requires the physical lease unit.
    env.store.source_budget_admission_unit = env.store._unit
    env, parent, path, *_ = await completed_source(env, tmp_path, monkeypatch)
    source = SourceCompletedOutputReadmissions(env.service)
    review = await source.candidate(env.root.name, parent["request"]["request_id"], path)
    parent = await source.admit(env.root.name, parent["request"]["request_id"], request(review, path))
    key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:4").hex
    env.task.status = "running"
    env.task.data["idempotency_key"] = key
    env.store.run.status = "developing"
    env.store.run.data.update(status="developing", implementation_rounds=4)
    env.store.run.data["provider_recovery"]["status"] = "consumed" if consumed else "completed"
    await env.journal._append(env.root.name, "implement", key, "intent", {"attempt": 4})
    if consumed:
        await env.journal._append(
            env.root.name,
            "implement",
            key + ":provider",
            "provider_dispatch_intent",
            {
                "version": 1,
                "task_key": key,
                "attempt": 4,
                "recovery_id": env.store.run.data["provider_recovery"]["request_id"],
            },
        )
    await env.journal.worker_feedback(
        env.root.name,
        WorkerFeedback(
            id="budget-held",
            role_id="implementer",
            task_id="implement",
            attempt=4,
            disposition="clarification",
            summary="The read_file lifetime ceiling was reached before an effect",
            request={
                "id": "read-budget-question",
                "kind": "other",
                "question": "May the native operator review a finite additional read allowance?",
                "context": "13 original tool receipts are retained; no command or effect was repeated.",
            },
        ),
    )
    await env.journal.worker_response(
        env.root.name,
        "read-budget-question",
        WorkerResponse(
            id="read-budget-answer",
            actor="Native operator",
            answer="This was a definite quota refusal; review an explicit finite allowance before any execution.",
        ),
    )
    history = [
        {
            "correlation_id": f"{i:032x}",
            "request_digest": f"{i + 100:064x}",
            "status": "succeeded",
            "admission": {"tool": name},
        }
        for i, name in enumerate(["read_file"] * 10 + ["list_files"] * 2 + ["search_text"], 1)
    ]

    async def invocations(run_id):
        assert run_id == env.root.name
        return copy.deepcopy(history)

    async def unresolved(run_id):
        return False

    env.recoveries.engine.tool_broker = SimpleNamespace(invocations=invocations, unresolved=unresolved)
    module.write_policy(
        env.store.configuration,
        module.SourceBudgetPolicy(
            enabled=True, max_read_file_lifetime_limit=20, max_activation_ceiling=5, max_additional_activations=1
        ),
    )
    return env, parent, path, history


def allowance_request(review, path):
    return module.SourceBudgetAllowanceInput(
        version=1,
        request_id=uuid4().hex,
        candidate_digest=review["digest"],
        admission_contract_digest=review["admission_contract_digest"],
        previous_catalog_path=path,
        valid_for_seconds=900,
        proposal={"version": 1, "read_file_lifetime_limit": 20, "activation_ceiling": 5, "additional_activations": 1},
        acknowledge_lifetime_history_preserved=True,
        acknowledge_original_commands_and_final_reserve=True,
        acknowledge_one_new_canonical_activation=True,
        acknowledge_no_replay=True,
    )


async def test_finite_allowance_queues_canonical_five_preserving_thirteen_and_original_budgets(
    reconciliation_service, tmp_path, monkeypatch
):
    env, parent, path, history = await answered_four(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    original = copy.deepcopy(env.store.run.data)
    wal = copy.deepcopy(await env.recoveries._entries(env.root.name))
    proposal = module.SourceBudgetProposal(
        version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
    )
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path, proposal)
    body = allowance_request(review, path)
    receipt = await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert receipt == await service.get(env.root.name, parent["request"]["request_id"], body.request_id)
    assert receipt == await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert env.task.attempt == 5 and env.task.status == "queued" and env.store.run.status == "queued"
    assert env.task.data["idempotency_key"] == uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:5").hex
    assert receipt["candidate"]["source_edit"]["command_limit"] == 35
    assert receipt["candidate"]["source_edit"]["remaining_commands"] == 7
    assert receipt["candidate"]["source_edit"]["budget_allowance"]["read_file_lifetime_limit"] == 20
    assert await env.recoveries.engine.tool_broker.invocations(env.root.name) == history
    assert (await env.recoveries._entries(env.root.name))[: len(wal)] == wal
    for key in ("commands", "repair_budget", "implementation_rounds", "tool_policy_snapshot", "worker_response"):
        assert env.store.run.data[key] == original[key]
    assert env.store.run.data.get("error") is None


async def test_current_sealed_budget_receipt_and_policy_revocation_fence_dispatch(
    reconciliation_service, tmp_path, monkeypatch
):
    from firefly_forge.source_edit_continuations import source_edit_receipt

    env, parent, path, history = await answered_four(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    proposal = module.SourceBudgetProposal(
        version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
    )
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path, proposal)
    receipt = await service.admit(env.root.name, parent["request"]["request_id"], allowance_request(review, path))
    assert await source_edit_receipt(env.recoveries, await env.store.get_run(env.root.name), qualify=True) == receipt
    module.write_policy(env.store.configuration, module.SourceBudgetPolicy())
    with pytest.raises(ValueError, match="policy|allowance|disabled"):
        await source_edit_receipt(env.recoveries, await env.store.get_run(env.root.name), qualify=True)
    assert await env.recoveries.engine.tool_broker.invocations(env.root.name) == history


@pytest.mark.parametrize("change", ["policy", "history", "question", "answer", "source", "actor", "unknown"])
async def test_allowance_drift_refuses_before_new_intent(reconciliation_service, tmp_path, monkeypatch, change):
    from firefly_forge.project_access import AccessDenied

    env, parent, path, history = await answered_four(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    proposal = module.SourceBudgetProposal(
        version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
    )
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path, proposal)
    body = allowance_request(review, path)
    if change == "policy":
        module.write_policy(env.store.configuration, module.SourceBudgetPolicy())
    elif change == "history":
        history[0]["request_digest"] = "d" * 64
    elif change == "question":
        env.store.run.data["worker_request"]["context"] = "Different public context"
    elif change == "answer":
        env.store.run.data["worker_response"]["answer"] = "Different public answer"
    elif change == "source":
        (env.root / "frontend/app.js").write_text("UNREVIEWED=1\n")
    elif change == "actor":
        env.identity.enabled = False
    else:
        await env.journal._append(
            env.root.name, "implement", "f" * 32, "workspace_reconciliation_intent", {"unknown": True}
        )
    with pytest.raises((ValueError, AccessDenied)):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert env.task.attempt == 4 and env.store.run.status == "paused"
    assert not any(r.data["idempotency_key"] == body.request_id for r in env.journal.records.rows)


async def test_policy_revoked_during_queue_cannot_get_a_completed_allowance(
    reconciliation_service, tmp_path, monkeypatch
):
    env, parent, path, _ = await answered_four(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    proposal = module.SourceBudgetProposal(
        version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
    )
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path, proposal)
    body = allowance_request(review, path)
    resume = env.journal.resume

    async def revoke_after_await(*args, **kwargs):
        value = await resume(*args, **kwargs)
        module.write_policy(env.store.configuration, module.SourceBudgetPolicy())
        return value

    monkeypatch.setattr(env.journal, "resume", revoke_after_await)
    with pytest.raises(ValueError, match="policy|allowance|disabled"):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert not any(
        r.data["kind"] == "workspace_reconciliation_receipt" and r.data["idempotency_key"] == body.request_id
        for r in env.journal.records.rows
    )


@pytest.mark.parametrize("after", ["lease", "stopping", "cancel", "actor"])
async def test_allowance_late_authority_refusal_leaves_no_completed_custody(
    reconciliation_service, tmp_path, monkeypatch, after
):
    from firefly_forge.project_access import AccessDenied

    env, parent, path, _ = await answered_four(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    review = await service.candidate(
        env.root.name,
        parent["request"]["request_id"],
        path,
        module.SourceBudgetProposal(
            version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
        ),
    )
    body = allowance_request(review, path)
    resume = env.journal.resume

    async def lose_after_queue(*args, **kwargs):
        value = await resume(*args, **kwargs)
        if after == "actor":
            env.identity.enabled = False
        elif after == "cancel":
            env.store.run.data["cancel_request"] = {"status": "requested"}
        else:

            async def lost():
                raise ValueError("Controlled lost ownership or execution stopping")

            monkeypatch.setattr(env.store, "assert_ownership", lost)
        return value

    monkeypatch.setattr(env.journal, "resume", lose_after_queue)
    with pytest.raises((ValueError, AccessDenied)):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert not any(
        r.data["kind"] == "workspace_reconciliation_receipt" and r.data["idempotency_key"] == body.request_id
        for r in env.journal.records.rows
    )


async def test_unknown_allowance_intent_cannot_be_replayed_with_same_or_new_id(
    reconciliation_service, tmp_path, monkeypatch
):
    env, parent, path, _ = await answered_four(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    proposal = module.SourceBudgetProposal(
        version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
    )
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path, proposal)
    body = allowance_request(review, path)

    async def interrupted(*args, **kwargs):
        raise OSError("Controlled uncertain queue outcome")

    monkeypatch.setattr(env.journal, "resume", interrupted)
    with pytest.raises(OSError):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    with pytest.raises(ValueError, match="unknown|replay"):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    with pytest.raises(ValueError, match="unknown"):
        await service.candidate(env.root.name, parent["request"]["request_id"], path, proposal)
    assert env.task.attempt == 4 and env.task.status == "paused"


async def test_only_exact_canonical_allowance_accepts_new_feedback_and_dispatch_once(
    reconciliation_service, tmp_path, monkeypatch
):
    from firefly_forge.agent_roles import SourceBudgetWorkerFeedback

    env, parent, path, history = await answered_four(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    review = await service.candidate(
        env.root.name,
        parent["request"]["request_id"],
        path,
        module.SourceBudgetProposal(
            version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
        ),
    )
    body = allowance_request(review, path)
    receipt = await service.admit(env.root.name, parent["request"]["request_id"], body)
    key = env.task.data["idempotency_key"]
    env.task.status = "running"
    env.store.run.status = "developing"
    env.store.run.data["status"] = "developing"
    env.store.run.data["provider_recovery"]["status"] = "completed"
    await env.journal._append(env.root.name, "implement", key, "intent", {"attempt": 5})
    assert (await module.active_feedback_binding(env.recoveries, await env.store.get_run(env.root.name)))[
        "attempt"
    ] == 5
    from firefly_forge.source_edit_cli_policy import SOURCE_EDIT_LAUNCH
    from firefly_forge.source_edit_continuations import SourceEditInvocation
    from firefly_forge.source_edit_models import READ_TOOLS

    with pytest.raises(ValueError, match="sealed native CLI channel"):
        await env.recoveries.normal_dispatch(env.root.name, key)
    # Host admission metadata only; the native fixture must prove the actual CLI/MCP transport.
    token = SOURCE_EDIT_LAUNCH.set(
        {
            "activation": env.store.run.data["source_edit_continuation"],
            "permissions_digest": receipt["candidate"]["source_edit"]["configuration_proof"]["permissions_digest"],
            "server": SimpleNamespace(
                tool_names=READ_TOOLS,
                source_callback=SourceEditInvocation(env.recoveries, env.root.name, key).run,
                accepting=True,
                endpoint="http://127.0.0.1:1/mcp",
                expires_at_ms=receipt["expires_at_ms"],
            ),
        }
    )
    try:
        await env.recoveries.normal_dispatch(env.root.name, key)
        with pytest.raises(ValueError, match="consumed"):
            await env.recoveries.normal_dispatch(env.root.name, key)
    finally:
        SOURCE_EDIT_LAUNCH.reset(token)
    report = SourceBudgetWorkerFeedback(
        protocol=module.PROTOCOL,
        id="budget-completed",
        role_id="implementer",
        task_id="implement",
        attempt=5,
        disposition="completed",
        summary="The bounded source implementation is ready for native final checks",
        allowance_id=body.request_id,
        allowance_digest=env.store.run.data["source_edit_continuation"]["receipt_digest"],
    )
    forged = report.model_copy(update={"allowance_digest": "f" * 64})
    with pytest.raises(ValueError, match="allowance"):
        await env.journal.worker_feedback(env.root.name, forged)
    value = await env.journal.worker_feedback(env.root.name, report)
    assert value == await env.journal.worker_feedback(env.root.name, report)
    assert env.task.attempt == 5 and await env.recoveries.engine.tool_broker.invocations(env.root.name) == history
