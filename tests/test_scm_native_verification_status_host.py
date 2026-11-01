# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read-only fact contracts preserve known partial results without adding authority."""

import importlib
import importlib.util

import pytest
from linked_verification_fixtures import candidate, receipt

from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import LinkedVerificationReceiptV1


def implementation():
    name = "firefly_forge.scm_native_verification_status_models"
    assert importlib.util.find_spec(name), "Typed native verification read projection is missing"
    return importlib.import_module(name)


def partial_operation(*, unknown=False):
    value = receipt(candidate(effect_budget=7), status="needs_reconciliation")
    value["progress"].update(started=5 if unknown else 4, completed=4, unknown=unknown)
    value = LinkedVerificationReceiptV1.model_validate(value).model_dump(mode="json")
    return dict(
        receipt=value,
        final_event_kind="linked_verification_held",
        hold_reason="unknown" if unknown else "inputs_changed",
        results=tuple(
            dict(ordinal=n, exit_code=0, command_digest=str(n) * 64, event_digest=str(n + 1) * 64) for n in range(1, 5)
        ),
    )


def test_read_projection_reports_four_known_results_without_claiming_all_seven():
    model = implementation()
    value = model.SCMNativeVerificationOperationV1.model_validate(partial_operation())
    assert value.known_passed == 4 and value.known_failed == 0
    assert value.receipt.candidate.effect_budget == 7
    assert value.receipt.status == "needs_reconciliation" and value.receipt.progress.unknown is False
    assert not hasattr(value, "current") and not hasattr(value, "execute")


def test_unknown_fifth_command_never_becomes_a_fifth_known_result():
    value = implementation().SCMNativeVerificationOperationV1.model_validate(partial_operation(unknown=True))
    assert value.known_passed == 4 and value.receipt.progress.started == 5
    assert value.receipt.progress.unknown is True


@pytest.mark.parametrize("change", ["missing", "extra", "ordinal", "exit_type", "failure"])
def test_result_projection_refuses_unbacked_or_contradictory_counts(change):
    value = partial_operation()
    results = list(value["results"])
    if change == "missing":
        results.pop()
    elif change == "extra":
        results.append({**results[-1], "ordinal": 5})
    elif change == "ordinal":
        results[-1]["ordinal"] = 2
    elif change == "exit_type":
        results[-1]["exit_code"] = True
    else:
        results[-1]["exit_code"] = 1
    value["results"] = tuple(results)
    with pytest.raises(ValueError):
        implementation().SCMNativeVerificationOperationV1.model_validate(value)


def status_input(*, unknown=False):
    operation = partial_operation(unknown=unknown)
    candidate = operation["receipt"]["candidate"]
    value = dict(
        version=1,
        source="validated_native_request_events",
        run_id=candidate["run_id"],
        project_id="1" * 32,
        repository_id="2" * 32,
        component_id="api",
        attempt=3,
        implementation_receipt_digest=candidate["implementation_receipt_digest"],
        terminal_receipt_digest=candidate["terminal_receipt_digest"],
        source_tree=candidate["source_tree"],
        read_scope_digest="f" * 64,
        parent_run_digest="e" * 64,
        current_request_id=operation["receipt"]["request_id"],
        operations=(operation,),
        other_implementation_operations=0,
        other_implementation_digest=digest({"receipts": []}),
        execution_grant=False,
        held_predecessor=dict(
            request_id=operation["receipt"]["request_id"],
            event_digest=operation["receipt"]["event_digest"],
            parent_attempt=3,
            review_only=True,
        ),
    )
    return {**value, "digest": digest(value)}


def test_known_closed_frontier_offers_only_a_typed_review_reference():
    view = implementation().SCMNativeVerificationStatusV1.model_validate(status_input())
    assert view.held_predecessor.request_id == view.current_request_id
    assert view.held_predecessor.review_only is True and view.execution_grant is False
    assert view.operations[0].known_passed == 4


@pytest.mark.parametrize("change", ["unknown", "foreign", "grant", "stale", "attempt"])
def test_status_cannot_offer_a_foreign_unknown_or_mutated_frontier(change):
    value = status_input(unknown=change == "unknown")
    if change == "foreign":
        value["current_request_id"] = "3" * 32
    elif change == "grant":
        value["execution_grant"] = True
    elif change == "stale":
        value["held_predecessor"]["event_digest"] = "3" * 64
    elif change == "attempt":
        value["attempt"] = True
    with pytest.raises(ValueError):
        implementation().SCMNativeVerificationStatusV1.model_validate(value)


def test_empty_history_cannot_offer_a_held_predecessor():
    value = status_input()
    value.update(operations=(), current_request_id=None)
    value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
    with pytest.raises(ValueError):
        implementation().SCMNativeVerificationStatusV1.model_validate(value)


async def test_read_rechecks_permission_after_final_native_receipt_await():
    from types import SimpleNamespace

    from firefly_forge.project_access import AccessDenied
    from firefly_forge.scm_native_verification_status import SCMNativeVerificationStatus

    # Both snapshots are equal; revocation occurs only at the last observation await.
    service = SCMNativeVerificationStatus(SimpleNamespace(store=object()), object())
    permitted, observations = True, 0

    async def authorize(*args):
        if not permitted:
            raise AccessDenied("Current ticket read permission was revoked")
        return {"id": "1" * 32}

    async def observe(*args):
        nonlocal permitted, observations
        observations += 1
        if observations == 2:
            permitted = False
        return None

    service._authorize, service._observe = authorize, observe
    from firefly_forge.scm_conversation_models import ConversationReadScopeV1, TicketContextReferenceV1

    scope = dict(
        version=1,
        action="status",
        transport="github_rest_poll_v1",
        project_id="1" * 32,
        repository_id="2" * 32,
        native_user_id="3" * 32,
        component_ids=["api"],
        binding_key="1" * 64,
        binding_digest="2" * 64,
        binding_request_id="4" * 32,
        verified_receipt_id="5" * 32,
        verified_reference_digest="3" * 64,
        project_binding_digest="4" * 64,
        observation_digest="5" * 64,
    )
    reference = dict(
        receipt_id="6" * 32,
        sequence=1,
        run_id="7" * 32,
        project_id="1" * 32,
        component_id="api",
        context_digest="6" * 64,
    )
    assert ConversationReadScopeV1.model_validate(scope) and TicketContextReferenceV1.model_validate(reference)
    with pytest.raises(AccessDenied):
        await service.read({}, scope, reference)


