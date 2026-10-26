# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable project references avoid duplicating the graph into every admission."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from test_run_admission_prepared_host import admission as admission

from firefly_forge.models import RunInput
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.store import PreparedRunAdmission

PROJECT, REPO = "a" * 32, "b" * 32


def record(count=2):
    value = ProjectDefinition.model_validate(
        {
            "name": "Fictional fleet",
            "description": "Independent owned services",
            "topology": "monorepo",
            "owners": ["Owner"],
            "roles": {"disabled": ["docs"]},
            "components": [
                {
                    "id": f"service-{i}",
                    "name": f"Service {i}",
                    "repository_id": REPO,
                    "path": f"services/{i}",
                    "responsibility": "Own this service's behavior",
                }
                for i in range(count)
            ],
        }
    ).model_dump()
    return {
        "id": PROJECT,
        "revision": 3,
        "digest": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(),
        "definition": value,
    }


def request(index=0):
    return RunInput(repository_id=REPO, project_id=PROJECT, component_id=f"service-{index}", brief="Maintain service")


def test_reference_preserves_selected_component_exact_roles_and_immutable_revision():
    from firefly_forge.project_snapshot import PreparedProjectRevision, snapshot_definition

    project = record()
    prepared = PreparedProjectRevision.capture(project)
    snapshot = prepared.component(request())
    assert "project_definition" not in snapshot
    assert snapshot["project_revision_ref"] == {
        "kind": "project_revision_reference",
        "version": 1,
        "project_id": PROJECT,
        "revision": 3,
        "digest": project["digest"],
    }
    assert snapshot["project_role_selection"] == project["definition"]["roles"]
    assert snapshot_definition(snapshot, project) == project["definition"]
    project["definition"]["components"][0]["responsibility"] = "Changed later"
    assert prepared.component(request())["responsibility"] == "Own this service's behavior"
    with pytest.raises(ValueError):
        snapshot_definition(snapshot, project)


@pytest.mark.parametrize(
    "change", ["digest", "revision", "project_id", "roles", "component", "missing", "version_type"]
)
def test_compact_reference_rejects_wrong_revision_or_changed_component(change):
    from firefly_forge.project_snapshot import PreparedProjectRevision, snapshot_definition

    project = record()
    snapshot = PreparedProjectRevision.capture(project).component(request())
    if change in {"digest", "revision", "project_id"}:
        snapshot["project_revision_ref"][change] = {"digest": "f" * 64, "revision": 4, "project_id": "c" * 32}[change]
    elif change == "version_type":
        snapshot["project_revision_ref"]["version"] = True
    elif change == "roles":
        snapshot["project_role_selection"] = None
    elif change == "component":
        snapshot["path"] = "other"
    else:
        project = {}
    with pytest.raises(ValueError):
        snapshot_definition(snapshot, project)


def test_legacy_embedded_graph_remains_readable_only_against_exact_historical_revision():
    from firefly_forge.project_snapshot import snapshot_definition

    project = record()
    legacy = {
        "project_id": PROJECT,
        "project_revision": 3,
        "project_digest": project["digest"],
        "project_definition": copy.deepcopy(project["definition"]),
    }
    assert snapshot_definition(legacy, project) == project["definition"]
    legacy["project_definition"]["owners"] = ["Someone else"]
    with pytest.raises(ValueError):
        snapshot_definition(legacy, project)


def test_admission_storage_grows_with_selected_children_not_children_times_graph():
    from firefly_forge.project_snapshot import PreparedProjectRevision

    measurements = []
    for count in (100, 400):
        project = record(count)
        shared = PreparedProjectRevision.capture(project)
        packets = [
            PreparedRunAdmission.capture({"run": {"component_snapshot": shared.component(request(i))}}, None)
            for i in range(count)
        ]
        size = sum(len(p.packet) for p in packets)
        measurements.append(size)
        assert all(b'"components"' not in p.packet for p in packets)
        assert all(p.unpack()["run"]["component_snapshot"]["project_revision"] == 3 for p in packets)
    assert measurements[1] < measurements[0] * 4.2


