# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host controls for pre-effect local waits; no native database or provider is used."""

import copy
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from test_scm_delegated_feedback import RECEIPT, received

from firefly_forge.collaboration import Collaboration
from firefly_forge.journal import Journal
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_connections import SCMConnections
from firefly_forge.scm_provider_backoff import ProviderBackoff, ProviderRequestDeferred

pytest_plugins = ["test_scm_delegated_feedback"]


async def test_local_provider_wait_retains_original_received_action(delegated_feedback, monkeypatch):
    service, row, run, sent, _ = delegated_feedback
    received(row, "respond")
    original = copy.deepcopy(row.data)
    deadline = time.time() + 120

    async def held(self, delivery):
        raise ProviderRequestDeferred("A provider request is already in progress", retry_at=deadline)

    monkeypatch.setattr(SCMActorAuthority, "resolve", held)
    result = await service._process_delegated(RECEIPT)

    assert result["status"] == "received", "A known pre-HTTP wait is not a permission denial"
    assert result["action_status"] == "not_dispatched"
    assert result["reason"] == "provider_request_deferred"
    assert result["action_deferred"]["outcome"] == "not_dispatched"
    assert result["action_deferred"]["retry_at"] == deadline
    assert result["command"] == original["command"] and result["id"] == original["id"]
    assert run["worker_request"]["status"] == "pending" and sent == []


async def test_actual_provider_lock_contention_has_zero_http_and_retains_action(
    delegated_feedback, monkeypatch, tmp_path
):
    monkeypatch.setattr("firefly_forge.scm_provider_backoff.LOCK_WAIT_SECONDS", 0.1)
    service, row, run, _, _ = delegated_feedback
    received(row, "respond")
    calls = []

    def unexpected(request):
        calls.append(request)
        raise AssertionError("The provider scope is already held; no HTTP is permitted")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        connections = SCMConnections(service.store, directory=tmp_path, client=client)

        async def resolve(self, delivery):
            await connections._request("GET", "https://api.github.com/installation/repositories")
            raise AssertionError("The occupied scope must not resolve a principal")

        monkeypatch.setattr(SCMActorAuthority, "resolve", resolve)
        async with ProviderBackoff(tmp_path, ["github", "https://api.github.com", "", "", ""]):
            result = await service._process_delegated(RECEIPT)
    assert result["status"] == "received" and result["action_deferred"]["outcome"] == "not_dispatched"
    assert run["worker_request"]["status"] == "pending" and calls == []


async def test_wait_survives_service_restart_without_io_until_due_then_applies_once(delegated_feedback, monkeypatch):
    service, row, run, sent, _ = delegated_feedback
    received(row, "respond")
    clock = [time.time()]
    monkeypatch.setattr("firefly_forge.collaboration.time", SimpleNamespace(time=lambda: clock[0]))
    original_resolve = SCMActorAuthority.resolve
    calls = []

    async def once(self, delivery):
        calls.append(delivery["id"])
        if len(calls) == 1:
            raise ProviderRequestDeferred("Local wait", retry_at=clock[0] + 10)
        return await original_resolve(self, delivery)

    monkeypatch.setattr(SCMActorAuthority, "resolve", once)
    first = await service.process(RECEIPT)
    assert first["status"] == "received" and calls == [RECEIPT]
    restarted = Collaboration(
        service.store, service.scm, settings_records=service.settings_records, inbox=service.inbox
    )
    assert await restarted.process(RECEIPT) == first
    assert calls == [RECEIPT] and run["worker_request"]["status"] == "pending" and sent == []
    clock[0] += 11
    completed = await restarted._process_delegated(RECEIPT)
    assert completed["status"] == "processed" and completed["action_status"] == "completed"
    assert completed["action_deferred"] is None
    assert run["worker_request"]["status"] == "answered" and len(calls) == 2
    assert await restarted._process_delegated(RECEIPT) == completed
    assert len(calls) == 2


