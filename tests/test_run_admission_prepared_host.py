# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host admission controls with explicit native repository/unit doubles."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.models import RunInput
from firefly_forge.projects import Projects
from firefly_forge.store import Store

REPOSITORY = "a" * 32
PROJECT = "b" * 32


@pytest.fixture
def admission(tmp_path, monkeypatch):
    state = {"depth": 0, "rows": {}, "events": [], "writes": [], "guard": False, "reads": []}
    repo = dict(id=REPOSITORY, name="Fictional stock", url=str(tmp_path), provider="git", skills=["security"])
    component = dict(
        project_id=PROJECT,
        project_name="Fictional stock",
        project_revision=1,
        project_digest="c" * 64,
        topology="monorepo",
        product_repository_id=REPOSITORY,
        project_definition={},
        id="api",
        backend="pyfly",
        frontend="react",
        verification=[{"argv": ["python", "-m", "pytest"], "timeout": 300}],
    )
    store = Store(tmp_path / "state" / "factory.db")

    async def execute(*args, **kwargs):
        return None

    @asynccontextmanager
    async def unit(*, autoflush=True):
        before = copy.deepcopy((state["rows"], state["events"], state["writes"]))
        state["depth"] += 1
        try:
            yield SimpleNamespace(execute=execute)
        except BaseException:
            state["rows"], state["events"], state["writes"] = before
            raise
        finally:
            state["depth"] -= 1

    async def repository(value, **kwargs):
        assert value == UUID(REPOSITORY)
        return SimpleNamespace(data=copy.deepcopy(repo))

    async def current(value):
        assert value == UUID(REPOSITORY)
        return None

    async def save(row):
        assert state["depth"]
        state["rows"][row.id.hex] = copy.deepcopy(row.data)
        state["writes"].append("run")

    async def find(value, **kwargs):
        data = state["rows"].get(value.hex)
        return SimpleNamespace(id=value, status=data["status"], data=copy.deepcopy(data)) if data else None

    async def event_save(row):
        assert state["depth"]
        state["events"].append(copy.deepcopy(row.data))
        state["writes"].append("event")

    async def events(value):
        return [SimpleNamespace(data=copy.deepcopy(data)) for data in state["events"]]

    async def admit(value):
        pass

    async def project_snapshot(service, request):
        return copy.deepcopy(component) if request.project_id else None

    read = ConfigurationStore._read
    write = ConfigurationStore._write

    def guarded_read(configuration, path):
        state["reads"].append(str(path))
        if state["guard"]:
            assert not state["depth"], "Configuration acquisition ran in a native unit"
        return read(configuration, path)

    def guarded_write(configuration, path, data):
        if state["guard"]:
            assert not state["depth"], "Configuration write ran in a native unit"
        return write(configuration, path, data)

    monkeypatch.setattr(ConfigurationStore, "_write", guarded_write)
    monkeypatch.setattr(ConfigurationStore, "_read", guarded_read)
    monkeypatch.setattr(Projects, "component_snapshot", project_snapshot)
    from firefly_forge.project_persistence import ForgeProjectRepository

    async def project_row(records, value, **kwargs):
        assert value == UUID(PROJECT)
        return SimpleNamespace(data={})

    monkeypatch.setattr(ForgeProjectRepository, "find_by_id", project_row)
    monkeypatch.setattr(store, "_unit", unit)
    store.vault_credential_admission = admit
    import firefly_forge.store as store_module

    monkeypatch.setattr(store_module, "current_unit_of_work", lambda: object() if state["depth"] else None)
    store.repository_records = SimpleNamespace(find_by_id=repository)
    store.product_records = SimpleNamespace(find_current=current)
    store.run_records = SimpleNamespace(save=save, find_by_id=find)
    store.event_records = SimpleNamespace(save=event_save, find_for_run=events)
    return store, state, repo, component


