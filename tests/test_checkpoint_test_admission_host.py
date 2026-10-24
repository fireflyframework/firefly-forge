# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Store preparation and insertion retain source provenance without changing the repository."""

import copy
from types import SimpleNamespace

import pytest
from test_checkpoint_test_source_host import PROJECT, REFERENCE, REPO, REVISION, TREE
from test_run_admission_prepared_host import admission as admission

from firefly_forge.models import RunInput


def source_request():
    return RunInput.model_validate(
        dict(
            repository_id=REPO,
            project_id=PROJECT,
            component_id="api",
            brief="Verify exact checkpoint",
            operation="test",
            source_checkpoint=REFERENCE,
        )
    )


async def prepared_source(store):
    prepared = await store.prepare_run(source_request())
    async with store._unit():
        await store.fence_prepared_runs([prepared])
        return await store.insert_prepared_run_locked(prepared)


async def test_checkpoint_cannot_silently_fall_back_to_default_branch_without_native_service(admission):
    store, state, repo, _ = admission
    with pytest.raises(ValueError, match="Checkpoint source admission is unavailable"):
        await prepared_source(store)
    assert state["writes"] == []


async def test_store_prepares_source_outside_unit_revalidates_before_insert_and_preserves_repo(admission):
    store, state, repo, _ = admission
    calls = []
    frozen = dict(version=1, revision=REVISION, source_tree=TREE, branch="forge/public-checkpoint")

    async def prepare(request):
        assert not state["depth"]
        calls.append("prepare")
        return copy.deepcopy(frozen)

    async def validate(request, snapshot):
        assert state["depth"]
        assert snapshot == frozen and request.source_checkpoint.model_dump() == REFERENCE
        calls.append("locked")

    store.checkpoint_sources = SimpleNamespace(store=store, prepare=prepare, revalidate_locked=validate)
    original = copy.deepcopy(repo)
    value = await prepared_source(store)
    assert calls == ["prepare", "locked"]
    assert value["source_checkpoint_snapshot"] == frozen
    assert value["repository_id"] == REPO and value["project_id"] == PROJECT and value["component_id"] == "api"
    assert repo == original and value["operation"] == "test" and value["publish"] is False


async def test_changed_checkpoint_rolls_back_the_whole_native_run_admission(admission):
    store, state, _, _ = admission

    async def prepare(request):
        return {"revision": REVISION}

    async def validate(request, snapshot):
        raise ValueError("Checkpoint provenance changed after preparation")

    store.checkpoint_sources = SimpleNamespace(store=store, prepare=prepare, revalidate_locked=validate)
    with pytest.raises(ValueError, match="provenance changed"):
        await prepared_source(store)
    assert state["writes"] == [] and state["rows"] == {}


from test_run_creation_requests_host import creation as creation  # noqa: E402


async def test_native_creation_receipt_retains_own_identity_and_resolved_checkpoint(creation):
    from firefly_forge.git_deliveries import delivery_digest
    from firefly_forge.run_creation_models import RunCreationInput

    manager, store, state, retained, _ = creation
    snapshot = dict(
        version=1,
        reference=REFERENCE,
        repository_id=REPO,
        project_id=PROJECT,
        component_id="api",
        revision=REVISION,
        source_tree=TREE,
        branch="forge/public-checkpoint",
        remote_receipt_digest="4" * 64,
        journal_head="5" * 64,
        parent_scope_digest="6" * 64,
    )
    snapshot["digest"] = delivery_digest(snapshot)

    async def prepare(request):
        return copy.deepcopy(snapshot)

    async def validate(request, value):
        assert value == snapshot

    store.checkpoint_sources = SimpleNamespace(store=store, prepare=prepare, revalidate_locked=validate)
    body = RunCreationInput(version=1, input=source_request())
    receipt, created = await manager.admit("7" * 32, body)
    assert created and receipt["source_checkpoint"] == snapshot
    assert receipt["request_id"] != REFERENCE["request_id"] and receipt["run_id"] != REFERENCE["run_id"]
    assert receipt["input"]["source_checkpoint"] == REFERENCE
    assert await manager.get("7" * 32) == receipt
    assert (await manager.admit("7" * 32, body)) == (receipt, False)
    assert len(state["rows"]) == len(retained) == 1
    state["rows"][receipt["run_id"]]["source_checkpoint_snapshot"]["revision"] = "0" * 40
    with pytest.raises(ValueError, match="source"):
        await manager.get("7" * 32)


async def test_foreign_store_source_resolver_cannot_authorize_a_native_admission(admission):
    store, state, _, _ = admission

    async def prepare(request):
        raise AssertionError("A foreign store must be refused before source lookup")

    store.checkpoint_sources = SimpleNamespace(store=object(), prepare=prepare)
    with pytest.raises(ValueError, match="Checkpoint source admission is unavailable"):
        await prepared_source(store)
    assert state["rows"] == {} and state["writes"] == []


async def test_legacy_creation_cannot_bypass_the_checkpoint_creation_receipt(admission):
    store, state, _, _ = admission
    calls = []

    async def prepare(request):
        calls.append("source lookup")
        return {"revision": REVISION}

    async def validate(*args):
        pass

    store.checkpoint_sources = SimpleNamespace(store=store, prepare=prepare, revalidate_locked=validate)
    with pytest.raises(ValueError, match="run-creation-requests"):
        await store.create_run(source_request())
    assert calls == [] and state["rows"] == {} and state["writes"] == []


def test_existing_sdk_and_cli_send_typed_checkpoint_via_explicit_creation_route(tmp_path, monkeypatch):
    import json
    from contextlib import contextmanager

    import httpx
    from click.testing import CliRunner

    import firefly_forge.cli as cli
    from firefly_forge.client import ForgeClient
    from firefly_forge.run_creation_models import RunCreationInput

    body = RunCreationInput(version=1, input=source_request())
    envelope = body.model_dump(mode="json", exclude_unset=True)
    calls = []

    def transport(request):
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"request_id": "7" * 32})

    @contextmanager
    def client():
        connection = ForgeClient("http://fixture.invalid", "public-fixture-token")
        connection.http.close()
        connection.http = httpx.Client(
            base_url="http://fixture.invalid/api/v1/", transport=httpx.MockTransport(transport)
        )
        with connection:
            yield connection

    with client() as connection:
        assert connection.request_run_creation("7" * 32, body) == {"request_id": "7" * 32}
    monkeypatch.setattr(cli, "client", client)
    document = tmp_path / "checkpoint-test.json"
    document.write_text(json.dumps(envelope))
    result = CliRunner().invoke(cli.main, ["request-run-creation", "7" * 32, str(document)])
    assert result.exit_code == 0, result.output
    assert calls == [("POST", "/api/v1/run-creation-requests/" + "7" * 32, envelope)] * 2