def test_native_qa_facts_are_separate_from_old_handoff_and_raw_run_checks():
    from test_communication_accepted_recovery_host import accepted_run

    from firefly_forge.communication import communication_facts

    value = status_input()
    value["operations"][0]["receipt"]["detail"] = "PRIVATE output and https://unverified.example"
    value["operations"][0]["receipt"]["hold_diagnostic"] = dict(
        phase="post_result_source", cause_code="selected_source_changed", exception_type="ValueError"
    )
    value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
    run = accepted_run() | {
        key: value[key] for key in ("run_id", "project_id", "repository_id", "component_id", "attempt")
    }
    run["id"] = run.pop("run_id")
    value["parent_run_digest"] = digest(run)
    value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
    view = implementation().SCMNativeVerificationStatusV1.model_validate(value)
    facts = communication_facts(run, native_verification=view)
    current = facts["native_verification"]["current"]
    assert current["known_passed"] == 4 and current["required"] == 7 and current["unknown"] is False
    assert current["status"] == "needs_reconciliation"
    assert current["hold_diagnostic"]["cause_code"] == "selected_source_changed"
    assert facts["factory_verification"]["checks"]["recorded"] == 7
    assert facts["check_status"]["recorded"] == 0
    assert facts["check_status"]["source"] == "parent_run_final_verification"
    assert facts["native_verification"]["execution_grant"] is False
    assert facts["native_verification"]["held_predecessor"]["review_only"] is True
    assert "PRIVATE" not in str(facts) and "unverified.example" not in str(facts)
    with pytest.raises(ValueError):
        communication_facts(run | {"component_id": "other"}, native_verification=view)


def test_current_successor_starts_at_zero_and_keeps_predecessor_results_historical():
    from test_linked_verification_successor_models_host import held, successor

    from firefly_forge.scm_native_verification_status_models import native_verification_facts

    parent = held()
    child = successor(parent)
    value = status_input()
    value.update(
        current_request_id=child.request_id,
        operations=(
            dict(
                receipt=parent.model_dump(mode="json"),
                final_event_kind="linked_verification_held",
                hold_reason="inputs_changed",
                results=tuple(
                    dict(ordinal=n, exit_code=0, command_digest=str(n) * 64, event_digest=str(n + 1) * 64)
                    for n in (1, 2)
                ),
            ),
            dict(
                receipt=child.model_dump(mode="json"),
                final_event_kind="linked_verification_admitted",
                hold_reason=None,
                results=(),
            ),
        ),
    )
    value.pop("held_predecessor")
    value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
    run = {k: value[k] for k in ("project_id", "repository_id", "component_id", "attempt")} | {"id": value["run_id"]}
    value["parent_run_digest"] = digest(run)
    value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
    view = implementation().SCMNativeVerificationStatusV1.model_validate(value)
    facts = native_verification_facts(view, run)
    assert facts["current"]["request_id"] == child.request_id
    assert facts["current"]["known_passed"] == 0 and facts["current"]["required"] == 3
    assert facts["historical_operations"][0]["known_passed"] == 2
    assert "held_predecessor" not in facts and facts["old_results_reused"] is False


def test_native_status_retains_more_than_twenty_configured_results():
    value = receipt(candidate(effect_budget=51), status="completed")
    operation = implementation().SCMNativeVerificationOperationV1.model_validate(
        dict(
            receipt=value,
            final_event_kind="linked_verification_completed",
            results=tuple(
                dict(ordinal=n, exit_code=0, command_digest=f"{n:064x}", event_digest=f"{n + 1:064x}")
                for n in range(1, 52)
            ),
        )
    )
    assert operation.known_passed == operation.receipt.candidate.effect_budget == 51


async def test_native_history_pages_past_fifty_and_refuses_cursor_or_count_changes():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import UUID

    from firefly_forge.scm_native_verification_status import SCMNativeVerificationStatus

    @asynccontextmanager
    async def unit():
        yield

    rows = [
        SimpleNamespace(
            id=UUID(int=n), run_id=UUID("1" * 32), kind="linked_verify", status="completed", request_key=str(n), data={}
        )
        for n in range(1, 131)
    ]
    calls, state = [], {"changed": False}

    async def count(*args):
        return len(rows) + int(state["changed"] and bool(calls))

    async def page(*args, after, limit):
        calls.append(after)
        return [row for row in rows if after is None or row.id > after][:limit]

    async def events(row):
        return [{"digest": f"{row.id.int:064x}"}]

    service = SCMNativeVerificationStatus(
        SimpleNamespace(store=SimpleNamespace(_unit=unit)),
        SimpleNamespace(
            requests=SimpleNamespace(records=SimpleNamespace(count_for_run=count, for_run_page=page)), _events=events
        ),
    )
    observed = [row.id async for row, _ in service._operations("1" * 32)]
    assert len(observed) == 130 and len(calls) == 3 and len(set(observed)) == 130
    calls.clear()
    state["changed"] = True
    with pytest.raises(ValueError, match="history changed"):
        _ = [row async for row in service._operations("1" * 32)]