async def test_run_admission_acquires_configuration_and_credentials_outside_native_unit(admission):
    store, state, _, _ = admission
    state["guard"] = True
    run = await store.create_run(RunInput(repository_id=REPOSITORY, brief="Verify fictional stock"))
    assert run["status"] == "queued"
    assert state["writes"] == ["run", "event"]


@pytest.mark.parametrize("explicit", [False, True])
async def test_legacy_run_preserves_explicit_command_and_policy_presence(admission, explicit):
    store, state, repo, component = admission
    fields = dict(repository_id=REPOSITORY, project_id=PROJECT, component_id="api", brief="  Fictional stock  ")
    if explicit:
        fields.update(commands=[], skills=[])
    request = RunInput.model_validate(fields)
    run = await store.create_run(request)
    assert run["commands"] == ([] if explicit else component["verification"])
    assert run["skills"] == ([] if explicit else repo["skills"])
    assert run["brief"] == "Fictional stock"
    assert run["component_snapshot"] == component
    assert run["agent"] == "codex" and run["operation"] == "build" and run["publish"] is False
    assert run["product_definition"] is None and run["execution_actor"] is None
    assert "git_delivery_policy" not in run
    assert state["writes"] == ["run", "event"]
    assert len(run["events"]) == 1 and run["events"][0]["stage"] == "queued"
    assert run["events"][0]["message"] == "Run queued"
    assert "creation_request" not in run and "request_id" not in run


async def test_maintenance_prepares_supported_run_before_incident_transaction(admission):
    from firefly_forge.application_operations import ApplicationOperations, MaintenanceRequest

    store, state, _, _ = admission
    incident = SimpleNamespace(
        status="open",
        application_id="stock",
        data={"repository_id": REPOSITORY, "summary": "Fictional stalled stock", "maintenance_run_ids": []},
    )

    async def find(value, **kwargs):
        return incident

    async def save(row):
        assert state["depth"] and row is incident

    operations = ApplicationOperations(store, None, None, incidents=SimpleNamespace(find_by_id=find, save=save))
    store.configuration._write(operations.path, {"stock": {"name": "Stock", "backend": "pyfly", "frontend": "react"}})
    state["guard"] = True
    run = await operations.maintenance(
        "d" * 32, MaintenanceRequest(brief="Unblock fictional stock", commands=[{"argv": ["pytest"]}])
    )
    assert run["operation"] == "maintain" and run["publish"] is False
    assert "Unblock fictional stock" in run["brief"]
    assert incident.data["maintenance_run_ids"] == [run["id"]]
    assert state["writes"] == ["run", "event"]


@pytest.mark.parametrize("drift", [False, True])
async def test_legacy_inbox_prepares_admission_before_receipt_transaction(admission, monkeypatch, drift):
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
            issue={"kind": "issue", "number": 1},
            command={"action": "run", "brief": "Fictional stock change"},
        ),
    )
    settings = CollaborationSettings(enabled=True, trusted_actors=["owner"], allowed_commands=["run"])

    async def setting(value):
        return SimpleNamespace(data=settings.model_dump(exclude={"webhook_secret"}))

    async def find(value, **kwargs):
        assert value.hex == receipt_id
        return row

    async def save(value):
        assert state["depth"] and value is row

    async def receipt(value):
        assert value == receipt_id
        return copy.deepcopy(row.data)

    async def feedback(value):
        return None

    service = Collaboration(store)
    service.products = store.product_records
    service.settings_records = SimpleNamespace(find_for_repository=setting)
    service.inbox = SimpleNamespace(find_by_id=find, save=save)
    monkeypatch.setattr(service, "receipt", receipt)
    monkeypatch.setattr(service, "feedback", feedback)
    monkeypatch.setattr(service, "_secret", lambda value: "")
    if drift:
        fence = store.fence_prepared_runs

        async def changed_command(prepared):
            await fence(prepared)
            row.data["command"] = {"action": "status"}

        monkeypatch.setattr(store, "fence_prepared_runs", changed_command)
        with pytest.raises(ValueError, match="command changed"):
            await service.process(receipt_id)
        assert not state["rows"] and not state["events"]
        assert row.status == "received"
        return
    state["guard"] = True
    result = await service.process(receipt_id)
    assert result["status"] == "processed"
    assert state["rows"][result["run_id"]]["brief"] == "Fictional stock change"
    assert state["writes"] == ["run", "event", "event"]


