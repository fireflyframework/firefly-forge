# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native bean links and bounded repository query construction without database effects."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from firefly_forge.api import FactoryConfiguration
from firefly_forge.execution_request_persistence import ForgeExecutionRequestEventRepository
from firefly_forge.linked_verifications import LinkedVerifications


def test_native_configuration_connects_one_verification_service_to_engine_and_owned_worker():
    store, journal, engine, requests = (SimpleNamespace() for _ in range(4))
    config = object.__new__(FactoryConfiguration)
    service = config.linked_verifications(store, journal, engine, requests)
    assert isinstance(service, LinkedVerifications)
    assert service.store is store and service.journal is journal
    assert service.engine is engine and service.requests is requests
    assert engine.linked_verifications is requests.linked_verifications is service


def test_native_configuration_connects_scm_consent_to_the_same_worker_and_conversation():
    store, polling, verifications = (SimpleNamespace() for _ in range(3))
    collaboration = SimpleNamespace(store=store, polling=polling)
    config = object.__new__(FactoryConfiguration)
    authority = config.scm_linked_verification_authority(store, collaboration, verifications)
    assert authority.store is store and authority.polling is polling
    assert authority.verifications is verifications
    assert authority.conversations.collaboration is collaboration
    assert store.scm_linked_verification_authority is collaboration.linked_verification_authority is authority


async def test_native_event_query_keeps_full_finite_ledger_above100_and_rejects_overflow(monkeypatch):
    queries = []
    rows = [object() for _ in range(104)]

    async def scalars(statement):
        queries.append(statement)
        return rows[: statement._limit_clause.value]

    session = SimpleNamespace(scalars=scalars)
    records = ForgeExecutionRequestEventRepository(session=session)
    monkeypatch.setattr(records, "_require_session", lambda: session)
    request_id = UUID(int=1)
    assert len(await records.linked_verification_events(request_id, 107)) == 104
    statement = queries[-1]
    assert statement._limit_clause.value == 108
    assert statement.compile().params["request_id_1"] == request_id
    assert "ORDER BY forge_execution_request_events.sequence" in str(statement)
    rows.extend(object() for _ in range(4))
    with pytest.raises(ValueError, match="event budget"):
        await records.linked_verification_events(request_id, 107)
    for maximum in (True, 0, -1):
        with pytest.raises(ValueError, match="command list"):
            await records.linked_verification_events(request_id, maximum)
    assert len(queries) == 2


@pytest.mark.parametrize(
    ("duration", "command_timeout", "expected"),
    [("legacy", 20, 20), (None, None, None), (None, 900, 900), (120, 900, 120), (120, None, 120)],
)
def test_dispatch_preserves_captured_legacy_or_explicit_work_deadline(duration, command_timeout, expected):
    from firefly_forge.linked_verifications import effective_command_timeout
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.work_duration_models import WorkDurationPolicyV2

    config = SandboxInput(
        timeout=300, work_duration=None if duration == "legacy" else WorkDurationPolicyV2(timeout_seconds=duration)
    )
    assert effective_command_timeout({"timeout": command_timeout}, config) == expected


def test_legacy_runner_refuses_an_unbounded_command_instead_of_inventing_a_deadline():
    from firefly_forge.linked_verifications import effective_command_timeout
    from firefly_forge.sandbox_models import SandboxInput

    with pytest.raises(ValueError, match="legacy runner"):
        effective_command_timeout({"timeout": None}, SandboxInput())
