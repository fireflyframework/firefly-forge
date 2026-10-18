# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host admission controls; native transactions and provider identity remain separate acceptance."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_scm_product_intake_contract import intake

from firefly_forge.collaboration import Collaboration
from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.scm_product_intake import STATE_KEY, SCMProductIntake
from firefly_forge.scm_product_intake_models import SCMProductIntakeConsentInputV1


async def test_intake_without_separate_native_consent_never_uses_legacy_channel_authority(tmp_path):
    row = SimpleNamespace(
        status="received",
        data={
            "id": "a" * 32,
            "repository_id": "b" * 32,
            "status": "received",
            "command": {"action": "intake", **intake()},
        },
    )
    calls = []

    @asynccontextmanager
    async def unit():
        yield

    async def find(*args, **kwargs):
        return row

    async def settings(*args):
        return SimpleNamespace(data={})

    async def save(value):
        return value

    async def legacy(*args):
        calls.append("unsafe legacy action")
        return {"status": "processed"}

    collaboration = Collaboration(
        SimpleNamespace(configuration=SimpleNamespace(directory=tmp_path, settings_generation=object()), _unit=unit),
        scm=SimpleNamespace(),
    )
    collaboration.inbox = SimpleNamespace(find_by_id=find, save=save)
    collaboration.settings_records = SimpleNamespace(find_for_repository=settings)
    collaboration._process_channel = legacy
    result = await collaboration.process("a" * 32)
    assert result["status"] == "denied"
    assert row.data["action_status"] == "not_dispatched" and calls == []


REPOSITORY, BOOTSTRAP, DELIVERY = "b" * 32, "4" * 32, "5" * 32
ACTOR = Principal(UUID(int=7), "local:bootstrap", "operator", authentication_method="local_bootstrap")
REFERENCE = dict(
    version=1,
    provider="github",
    api_origin="https://api.github.com",
    web_origin="https://github.com",
    installation_id="9",
    remote_repository_id="42",
    external_actor_id="7",
    connection_revision="a" * 32,
    connection_scope_digest="e" * 64,
    webhook_key_revision="c" * 32,
    verification_method="raw_body_hmac_sha256",
    payload_digest="d" * 64,
)


