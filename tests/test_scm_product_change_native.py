# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared operator-only PG/role-API cases; no real provider, worker or model execution."""

import asyncio
import copy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_collaboration import product
from test_scm_resume_native import native_resume  # noqa: F401
from worker_reply_fixtures import authenticated_operator

from firefly_forge.access_persistence import ForgeMembershipRepository
from firefly_forge.collaboration_persistence import ForgeCollaborationSettingsRepository
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.product_lifecycle import ProductLifecycle
from firefly_forge.project_access import AccessDenied
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.scm_product_change_controllers import SCMProductChangeController
from firefly_forge.scm_product_change_models import SCMProductChangeCommandV1, SCMProductChangeGrantInputV1
from firefly_forge.scm_product_change_service import STATE_KEY, SCMProductChangeAuthority
from firefly_forge.store import Store


@pytest.fixture
def native_product(native_resume):  # noqa: F811
    case = native_resume
    service = case.http.app.state.runtime.context.get_bean(SCMProductChangeAuthority)
    controller = case.http.app.state.runtime.context.get_bean(SCMProductChangeController)
    assert service.store is case.store and controller.authority is service
    assert case.collaboration.product_change_authority is service
    path = f"/api/v1/projects/{case.project['id']}/repositories/{case.repo['id']}"
    definition = product().model_dump(mode="json")
    definition["customer"] = "Fictional café"
    saved = case.http.put(f"/api/v1/repositories/{case.repo['id']}/product", json=definition)
    assert saved.status_code == 200, saved.text
    original = saved.json()

    def consent():
        body = SCMProductChangeGrantInputV1(
            version=1,
            request_id=uuid4().hex,
            expected_previous_digest=None,
            expected_project_binding_digest=case.binding["project_binding_digest"],
            expected_binding_request_id=case.binding_body["request_id"],
            expected_binding_digest=case.binding["digest"],
            component_ids=[item["id"] for item in case.project["definition"]["components"]],
            allowed_actions=["product_change"],
            active=True,
            acknowledge_product_definition_edit=True,
        )
        response = case.http.post(path + "/scm-product-change-grants", json=body.model_dump(mode="json"))
        assert response.status_code == 201, response.text
        return response.json()

    def command(grant, previous=None, **changes):
        base = previous or original
        return SCMProductChangeCommandV1.model_validate(
            dict(
                version=1,
                action="product_change",
                project_id=case.project["id"],
                expected_project_binding_digest=case.binding["project_binding_digest"],
                expected_grant_request_id=grant["request"]["request_id"],
                expected_grant_digest=grant["digest"],
                change=dict(
                    request_id=uuid4().hex,
                    base_revision=base["revision"],
                    base_digest=base["digest"],
                    append_journeys=["Escalate ticket"],
                    **changes,
                ),
            )
        ).model_dump(mode="json", exclude={"action"})

    def receive(command):
        delivery = case.receive("product_change", command)
        assert delivery["status"] == "received", delivery
        return delivery

    def current():
        response = case.http.get(f"/api/v1/repositories/{case.repo['id']}/product")
        assert response.status_code == 200, response.text
        return response.json()

    return SimpleNamespace(
        case=case,
        service=service,
        path=path,
        original=original,
        consent=consent,
        command=command,
        receive=receive,
        current=current,
    )


def test_native_product_edit_roundtrip_has_exact_grant_and_product_without_execution(native_product):
    p = native_product
    c = p.case
    before_run = c.http.portal.call(c.store.get_run, c.run["id"])
    before_journal = c.http.portal.call(c.journal.entries, c.run["id"])
    before_runs = c.http.portal.call(c.store.runs)
    before_comments = copy.deepcopy(c.comments)
    grant = p.consent()
    for suffix in ["/requests/" + grant["request"]["request_id"], "/current/" + c.binding_body["request_id"]]:
        response = c.http.get(p.path + "/scm-product-change-grants" + suffix)
        assert response.status_code == 200 and response.json() == grant
    delivery = p.receive(p.command(grant))
    result = c.process(delivery)
    assert result["status"] == "processed" and result["action_status"] == "completed"
    receipt = result["action_result"]
    current = p.current()
    assert current["revision"] == 2 and current["change_receipt"]["actor_id"] == c.binding["request"]["native_user_id"]
    assert current["change_receipt"]["implementation_status"] == "not_started"
    assert current["definition"]["journey"] == [*p.original["definition"]["journey"], "Escalate ticket"]
    assert current["definition"]["features"] == p.original["definition"]["features"]
    assert not current["definition"]["functional_reviewed"]
    assert receipt["product_digest"] == current["digest"] and receipt["authentication_method"] == "scm_mapping"
    exact = c.http.get(p.path + "/scm-product-changes/requests/" + receipt["request"]["change"]["request_id"])
    assert exact.status_code == 200 and exact.json() == receipt
    assert c.process(delivery) == result
    assert c.http.portal.call(c.store.get_run, c.run["id"]) == before_run
    assert c.http.portal.call(c.journal.entries, c.run["id"]) == before_journal
    assert c.http.portal.call(c.store.runs) == before_runs and c.comments == before_comments


