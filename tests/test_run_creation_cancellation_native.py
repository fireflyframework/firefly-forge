# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED ONLY for the sole native operator; no model or worker execution."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_run_creation_requests_native import registered
from test_run_creation_scopes_native import nonadministrator

from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.run_creation_models import RunCreationCancelInput, RunCreationInput
from firefly_forge.run_creation_requests import RunCreationRequests


def cancellation(body):
    original = RunCreationInput.model_validate(body).input.model_dump(mode="json", exclude_unset=True)
    return RunCreationCancelInput.model_validate(
        {
            "version": 1,
            "request_digest": digest({"version": 1, "input": original}),
            **{key: original.get(key) for key in ("repository_id", "project_id", "component_id")},
        }
    )


async def counts(store):
    async with store._unit() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in (
                    "forge_runs",
                    "forge_events",
                    "forge_run_creation_requests",
                    "forge_run_creation_reservations",
                    "forge_run_creation_cancellations",
                )
            ]
        )


async def test_native_cancellation_commits_while_original_prepares_and_blocks_its_late_insert(
    store, store_factory, tmp_path, monkeypatch
):
    async with registered(store, tmp_path) as (_, _, body):
        other = await store_factory(tmp_path / "other" / "factory.db")
        original, cancelling = RunCreationRequests(store), RunCreationRequests(other)
        prepared, resume = asyncio.Event(), asyncio.Event()
        prepare = store.prepare_run

        async def paused(request):
            value = await prepare(request)
            prepared.set()
            await resume.wait()
            return value

        monkeypatch.setattr(store, "prepare_run", paused)
        request_id = uuid4().hex
        task = asyncio.create_task(original.admit(request_id, body))
        try:
            await asyncio.wait_for(prepared.wait(), 10)
            assert await counts(store) == (0, 0, 0, 1, 0)
            decision, created = await cancelling.cancel(request_id, cancellation(body.model_dump(exclude_unset=True)))
            assert created and decision["outcome"] == "cancelled"
        finally:
            resume.set()
        with pytest.raises(ValueError, match="cancelled"):
            await task
        assert await counts(store) == (0, 0, 0, 1, 1)
        assert await original.decision(request_id) == decision


async def test_native_concurrent_admit_cancel_has_one_immutable_terminal_outcome(store, store_factory, tmp_path):
    async with registered(store, tmp_path) as (_, _, body):
        other = await store_factory(tmp_path / "other" / "factory.db")
        original, cancelling = RunCreationRequests(store), RunCreationRequests(other)
        request_id = uuid4().hex
        admitted, cancelled = await asyncio.gather(
            original.admit(request_id, body),
            cancelling.cancel(request_id, cancellation(body.model_dump(exclude_unset=True))),
            return_exceptions=True,
        )
        assert not isinstance(cancelled, BaseException)
        terminal = await original.decision(request_id)
        if terminal["outcome"] == "cancelled":
            assert isinstance(admitted, ValueError)
            assert await counts(store) == (0, 0, 0, 1, 1)
        else:
            assert not isinstance(admitted, BaseException)
            assert terminal["receipt"] == admitted[0]
            assert cancelled == (terminal, False)
            assert await counts(store) == (1, 1, 1, 1, 0)
        assert await cancelling.decision(request_id) == terminal


async def test_native_cancel_append_failure_rolls_back_new_binding_and_control_rows(store, tmp_path, monkeypatch):
    async with registered(store, tmp_path) as (_, _, body):
        manager = RunCreationRequests(store)
        save = manager.controls.cancellations.save

        async def fail(row):
            await save(row)
            raise ValueError("Owned cancellation insert rollback")

        monkeypatch.setattr(manager.controls.cancellations, "save", fail)
        request_id = uuid4().hex
        with pytest.raises(ValueError, match="rollback"):
            await manager.cancel(request_id, cancellation(body.model_dump(exclude_unset=True)))
        assert await counts(store) == (0, 0, 0, 0, 0)
        with pytest.raises(KeyError):
            await manager.decision(request_id)


async def test_native_reservation_and_cancellation_are_immutable_without_storing_raw_input(store, tmp_path):
    async with registered(store, tmp_path) as (_, _, body):
        manager, request_id = RunCreationRequests(store), uuid4().hex
        decision, _ = await manager.cancel(request_id, cancellation(body.model_dump(exclude_unset=True)))
        for table in ("forge_run_creation_reservations", "forge_run_creation_cancellations"):
            async with store._unit() as session:
                data = await session.scalar(text(f"SELECT data FROM {table} WHERE id=:id"), {"id": request_id})
                assert "Fictional admission only" not in str(data) and "brief" not in data and "input" not in data
            for operation in (
                f"UPDATE {table} SET data='{{}}'::jsonb WHERE id=:id",
                f"DELETE FROM {table} WHERE id=:id",
            ):
                with pytest.raises(DBAPIError):
                    async with store._unit() as session:
                        await session.execute(text(operation), {"id": request_id})
        assert await manager.decision(request_id) == decision
        with pytest.raises(ValueError, match="cancelled"):
            await manager.admit(request_id, body)


