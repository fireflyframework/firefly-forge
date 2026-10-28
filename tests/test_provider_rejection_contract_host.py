# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reviewed refusal data cannot imply a new grant, mutable schema or automatic retry."""

import json
from pathlib import Path

import httpx
import pytest
from test_original_selection_clients_host import connection

from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1
from firefly_forge.provider_rejection_models import RejectedBoundaryRecoveryInputV1, ReviewedProviderRejectionV1


def reviewed():
    return dict(
        version=1,
        provider="bridge",
        classification="request_refused_before_admission",
        completion_request_id="fbr1_" + "a" * 32,
        intent_digest="1" * 64,
        checkpoint_record_digest="2" * 64,
        wire_sha256="3" * 64,
        wire_bytes=1234,
        endpoint="http://localhost:18181/v1",
        principal_id="fixture",
        model="fixture/provider",
        executed_epoch=dict(
            executable_sha256="4" * 64, source_revision="5" * 40, source_tree="6" * 40, attestation_sha256="7" * 64
        ),
        validation_epoch=dict(
            executable_sha256="4" * 64,
            source_revision="5" * 40,
            source_tree="6" * 40,
            validator_sha256="8" * 64,
            route_sha256="9" * 64,
            report_sha256="a" * 64,
            binary_probe_report_sha256="b" * 64,
        ),
        http_status=400,
        admissions=0,
        jobs=0,
        provider_dispatches=0,
        tool_callbacks=0,
    )


def admission():
    return dict(
        version=1,
        candidate_digest="c" * 64,
        evidence=reviewed(),
        valid_for_seconds=30,
        acknowledge_reviewed_provider_epoch=True,
        acknowledge_no_original_response_or_tool_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )


def test_pre_admission_recovery_defaults_off_and_does_not_modify_preclaim_inputs():
    from firefly_forge.invalid_read_recovery_models import InvalidReadRecoveryInputV1

    assert ContinuityLeasePolicyV1(version=1).allow_pre_admission_recovery is False
    assert "evidence" not in InvalidReadRecoveryInputV1.model_fields
    assert "acknowledge_never_claimed" not in RejectedBoundaryRecoveryInputV1.model_fields


@pytest.mark.parametrize("field", [k for k in admission() if k.startswith("acknowledge_")])
@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_exact_acknowledgements_refuse_before_transport(field, value):
    calls = []
    with connection(lambda r: calls.append(r)) as client, pytest.raises(ValueError):
        client.request_rejected_boundary_recovery("a" * 32, "b" * 32, "c" * 32, {**admission(), field: value})
    assert calls == []


@pytest.mark.parametrize("field", ["admissions", "jobs", "provider_dispatches", "tool_callbacks"])
@pytest.mark.parametrize("value", [False, True, 1, -1, "0", None])
def test_unknown_or_nonzero_provider_effects_are_not_a_pre_admission_refusal(field, value):
    with pytest.raises(ValueError):
        ReviewedProviderRejectionV1.model_validate({**reviewed(), field: value})


@pytest.mark.parametrize("change", ["source_revision", "source_tree", "executable_sha256"])
def test_validator_must_match_the_reviewed_executed_epoch(change):
    body = reviewed()
    body["validation_epoch"][change] = "d" * (40 if change.startswith("source_") else 64)
    with pytest.raises(ValueError, match="exact executed"):
        ReviewedProviderRejectionV1.model_validate(body)


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", True),
        ("valid_for_seconds", True),
        ("valid_for_seconds", 0),
        ("valid_for_seconds", 7201),
        ("unknown", 1),
    ],
)
def test_admission_rejects_unknown_fields_and_invalid_finite_windows(key, value):
    with pytest.raises(ValueError):
        RejectedBoundaryRecoveryInputV1.model_validate({**admission(), key: value})


def test_sdk_uses_one_original_ticket_namespace_and_does_not_retry_submission():
    calls = []
    with connection(lambda r: calls.append(r) or httpx.Response(200, json={"ok": True})) as client:
        client.rejected_boundary_recovery_candidate("a" * 32, "b" * 32, reviewed())
        client.request_rejected_boundary_recovery("a" * 32, "b" * 32, "c" * 32, admission())
        client.rejected_boundary_recovery("a" * 32, "b" * 32)
    base = "/api/v1/runs/" + "a" * 32 + "/invalid-read-continuity/requests/" + "b" * 32
    assert [(r.method, r.url.path) for r in calls] == [
        ("POST", base + "/rejected-boundary-candidate"),
        ("POST", base + "/rejected-boundary-recoveries/" + "c" * 32),
        ("GET", base + "/rejected-boundary-recovery"),
    ]
    assert json.loads(calls[1].content) == admission()