def test_native_unmapped_product_edit_is_known_setup_refusal_without_action_intent(native_product):
    p = native_product
    absent = {"request": {"request_id": uuid4().hex}, "digest": "f" * 64}
    result = p.case.process(p.receive(p.command(absent)))
    assert result["status"] == "denied"
    # The normal receipts list also exposes this typed pre-admission refusal.
    rows = p.case.http.get("/api/v1/collaboration/receipts").json()
    row = next(item for item in rows if item["id"] == result["id"])
    assert row["action_status"] == "not_dispatched" and "action_result" not in row
    assert p.current() == p.original


def test_native_same_request_returns_original_product_after_later_revision(native_product):
    p = native_product
    command = p.command(p.consent())
    first = p.case.process(p.receive(command))["action_result"]
    current = p.current()
    newer = p.case.http.post(
        f"/api/v1/repositories/{p.case.repo['id']}/product/changes",
        json=dict(
            request_id=uuid4().hex,
            base_revision=2,
            base_digest=current["digest"],
            append_journeys=["Reopen ticket"],
        ),
    )
    assert newer.status_code == 201 and newer.json()["revision"] == 3
    second = p.case.process(p.receive(command))
    assert second["action_result"] == first and p.current() == newer.json()
    exact = p.case.http.get(p.path + "/scm-product-changes/requests/" + command["change"]["request_id"])
    assert exact.status_code == 200 and exact.json() == first


@pytest.mark.parametrize("drift", ["product", "project", "foreign_project"])
def test_native_stale_or_foreign_product_cut_writes_nothing(native_product, drift):
    p = native_product
    command = p.command(p.consent())
    if drift == "product":
        changed = p.case.http.post(
            f"/api/v1/repositories/{p.case.repo['id']}/product/changes",
            json=dict(
                request_id=uuid4().hex,
                base_revision=1,
                base_digest=p.original["digest"],
                append_journeys=["Reopen ticket"],
            ),
        )
        assert changed.status_code == 201
    elif drift == "project":

        async def change_project():
            definition = ProjectDefinition.model_validate(p.case.project["definition"])
            definition.name = "Explicitly changed project"
            await Projects(p.case.store).configure(definition, p.case.project["id"])

        p.case.http.portal.call(change_project)
    else:
        command["project_id"] = uuid4().hex
    before = p.current()
    result = p.case.process(p.receive(command))
    assert result["status"] == "denied" and p.current() == before


@pytest.mark.parametrize("revocation", ["identity", "membership", "late_credentials"])
def test_native_revocation_or_fresh_credentials_refuse_product_write(native_product, monkeypatch, revocation):
    p = native_product
    command = p.command(p.consent())
    delivery = p.receive(command)
    original = p.service._preflight
    lifecycle_save = ProductLifecycle._save_prepared

    async def revoke_after_provider(context):
        await original(context)
        if revocation in {"identity", "membership"}:
            async with p.case.store._unit():
                user_id = UUID(p.case.binding["request"]["native_user_id"])
                if revocation == "identity":
                    users = ForgeIdentityUserRepository()
                    user = await users.find_by_id(user_id)
                    user.enabled = False
                    await users.save(user)
                else:
                    memberships = ForgeMembershipRepository()
                    for row in await memberships.for_user(user_id):
                        if row.project_id.hex == p.case.project["id"]:
                            row.role = "viewer"
                            await memberships.save(row)

    async def save_then_register(self, *args, **kwargs):
        result = await lifecycle_save(self, *args, **kwargs)
        monkeypatch.setattr(p.case.store, "credential_registry", lambda: {"OWNED_FIXTURE_API_KEY": "Escalate ticket"})
        return result

    monkeypatch.setattr(p.service, "_preflight", revoke_after_provider)
    if revocation == "late_credentials":
        monkeypatch.setattr(ProductLifecycle, "_save_prepared", save_then_register)
    assert p.case.process(delivery)["status"] == "denied"
    assert p.current() == p.original
    missing = p.case.http.get(p.path + "/scm-product-changes/requests/" + command["change"]["request_id"])
    assert missing.status_code == 404