@pytest.fixture
async def native_seam(tmp_path):
    state = dict(depth=0, calls=[], enabled=True, admin=True, key=True, after_remote=None, after_add=None, tables={})
    repo = {
        "id": REPOSITORY,
        "name": "team/stock",
        "url": "https://github.com/team/stock.git",
        "connection_provider": "github",
        "remote_repository_id": "42",
    }
    state["repo"] = repo
    settings = SimpleNamespace(
        id=uuid4(),
        repository_id=UUID(REPOSITORY),
        data={"enabled": True, "trusted_actors": ["7"], "configuration_actors": ["7"]},
    )
    user = SimpleNamespace(id=ACTOR.id, issuer=ACTOR.issuer, subject=ACTOR.subject, enabled=True)
    state["user"], state["settings"] = user, settings

    class Records:
        def __init__(self, table):
            self.table = table
            state["tables"].setdefault(table, {})

        async def find_by_id(self, identifier, **kwargs):
            return state["tables"][self.table].get(identifier)

        async def find_for_repository(self, identifier):
            return state["settings"] if identifier.hex == REPOSITORY else None

        async def find_current(self, identifier):
            return next(iter(state["tables"][self.table].values()), None)

        async def latest(self):
            return list(state["tables"][self.table].values())

        async def save(self, row):
            if self.table == "settings":
                state["settings"] = row
            else:
                state["tables"][self.table][row.id] = row
            return row

    class Session:
        async def execute(self, *args):
            pass

        async def scalar(self, query):
            return None

        async def flush(self):
            pass

        def add(self, row):
            if row.id is None:
                row.id = uuid4()
            state["tables"].setdefault(row.__tablename__, {})[row.id] = row
            if state["after_add"]:
                state["after_add"](row)

    @asynccontextmanager
    async def unit():
        outer = state["depth"] == 0
        before = copy.deepcopy((state["tables"], state["settings"])) if outer else None
        state["depth"] += 1
        try:
            yield Session()
        except BaseException:
            if outer:
                state["tables"], state["settings"] = before
            raise
        finally:
            state["depth"] -= 1

    async def repository(identifier):
        assert identifier == REPOSITORY
        return copy.deepcopy(repo)

    async def repositories(provider):
        assert state["depth"] == 0 and provider == "github"
        state["calls"].append("grant GET")
        if state["after_remote"]:
            state["after_remote"]()
        return [{"id": "42", "name": "team/stock", "url": "https://github.com/team/stock.git"}]

    async def admit(value):
        assert state["depth"] == 0

    async def authorize(*args):
        if not state["admin"]:
            raise AccessDenied("Current native administrator revoked")
        return True

    repo_records = Records("repositories")
    state["tables"]["repositories"][UUID(REPOSITORY)] = SimpleNamespace(id=UUID(REPOSITORY), data=repo)
    store = SimpleNamespace(
        schema="host_seam",
        _unit=unit,
        repository=repository,
        repository_records=repo_records,
        admit_credentials=admit,
        reject_credentials=lambda value: None,
        configuration=SimpleNamespace(directory=tmp_path, _read=lambda path: {}),
        roles=SimpleNamespace(snapshot=lambda *a: None),
        policies=SimpleNamespace(selection=lambda: {}),
    )
    scm = SimpleNamespace(
        repositories=repositories,
        delegation_scope=lambda repository: {
            k: REFERENCE[k]
            for k in (
                "provider",
                "api_origin",
                "web_origin",
                "installation_id",
                "remote_repository_id",
                "connection_revision",
                "connection_scope_digest",
            )
        },
    )
    inbox, events = Records("inbox"), Records("forge_access_events")
    service = SCMProductIntake(store, scm, Records("settings"), inbox, events)
    service.users = Records("users")
    state["tables"]["users"][ACTOR.id] = user
    service.projects, service.products = Records("forge_projects"), Records("forge_product_definitions")
    service.access.authorize_actor = authorize
    service.authority._key_current = lambda *args: state["key"]

    def delivery(identifier, command):
        data = {
            "id": identifier,
            "repository_id": REPOSITORY,
            "provider": "github",
            "remote_id": "42",
            "actor": "7",
            "digest": REFERENCE["payload_digest"],
            "verification": copy.deepcopy(REFERENCE),
            "status": "received",
            "command": command,
        }
        row = SimpleNamespace(
            id=UUID(identifier), repository_id=UUID(REPOSITORY), provider="github", status="received", data=data
        )
        state["tables"]["inbox"][row.id] = row
        return row

    delivery(BOOTSTRAP, {"action": "project"})
    context = CURRENT_PRINCIPAL.set(ACTOR)
    try:
        consent = await service.configure(
            REPOSITORY,
            SCMProductIntakeConsentInputV1(
                version=1,
                request_id="2" * 32,
                expected_previous_digest=None,
                verified_receipt_id=BOOTSTRAP,
                expected_verified_reference_digest=digest(REFERENCE),
                active=True,
                acknowledge_product_intake=True,
            ),
        )
    finally:
        CURRENT_PRINCIPAL.reset(context)
    body = {"action": "intake", **intake(), "expected_consent_digest": consent["digest"]}
    row = delivery(DELIVERY, body)
    state["calls"].clear()
    return service, state, row, delivery, body


async def test_native_seam_intake_registers_original_product_project_owner_and_receipt_once(native_seam):
    service, state, row, _, _ = native_seam
    result = await service.process(DELIVERY)
    assert result["status"] == "processed" and result["action_result"]["status"] == "registered"
    original = result["action_result"]
    assert original["verified_receipt_id"] == DELIVERY and original["native_user_id"] == ACTOR.id.hex
    product = next(iter(state["tables"]["forge_product_definitions"].values()))
    assert product.data["definition"]["customer"] == "Warehouse operator" and product.data["origin"] == {
        "kind": "manual"
    }
    assert product.data["definition"]["functional_reviewed"] is False
    assert (await service.process(DELIVERY))["action_result"] == original
    assert len(state["tables"]["forge_projects"]) == len(state["tables"]["forge_product_definitions"]) == 1
    assert len(state["tables"]["forge_access_events"]) == 2
    assert len(state["tables"]["forge_project_memberships"]) == 1
    assert not {"forge_runs", "forge_deployments", "forge_execution_requests"}.intersection(state["tables"])