async def test_preparation_is_detached_and_inserts_nothing(admission):
    store, state, _, _ = admission
    request = RunInput(repository_id=REPOSITORY, brief="Retained fictional input")
    prepared = await store.prepare_run(request)
    request.brief = "Later caller edit"
    view = prepared.unpack()
    view["run"]["brief"] = "Later view edit"
    assert prepared.unpack()["run"]["brief"] == "Retained fictional input"
    assert state["rows"] == {} and state["events"] == []
    assert type(prepared.packet) is bytes


async def test_ambient_preparation_refuses_before_acquisition(admission):
    store, state, _, _ = admission
    state["depth"] = 1
    try:
        with pytest.raises(ValueError, match="outside"):
            await store.prepare_run(RunInput(repository_id=REPOSITORY, brief="Fictional stock"))
        assert state["reads"] == [] and state["rows"] == {}
    finally:
        state["depth"] = 0


async def test_prepared_run_refuses_repository_drift_without_insert(admission):
    from firefly_forge.store import prepared_credential_rejection

    store, state, repo, _ = admission
    prepared = await store.prepare_run(RunInput(repository_id=REPOSITORY, brief="Fictional stock"))
    repo["default_branch"] = "changed"
    with prepared_credential_rejection(store), pytest.raises(ValueError, match="repository changed"):
        async with store._unit():
            await store.insert_prepared_run_locked(prepared)
    assert state["rows"] == {} and state["events"] == []


async def test_prepared_run_rejects_generation_drift_before_insert(admission):
    from firefly_forge.store import prepared_credential_rejection

    store, state, _, _ = admission
    prepared = await store.prepare_run(RunInput(repository_id=REPOSITORY, brief="Fictional stock"))
    store.configuration._write(store.configuration.directory / "changed.json", {"changed": True})
    with prepared_credential_rejection(store), pytest.raises(ValueError, match="settings changed"):
        async with store._unit():
            await store.insert_prepared_run_locked(prepared)
    assert state["rows"] == {} and state["events"] == []


async def test_run_and_queued_event_rollback_together(admission, monkeypatch):
    store, state, _, _ = admission
    event_save = store.event_records.save

    async def fail(row):
        await event_save(row)
        raise ValueError("Owned queued-event failure")

    monkeypatch.setattr(store.event_records, "save", fail)
    with pytest.raises(ValueError, match="queued-event failure"):
        await store.create_run(RunInput(repository_id=REPOSITORY, brief="Fictional stock"))
    assert state["rows"] == {} and state["events"] == [] and state["writes"] == []


async def test_late_credential_registry_replacement_rolls_back(admission, monkeypatch):
    store, state, _, _ = admission
    save = store.run_records.save

    async def replace_registry(row):
        await save(row)
        store.credential_registry = lambda: {"new": "Fictional stock"}

    monkeypatch.setattr(store.run_records, "save", replace_registry)
    with pytest.raises(ValueError, match="Credential sources changed"):
        await store.create_run(RunInput(repository_id=REPOSITORY, brief="Fictional stock"))
    assert state["rows"] == {} and state["events"] == []


async def test_postcommit_refusal_does_not_imply_no_admission(admission):
    store, state, _, _ = admission

    async def deny_after_commit(value):
        if state["rows"] and not state["depth"]:
            raise ValueError("Current credential readback refused")

    store.vault_credential_admission = deny_after_commit
    with pytest.raises(ValueError, match="readback refused"):
        await store.create_run(RunInput(repository_id=REPOSITORY, brief="Fictional stock"))
    assert len(state["rows"]) == 1 and state["writes"] == ["run", "event"]


