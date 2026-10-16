# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Conditional save behavior through real service methods with explicit host persistence doubles."""

import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from test_collaboration import product

from firefly_forge import collaboration as module
from firefly_forge.access_routes import AccessRoutes
from firefly_forge.project_access import AccessDenied

REPO = "a" * 32


def condition(revision=0, digest=None):
    assert hasattr(module, "ConditionalProductSave"), "Conditional product-save contract is missing"
    return module.ConditionalProductSave(
        expected_revision=revision, expected_digest=digest, definition=product().model_dump(mode="json")
    )


@pytest.fixture
async def harness(monkeypatch, tmp_path):
    rows, state = [], {"locked": False, "fail": False}
    lock = asyncio.Lock()

    @asynccontextmanager
    async def unit():
        async with lock:
            before = rows.copy()
            state["locked"] = True
            try:
                yield
            except BaseException:
                rows[:] = before
                raise
            finally:
                state["locked"] = False

    async def admit(value):
        assert not state["locked"]

    async def repo_lock(repository):
        assert state["locked"] and repository == REPO

    async def current(repository):
        assert state["locked"] and repository.hex == REPO
        return rows[-1] if rows else None

    async def save(row):
        assert state["locked"]
        rows.append(row)
        if state["fail"]:
            raise ValueError("Owned fixture insert failed")

    async def review(self, repository, definition):
        assert not state["locked"]

    monkeypatch.setattr(module.ProductDesign, "validate_review", review)
    service = object.__new__(module.Collaboration)
    service.store = SimpleNamespace(
        admit_credentials=admit, _unit=unit, configuration=SimpleNamespace(directory=tmp_path)
    )
    service._repo_lock = repo_lock
    service.products = SimpleNamespace(find_current=current, save=save)
    return service, rows, state


async def test_conditional_same_cut_cannot_create_a_second_revision(harness):
    service, rows, _ = harness
    request = condition()
    first = await service.save_product(REPO, request.definition, condition=request)
    with pytest.raises(ValueError, match="changed"):
        await service.save_product(REPO, request.definition, condition=request)
    assert len(rows) == 1 and first["revision"] == 1
    assert set(first) == {"repository_id", "revision", "definition", "created_at", "origin", "digest"}


async def test_conditional_conflicting_writes_share_the_existing_repository_fence(harness):
    service, rows, _ = harness
    first = await service.save_product(REPO, product())
    a = condition(1, first["digest"])
    b = a.model_copy(deep=True)
    b.definition.customer = "Different writer"
    results = await asyncio.gather(
        service.save_product(REPO, a.definition, condition=a),
        service.save_product(REPO, b.definition, condition=b),
        return_exceptions=True,
    )
    assert len([result for result in results if isinstance(result, dict)]) == 1
    assert len([result for result in results if isinstance(result, ValueError)]) == 1
    assert len(rows) == 2 and rows[-1].revision == 2


async def test_conditional_failed_insert_rolls_back_and_old_endpoint_remains_unconditional(harness):
    service, rows, state = harness
    state["fail"] = True
    with pytest.raises(ValueError, match="insert failed"):
        await service.save_product(REPO, product(), condition=condition())
    assert rows == []
    state["fail"] = False
    old = await service.save_product(REPO, product())
    newer = await service.save_product(REPO, product())
    assert old["revision"] == 1 and newer["revision"] == 2 and len(rows) == 2
    assert old["definition"] == newer["definition"] == product().model_dump()


@pytest.mark.parametrize(("revision", "digest"), [(True, None), (0, "f" * 64), (1, None), (-1, None)])
def test_conditional_cut_must_be_explicit_and_consistent(revision, digest):
    assert hasattr(module, "ConditionalProductSave"), "Conditional product-save contract is missing"
    with pytest.raises(ValueError):
        condition(revision, digest)


async def test_conditional_nested_review_cannot_bypass_current_route_permission():
    permissions = []

    async def administrator(user):
        return False

    async def repository(user, repository, permission):
        assert repository == REPO
        permissions.append(permission)
        if permission == "review":
            raise AccessDenied("Current review authority required")

    access = SimpleNamespace(current_administrator=administrator, require_repository=repository)
    body = {"expected_revision": 0, "expected_digest": None, "definition": {"functional_reviewed": True}}
    import json

    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw}

    request = Request(
        {"type": "http", "method": "PUT", "path": f"/api/v1/repositories/{REPO}/product/conditional", "headers": []},
        receive,
    )
    with pytest.raises(AccessDenied, match="review authority"):
        await AccessRoutes(access, SimpleNamespace()).authorize(SimpleNamespace(), request)
    assert permissions == ["edit", "review"]


async def test_late_original_cannot_overwrite_new_edit_after_same_content_writer(harness):
    service, rows, _ = harness
    base = await service.save_product(REPO, product())
    original = condition(1, base["digest"])
    matching = await service.save_product(REPO, original.definition, condition=original)
    newer = condition(2, matching["digest"])
    newer.definition.problem = "Keep the later human edit"
    retained = await service.save_product(REPO, newer.definition, condition=newer)
    with pytest.raises(ValueError, match="changed"):
        await service.save_product(REPO, original.definition, condition=original)
    assert len(rows) == 3 and rows[-1].data == retained
    assert rows[-1].data["definition"]["problem"] == "Keep the later human edit"


async def test_conditional_controller_marks_only_proven_preinsert_rejection(harness, monkeypatch):
    from firefly_forge.collaboration_controllers import CollaborationController

    service, rows, state = harness
    controller = CollaborationController(service)
    request = condition()

    async def refused(self, repository, definition):
        raise ValueError("Product brand changed after design review")

    monkeypatch.setattr(module.ProductDesign, "validate_review", refused)
    response = await controller.save_product_conditionally(REPO, request)
    assert response.status_code == 409
    assert b'"code":"product_save_not_applied"' in response.body
    assert rows == []

    async def valid(self, repository, definition):
        return None

    monkeypatch.setattr(module.ProductDesign, "validate_review", valid)
    state["fail"] = True
    with pytest.raises(ValueError, match="insert failed") as caught:
        await controller.save_product_conditionally(REPO, request)
    assert not isinstance(caught.value, module.ConditionalProductSaveRejected)
    assert rows == []


async def test_conditional_request_is_frozen_before_credential_await(harness):
    service, rows, _ = harness
    request = condition()
    original = copy.deepcopy(request.definition.model_dump())

    async def mutate(value):
        request.definition.customer = "Changed by caller after admission began"

    service.store.admit_credentials = mutate
    saved = await service.save_product(REPO, request.definition, condition=request)
    assert saved["definition"] == original
    assert rows[-1].data["definition"] == original