@pytest.mark.parametrize("cause", ["disabled", "administrator", "signing_key", "consent", "caller"])
async def test_late_intake_revocation_refuses_before_any_native_product_write(native_seam, cause):
    service, state, row, _, _ = native_seam

    def revoke():
        if cause == "disabled":
            state["user"].enabled = False
        elif cause == "administrator":
            state["admin"] = False
        elif cause == "signing_key":
            state["key"] = False
        elif cause == "consent":
            state["settings"].data[STATE_KEY]["head"]["request"]["active"] = False
        else:
            CURRENT_PRINCIPAL.set(ACTOR)

    state["after_remote"] = revoke
    original = CURRENT_PRINCIPAL.get()
    try:
        with pytest.raises(AccessDenied):
            await service.process(DELIVERY)
        assert state["tables"]["forge_projects"] == state["tables"]["forge_product_definitions"] == {}
        assert state["tables"]["forge_access_events"] == {} and row.status == "received"
    finally:
        CURRENT_PRINCIPAL.set(original)


async def test_same_request_in_another_delivery_retains_the_first_exact_receipt(native_seam):
    service, state, _, delivery, body = native_seam
    original = (await service.process(DELIVERY))["action_result"]
    delivery("6" * 32, body)
    replay = await service.process("6" * 32)
    assert replay["action_result"] == original and replay["action_result"]["verified_receipt_id"] == DELIVERY
    assert len(state["tables"]["forge_projects"]) == len(state["tables"]["forge_product_definitions"]) == 1


async def test_conflicting_body_cannot_overwrite_the_original_registered_product(native_seam):
    service, state, _, delivery, body = native_seam
    original = (await service.process(DELIVERY))["action_result"]
    delivery("6" * 32, {**body, "name": "A different project"})
    with pytest.raises(AccessDenied):
        await service.process("6" * 32)
    assert len(state["tables"]["forge_projects"]) == 1 and len(state["tables"]["forge_product_definitions"]) == 1
    assert (await service.process(DELIVERY))["action_result"] == original


async def test_intake_native_failure_does_not_leave_a_project_or_claim_of_success(native_seam):
    service, state, row, _, _ = native_seam

    def fail(record):
        if record.__tablename__ == "forge_product_definitions":
            raise RuntimeError("controlled native insertion loss")

    state["after_add"] = fail
    with pytest.raises(RuntimeError, match="insertion"):
        await service.process(DELIVERY)
    assert state["tables"]["forge_projects"] == state["tables"]["forge_product_definitions"] == {}
    assert not state["settings"].data[STATE_KEY]["operation"]
    assert state["tables"]["inbox"][UUID(DELIVERY)].status == "received"


async def test_command_drift_during_grant_readback_cannot_bind_another_body_to_the_original_event(native_seam):
    service, state, row, _, _ = native_seam
    state["after_remote"] = lambda: row.data["command"]["definition"].update(problem="Changed after admission")
    with pytest.raises(AccessDenied):
        await service.process(DELIVERY)
    assert state["tables"]["forge_projects"] == state["tables"]["forge_product_definitions"] == {}


async def test_feedback_cannot_post_a_result_that_differs_from_the_immutable_operation(native_seam):
    service, state, row, _, _ = native_seam
    await service.process(DELIVERY)
    row.data["action_result"]["status"] = "built"
    with pytest.raises(AccessDenied):
        await service.authorize_feedback(DELIVERY)


async def test_explicit_consent_revocation_does_not_require_a_live_provider(native_seam):
    service, state, _, _, _ = native_seam
    original = state["settings"].data[STATE_KEY]["head"]

    async def unavailable(*args):
        raise RuntimeError("Provider intentionally unavailable")

    service.scm.repositories = unavailable
    request = {
        **original["request"],
        "request_id": "7" * 32,
        "expected_previous_digest": original["digest"],
        "active": False,
    }
    context = CURRENT_PRINCIPAL.set(ACTOR)
    try:
        revoked = await service.configure(REPOSITORY, SCMProductIntakeConsentInputV1.model_validate(request))
        assert revoked["request"]["active"] is False
    finally:
        CURRENT_PRINCIPAL.reset(context)
    with pytest.raises(AccessDenied):
        await service.process(DELIVERY)
    assert state["tables"]["forge_projects"] == state["tables"]["forge_product_definitions"] == {}


