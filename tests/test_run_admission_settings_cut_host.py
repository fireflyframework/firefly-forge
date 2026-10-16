# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Mutable settings cannot be refreshed into an older admission snapshot."""

import copy
from types import SimpleNamespace

import pytest
from test_run_admission_prepared_host import PROJECT, REPOSITORY
from test_run_admission_prepared_host import admission as admission

from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.models import RunInput
from firefly_forge.projects import Projects


@pytest.mark.parametrize("kind", ["provider", "provider_restored", "policy"])
async def test_current_settings_changed_during_component_read_cannot_enter_run(admission, monkeypatch, kind):
    store, state, _, _ = admission
    store.configuration.update("codex", ProviderConfiguration(values={"model": "initial-model"}))
    snapshot = Projects.component_snapshot
    mutated = False

    async def changed(service, request):
        nonlocal mutated
        value = await snapshot(service, request)
        if mutated:
            return value
        mutated = True
        if kind == "policy":
            current = store.policies.current()
            document = {"version": current["version"], "skills": copy.deepcopy(current["skills"])}
            document["skills"][0]["instructions"] += "\nChanged during admission."
            store.policies.private._write(
                store.policies.path, {"revision": "changed", "source": {"kind": "builtin"}, "document": document}
            )
        else:
            store.configuration.update("codex", ProviderConfiguration(values={"model": "changed-model"}))
            if kind == "provider_restored":
                store.configuration.update("codex", ProviderConfiguration(values={"model": "initial-model"}))
        return value

    monkeypatch.setattr(Projects, "component_snapshot", changed)
    with pytest.raises(ValueError, match="settings changed"):
        await store.create_run(
            RunInput(repository_id=REPOSITORY, project_id=PROJECT, component_id="api", brief="Fictional stock")
        )
    assert state["rows"] == {} and state["events"] == []


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("phase", ["prepare", "last_write"])
async def test_global_actor_revocation_during_prepare_never_enqueues(admission, monkeypatch, restored, phase):
    from firefly_forge.collaboration import Collaboration, CollaborationSettings

    store, state, repo, _ = admission
    repo.update(connection_provider="github", remote_repository_id="1")
    receipt_id = "e" * 32
    row = SimpleNamespace(
        provider="github",
        status="received",
        data=dict(
            id=receipt_id,
            provider="github",
            status="received",
            repository_id=REPOSITORY,
            actor="owner",
            remote_id="1",
            global_ingress=True,
            issue={"kind": "issue", "number": 1},
            command={"action": "run", "brief": "Fictional stock change"},
        ),
    )
    settings = CollaborationSettings(enabled=True, trusted_actors=["owner"], allowed_commands=["run"])
    channel = {"github": {"enabled": True, "trusted_actors": ["owner"]}}
    path = store.configuration.directory / "channel-bootstrap.json"
    store.configuration._write(path, channel)

    async def setting(value):
        return SimpleNamespace(data=settings.model_dump(exclude={"webhook_secret"}))

    async def find(value, **kwargs):
        assert value.hex == receipt_id
        return row

    async def save(value):
        assert value is row
        if phase == "last_write" and row.status == "processed":
            store.configuration._write(path, {"github": {"enabled": False, "trusted_actors": []}})
            if restored:
                store.configuration._write(path, channel)

    async def receipt(value):
        return copy.deepcopy(row.data)

    async def grants(provider):
        return [{"id": "1", "url": repo["url"]}]

    async def feedback(value):
        pass

    prepare = store.prepare_run

    async def revoked(request, **kwargs):
        if phase == "prepare":
            store.configuration._write(path, {"github": {"enabled": False, "trusted_actors": []}})
            if restored:
                store.configuration._write(path, channel)
        return await prepare(request, **kwargs)

    service = Collaboration(store)
    service.products = store.product_records
    service.settings_records = SimpleNamespace(find_for_repository=setting)
    service.inbox = SimpleNamespace(find_by_id=find, save=save)
    service.scm = SimpleNamespace(repositories=grants)
    monkeypatch.setattr(service, "receipt", receipt)
    monkeypatch.setattr(service, "feedback", feedback)
    monkeypatch.setattr(service, "_secret", lambda value: "")
    monkeypatch.setattr(store, "prepare_run", revoked)
    with pytest.raises(ValueError, match="settings changed|channel changed"):
        await service.process(receipt_id)
    assert state["rows"] == {} and state["events"] == []
    if phase == "prepare":
        assert row.status == "received"