async def test_due_wait_rechecks_current_authority_and_can_be_denied(delegated_feedback, monkeypatch):
    service, row, run, sent, _ = delegated_feedback
    received(row, "respond")
    clock = [time.time()]
    monkeypatch.setattr("firefly_forge.collaboration.time", SimpleNamespace(time=lambda: clock[0]))

    async def held(self, delivery):
        raise ProviderRequestDeferred("Local wait", retry_at=clock[0] + 10)

    monkeypatch.setattr(SCMActorAuthority, "resolve", held)
    await service._process_delegated(RECEIPT)
    clock[0] += 11

    async def revoked(self, delivery):
        raise AccessDenied("Private reason must never appear in a receipt")

    monkeypatch.setattr(SCMActorAuthority, "resolve", revoked)
    result = await service._process_delegated(RECEIPT)
    assert result["status"] == "denied"
    assert result["action_diagnostic"] == {"stage": "resolve", "code": "authority_denied"}
    assert "Private reason" not in str(result)
    assert run["worker_request"]["status"] == "pending" and sent == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AccessDenied("Sensitive authority detail"), "authority_denied"),
        (ValueError("Sensitive validation detail"), "validation_failed"),
        (KeyError("Sensitive reference detail"), "reference_unavailable"),
    ],
)
async def test_resolve_refusals_have_safe_stage_and_code(delegated_feedback, monkeypatch, caplog, error, code):
    service, row, _, _, _ = delegated_feedback
    received(row, "respond")

    async def refused(self, delivery):
        raise error

    monkeypatch.setattr(SCMActorAuthority, "resolve", refused)
    result = await service._process_delegated(RECEIPT)
    assert result["status"] == "denied" and result["action_status"] == "not_dispatched"
    assert result["action_diagnostic"] == {"stage": "resolve", "code": code}
    assert "Sensitive" not in str(result)
    record = next(r for r in caplog.records if r.message.startswith("SCM delegated admission diagnostic "))
    safe = json.loads(record.message.removeprefix("SCM delegated admission diagnostic "))
    assert safe["stage"] == "resolve" and safe["exception_type"] == type(error).__name__
    assert safe["location"]["module"] == "test_scm_delegated_deferred_host.py"
    assert safe["location"]["function"] == "refused" and safe["location"]["line"] > 0
    assert "Sensitive" not in record.message and "/private/" not in record.message


@pytest.mark.parametrize("status", ["denied", "processed", "needs_reconciliation"])
async def test_terminal_child_never_reenters_authority(delegated_feedback, monkeypatch, status):
    service, row, _, sent, _ = delegated_feedback
    received(row, "respond")
    row.status = row.data["status"] = status
    original = copy.deepcopy(row.data)

    async def forbidden(*args, **kwargs):
        raise AssertionError("A terminal or uncertain action must not enter authority resolution again")

    monkeypatch.setattr(SCMActorAuthority, "resolve", forbidden)
    assert await service._process_delegated(RECEIPT) == original and sent == []


async def test_locked_pre_effect_wait_is_retryable_but_post_effect_wait_is_unknown(delegated_feedback, monkeypatch):
    service, row, run, sent, _ = delegated_feedback
    received(row, "respond")
    original = ProjectAccess.authorize_actor

    async def held(self, *args, **kwargs):
        raise ProviderRequestDeferred("Current scope request held")

    monkeypatch.setattr(ProjectAccess, "authorize_actor", held)
    result = await service._process_delegated(RECEIPT)
    assert result["status"] == "received" and result["action_status"] == "not_dispatched"
    assert result["action_diagnostic"]["stage"] == "locked_admission"
    assert run["worker_request"]["status"] == "pending"
    received(row, "respond")
    monkeypatch.setattr(ProjectAccess, "authorize_actor", original)

    async def uncertain(*args, **kwargs):
        raise ProviderRequestDeferred("A downstream operation might already have happened")

    monkeypatch.setattr(Journal, "worker_response", uncertain)
    result = await service._process_delegated(RECEIPT)
    assert result["status"] == "needs_reconciliation" and result["action_status"] == "needs_reconciliation"
    assert result.get("action_deferred") is None and sent == []
    assert await service._process_delegated(RECEIPT) == result


@pytest.mark.parametrize("mutation", ["command", "outcome", "deadline", "extra"])
async def test_changed_nondispatch_evidence_does_not_retry(delegated_feedback, monkeypatch, mutation):
    service, row, run, sent, _ = delegated_feedback
    received(row, "respond")
    calls = []

    async def held(self, delivery):
        calls.append(delivery["id"])
        raise ProviderRequestDeferred("Local wait", retry_at=time.time() + 60)

    monkeypatch.setattr(SCMActorAuthority, "resolve", held)
    await service._process_delegated(RECEIPT)
    if mutation == "command":
        row.data["command"]["answer"] = "Different answer"
    elif mutation == "outcome":
        row.data["action_deferred"]["outcome"] = "unknown"
    elif mutation == "deadline":
        row.data["action_deferred"]["retry_at"] = True
    else:
        row.data["action_deferred"]["extra"] = True
    result = await service._process_delegated(RECEIPT)
    assert result["status"] == "denied" and len(calls) == 1
    assert result["action_diagnostic"] == {"stage": "retained_wait", "code": "validation_failed"}
    assert run["worker_request"]["status"] == "pending" and sent == []