async def test_projects_snapshot_loads_current_graph_and_resolves_historical_reference(monkeypatch):
    from contextlib import asynccontextmanager

    from firefly_forge.projects import Projects

    project = record()

    @asynccontextmanager
    async def unit():
        yield None

    async def find(*args, **kwargs):
        return SimpleNamespace(data=copy.deepcopy(project))

    service = Projects(
        SimpleNamespace(_unit=unit), records=SimpleNamespace(find_by_id=find), revisions=SimpleNamespace(revision=find)
    )
    snapshot = await service.component_snapshot(request())
    assert "project_definition" not in snapshot
    assert await service.snapshot_definition(snapshot) == project["definition"]


async def test_store_prepared_packet_memory_and_storage_growth_are_linear_for_sparse_project(
    admission, record_property
):
    import tracemalloc

    from test_run_admission_prepared_host import PROJECT as admitted_project
    from test_run_admission_prepared_host import REPOSITORY as admitted_repository

    from firefly_forge.project_snapshot import PreparedProjectRevision

    store, _, _, _ = admission
    observations = []
    for count in (16, 64):
        project = record(count)
        project["id"] = admitted_project
        project["definition"]["product_repository_id"] = admitted_repository
        for component in project["definition"]["components"]:
            component["repository_id"] = admitted_repository
        project["digest"] = hashlib.sha256(json.dumps(project["definition"], sort_keys=True).encode()).hexdigest()
        if count == 16:
            await store.prepare_run(
                RunInput(
                    repository_id=admitted_repository,
                    project_id=admitted_project,
                    component_id="service-0",
                    brief="Warm configured admission metadata",
                ),
                prepared_project=PreparedProjectRevision.capture(project),
            )
        tracemalloc.start()
        shared = PreparedProjectRevision.capture(project)
        packets = []
        for index in range(count):
            packets.append(
                await store.prepare_run(
                    RunInput(
                        repository_id=admitted_repository,
                        project_id=admitted_project,
                        component_id=f"service-{index}",
                        brief="Maintain owned service",
                    ),
                    prepared_project=shared,
                )
            )
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        packet_bytes = sum(len(packet.packet) for packet in packets)
        legacy_bytes = 0
        for packet in packets:
            embedded = packet.unpack()
            component = embedded["run"]["component_snapshot"]
            component.pop("project_revision_ref")
            component.pop("project_role_selection")
            component["project_definition"] = project["definition"]
            legacy_bytes += len(json.dumps(embedded, separators=(",", ":"), ensure_ascii=False).encode())
        assert all(b'"project_definition"' not in packet.packet for packet in packets)
        assert all(packet.unpack()["run"]["role_selection_source"] == "project" for packet in packets)
        observations.append(
            {
                "children": count,
                "packet_bytes": packet_bytes,
                "legacy_embedded_equivalent_bytes": legacy_bytes,
                "retained_python_bytes": retained,
                "peak_python_bytes": peak,
            }
        )
    assert observations[1]["packet_bytes"] < observations[0]["packet_bytes"] * 4.3
    record_property("prepared_admission_measurements", json.dumps(observations, sort_keys=True))


