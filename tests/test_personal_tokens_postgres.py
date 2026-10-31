# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real PostgreSQL lock ordering, migration, current grants and one-time custody."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import func, select, text

from firefly_forge.access_persistence import ForgeMembershipEntity, ForgeMembershipRepository
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
from firefly_forge.migrations import SCHEMA_REVISION, migrate
from firefly_forge.personal_token_models import PersonalTokenInput, PersonalTokenRotationInput
from firefly_forge.personal_token_persistence import (
    ForgePersonalTokenEntity,
    ForgePersonalTokenEventEntity,
    ForgePersonalTokenRepository,
)
from firefly_forge.personal_tokens import PersonalTokenDisclosure, PersonalTokens
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectEntity, ForgeProjectRepository
from firefly_forge.store import Store


@pytest.fixture
async def pg_tokens(store):
    owner = Principal(uuid4(), "https://identity.example/realms/forge", "owner", authentication_method="oidc_session")
    other = Principal(uuid4(), owner.issuer, "other-owner", authentication_method="oidc_session")
    project, other_project, membership_id = uuid4(), uuid4(), uuid4()
    async with store._unit():
        for user in (owner, other):
            await ForgeIdentityUserRepository().save(
                ForgeIdentityUserEntity(
                    id=user.id, issuer=user.issuer, subject=user.subject, enabled=True, api_roles=[]
                )
            )
        for project_id in (project, other_project):
            await ForgeProjectRepository().save(
                ForgeProjectEntity(id=project_id, data={"name": "Retained PostgreSQL fixture", "version": 1})
            )
        for user, project_id, row_id in ((owner, project, membership_id), (other, other_project, uuid4())):
            await ForgeMembershipRepository().save(
                ForgeMembershipEntity(id=row_id, project_id=project_id, user_id=user.id, role="owner", active=True)
            )
    tokens = PersonalTokens(store)
    store.personal_tokens = tokens
    return SimpleNamespace(
        store=store,
        owner=owner,
        other=other,
        project=project,
        other_project=other_project,
        membership_id=membership_id,
        tokens=tokens,
    )


def request(c, **changes):
    return PersonalTokenInput(
        **{
            "request_id": uuid4().hex,
            "name": "PostgreSQL observer",
            "project_ids": [c.project.hex],
            "expires_at": datetime.now(UTC) + timedelta(days=1),
            **changes,
        }
    )


def rotation():
    return PersonalTokenRotationInput(request_id=uuid4().hex, expires_at=datetime.now(UTC) + timedelta(days=2))


async def counts(store):
    async with store._unit() as session:
        return (
            await session.scalar(select(func.count()).select_from(ForgePersonalTokenEntity)),
            await session.scalar(select(func.count()).select_from(ForgePersonalTokenEventEntity)),
        )


async def stored_document(store):
    async with store._unit() as session:
        tokens = list(await session.scalars(text("SELECT to_jsonb(t) FROM forge_personal_tokens t")))
        events = list(await session.scalars(text("SELECT to_jsonb(e) FROM forge_personal_token_events e")))
    return json.dumps({"tokens": tokens, "events": events}, sort_keys=True)


async def test_revision31_upgrade_keeps_legacy_data_and_adds_native_jsonb_timezone_and_constraints(pg_tokens):
    c = pg_tokens
    assert c.store.schema.startswith("forge_test_")
    async with c.store._unit() as session:
        before = await session.scalar(text("SELECT jsonb_agg(to_jsonb(u) ORDER BY id) FROM forge_identity_users u"))
        project_before = await session.scalar(text("SELECT jsonb_agg(to_jsonb(p) ORDER BY id) FROM forge_projects p"))
        # These two new tables are empty and belong solely to this disposable fixture schema.
        await session.execute(text("DROP TABLE forge_personal_token_events"))
        await session.execute(text("DROP TABLE forge_personal_tokens"))
        await session.execute(text("DELETE FROM forge_schema_migrations WHERE revision>31"))
        await session.execute(text("INSERT INTO forge_schema_migrations(revision) VALUES(31) ON CONFLICT DO NOTHING"))
    async with c.store._unit() as session:
        await migrate(session, c.store.schema)
        await migrate(session, c.store.schema)
        assert (
            await session.scalar(text("SELECT jsonb_agg(to_jsonb(u) ORDER BY id) FROM forge_identity_users u"))
            == before
        )
        assert (
            await session.scalar(text("SELECT jsonb_agg(to_jsonb(p) ORDER BY id) FROM forge_projects p"))
            == project_before
        )
        assert await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations")) == SCHEMA_REVISION
        columns = dict(
            (
                await session.execute(
                    text(
                        "SELECT column_name,data_type FROM information_schema.columns "
                        "WHERE table_schema=:schema AND table_name='forge_personal_tokens'"
                    ),
                    {"schema": c.store.schema},
                )
            ).all()
        )
        assert columns["project_ids"] == columns["permissions"] == "jsonb"
        assert columns["expires_at"] == columns["revoked_at"] == "timestamp with time zone"
        assert "token" not in columns and "secret" not in columns and "secret_digest" in columns
    issued = await c.tokens.create(c.owner, request(c))
    assert (await c.tokens.resolve(issued.secret.get_secret_value())).principal.id == c.owner.id


