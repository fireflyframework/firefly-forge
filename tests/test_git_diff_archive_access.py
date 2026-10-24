# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Diff pages retain current run authority and exact immutable evidence."""

import importlib
import importlib.util

import pytest
from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_operational import operational as operational

from firefly_forge.git_diff_artifacts import retain_git_diff
from firefly_forge.git_diff_models import GitDiffBudget
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


async def prepared(case):
    assert importlib.util.find_spec("firefly_forge.git_diff_archive"), "Authorized diff page service is missing"
    module = importlib.import_module("firefly_forge.git_diff_archive")
    (case.root / "product.py").write_text("value = 2\n")
    root = case.service.engine.workspaces / ".diff-artifacts" / ("1" * 32)
    root.mkdir(parents=True, mode=0o700)

    async def controlled_scan(_chunk, _final):
        pass

    ref = await retain_git_diff(
        case.root, case.base, root, budget=GitDiffBudget(min_free_bytes=0), scan_chunk=controlled_scan, cached=False
    )
    case.state["run"]["diff_artifact"] = ref.model_dump(mode="json")
    return module.GitDiffArchive(case.service), ref


async def test_diff_pages_return_only_the_requested_verified_file_window(operational):
    archive, ref = await prepared(operational)
    page = await archive.index("1" * 32, ref.manifest_sha256, page=0)
    assert page["artifact_id"] == ref.artifact_id and page["files"][0]["path"] == "product.py"
    content = await archive.file("1" * 32, ref.manifest_sha256, file_index=0, offset=0, limit=32)
    assert content["byte_count"] <= 32 and content["file_index"] == 0
    assert content["manifest_sha256"] == ref.manifest_sha256


async def test_another_manifest_or_an_unsigned_reader_cannot_read_diff_content(operational):
    archive, ref = await prepared(operational)
    with pytest.raises(ValueError):
        await archive.index("1" * 32, "0" * 64, page=0)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        with pytest.raises(AccessDenied):
            await archive.index("1" * 32, ref.manifest_sha256, page=0)
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("change", ["permission", "reference"])
async def test_authority_and_reference_are_rechecked_after_artifact_io(operational, monkeypatch, change):
    case = operational
    archive, ref = await prepared(case)
    module = importlib.import_module("firefly_forge.git_diff_archive")
    reader = module.read_diff_index_page

    def changed(*args, **kwargs):
        result = reader(*args, **kwargs)
        if change == "permission":
            case.state["allowed"] = False
        else:
            case.state["run"]["diff_artifact"] = {**ref.model_dump(mode="json"), "manifest_sha256": "e" * 64}
        return result

    monkeypatch.setattr(module, "read_diff_index_page", changed)
    with pytest.raises((PermissionError, ValueError)):
        await archive.index("1" * 32, ref.manifest_sha256, page=0)


async def test_reference_change_during_final_authorization_is_not_returned(operational, monkeypatch):
    case = operational
    archive, ref = await prepared(case)
    authorize = case.service._authorize
    calls = 0

    async def changed(*args):
        nonlocal calls
        await authorize(*args)
        calls += 1
        if calls == 2:
            case.state["run"]["diff_artifact"] = {**ref.model_dump(mode="json"), "manifest_sha256": "e" * 64}

    monkeypatch.setattr(case.service, "_authorize", changed)
    with pytest.raises(ValueError):
        await archive.index("1" * 32, ref.manifest_sha256)


async def test_checkpoint_reference_is_selected_only_from_exact_authorized_receipt(operational, monkeypatch):
    archive, ref = await prepared(operational)
    request = "a" * 32

    async def view(run_id, *, actor):
        assert run_id == "1" * 32 and actor["user_id"] == "4" * 32
        return {"items": [{"request_id": request, "source_evidence": {"diff_artifact": ref.model_dump(mode="json")}}]}

    monkeypatch.setattr(operational.service, "view", view)
    operational.state["run"].pop("diff_artifact")
    result = await archive.file("1" * 32, ref.manifest_sha256, file_index=0, limit=12, request_id=request)
    assert result["byte_count"] == 12
    with pytest.raises(ValueError):
        await archive.index("1" * 32, ref.manifest_sha256, request_id="b" * 32)


async def test_run_scope_change_during_read_is_refused(operational, monkeypatch):
    archive, ref = await prepared(operational)
    module = importlib.import_module("firefly_forge.git_diff_archive")
    reader = module.read_diff_file_page

    def changed(*args, **kwargs):
        result = reader(*args, **kwargs)
        operational.state["run"]["project_id"] = "e" * 32
        return result

    monkeypatch.setattr(module, "read_diff_file_page", changed)
    with pytest.raises(ValueError):
        await archive.file("1" * 32, ref.manifest_sha256, file_index=0)


