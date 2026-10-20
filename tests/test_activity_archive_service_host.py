# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current authority and full-message credential fences precede chunk projection."""

import copy
import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_activity_archive import EVENT, RUN

from firefly_forge.activity_models import ActivityContentQuery, ActivityUnavailable, project_activity
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

REPO = "c" * 32


@pytest.mark.parametrize("failure", [None, "revocation", "reader", "cut", "late_credential"])
async def test_full_message_read_has_current_authority_and_late_credential_fences(monkeypatch, failure):
    module = importlib.import_module("firefly_forge.activity_service")
    secret = "fictional-boundary-credential"
    message = "x" * 16380 + secret + "rest"
    projection, payload = project_activity(RUN, EVENT, "stdout", message)
    descriptor = projection["activity_content"]
    state = dict(allowed=True, registered=False, active=False, changed=False)
    calls = []
    run = {"id": RUN, "repository_id": REPO, "project_id": None, "component_id": None}
    observation = module.ActivityObservation(
        run, frozenset({REPO}), {"id": EVENT, "stage": "stdout", **projection}, "1" * 64
    )
    actor = Principal(UUID(int=9), "local", "reader", authentication_method="local_bootstrap")

    @asynccontextmanager
    async def unit():
        state["active"] = True
        try:
            yield None
        finally:
            state["active"] = False

    async def admit(value):
        assert not state["active"] and secret in value
        calls.append("full-admission")
        if failure == "revocation":
            state["allowed"] = False
        elif failure == "reader":
            CURRENT_PRINCIPAL.set(None)
        elif failure == "cut":
            state["changed"] = True
        elif failure == "late_credential":
            state["registered"] = True

    def reject(value):
        assert secret in value
        calls.append("full-local-fence")
        if state["registered"]:
            raise ValueError("Late credential spans a chunk boundary")

    async def content(*args):
        assert state["active"]
        calls.append("payload")
        return payload

    async def capture(*args):
        item = copy.deepcopy(observation)
        if state["changed"]:
            return module.ActivityObservation(item.run, item.repositories, item.event, "2" * 64)
        return item

    async def header(*args):
        return run

    class Access:
        async def current_administrator(self, principal):
            calls.append("authorize")
            if not state["allowed"]:
                raise AccessDenied("Revoked")
            return True

        async def require_repository(self, principal, repository, permission):
            assert repository == REPO and permission == "read"

    store = SimpleNamespace(_unit=unit, admit_credentials=admit, reject_credentials=reject)
    service = module.ActivityArchive(store, records=SimpleNamespace(payload=content))
    service.evidence.access = Access()
    monkeypatch.setattr(service, "_capture", capture)
    monkeypatch.setattr(service, "_header", header)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        query = ActivityContentQuery(expected_descriptor=descriptor["descriptor_sha256"])
        if failure:
            with pytest.raises((AccessDenied, ActivityUnavailable)):
                await service.content(RUN, EVENT, query)
        else:
            response = await service.content(RUN, EVENT, query)
            assert response["next_chunk"] == 1 and response["total_bytes"] == len(payload)
        assert calls[0] == "authorize" and calls.count("payload") == 1
        assert calls.count("full-admission") == 1
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_missing_native_reader_refuses_before_any_private_header(monkeypatch):
    module = importlib.import_module("firefly_forge.activity_service")
    service = module.ActivityArchive(SimpleNamespace())

    async def no_header(*args):
        pytest.fail("Unauthenticated reader reached private metadata")

    monkeypatch.setattr(service, "_header", no_header)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        with pytest.raises(AccessDenied):
            await service.content(RUN, EVENT, ActivityContentQuery(expected_descriptor="a" * 64))
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_injected_evidence_authority_withholds_content_before_snapshot(monkeypatch):
    from firefly_forge.activity_service import ActivityArchive
    from firefly_forge.run_evidence import RunEvidence

    store = SimpleNamespace()
    evidence = RunEvidence(store)

    class RevokedAccess:
        async def current_administrator(self, reader):
            raise AccessDenied("Current evidence reader was revoked")

    evidence.access = RevokedAccess()
    service = ActivityArchive(store, evidence=evidence)

    async def header(run_id):
        assert run_id == RUN
        return {"id": RUN, "repository_id": REPO, "project_id": None}

    async def no_capture(*args):
        pytest.fail("Revoked registered evidence reader reached retained content")

    monkeypatch.setattr(service, "_header", header)
    monkeypatch.setattr(service, "_capture", no_capture)
    actor = Principal(UUID(int=9), "local", "reader", authentication_method="local_bootstrap")
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises(AccessDenied, match="revoked"):
            await service.content(RUN, EVENT, ActivityContentQuery(expected_descriptor="a" * 64))
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_native_blob_scalar_preflight_refuses_before_materialization():
    from firefly_forge.activity_persistence import ForgeEventContentRepository

    class Session:
        async def execute(self, statement):
            return SimpleNamespace(one_or_none=lambda: (1, 4194305, "a" * 64, 4194305))

        async def scalar(self, statement):
            pytest.fail("Oversized event blob was materialized")

    records = ForgeEventContentRepository(session=Session())
    with pytest.raises(ActivityUnavailable, match="activity_integrity"):
        await records.payload(UUID(RUN), UUID(EVENT), 12, "a" * 64)
