# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host scheduling/admission controls with explicit fake native persistence boundaries."""

import copy
import importlib
import importlib.util
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_scm_product_change_contract import command

from firefly_forge.journal import digest
from firefly_forge.project_access import AccessDenied


def implementation():
    assert importlib.util.find_spec("firefly_forge.scm_product_change_service") is not None, (
        "Product authority service missing"
    )
    return importlib.import_module("firefly_forge.scm_product_change_service")


@pytest.fixture
async def product_service():
    module = implementation()
    request = command()
    state = {
        "locked": False,
        "revoked": False,
        "drift_during_preflight": False,
        "late_refusal": False,
        "writes": 0,
        "marks": 0,
        "external": 0,
        "operation": None,
    }
    row = SimpleNamespace(
        status="received",
        data=dict(
            id="a" * 32, repository_id="b" * 32, status="received", command=request, action_status="not_dispatched"
        ),
    )
    context = {
        "project": {"id": request["project_id"], "definition": {"components": [{"id": "api"}]}},
        "repository": "b" * 32,
        "binding": {"digest": "c" * 64, "request": {"request_id": "d" * 32}},
        "grant": {
            "digest": request["expected_grant_digest"],
            "request": {"request_id": request["expected_grant_request_id"]},
        },
        "user_id": "e" * 32,
        "projects": [request["project_id"]],
        "reference": {},
    }

    async def admit(value):
        assert not state["locked"]
        if state["late_refusal"] and state["writes"]:
            raise AccessDenied("Permission to return revoked after commit")

    class Harness(module.SCMProductChangeAuthority):
        async def _delivery(self, identifier):
            return copy.deepcopy(row.data)

        async def _context(self, delivery):
            if state["revoked"]:
                raise AccessDenied("Current native edit or capability revoked")
            return copy.deepcopy(context)

        async def _preflight(self, cut):
            assert not state["locked"]
            state["external"] += 1
            if state["drift_during_preflight"]:
                state["revoked"] = True

        async def _operation(self, project, repository, request_id):
            return copy.deepcopy(state["operation"])

        async def _product(self, repository, revision=None):
            return {"revision": 1, "digest": request["change"]["base_digest"]}

        async def _save_operation(self, delivery, cut, command, product):
            assert state["locked"]
            state["operation"] = {
                "request_digest": digest(command.model_dump(mode="json")),
                "native_user_id": context["user_id"],
                "grant_digest": context["grant"]["digest"],
                "product_revision": product["revision"],
                "product_digest": product["digest"],
            }
            if state.get("revoke_after_save"):
                state["revoked"] = True
            return copy.deepcopy(state["operation"])

        async def _same_operation(self, operation, cut, command):
            if (
                operation["request_digest"] != digest(command.model_dump(mode="json"))
                or operation["native_user_id"] != cut["user_id"]
            ):
                raise AccessDenied("Conflicting request or native actor")

        async def _mark_processed(self, current, result):
            assert state["locked"]
            state["marks"] += 1
            current.status = "processed"
            current.data.update(
                status="processed", action_status="completed", action_result=result, updated_at=str(state["marks"])
            )

        @asynccontextmanager
        async def _locked(self, delivery, cut):
            state["locked"] = True
            before = copy.deepcopy((row.data, state["operation"], state["writes"]))
            try:
                yield row
            except BaseException:
                row.data, state["operation"], state["writes"] = before
                row.status = row.data["status"]
                raise
            finally:
                state["locked"] = False

    class Lifecycle:
        async def prepare(self, *args, **kwargs):
            assert not state["locked"]
            return {"admitted": True, "prepared": {"definition": {"features": []}}}

        async def apply_prepared_locked(self, *args, **kwargs):
            assert state["locked"]
            state["writes"] += 1
            return {"revision": 2, "digest": "f" * 64}

    service = Harness(
        SimpleNamespace(admit_credentials=admit, reject_credentials=lambda value: None), None, None, None, None
    )
    return service, Lifecycle(), state, row, context


async def test_new_product_change_preflight_then_atomic_write_and_exact_replay(product_service):
    service, lifecycle, state, row, _ = product_service
    result = await service.process(row.data["id"], lifecycle)
    assert result["status"] == "processed" and result["action_result"]["product_revision"] == 2
    assert await service.process(row.data["id"], lifecycle) == result
    assert state["writes"] == 1


@pytest.mark.parametrize("cause", ["revoked", "drift_during_preflight"])
async def test_known_setup_refusal_never_writes_or_persists_unknown_intent(product_service, cause):
    service, lifecycle, state, row, _ = product_service
    state[cause] = True
    before = copy.deepcopy(row.data)
    with pytest.raises(AccessDenied):
        await service.process(row.data["id"], lifecycle)
    assert state["writes"] == 0 and state["operation"] is None and row.data == before