async def test_application_changed_before_preparation_cannot_create_maintenance_run(admission, monkeypatch):
    from firefly_forge.application_operations import ApplicationOperations, MaintenanceRequest

    store, state, _, _ = admission
    incident = SimpleNamespace(
        status="open",
        application_id="stock",
        data={"repository_id": REPOSITORY, "summary": "Fictional incident", "maintenance_run_ids": []},
    )

    async def find(value, **kwargs):
        return incident

    async def save(value):
        pass

    operations = ApplicationOperations(store, None, None, incidents=SimpleNamespace(find_by_id=find, save=save))
    settings = {"stock": {"name": "Stock", "backend": "pyfly", "frontend": "react"}}
    store.configuration._write(operations.path, settings)
    prepare = store.prepare_run

    async def changed(request, **kwargs):
        store.configuration._write(operations.path, {"stock": {**settings["stock"], "name": "Changed application"}})
        return await prepare(request, **kwargs)

    monkeypatch.setattr(store, "prepare_run", changed)
    with pytest.raises(ValueError, match="settings changed|application changed"):
        await operations.maintenance(
            "d" * 32, MaintenanceRequest(brief="Restore stock", commands=[{"argv": ["pytest"]}])
        )
    assert state["rows"] == {} and state["events"] == []
    assert incident.data["maintenance_run_ids"] == []


@pytest.mark.parametrize("directory", ["revisions", "model-profile-revisions"])
def test_owned_snapshot_writes_preserve_mutable_cut_but_change_full_write_cut(admission, directory):
    store, _, _, _ = admission
    private = store.configuration
    target = private.directory / directory
    target.mkdir(exist_ok=True)
    settings, writes = private.settings_generation, private.write_generation
    private._write(target / ("f" * 32 + ".json"), {"snapshot": "fictional"})
    assert private.settings_generation is settings
    assert private.write_generation is not writes


@pytest.mark.parametrize("kind", ["ordinary", "similar_name", "outside", "symlink"])
def test_mutable_cut_exclusion_is_exact_and_confined(admission, tmp_path, kind):
    store, _, _, _ = admission
    private = store.configuration
    if kind == "ordinary":
        target = private.directory
    elif kind == "similar_name":
        target = private.directory / "other-revisions"
        target.mkdir()
    else:
        target = tmp_path / "external" / "revisions"
        target.mkdir(parents=True)
        if kind == "symlink":
            link = private.directory / "model-profile-revisions"
            link.symlink_to(target, target_is_directory=True)
            target = link
    settings = private.settings_generation
    private._write(target / "fictional.json", {"enabled": False})
    assert private.settings_generation is not settings


async def test_mutable_write_during_insert_rolls_back_run_and_event(admission, monkeypatch):
    store, state, _, _ = admission
    save = store.event_records.save

    async def changed(row):
        await save(row)
        store.configuration.update("codex", ProviderConfiguration(values={"model": "late-model"}))

    monkeypatch.setattr(store.event_records, "save", changed)
    with pytest.raises(ValueError, match="sources changed|settings changed"):
        await store.create_run(RunInput(repository_id=REPOSITORY, brief="Fictional stock"))
    assert state["rows"] == {} and state["events"] == []


async def test_mutable_write_before_exposure_refuses_without_claiming_rollback(admission, monkeypatch):
    store, state, _, _ = admission
    admit = store.admit_credentials

    async def changed(value):
        await admit(value)
        if not state["depth"] and isinstance(value, dict) and value.get("status") == "queued":
            store.configuration.update("codex", ProviderConfiguration(values={"model": "after-commit-model"}))

    monkeypatch.setattr(store, "admit_credentials", changed)
    with pytest.raises(ValueError, match="settings changed"):
        await store.create_run(RunInput(repository_id=REPOSITORY, brief="Fictional stock"))
    assert len(state["rows"]) == len(state["events"]) == 1