async def test_channel_status_resolves_both_snapshot_shapes_before_current_graph_authorization():
    from firefly_forge.channel_products import ChannelProducts
    from firefly_forge.project_snapshot import PreparedProjectRevision, snapshot_definition

    project = record()
    compact = PreparedProjectRevision.capture(project).component(request())
    legacy = {
        **compact,
        "project_definition": copy.deepcopy(project["definition"]),
    }
    legacy.pop("project_revision_ref")
    legacy.pop("project_role_selection")
    runs = [
        {
            "id": str(i) * 32,
            "project_id": PROJECT,
            "component_id": "service-0",
            "repository_id": REPO,
            "component_snapshot": c,
        }
        for i, c in enumerate([compact, legacy], 1)
    ]
    runs.append(
        {
            "id": "9" * 32,
            "project_id": PROJECT,
            "component_id": "service-1",
            "repository_id": REPO,
            "component_snapshot": compact,
        }
    )
    grants = [{"id": "current-grant"}]
    state = {"revoked": False, "checked": []}

    async def all_runs():
        return runs

    async def repositories(provider):
        return grants

    async def resolve(component, **scope):
        return snapshot_definition(component, project, **scope)

    async def members(repo, command, receipt, definitions, *, configure, grants):
        assert not configure and grants == [{"id": "current-grant"}]
        assert definitions[-1] == project["definition"]
        state["checked"].append(definitions)
        if state["revoked"]:
            raise ValueError("Current graph reader denied")

    service = ChannelProducts(
        SimpleNamespace(runs=all_runs), SimpleNamespace(scm=SimpleNamespace(repositories=repositories))
    )
    service.projects = SimpleNamespace(snapshot_definition=resolve)
    service._members = members
    context = {"project": project, "graphs": [project["definition"]], "receipt": {"provider": "github"}}
    visible = await service._project_status({}, None, context)
    assert len(visible["runs"]) == 2 and len(state["checked"]) == 2
    state["revoked"] = True
    assert (await service._project_status({}, None, context))["runs"] == []


@pytest.mark.parametrize("changed", [False, True])
def test_evidence_consumer_verifies_compact_reference_against_exact_historical_graph(changed):
    from test_run_evidence import sample

    from firefly_forge.journal import digest
    from firefly_forge.project_snapshot import PreparedProjectRevision
    from firefly_forge.run_evidence import EvidenceUnavailable, build_evidence

    project = record()
    component = PreparedProjectRevision.capture(project).component(request())
    if changed:
        component["project_role_selection"]["disabled"] = []
    data = sample()
    scope = {"project_id": PROJECT, "component_id": "service-0", "component_snapshot": component}
    data["run"].update(scope)
    data["plan"].update(scope)
    data["plan"]["digest"] = digest({k: v for k, v in data["plan"].items() if k != "digest"})
    data["journal"][0]["payload"]["digest"] = data["plan"]["digest"]
    data["journal"][0]["digest"] = digest({k: v for k, v in data["journal"][0].items() if k != "digest"})
    data["project_revision"] = project
    if changed:
        with pytest.raises(EvidenceUnavailable):
            build_evidence(**data)
    else:
        assert build_evidence(**data).repositories == frozenset({REPO})


def test_campaign_selection_keeps_frozen_component_order_and_dependency_closure():
    from firefly_forge.project_campaigns import ProjectCampaigns

    value = record(4)["definition"]
    value["components"][3]["depends_on"] = ["service-0"]
    definition = ProjectDefinition.model_validate(value)
    assert ProjectCampaigns._scope(definition, ["service-3", "service-1"]) == ["service-0", "service-1", "service-3"]


async def test_native_admission_fence_is_explicit_and_rejects_reuse_in_another_unit(admission):
    store, _, _, _ = admission
    from test_run_admission_prepared_host import REPOSITORY

    prepared = await store.prepare_run(RunInput(repository_id=REPOSITORY, brief="Retain owner admission"))
    async with store._unit():
        fence = await store.fence_prepared_runs([prepared])
        assert fence is not None
    with pytest.raises(ValueError, match="fence"):
        async with store._unit():
            await store.insert_prepared_run_locked(prepared, fence=fence)


async def fenced_batch(admission, count=4):
    from test_run_admission_prepared_host import PROJECT as pid
    from test_run_admission_prepared_host import REPOSITORY as rid

    from firefly_forge.project_snapshot import PreparedProjectRevision

    store, state, _, _ = admission
    project = record(count)
    project["id"] = pid
    project["definition"]["product_repository_id"] = rid
    for component in project["definition"]["components"]:
        component["repository_id"] = rid
    project["digest"] = hashlib.sha256(json.dumps(project["definition"], sort_keys=True).encode()).hexdigest()
    row = SimpleNamespace(data=project)
    state["project_rows"] = {pid: row}
    shared = PreparedProjectRevision.capture(project)
    prepared = [
        await store.prepare_run(
            RunInput(repository_id=rid, project_id=pid, component_id=f"service-{i}", brief="Retain exact project"),
            prepared_project=shared,
        )
        for i in range(count)
    ]
    from dataclasses import replace

    from firefly_forge.journal import Journal
    from firefly_forge.project_campaigns import ProjectCampaigns

    manager = ProjectCampaigns(store, SimpleNamespace(connections=None, search=None), Journal(store))
    vector = manager._configuration_vector(rid, {"codex"})
    assert all(item.settings_generation is store.configuration.settings_generation for item in prepared)
    for item in prepared:
        manager._validate_admission_snapshots(item.unpack()["run"], vector)
    prepared = [replace(item, generation=store.configuration.write_generation) for item in prepared]
    return store, state, row, prepared