async def test_late_permission_to_return_refusal_keeps_exact_committed_identity(product_service):
    service, lifecycle, state, row, _ = product_service
    state["late_refusal"] = True
    with pytest.raises(AccessDenied, match="after commit"):
        await service.process(row.data["id"], lifecycle)
    assert state["writes"] == 1 and row.status == "processed" and state["operation"] is not None
    state["late_refusal"] = False
    assert (await service.process(row.data["id"], lifecycle))["action_result"] == state["operation"]
    assert state["writes"] == 1


async def test_collaboration_denies_known_unmapped_edit_before_any_action_intent(tmp_path):
    from firefly_forge.collaboration import Collaboration

    row = SimpleNamespace(status="received", data={"id": "a" * 32, "status": "received", "command": command()})

    @asynccontextmanager
    async def unit():
        yield

    async def find(*args, **kwargs):
        return row

    async def refuse(*args, **kwargs):
        raise AccessDenied("The actor has no separately approved product editing capability")

    collaboration = Collaboration(
        SimpleNamespace(_unit=unit, configuration=SimpleNamespace(directory=tmp_path, settings_generation=0)),
        scm=SimpleNamespace(),
        product_change_authority=SimpleNamespace(process=refuse),
    )
    collaboration.inbox = SimpleNamespace(find_by_id=find, save=lambda row: None)

    async def receipt(identifier):
        return copy.deepcopy(row.data)

    async def write(current, **changes):
        current.data.update(changes)
        current.status = current.data["status"]

    collaboration.receipt, collaboration._write_row = receipt, write
    result = await collaboration.process(row.data["id"])
    assert result["status"] == row.status == "denied"
    assert row.data["action_status"] == "not_dispatched"
    assert "action_result" not in row.data and "resume_intent" not in row.data


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "partial",
        "product_repository",
        "project_drift",
        "binding_inactive",
        "head_drift",
        "key_revoked",
        "native_edit_revoked",
    ],
)
async def test_whole_product_requires_exact_current_native_binding_and_scope(mutation):
    from test_scm_product_change_contract import grant

    from firefly_forge.scm_product_change_models import SCMProductChangeGrantInputV1

    module = implementation()
    project = {
        "id": "5" * 32,
        "definition": {"components": [{"id": "api"}, {"id": "web"}], "product_repository_id": "b" * 32},
    }
    body = {**grant(), "expected_project_binding_digest": digest(project)}
    binding = {
        "digest": body["expected_binding_digest"],
        "binding_key": "c" * 64,
        "project_binding_digest": digest(project),
        "request": {"active": True, "native_user_id": "e" * 32},
        "verified_reference": {"provider": "github"},
    }

    async def existing(*args):
        return copy.deepcopy(binding)

    async def head(*args):
        return {**binding, "digest": "0" * 64} if mutation == "head_drift" else copy.deepcopy(binding)

    async def project_cut(*args):
        return copy.deepcopy(project)

    async def repository(*args):
        return {"id": "b" * 32}

    async def permissions(*args):
        if mutation == "native_edit_revoked":
            raise AccessDenied("Native edit revoked")
        return [project["id"]]

    authority = SimpleNamespace(
        _existing=existing,
        _head=head,
        _key_current=lambda *args: mutation != "key_revoked",
        scm=SimpleNamespace(delegation_scope=lambda repository: {"provider": "github"}),
    )
    service = module.SCMProductChangeAuthority(SimpleNamespace(repository=repository), authority, None, None, None)
    service._project, service._permissions = project_cut, permissions
    if mutation == "partial":
        body["component_ids"] = ["api"]
    elif mutation == "product_repository":
        project["definition"]["product_repository_id"] = "f" * 32
    elif mutation == "project_drift":
        project["revision"] = 2
    elif mutation == "binding_inactive":
        binding["request"]["active"] = False
    request = SCMProductChangeGrantInputV1.model_validate(body)
    if mutation is None:
        assert await service._binding(project["id"], "b" * 32, request) == binding
    else:
        with pytest.raises(AccessDenied):
            await service._binding(project["id"], "b" * 32, request)


@pytest.mark.parametrize("reference", ["feature", "owned_integration"])
def test_functional_references_cannot_escape_the_explicit_whole_product_graph(reference):
    module = implementation()
    definition = {"features": [{"component_refs": ["api"]}], "integrations": []}
    project = {"definition": {"components": [{"id": "api"}]}}
    assert module.validate_product_scope(project, definition) is None
    if reference == "feature":
        definition["features"][0]["component_refs"] = ["foreign"]
    else:
        definition["integrations"] = [{"kind": "owned", "service_ref": "foreign"}]
    with pytest.raises(ValueError, match="component"):
        module.validate_product_scope(project, definition)


async def test_native_authority_is_rechecked_after_awaited_persistence_before_commit(product_service):
    service, lifecycle, state, row, _ = product_service
    state["revoke_after_save"] = True
    with pytest.raises(AccessDenied):
        await service.process(row.data["id"], lifecycle)
    assert state["writes"] == 0 and state["operation"] is None and row.status == "received"