async def test_token_and_event_custody_survive_cold_native_store_restart_without_secret_storage(pg_tokens):
    c = pg_tokens
    pending = request(c)
    issued = await c.tokens.create(c.owner, pending)
    raw = issued.secret.get_secret_value()
    before = await stored_document(c.store)
    if raw in before:
        pytest.fail("A raw personal credential was persisted")
    await c.store.close()
    restarted = Store(c.store.path, database_url=c.store.database_url, schema=c.store.schema)
    try:
        await restarted.initialize(existing_only=True)
        tokens = PersonalTokens(restarted)
        assert await stored_document(restarted) == before
        assert (await tokens.resolve(raw)).principal.id == c.owner.id
        repeated = await tokens.create(c.owner, pending)
        assert repeated.secret is None and repeated.credential["id"] == issued.credential["id"]
        assert await counts(restarted) == (1, 1)
    finally:
        await restarted.close()


async def test_parallel_same_owner_request_commits_one_token_and_discloses_only_once(pg_tokens):
    c = pg_tokens
    pending = request(c)
    results = await asyncio.gather(*(c.tokens.create(c.owner, pending) for _ in range(4)))
    assert len({result.credential["id"] for result in results}) == 1
    assert sum(result.secret is not None for result in results) == 1
    assert await counts(c.store) == (1, 1)
    disclosed = next(result for result in results if result.secret is not None)
    assert (await c.tokens.resolve(disclosed.secret.get_secret_value())).principal.id == c.owner.id


async def test_parallel_conflicting_same_owner_request_has_one_winner_not_duplicate_or_integrity_error(pg_tokens):
    c = pg_tokens
    left = request(c)
    right = left.model_copy(update={"name": "Different intended credential"})
    results = await asyncio.gather(
        c.tokens.create(c.owner, left), c.tokens.create(c.owner, right), return_exceptions=True
    )
    assert sum(isinstance(result, PersonalTokenDisclosure) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert await counts(c.store) == (1, 1)


async def test_same_request_id_under_same_issuer_does_not_merge_two_owners_or_projects(pg_tokens):
    c = pg_tokens
    left = request(c)
    right = left.model_copy(update={"project_ids": [c.other_project.hex]})
    issued = await asyncio.gather(c.tokens.create(c.owner, left), c.tokens.create(c.other, right))
    assert issued[0].credential["id"] != issued[1].credential["id"]
    assert all(value.secret is not None for value in issued) and await counts(c.store) == (2, 2)
    for user, project, value in zip((c.owner, c.other), (c.project, c.other_project), issued, strict=True):
        grant = await c.tokens.resolve(value.secret.get_secret_value())
        assert grant.principal.id == user.id and grant.memberships[0]["project_id"] == project.hex
    with pytest.raises(KeyError):
        await c.tokens.revoke(c.other, issued[0].credential["id"])


async def test_competing_rotations_create_one_successor_and_never_revive_old_token(pg_tokens):
    c = pg_tokens
    issued = await c.tokens.create(c.owner, request(c))
    results = await asyncio.gather(
        c.tokens.rotate(c.owner, issued.credential["id"], rotation()),
        c.tokens.rotate(c.owner, issued.credential["id"], rotation()),
        return_exceptions=True,
    )
    successors = [value for value in results if isinstance(value, PersonalTokenDisclosure)]
    assert len(successors) == 1 and sum(isinstance(value, ValueError) for value in results) == 1
    assert await counts(c.store) == (2, 3)
    with pytest.raises(AccessDenied):
        await c.tokens.resolve(issued.secret.get_secret_value())
    assert (await c.tokens.resolve(successors[0].secret.get_secret_value())).principal.id == c.owner.id


class OwnerLockProbe(ForgeIdentityUserRepository):
    """Observe actual backend locks; pause only after PostgreSQL has granted them."""

    def __init__(self, *, pause=False):
        super().__init__()
        self.pause = pause
        self.requested = asyncio.Event()
        self.acquired = asyncio.Event()
        self.release = asyncio.Event()
        self.pid = None

    async def find_by_id(self, id, *, load=None, lock=None):
        if lock is not None:
            self.pid = await self._require_session().scalar(text("SELECT pg_backend_pid()"))
            self.requested.set()
        row = await super().find_by_id(id, load=load, lock=lock)
        if lock is not None:
            self.acquired.set()
            if self.pause:
                await self.release.wait()
        return row


async def actual_blocked_by(store, waiting, holding):
    # This is a test watchdog, not a production execution deadline.
    async with asyncio.timeout(10):
        while True:
            async with store._unit() as session:
                blockers = await session.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting.pid})
            if holding.pid in blockers:
                return
            await asyncio.sleep(0.01)


