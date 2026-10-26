# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Entity cardinality is separate from finite, cumulative execution reservations."""

import pytest
from pydantic import ValidationError

from firefly_forge.project_campaigns import CampaignLaunch, CampaignRetry


def test_component_selection_is_not_an_execution_budget():
    components = [f"service-{index}" for index in range(5000)]
    assert CampaignLaunch(request_id="a" * 32, brief="Deliver", component_ids=components).component_ids == components
    assert CampaignRetry(request_id="b" * 32, component_ids=components).component_ids == components


def test_project_dispatch_and_parallelism_policy_has_no_fixed_project_size_ceiling():
    from firefly_forge.campaign_execution_budget import CampaignExecutionPolicyV1

    policy = CampaignExecutionPolicyV1(dispatch_window=5000, max_parallel_runs=128)
    assert policy.dispatch_window == 5000 and policy.max_parallel_runs == 128


@pytest.mark.parametrize("field", ["dispatch_window", "max_parallel_runs"])
@pytest.mark.parametrize("value", [0, -1, True, None, "10"])
def test_execution_policy_stays_explicitly_finite_and_typed(field, value):
    from firefly_forge.campaign_execution_budget import CampaignExecutionPolicyV1

    with pytest.raises(ValidationError):
        CampaignExecutionPolicyV1.model_validate({field: value})


def test_budget_projection_keeps_all_prior_consumption_when_renewed():
    from firefly_forge.campaign_execution_budget import derive_budget, freeze_budget

    frozen = freeze_budget({"dispatch_window": 2, "max_parallel_runs": 1}, "c" * 64)
    entries = [
        {"kind": "released", "payload": {"run_id": "a" * 32}},
        {"kind": "released", "payload": {"run_id": "b" * 32}},
    ]
    exhausted = derive_budget(frozen, entries)
    assert exhausted["consumed_dispatches"] == 2 and exhausted["remaining_dispatches"] == 0
    entries.append(
        {
            "kind": "budget_renewal",
            "payload": {
                "additional_dispatches": 3,
                "previous_granted_dispatches": 2,
                "granted_dispatches": 5,
                "max_parallel_runs": 2,
                "policy": {"enabled": True, "dispatch_window": 3, "max_parallel_runs": 2},
                "policy_digest": "d" * 64,
                "actor": {"user_id": "e" * 32},
            },
        }
    )
    renewed = derive_budget(frozen, entries)
    assert renewed["consumed_dispatches"] == 2
    assert renewed["granted_dispatches"] == 5 and renewed["remaining_dispatches"] == 3
    assert renewed["max_parallel_runs"] == 2
    assert frozen["policy"]["dispatch_window"] == 2


@pytest.mark.parametrize("mutation", ["duplicate_dispatch", "overdraw", "reset", "oversized_grant"])
def test_corrupt_budget_history_cannot_create_new_execution_capacity(mutation):
    from firefly_forge.campaign_execution_budget import derive_budget, freeze_budget

    frozen = freeze_budget({"dispatch_window": 1, "max_parallel_runs": 1}, "c" * 64)
    released = {"kind": "released", "payload": {"run_id": "a" * 32}}
    entries = [released]
    if mutation == "duplicate_dispatch":
        entries.append(released)
    elif mutation == "overdraw":
        entries.append({"kind": "released", "payload": {"run_id": "b" * 32}})
    else:
        entries.append(
            {
                "kind": "budget_renewal",
                "payload": {
                    "additional_dispatches": 2 if mutation == "oversized_grant" else 1,
                    "previous_granted_dispatches": 0 if mutation == "reset" else 1,
                    "granted_dispatches": 3 if mutation == "oversized_grant" else 1,
                    "max_parallel_runs": 1,
                    "policy": {"enabled": True, "dispatch_window": 1, "max_parallel_runs": 1},
                    "policy_digest": "d" * 64,
                    "actor": {"user_id": "e" * 32},
                },
            }
        )
    with pytest.raises(ValueError, match="budget"):
        derive_budget(frozen, entries)


def test_legacy_campaign_keeps_its_explicit_legacy_contract():
    from firefly_forge.campaign_execution_budget import derive_budget

    assert derive_budget(None, []) is None