def test_each_new_security_module_invalidates_both_explicit_source_contracts(monkeypatch):
    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.source_budget_allowances import admission_contract_digest

    baseline = runtime_contract(), admission_contract_digest()
    original = Path.read_bytes
    for name in (
        "provider_rejection_models.py",
        "provider_rejection_recoveries.py",
        "pending_checkpoint_reconstruction.py",
    ):
        with monkeypatch.context() as scoped:
            scoped.setattr(Path, "read_bytes", lambda p, n=name: original(p) + (b"\n" if p.name == n else b""))
            assert runtime_contract() != baseline[0]
            assert admission_contract_digest() != baseline[1]
    assert (runtime_contract(), admission_contract_digest()) == baseline


def test_verified_refusal_metadata_projection_retains_every_other_authority_field():
    from firefly_forge.provider_rejection_recoveries import _run_authority

    run = {
        "updated_at": "old",
        "verification_refusal": None,
        "brief": "retained",
        "status": "needs_reconciliation",
        "execution_actor": {"user_id": "a" * 32},
        "source_tree": "b" * 40,
        "attempt": 2,
        "repair_budget": 2,
        "usage": {"requests": 7, "tool_calls": 11},
        "commands": ["approved"],
        "config_revision": "c" * 32,
        "worker_request": {"id": "question"},
        "future_authority_field": "retained",
    }
    original = _run_authority(run)
    assert _run_authority({**run, "updated_at": "later", "verification_refusal": {"status": "held"}}) == original
    for field in run.keys() - {"updated_at", "verification_refusal"}:
        assert _run_authority({**run, field: {"changed": True}}) != original


async def test_controller_uses_current_service_and_sanitizes_refusals():
    from types import SimpleNamespace

    from firefly_forge.invalid_read_continuity_controllers import InvalidReadContinuityController

    calls = []

    async def invoke(*args):
        calls.append(args)
        return {"version": 1, "status": "queued"}

    controller = InvalidReadContinuityController(
        SimpleNamespace(
            rejections=SimpleNamespace(
                candidate=invoke,
                request=invoke,
                status=invoke,
            )
        )
    )
    proof = ReviewedProviderRejectionV1.model_validate(reviewed())
    body = RejectedBoundaryRecoveryInputV1.model_validate(admission())
    await controller.rejected_boundary_candidate("a" * 32, "b" * 32, proof)
    await controller.recover_rejected_boundary("a" * 32, "b" * 32, "c" * 32, body)
    await controller.rejected_boundary_recovery("a" * 32, "b" * 32)
    assert [len(call) for call in calls] == [3, 4, 2]

    async def refuse(*args):
        raise ValueError("Private refusal details")

    controller.continuities.rejections.candidate = refuse
    with pytest.raises(ValueError, match="unavailable") as caught:
        await controller.rejected_boundary_candidate("a" * 32, "b" * 32, proof)
    assert "Private" not in str(caught.value)


async def test_rejection_witness_journal_read_screens_all_new_record_fields(tmp_path):
    from test_bridge_inflight_cancel import RUN, MemoryStore, Rows

    from firefly_forge.journal import Journal

    store = MemoryStore(tmp_path / "memory")

    def screen(value):
        if "newly-registered-credential" in json.dumps(value):
            raise ValueError("Credential marker refused")

    async def admitted(value):
        screen(value)

    store.admit_credentials = admitted
    store.reject_credentials = screen
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=Rows())
    await journal._append(
        RUN,
        "implement",
        "fixture-refusal",
        "provider_rejection_recovery",
        {
            "diagnostic": "newly-registered-credential",
        },
    )
    with pytest.raises(ValueError, match="Credential"):
        await journal.entries(RUN)


async def test_unknown_nested_rejection_checkpoint_is_not_exposed_as_normal_metadata(tmp_path):
    from test_bridge_inflight_cancel import RUN, MemoryStore, Rows

    from firefly_forge.journal import Journal

    store = MemoryStore(tmp_path / "memory")
    store.reject_credentials = lambda value: None
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=Rows())
    await journal._append(
        RUN,
        "implement",
        "fixture-refusal",
        "provider_rejection_recovery",
        {
            "failed_ticket": {"checkpoint": "unrecognized-encoded-custody", "checkpoint_digest": "f" * 64},
        },
    )
    with pytest.raises(ValueError, match="custody"):
        await journal.entries(RUN)