async def test_atomic_campaign_fence_validates_graph_once_and_retains_all_child_checks(admission, monkeypatch):
    from firefly_forge.project_snapshot import PreparedProjectRevision

    store, state, _, prepared = await fenced_batch(admission)
    original = PreparedProjectRevision.capture.__func__
    captures = []

    def capture(cls, project):
        captures.append(project["id"])
        return original(cls, project)

    monkeypatch.setattr(PreparedProjectRevision, "capture", classmethod(capture))
    async with store._unit():
        fence = await store.fence_prepared_runs(prepared)
        for item in prepared:
            await store.insert_prepared_run_locked(item, fence=fence)
    assert len(captures) == 1
    assert len(state["rows"]) == len(state["events"]) == len(prepared)
    assert all(row["role_selection_source"] == "project" for row in state["rows"].values())


@pytest.mark.parametrize("change", ["foreign_packet", "project_update", "late_project_update"])
async def test_fence_does_not_adopt_foreign_or_changed_project_admission(admission, change):
    from firefly_forge.store import PreparedRunAdmission

    store, state, row, prepared = await fenced_batch(admission, 1)
    item = prepared[0]
    with pytest.raises(ValueError, match="fence"):
        async with store._unit():
            fence = await store.fence_prepared_runs(prepared)
            if change == "foreign_packet":
                packet = item.unpack()
                packet["run"]["brief"] = "Another request"
                item = PreparedRunAdmission.capture(packet, item.generation, item.settings_generation)
            elif change == "project_update":
                row.data = {**row.data, "revision": row.data["revision"] + 1}
            else:
                admission_callback = store.vault_credential_admission

                async def update_after_wait(value):
                    await admission_callback(value)
                    row.data = {**row.data, "revision": row.data["revision"] + 1}

                store.vault_credential_admission = update_after_wait
            await store.insert_prepared_run_locked(item, fence=fence)
    assert state["rows"] == {} and state["events"] == []


async def test_current_component_snapshot_refuses_missing_persisted_revision():
    from contextlib import asynccontextmanager

    from firefly_forge.projects import Projects

    project = record()

    @asynccontextmanager
    async def unit():
        yield None

    async def find(*args, **kwargs):
        return SimpleNamespace(data=project)

    async def missing(*args, **kwargs):
        return None

    service = Projects(
        SimpleNamespace(_unit=unit),
        records=SimpleNamespace(find_by_id=find),
        revisions=SimpleNamespace(revision=missing),
    )
    with pytest.raises(KeyError, match="revision"):
        await service.component_snapshot(request())


@pytest.mark.parametrize("legacy", [False, True])
def test_evidence_rejects_same_repository_sibling_component_reference(legacy):
    from test_run_evidence import sample

    from firefly_forge.journal import digest
    from firefly_forge.project_snapshot import PreparedProjectRevision
    from firefly_forge.run_evidence import EvidenceUnavailable, build_evidence

    project = record()
    component = PreparedProjectRevision.capture(project).component(request(1))
    if legacy:
        component.pop("project_revision_ref")
        component.pop("project_role_selection")
        component["project_definition"] = project["definition"]
    data = sample()
    scope = {"project_id": PROJECT, "component_id": "service-0", "component_snapshot": component}
    data["run"].update(scope)
    data["plan"].update(scope)
    data["plan"]["digest"] = digest({k: v for k, v in data["plan"].items() if k != "digest"})
    data["journal"][0]["payload"]["digest"] = data["plan"]["digest"]
    data["journal"][0]["digest"] = digest({k: v for k, v in data["journal"][0].items() if k != "digest"})
    data["project_revision"] = project
    with pytest.raises(EvidenceUnavailable):
        build_evidence(**data)