@pytest.fixture
async def budget_campaign():
    import copy
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import UUID

    from firefly_forge.campaign_execution_budget import freeze_budget
    from firefly_forge.project_campaigns import ProjectCampaigns

    runs = {
        f"service-{index}": {
            "id": str(index + 1) * 32,
            "status": "campaign_blocked",
            "campaign_context": {"source_base": "1" * 40, "upstream_receipts": []},
        }
        for index in range(3)
    }
    history = []
    state = {"authorized": True}
    row = SimpleNamespace(
        id=UUID("d" * 32),
        project_id=UUID("e" * 32),
        status="running",
        data={
            "launch": {"dependency_gate": "verified"},
            "current_attempts": dict.fromkeys(runs, 1),
            "execution_budget": freeze_budget({"dispatch_window": 2, "max_parallel_runs": 1}, "c" * 64),
        },
    )
    bindings = [
        SimpleNamespace(
            component_id=name,
            attempt=1,
            run_id=UUID(run["id"]),
            data={"depends_on": [], "admission_digest": "f" * 64},
        )
        for name, run in runs.items()
    ]

    async def get_run(run_id):
        return copy.deepcopy(next(run for run in runs.values() if run["id"] == run_id))

    async def update(run_id, **values):
        next(run for run in runs.values() if run["id"] == run_id).update(copy.deepcopy(values))

    async def children(_):
        return bindings

    async def events(_):
        return history

    async def save_event(event):
        history.append(event)

    async def save(_):
        pass

    async def plan(_):
        raise KeyError("No execution tasks in the controlled coordination fixture")

    async def find_campaign(*args, **kwargs):
        return row

    async def ownership():
        pass

    async def admit(_):
        pass

    async def actor(*args, **kwargs):
        from firefly_forge.project_access import AccessDenied

        if not state["authorized"]:
            raise AccessDenied("Current campaign execution authority required")
        return {"user_id": "f" * 32}

    @asynccontextmanager
    async def unit():
        before = copy.deepcopy((runs, history, row.data, row.status))

        async def execute(*args):
            pass

        try:
            yield SimpleNamespace(execute=execute)
        except BaseException:
            runs.clear()
            runs.update(before[0])
            history[:] = before[1]
            row.data, row.status = before[2:]
            raise

    policy = {
        "policy": {"enabled": True, "dispatch_window": 2, "max_parallel_runs": 1},
        "effective_digest": "c" * 64,
    }
    project = SimpleNamespace(data={})

    async def find_project(*args, **kwargs):
        return project

    service = ProjectCampaigns(
        SimpleNamespace(
            get_run=get_run,
            update=update,
            _unit=unit,
            admit_credentials=admit,
            runtime_roles=None,
            schema="public_fixture",
        ),
        SimpleNamespace(assert_ownership=ownership),
        SimpleNamespace(plan=plan),
        records=SimpleNamespace(save=save, find_by_id=find_campaign),
        children=SimpleNamespace(children=children),
        events=SimpleNamespace(events=events, save=save_event),
        project_records=SimpleNamespace(find_by_id=find_project),
    )
    service._budget_actor = actor
    service.execution_policies = SimpleNamespace(_snapshot=lambda _: policy)
    service.fixture_budget_state = state
    service.fixture_policy = policy
    await service._append(row, "launch", "launch", {"execution_budget": row.data["execution_budget"]})
    return service, row, runs, history


async def test_release_reserves_only_available_parallel_slots(budget_campaign):
    service, row, runs, history = budget_campaign
    await service._release(row)
    assert [run["status"] for run in runs.values()] == ["queued", "campaign_blocked", "campaign_blocked"]
    assert sum(event.data["kind"] == "released" for event in history) == 1
    await service._release(row)
    assert sum(event.data["kind"] == "released" for event in history) == 1


async def test_exhausted_dispatch_window_pauses_without_recreating_children_or_resetting_consumption(budget_campaign):
    from firefly_forge.campaign_execution_budget import derive_budget

    service, row, runs, history = budget_campaign
    identities = [run["id"] for run in runs.values()]
    await service._release(row)
    runs["service-0"]["status"] = "succeeded"
    await service._release(row)
    assert runs["service-1"]["status"] == "queued"
    runs["service-1"]["status"] = "succeeded"
    await service._release(row)
    assert row.status == "paused" and runs["service-2"]["status"] == "campaign_blocked"
    assert "budget" in runs["service-2"]["campaign_context"]["blocked_reason"].lower()
    assert [run["id"] for run in runs.values()] == identities
    state = derive_budget(row.data["execution_budget"], [event.data for event in history])
    assert state["consumed_dispatches"] == 2 and state["remaining_dispatches"] == 0