@pytest.mark.parametrize(
    "query", [{"file_index": True}, {"file_index": 0, "offset": -1}, {"file_index": 0, "limit": 1048577}]
)
async def test_invalid_file_windows_are_rejected_before_disk_read(operational, monkeypatch, query):
    archive, ref = await prepared(operational)
    module = importlib.import_module("firefly_forge.git_diff_archive")

    def forbidden(*args):
        pytest.fail("Invalid page parameters reached artifact I/O")

    monkeypatch.setattr(module, "read_diff_file_page", forbidden)
    with pytest.raises(ValueError):
        await archive.file("1" * 32, ref.manifest_sha256, **query)


async def test_native_controller_returns_private_pages_and_checkpoint_windows(operational, monkeypatch):
    from firefly_forge.git_diff_controllers import GitDiffController

    archive, ref = await prepared(operational)
    controller = GitDiffController(archive)
    response = await controller.index("1" * 32, ref.manifest_sha256, 0)
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    response = await controller.file("1" * 32, 0, ref.manifest_sha256, 0, 12)
    import json

    assert json.loads(response.body)["byte_count"] == 12
    assert str(operational.root) not in response.body.decode()

    async def view(run_id, *, actor):
        return {"items": [{"request_id": "a" * 32, "source_evidence": {"diff_artifact": ref.model_dump(mode="json")}}]}

    monkeypatch.setattr(operational.service, "view", view)
    response = await controller.checkpoint_index("1" * 32, "a" * 32, ref.manifest_sha256, 0)
    assert json.loads(response.body)["files"][0]["path"] == "product.py"
    response = await controller.checkpoint_file("1" * 32, "a" * 32, 0, ref.manifest_sha256, 0, 9)
    assert json.loads(response.body)["byte_count"] == 9
    assert response.headers["cache-control"] == "private, no-store"


async def test_native_reader_change_during_final_authorization_is_refused(operational, monkeypatch):
    archive, ref = await prepared(operational)
    authorize = operational.service._authorize
    principal = CURRENT_PRINCIPAL.get()
    calls = 0

    async def changed(*args):
        nonlocal calls
        await authorize(*args)
        calls += 1
        if calls == 2:
            CURRENT_PRINCIPAL.set(None)

    monkeypatch.setattr(operational.service, "_authorize", changed)
    try:
        with pytest.raises(AccessDenied):
            await archive.index("1" * 32, ref.manifest_sha256)
    finally:
        CURRENT_PRINCIPAL.set(principal)


async def test_request_reference_change_after_io_is_refused(operational, monkeypatch):
    archive, ref = await prepared(operational)
    request = "a" * 32
    values = {"ref": ref.model_dump(mode="json")}

    async def view(run_id, *, actor):
        return {"items": [{"request_id": request, "source_evidence": {"diff_artifact": values["ref"]}}]}

    module = importlib.import_module("firefly_forge.git_diff_archive")
    reader = module.read_diff_index_page

    def changed(*args):
        result = reader(*args)
        values["ref"] = {**ref.model_dump(mode="json"), "manifest_sha256": "e" * 64}
        return result

    monkeypatch.setattr(operational.service, "view", view)
    monkeypatch.setattr(module, "read_diff_index_page", changed)
    with pytest.raises(ValueError):
        await archive.index("1" * 32, ref.manifest_sha256, request_id=request)


async def test_native_di_collects_all_four_private_diff_routes_without_lifecycle(operational, tmp_path):
    import httpx
    from pyfly.web.adapters.starlette.controller import ControllerRegistrar
    from starlette.applications import Starlette

    from firefly_forge.api import create_runtime
    from firefly_forge.git_diff_archive import GitDiffArchive

    archive, ref = await prepared(operational)
    runtime = create_runtime(tmp_path / "api", "public-diff-route-fixture-token-32", role="api")
    assert GitDiffArchive in runtime.context.container._registrations
    runtime.context.container.register_instance(GitDiffArchive, archive)
    routes = [
        route
        for route in ControllerRegistrar().collect_routes(runtime.context)
        if "/diff/" in route.path or route.path.endswith("/diff")
    ]
    assert {route.path for route in routes} == {
        "/api/v1/runs/{run_id}/diff/index",
        "/api/v1/runs/{run_id}/diff/files/{file_index}",
        "/api/v1/runs/{run_id}/git-deliveries/requests/{request_id}/diff",
        "/api/v1/runs/{run_id}/git-deliveries/requests/{request_id}/diff/files/{file_index}",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=Starlette(routes=routes)), base_url="http://fixture.invalid"
    ) as client:
        response = await client.get(
            f"/api/v1/runs/{'1' * 32}/diff/files/0", params={"expected_manifest": ref.manifest_sha256, "limit": 17}
        )
        assert response.status_code == 200 and response.json()["byte_count"] == 17
        assert response.headers["cache-control"] == "private, no-store"