async def test_actor_context_switch_during_preparation_is_refused(admission):
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    store, state, _, _ = admission
    original = CURRENT_PRINCIPAL.set(None)

    async def switch(value):
        CURRENT_PRINCIPAL.set(Principal(UUID("f" * 32), "https://fictional.example", "different"))

    store.vault_credential_admission = switch
    try:
        with pytest.raises(ValueError, match="actor changed"):
            await store.prepare_run(RunInput(repository_id=REPOSITORY, brief="Fictional stock"))
        assert state["rows"] == {}
    finally:
        CURRENT_PRINCIPAL.reset(original)


async def test_campaign_prepares_all_children_before_one_native_admission(admission, monkeypatch):
    from firefly_forge.journal import Journal
    from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns

    store, state, _, component = admission
    project = {
        "id": PROJECT,
        "revision": 1,
        "digest": "c" * 64,
        "definition": {
            "name": "Fictional stock",
            "description": "Fictional API",
            "topology": "monorepo",
            "owners": ["Owner"],
            "components": [{"id": "api", "name": "API", "repository_id": REPOSITORY, "responsibility": "Serve stock"}],
        },
    }
    headers, children, entries = [], [], []

    async def ownership():
        pass

    async def source(repo):
        return "1" * 40

    manager = ProjectCampaigns(
        store,
        SimpleNamespace(assert_ownership=ownership, connections=None, search=None),
        Journal(store),
        source_resolver=source,
    )

    async def get_project(value):
        assert value == PROJECT
        return copy.deepcopy(project)

    async def existing(project_id, request_id):
        return None

    async def save_header(row):
        assert state["depth"] and not state["rows"]
        headers.append(row)

    async def current(value, **kwargs):
        return SimpleNamespace(data=copy.deepcopy(project))

    async def save_child(row):
        assert state["depth"]
        children.append(row)

    async def append(row, key, kind, value):
        entries.append((key, kind, copy.deepcopy(value)))

    async def release(row):
        pass

    async def read(project_id, campaign_id):
        return {"id": campaign_id, "children": [child.run_id.hex for child in children]}

    manager.records = SimpleNamespace(request=existing, save=save_header)
    manager.project_records = SimpleNamespace(find_by_id=current)
    manager.children = SimpleNamespace(save=save_child)
    monkeypatch.setattr(manager.projects, "project", get_project)
    monkeypatch.setattr(manager, "_append", append)
    monkeypatch.setattr(manager, "_release", release)
    monkeypatch.setattr(manager, "campaign", read)
    state["guard"] = True
    result = await manager.launch(PROJECT, CampaignLaunch(request_id="e" * 32, brief="Deliver fictional stock"))
    assert len(headers) == len(children) == 1
    run = state["rows"][result["children"][0]]
    assert run["status"] == "campaign_blocked" and run["component_snapshot"] == component
    assert run["campaign_context"]["source_base"] == "1" * 40
    assert run["commands"] == component["verification"]
    frozen = children[0].data["admission"]
    assert {**frozen, "updated_at": run["updated_at"]} == run
    assert frozen["updated_at"] <= run["updated_at"]
    assert entries[0][0:2] == ("launch", "launch")