async def test_explicit_budget_renewal_is_head_bound_idempotent_and_never_resets_spent(budget_campaign):
    from firefly_forge.campaign_execution_budget import CampaignBudgetRenewalV1, derive_budget

    service, row, runs, history = budget_campaign
    await service._release(row)
    runs["service-0"]["status"] = "succeeded"
    await service._release(row)
    runs["service-1"]["status"] = "succeeded"
    await service._release(row)
    body = CampaignBudgetRenewalV1(
        request_id="a" * 32,
        expected_journal_head=history[-1].data["digest"],
        expected_policy_digest="c" * 64,
        additional_dispatches=1,
        max_parallel_runs=1,
    )
    await service.renew_budget(row.project_id.hex, row.id.hex, body)
    assert runs["service-2"]["status"] == "queued"
    state = derive_budget(row.data["execution_budget"], [event.data for event in history])
    assert state["granted_dispatches"] == 3 and state["consumed_dispatches"] == 3
    count = len(history)
    await service.renew_budget(row.project_id.hex, row.id.hex, body)
    assert len(history) == count
    with pytest.raises(ValueError, match="changed"):
        await service.renew_budget(row.project_id.hex, row.id.hex, body.model_copy(update={"request_id": "b" * 32}))
    assert len(history) == count


async def test_revoked_actor_cannot_renew_even_an_idempotent_budget_request(budget_campaign):
    from firefly_forge.campaign_execution_budget import CampaignBudgetRenewalV1
    from firefly_forge.project_access import AccessDenied

    service, row, _, history = budget_campaign
    body = CampaignBudgetRenewalV1(
        request_id="a" * 32,
        expected_journal_head=history[-1].data["digest"],
        expected_policy_digest="c" * 64,
        additional_dispatches=1,
        max_parallel_runs=1,
    )
    service.fixture_budget_state["authorized"] = False
    with pytest.raises(AccessDenied):
        await service.renew_budget(row.project_id.hex, row.id.hex, body)
    assert len(history) == 1


@pytest.mark.parametrize("held", ["paused", "interrupted"])
async def test_resume_keeps_the_existing_parallel_reservation(budget_campaign, held):
    service, row, runs, history = budget_campaign
    await service._release(row)
    runs["service-0"]["status"] = held
    await service._release(row)
    assert runs["service-1"]["status"] == "campaign_blocked"
    runs["service-0"]["status"] = "queued"
    await service._release(row)
    assert sum(event.data["kind"] == "released" for event in history) == 1


async def test_current_disabled_policy_holds_new_dispatches_without_rewriting_spent_or_children(budget_campaign):
    service, row, runs, history = budget_campaign
    service.fixture_policy["policy"]["enabled"] = False
    await service._release(row)
    assert all(run["status"] == "campaign_blocked" for run in runs.values())
    assert row.status == "paused" and not any(event.data["kind"] == "released" for event in history)


async def test_mutated_mutable_budget_header_cannot_replace_immutable_launch_grant(budget_campaign):
    service, row, _, _ = budget_campaign
    row.data["execution_budget"]["policy"]["dispatch_window"] += 100
    with pytest.raises(ValueError, match="immutable launch"):
        await service._release(row)


async def test_renewal_does_not_replay_or_adopt_an_unknown_child_effect(budget_campaign):
    from firefly_forge.campaign_execution_budget import CampaignBudgetRenewalV1

    service, row, runs, history = budget_campaign
    runs["service-0"]["status"] = "needs_reconciliation"
    body = CampaignBudgetRenewalV1(
        request_id="a" * 32,
        expected_journal_head=history[-1].data["digest"],
        expected_policy_digest="c" * 64,
        additional_dispatches=1,
        max_parallel_runs=1,
    )
    with pytest.raises(ValueError, match="reconciliation"):
        await service.renew_budget(row.project_id.hex, row.id.hex, body)
    assert len(history) == 1


async def test_late_revocation_rolls_back_grant_receipt_and_queued_dispatch(budget_campaign):
    from firefly_forge.campaign_execution_budget import CampaignBudgetRenewalV1
    from firefly_forge.project_access import AccessDenied

    service, row, runs, history = budget_campaign
    body = CampaignBudgetRenewalV1(
        request_id="a" * 32,
        expected_journal_head=history[-1].data["digest"],
        expected_policy_digest="c" * 64,
        additional_dispatches=1,
        max_parallel_runs=1,
    )
    append = service._append

    async def revoked(*args, **kwargs):
        result = await append(*args, **kwargs)
        if args[2] == "budget_renewal":
            service.fixture_budget_state["authorized"] = False
        return result

    service._append = revoked
    with pytest.raises(AccessDenied):
        await service.renew_budget(row.project_id.hex, row.id.hex, body)
    assert len(history) == 1
    assert all(run["status"] == "campaign_blocked" for run in runs.values())
    assert not row.data.get("pending_action")