async def test_native_user_quota_excludes_terminal_rows_and_never_blocks_cancel(store, tmp_path):
    async with registered(store, tmp_path) as (actor, _, body):
        manager = RunCreationRequests(store)
        incoming = cancellation(body.model_dump(exclude_unset=True)).model_dump()
        request_ids = [uuid4().hex for _ in range(50)]
        for request_id in request_ids:
            assert await manager.controls.reserve(actor, request_id, incoming) is None
        async with store._unit():
            assert await manager.controls.reservations.pending_for_actor(actor.id) == 50
        with pytest.raises(ValueError, match="Resolve"):
            await manager.admit(uuid4().hex, body)
        await manager.cancel(uuid4().hex, RunCreationCancelInput.model_validate(incoming))
        await manager.cancel(request_ids[0], RunCreationCancelInput.model_validate(incoming))
        receipt, created = await manager.admit(uuid4().hex, body)
        assert created and receipt["run_id"]
        async with store._unit():
            assert await manager.controls.reservations.pending_for_actor(actor.id) == 49


async def test_native_read_only_relogin_can_cancel_but_revoked_reader_and_foreign_owner_cannot(store, tmp_path):
    async with nonadministrator(store, tmp_path) as c:
        identity, issue, _, actor, access, _, projects, body = c
        manager, request_id = RunCreationRequests(store), uuid4().hex
        incoming = cancellation(body)
        token = CURRENT_PRINCIPAL.set(actor)
        try:
            await manager.controls.reserve(actor, request_id, incoming.model_dump())
            foreign = await identity.authenticate(
                __import__("test_identity").request(
                    bearer="Bearer " + issue("forge-api", roles=["forge-worker"], sub="foreign-native-owner")
                )
            )
            await access.set_membership(projects[0]["id"], foreign, "developer")
            foreign_context = CURRENT_PRINCIPAL.set(foreign)
            try:
                with pytest.raises(AccessDenied):
                    await manager.cancel(request_id, incoming)
            finally:
                CURRENT_PRINCIPAL.reset(foreign_context)
            await access.set_membership(projects[0]["id"], actor, "viewer")
            session = replace(actor, authentication_method="oidc_session")
            session_context = CURRENT_PRINCIPAL.set(session)
            try:
                with pytest.raises(ValueError, match="authentication method"):
                    await manager.admit(request_id, RunCreationInput.model_validate(body))
                decision, _ = await manager.cancel(request_id, incoming)
                assert decision["cancellation"]["original_actor"]["authentication_method"] == "oidc_bearer"
                assert decision["cancellation"]["cancelled_by"]["authentication_method"] == "oidc_session"
                assert await manager.decision(request_id) == decision
                await access.set_membership(projects[0]["id"], actor, None)
                with pytest.raises(AccessDenied):
                    await manager.decision(request_id)
            finally:
                CURRENT_PRINCIPAL.reset(session_context)
        finally:
            CURRENT_PRINCIPAL.reset(token)
        assert await counts(store) == (0, 0, 0, 1, 1)


async def test_registered_native_cancel_decision_api_closes_a_refused_original_without_another_run(
    store, tmp_path, monkeypatch
):
    import json

    from httpx import ASGITransport, AsyncClient

    from firefly_forge.api import create_application
    from firefly_forge.identity import IdentityService

    async with nonadministrator(store, tmp_path) as c:
        identity, _, credential, actor, access, _, projects, body = c
        for key, value in {
            "FORGE_AUTH_MODE": "oidc",
            "FORGE_API_TOKEN": "",
            "FORGE_OIDC_ISSUER": identity.config.issuer,
            "FORGE_OIDC_CLIENT_ID": identity.config.client_id,
            "FORGE_OIDC_CLIENT_SECRET": identity.config.client_secret.get_secret_value(),
            "FORGE_OIDC_API_AUDIENCE": identity.config.api_audience,
            "FORGE_OIDC_REDIRECT_URI": identity.config.redirect_uri,
            "FORGE_IDENTITY_ENCRYPTION_KEY": identity.config.encryption_key.get_secret_value(),
        }.items():
            monkeypatch.setenv(key, value)
        app = create_application(tmp_path / "api", role="api")
        async with app.router.lifespan_context(app):
            native_identity = app.state.runtime.context.get_bean(IdentityService)
            await native_identity.client.aclose()
            native_identity.client = identity.client
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="https://testserver",
                headers={"Authorization": "Bearer " + credential},
            ) as client:
                refused_body = {"version": 1, "input": {**body["input"], "sandbox": {"mode": "host"}}}
                url = "/api/v1/run-creation-requests/" + uuid4().hex
                assert (await client.post(url, json=refused_body)).status_code == 409
                assert await counts(store) == (0, 0, 0, 1, 0)
                assert (await client.get(url + "/decision")).status_code == 404
                await access.set_membership(projects[0]["id"], actor, "viewer")
                cancelled = await client.post(url + "/cancel", json=cancellation(refused_body).model_dump())
                assert cancelled.status_code == 201, cancelled.text
                assert cancelled.json()["outcome"] == "cancelled"
                read = await client.get(url + "/decision")
                assert read.status_code == 200 and read.content == cancelled.content
                assert (
                    await client.post(url + "/cancel", json=cancellation(refused_body).model_dump())
                ).content == read.content
                await access.set_membership(projects[0]["id"], actor, "developer")
                assert (await client.post(url, json=refused_body)).status_code == 409
                assert (await client.post(url + "/cancel", content=b"x" * 4097)).status_code == 413
                assert await counts(store) == (0, 0, 0, 1, 1)
                other_url = "/api/v1/run-creation-requests/" + uuid4().hex
                admitted = await client.post(other_url, json=body)
                assert admitted.status_code == 201, admitted.text
                already = await client.post(other_url + "/cancel", json=cancellation(body).model_dump())
                assert already.status_code == 200 and already.json()["outcome"] == "admitted"
                assert (
                    json.dumps(already.json()["receipt"], sort_keys=True, separators=(",", ":")).encode()
                    == admitted.content
                )
                assert await counts(store) == (1, 1, 1, 2, 1)