async def test_intake_consent_does_not_follow_a_changed_native_repository_url(native_seam):
    service, state, _, _, _ = native_seam
    await service.process(DELIVERY)
    state["repo"]["url"] = "https://github.com/other/private.git"
    with pytest.raises(AccessDenied):
        await service.authorize_feedback(DELIVERY, remote=False)
    assert len(state["tables"]["forge_projects"]) == 1


async def test_unavailable_application_grant_never_creates_an_intake(native_seam):
    service, state, _, _, _ = native_seam

    async def revoked(provider):
        assert state["depth"] == 0
        return []

    service.scm.repositories = revoked
    with pytest.raises(AccessDenied):
        await service.process(DELIVERY)
    assert state["tables"]["forge_projects"] == state["tables"]["forge_product_definitions"] == {}


async def test_other_native_administrator_claim_cannot_reuse_the_original_consent_identity(native_seam):
    service, state, _, _, _ = native_seam
    original = state["settings"].data[STATE_KEY]["head"]
    state["admin"] = False
    context = CURRENT_PRINCIPAL.set(ACTOR)
    try:
        with pytest.raises(AccessDenied):
            await service.configure(REPOSITORY, SCMProductIntakeConsentInputV1.model_validate(original["request"]))
        assert len(state["settings"].data[STATE_KEY]["requests"]) == 1 and state["calls"] == []
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_committed_intake_is_retained_when_permission_to_return_is_lost(native_seam):
    service, state, _, _, _ = native_seam

    async def late_loss(value):
        if state["tables"]["forge_projects"]:
            state["user"].enabled = False

    service.store.admit_credentials = late_loss
    with pytest.raises(AccessDenied):
        await service.process(DELIVERY)
    assert len(state["tables"]["forge_projects"]) == len(state["tables"]["forge_product_definitions"]) == 1

    async def allow(value):
        pass

    service.store.admit_credentials = allow
    state["tables"]["users"][ACTOR.id].enabled = True
    assert (await service.process(DELIVERY))["action_result"]["verified_receipt_id"] == DELIVERY
    assert len(state["tables"]["forge_product_definitions"]) == 1


async def test_lost_consent_during_bot_feedback_withholds_the_completed_intake_response(tmp_path):
    state = {"revoked": False}
    output = {"id": "a" * 32, "status": "processed", "command": {"action": "intake"}}

    async def process(receipt):
        return output

    async def result(receipt):
        if state["revoked"]:
            raise AccessDenied("Permission to return the intake changed")
        return output

    async def receipt(identifier):
        return output

    async def feedback(identifier):
        state["revoked"] = True

    collaboration = Collaboration(
        SimpleNamespace(configuration=SimpleNamespace(directory=tmp_path, settings_generation=object())),
        scm=SimpleNamespace(),
        product_intake_authority=SimpleNamespace(process=process, result=result),
    )
    collaboration.receipt = receipt
    collaboration.feedback = feedback
    with pytest.raises(AccessDenied):
        await collaboration.process("a" * 32)


async def test_active_consent_reserves_the_last_history_slot_for_local_revocation(native_seam):
    service, state, _, _, _ = native_seam
    index = state["settings"].data[STATE_KEY]
    head = index["head"]
    index["requests"] = {UUID(int=number).hex: copy.deepcopy(head) for number in range(31)}
    context = CURRENT_PRINCIPAL.set(ACTOR)
    try:
        body = {**head["request"], "request_id": uuid4().hex, "expected_previous_digest": head["digest"]}
        with pytest.raises(AccessDenied):
            await service.configure(REPOSITORY, SCMProductIntakeConsentInputV1.model_validate(body))
        revoked = await service.configure(
            REPOSITORY, SCMProductIntakeConsentInputV1.model_validate({**body, "active": False})
        )
        assert revoked["request"]["active"] is False
        assert len(state["settings"].data[STATE_KEY]["requests"]) == 32
    finally:
        CURRENT_PRINCIPAL.reset(context)