@pytest.mark.parametrize("history", ["valid", "missing", "damaged"])
async def test_activity_keeps_small_reference_for_large_historical_project(monkeypatch, history):
    from contextlib import asynccontextmanager

    from firefly_forge import activity_service
    from firefly_forge.activity_models import ActivityUnavailable, project_activity
    from firefly_forge.journal import digest
    from firefly_forge.persistence import ForgeEventEntity, ForgePlanEntity
    from firefly_forge.project_persistence import ForgeProjectRevisionEntity
    from firefly_forge.project_snapshot import PreparedProjectRevision
    from firefly_forge.projects import Projects

    project = record(5000)
    assert len(json.dumps(project["definition"]).encode()) > 1048576
    component = PreparedProjectRevision.capture(project).component(request())
    run_id, event_id = "c" * 32, "d" * 32
    header = {"id": run_id, "repository_id": REPO, "project_id": PROJECT, "component_id": "service-0"}
    plan_data = {
        "run_id": run_id,
        **{k: header[k] for k in ("repository_id", "project_id", "component_id")},
        "component_snapshot": component,
    }
    plan = SimpleNamespace(data=plan_data, digest=digest(plan_data))
    projected, _ = project_activity(run_id, event_id, "stdout", "observed output " * 1500)
    event = SimpleNamespace(data={"id": event_id, "stage": "stdout", **projected})
    spent = []
    resolved = []

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace()

    class Transaction:
        def __init__(self, *args, **kwargs):
            assert kwargs["read_only"] is True

        @asynccontextmanager
        async def transaction(self):
            yield None

    async def bounded(session, statement, limit, remaining):
        entity = statement.column_descriptions[0]["entity"]
        row = (
            plan
            if entity is ForgePlanEntity
            else event
            if entity is ForgeEventEntity
            else SimpleNamespace(data=project)
        )
        assert entity in {ForgePlanEntity, ForgeEventEntity, ForgeProjectRevisionEntity}
        size = len(json.dumps(row.data).encode())
        if size > remaining[0]:
            raise ValueError("Historical graph exceeded the activity ledger envelope")
        remaining[0] -= size
        spent.append(size)
        return [row]

    async def historical(self, project_id, revision):
        assert project_id == PROJECT and revision == 3
        resolved.append((project_id, revision))
        if history == "missing":
            raise KeyError("Product project revision not found")
        value = copy.deepcopy(project)
        if history == "damaged":
            value["digest"] = "0" * 64
        return value

    async def get_header(run):
        assert run == run_id
        return header

    monkeypatch.setattr(activity_service, "TransactionTemplate", Transaction)
    monkeypatch.setattr(activity_service, "current_unit_of_work", lambda: SimpleNamespace(manager=object()))
    monkeypatch.setattr(Projects, "revision", historical)
    archive = activity_service.ActivityArchive(SimpleNamespace(_unit=unit), evidence=SimpleNamespace(_rows=bounded))
    monkeypatch.setattr(archive, "_header", get_header)
    if history == "valid":
        observation = await archive._capture(run_id, event_id)
        assert observation.project_revision_reference == {
            "kind": "project_revision_reference",
            "version": 1,
            "project_id": PROJECT,
            "revision": 3,
            "digest": project["digest"],
        }
        assert resolved == [(PROJECT, 3)] and sum(spent) < 1048576
    else:
        with pytest.raises(ActivityUnavailable):
            await archive._capture(run_id, event_id)


async def test_fence_accepts_repository_refresh_of_the_same_locked_revision(admission):
    store, state, row, prepared = await fenced_batch(admission, 1)
    async with store._unit():
        fence = await store.fence_prepared_runs(prepared)
        row.data = copy.deepcopy(row.data)
        await store.insert_prepared_run_locked(prepared[0], fence=fence)
    assert len(state["rows"]) == len(state["events"]) == 1