async def test_saved_reviewed_design_is_supported_without_file_reads_under_unit(admission, monkeypatch):
    import hashlib
    import json
    from pathlib import Path

    from test_collaboration import product

    from firefly_forge.product_design import DesignReview, ProductDesign, _digest, definition_digest

    store, state, _, _ = admission
    definition = product()
    bundle = {
        "repository_id": REPOSITORY,
        "definition_digest": definition_digest(definition),
        "brand_digest": None,
        "method": "wireframe",
        "screens": [{"id": "stock"}],
    }
    bundle["digest"] = _digest(bundle)
    definition.design_review = DesignReview(
        digest=bundle["digest"],
        reviewed=True,
        screen_ids=["stock"],
        viewports=["desktop", "mobile"],
        themes=["light", "dark"],
    )
    design = ProductDesign(store)
    directory = design.directory / REPOSITORY
    directory.mkdir(parents=True)
    path = directory / (bundle["digest"] + ".json")
    path.write_text(json.dumps(bundle))
    read = Path.read_bytes

    def guarded(path):
        if path == directory / (bundle["digest"] + ".json"):
            assert not state["depth"], "Reviewed design read ran in a native unit"
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    snapshot = dict(repository_id=REPOSITORY, revision=1, definition=definition.model_dump(), created_at="original")
    snapshot["digest"] = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    state["guard"] = True
    run = await store.create_run(
        RunInput(repository_id=REPOSITORY, brief="Use reviewed stock design", product_definition=snapshot)
    )
    for key, value in snapshot["definition"].items():
        assert run["product_definition"]["definition"][key] == value, key
    assert run["product_definition"] == snapshot
    assert run["design_snapshot"] == bundle and run["branding_snapshot"] is None
    assert state["writes"] == ["run", "event"]


@pytest.mark.parametrize("agent,accepted", [(None, True), ("api", True), ("codex", False)])
async def test_named_profile_keeps_explicit_agent_selection_meaning(admission, agent, accepted):
    store, state, _, _ = admission

    async def resolve(stage, profile):
        assert not state["depth"]
        return (
            {"provider": "agentic", "config_revision": "d" * 32, "execution_config": {}}
            if stage == "implementation"
            else None
        )

    store.task_router = SimpleNamespace(resolve=resolve)
    fields = dict(repository_id=REPOSITORY, brief="Use fictional named profile")
    if agent is not None:
        fields["agent"] = agent
    if not accepted:
        with pytest.raises(ValueError, match="conflicts"):
            await store.create_run(RunInput.model_validate(fields))
        assert state["rows"] == {}
    else:
        run = await store.create_run(RunInput.model_validate(fields))
        assert run["agent"] == "agentic" and run["config_revision"] == "d" * 32
        assert state["writes"] == ["run", "event"]


async def test_lost_administrator_authority_cannot_admit_a_prepared_host_runner(admission, monkeypatch):
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
    from firefly_forge.store import PreparedRunAdmission

    store, _, _, _ = admission
    prepared = await store.prepare_run(
        RunInput(repository_id=REPOSITORY, project_id=PROJECT, component_id="api", brief="Fictional stock")
    )
    actor = Principal(UUID("f" * 32), "https://fictional.example", "owner", api_roles=("forge-admin",))
    packet = prepared.unpack()
    packet["run"]["execution_actor"] = ProjectAccess.actor_snapshot(actor)
    prepared = PreparedRunAdmission.capture(packet, prepared.generation)

    async def not_administrator(access, principal):
        return False

    async def has_project_execution(access, principal, project, permission):
        assert permission == "execute"

    monkeypatch.setattr(ProjectAccess, "current_administrator", not_administrator)
    monkeypatch.setattr(ProjectAccess, "require_project", has_project_execution)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises(ValueError, match="isolated"):
            await store._prepared_run_actor(prepared)
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_prepared_request_keeps_omitted_agent_before_profile_resolution(admission):
    store, _, _, _ = admission

    async def resolve(stage, profile):
        return (
            {"provider": "agentic", "config_revision": "d" * 32, "execution_config": {}}
            if stage == "implementation"
            else None
        )

    store.task_router = SimpleNamespace(resolve=resolve)
    fields = {"repository_id": REPOSITORY, "brief": "Fictional profile selection"}
    prepared = await store.prepare_run(RunInput.model_validate(fields))
    assert prepared.unpack()["request"] == fields
    assert prepared.unpack()["run"]["agent"] == "agentic"