def test_native_product_commit_serializes_with_membership_revocation(native_product, monkeypatch):
    p = native_product
    delivery = p.receive(p.command(p.consent()))

    async def exercise():
        from firefly_forge.identity import Principal
        from firefly_forge.project_access import ProjectAccess

        second = Store(p.case.store.path)
        await second.initialize()
        reached, release, pid_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = p.service._save_operation
        pid = None

        async def hold(*args):
            result = await original(*args)
            reached.set()
            await release.wait()
            return result

        async def revoke():
            nonlocal pid
            async with authenticated_operator(second), second._unit() as session:
                pid = await session.scalar(text("SELECT pg_backend_pid()"))
                user = await ForgeIdentityUserRepository().find_by_id(UUID(p.case.binding["request"]["native_user_id"]))
                target = Principal(user.id, user.issuer, user.subject)
                pid_ready.set()
                await ProjectAccess(second).set_membership(p.case.project["id"], target, "viewer")

        monkeypatch.setattr(p.service, "_save_operation", hold)
        processing = asyncio.create_task(p.service.process(delivery["id"], ProductLifecycle(p.case.collaboration)))
        revoker = None
        try:
            await asyncio.wait_for(reached.wait(), 5)
            revoker = asyncio.create_task(revoke())
            await asyncio.wait_for(pid_ready.wait(), 5)
            async with asyncio.timeout(5):
                while True:
                    async with p.case.store._unit() as session:
                        waiting = await session.scalar(
                            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}
                        )
                    if waiting == "Lock":
                        break
                    await asyncio.sleep(0.01)
            assert not revoker.done()
            release.set()
            outcomes = await asyncio.gather(processing, revoker, return_exceptions=True)
            assert outcomes[1] is None and isinstance(outcomes[0], (dict, AccessDenied))
            assert (await p.case.collaboration.product(p.case.repo["id"]))["revision"] == 2
            with pytest.raises(AccessDenied):
                await p.service.process(delivery["id"], ProductLifecycle(p.case.collaboration))
        finally:
            release.set()
            await asyncio.gather(processing, *([revoker] if revoker else []), return_exceptions=True)
            await second.close()

    p.case.http.portal.call(exercise)


def test_native_postcommit_delivery_loss_keeps_same_exact_request(native_product, monkeypatch):
    p = native_product
    command = p.command(p.consent())
    original = p.case.store.admit_credentials

    async def lose_return(value):
        await original(value)
        if isinstance(value, dict) and value.get("action_status") == "completed":
            raise AccessDenied("Owned post-commit permission-to-return refusal")

    monkeypatch.setattr(p.case.store, "admit_credentials", lose_return)
    delivery = p.receive(command)
    assert p.case.process(delivery)["status"] == "withheld"
    monkeypatch.setattr(p.case.store, "admit_credentials", original)
    exact = p.case.http.get(p.path + "/scm-product-changes/requests/" + command["change"]["request_id"])
    assert exact.status_code == 200 and exact.json()["product_revision"] == 2
    assert p.case.process(delivery)["action_result"] == exact.json() and p.current()["revision"] == 2
    missing = p.case.http.get(p.path + "/scm-product-changes/requests/" + uuid4().hex)
    assert missing.status_code == 404 and p.current()["revision"] == 2


def test_native_quota_is_atomic_and_existing_receipts_remain_immutable(native_product):
    p = native_product
    grant = p.consent()
    command = p.command(grant)

    async def fill_index():
        async with p.case.store._unit():
            records = ForgeCollaborationSettingsRepository()
            row = await records.find_for_repository(UUID(p.case.repo["id"]))
            state = copy.deepcopy(row.data[STATE_KEY])
            state["operations"] = {
                p.case.project["id"] + ":" + f"{n:032x}": {"event_id": "f" * 32, "digest": "f" * 64}
                for n in range(5000)
            }
            row.data = {**row.data, STATE_KEY: state}
            await records.save(row)

    p.case.http.portal.call(fill_index)
    assert p.case.process(p.receive(command))["status"] == "denied"
    assert p.current() == p.original

    async def mutate():
        async with p.case.store._unit() as session:
            await session.execute(
                text(
                    f'UPDATE "{p.case.store.schema}".forge_access_events '
                    "SET data = data || CAST(:changed AS jsonb) WHERE id=:id"
                ),
                {"id": UUID(grant["event_id"]), "changed": '{"changed":true}'},
            )

    with pytest.raises(DBAPIError):
        p.case.http.portal.call(mutate)
    exact = p.case.http.get(p.path + "/scm-product-change-grants/requests/" + grant["request"]["request_id"])
    assert exact.status_code == 200 and exact.json() == grant