async def ordered_race(c, first_call, second_call):
    first_repo, second_repo = OwnerLockProbe(pause=True), OwnerLockProbe()
    first_service, second_service = (
        PersonalTokens(c.store, users=first_repo),
        PersonalTokens(c.store, users=second_repo),
    )
    first = asyncio.create_task(first_call(first_service))
    second = None
    try:
        await asyncio.wait_for(first_repo.acquired.wait(), timeout=10)
        second = asyncio.create_task(second_call(second_service))
        await asyncio.wait_for(second_repo.requested.wait(), timeout=10)
        await actual_blocked_by(c.store, second_repo, first_repo)
        assert not second.done()
        first_repo.release.set()
        return await asyncio.gather(first, second, return_exceptions=True)
    finally:
        first_repo.release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("first_effect", ["rotate", "revoke"])
async def test_rotate_vs_revoke_uses_actual_owner_lock_order_and_one_durable_result(pg_tokens, first_effect):
    c = pg_tokens
    issued = await c.tokens.create(c.owner, request(c))
    token_id = issued.credential["id"]

    async def rotate(tokens):
        return await tokens.rotate(c.owner, token_id, rotation())

    async def revoke(tokens):
        return await tokens.revoke(c.owner, token_id)

    results = await ordered_race(
        c, rotate if first_effect == "rotate" else revoke, revoke if first_effect == "rotate" else rotate
    )
    if first_effect == "rotate":
        assert isinstance(results[0], PersonalTokenDisclosure) and isinstance(results[1], dict)
        assert (await c.tokens.resolve(results[0].secret.get_secret_value())).principal.id == c.owner.id
        assert await counts(c.store) == (2, 3)
    else:
        assert isinstance(results[0], dict) and isinstance(results[1], ValueError)
        assert await counts(c.store) == (1, 2)
    with pytest.raises(AccessDenied):
        await c.tokens.resolve(issued.secret.get_secret_value())


@pytest.mark.parametrize("first_effect", ["authenticate", "revoke"])
async def test_revoke_vs_authentication_reloads_locked_token_and_rechecks_captured_principal(pg_tokens, first_effect):
    c = pg_tokens
    issued = await c.tokens.create(c.owner, request(c))

    async def authenticate(tokens):
        return await tokens.resolve(issued.secret.get_secret_value())

    async def revoke(tokens):
        return await tokens.revoke(c.owner, issued.credential["id"])

    results = await ordered_race(
        c,
        authenticate if first_effect == "authenticate" else revoke,
        revoke if first_effect == "authenticate" else authenticate,
    )
    if first_effect == "authenticate":
        assert results[0].principal.id == c.owner.id and isinstance(results[1], dict)
        with pytest.raises(AccessDenied):
            await ProjectAccess(c.store).require_project(results[0].principal, c.project.hex, "read")
    else:
        assert isinstance(results[0], dict) and isinstance(results[1], AccessDenied)
    assert await counts(c.store) == (1, 2)
    with pytest.raises(AccessDenied):
        await c.tokens.resolve(issued.secret.get_secret_value())


async def test_membership_loss_committed_during_authentication_is_observed_before_grant(pg_tokens):
    c = pg_tokens
    issued = await c.tokens.create(c.owner, request(c))
    probe = OwnerLockProbe(pause=True)
    resolving = asyncio.create_task(PersonalTokens(c.store, users=probe).resolve(issued.secret.get_secret_value()))
    try:
        await asyncio.wait_for(probe.acquired.wait(), timeout=10)
        async with c.store._unit():
            membership = await ForgeMembershipRepository().find_by_id(c.membership_id, lock=LockMode.PESSIMISTIC_WRITE)
            membership.active = False
            await ForgeMembershipRepository().save(membership)
        probe.release.set()
        with pytest.raises(AccessDenied):
            await resolving
        assert await counts(c.store) == (1, 1)
        async with c.store._unit():
            row = await ForgePersonalTokenRepository().find_by_id(UUID(hex=issued.credential["id"]))
            assert row.revoked_at is None
    finally:
        probe.release.set()
        if not resolving.done():
            resolving.cancel()
        await asyncio.gather(resolving, return_exceptions=True)


async def test_event_failure_rolls_back_native_postgres_token_and_predecessor_changes(pg_tokens, monkeypatch):
    c = pg_tokens
    issued = await c.tokens.create(c.owner, request(c))

    async def fail_event(_event):
        raise ValueError("Controlled PostgreSQL audit failure")

    monkeypatch.setattr(c.tokens.events, "save", fail_event)
    with pytest.raises(ValueError, match="audit failure"):
        await c.tokens.rotate(c.owner, issued.credential["id"], rotation())
    assert await counts(c.store) == (1, 1)
    assert (await c.tokens.resolve(issued.secret.get_secret_value())).principal.id == c.owner.id
