# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Maintenance projection binds new QA inputs without rewriting logical history."""

import copy
from types import SimpleNamespace

import pytest

from firefly_forge.journal import digest
from firefly_forge.native_location_projection import project_native_argv
from tests.test_native_location_ledger_host import events, ledger_case
from tests.test_verification_scope_commit_host import build_scope_case as build_scope_case


@pytest.fixture
def relocated_scope(build_scope_case, tmp_path, monkeypatch):
    from firefly_forge import verification_scope_attestation as scope

    c = build_scope_case
    (tmp_path / "ledger").mkdir()
    case = ledger_case.__wrapped__(tmp_path / "ledger")
    _, _, _, candidate, request = case
    logical_workspace = "/retained/storage/workspaces/" + c.run["id"]
    candidate["plan"]["source_storage"] = "/retained/storage"
    candidate["plan"]["target_storage"] = str(c.workspace.parent)
    target = c.workspace.parent / "workspaces" / c.run["id"]
    target.parent.mkdir()
    c.workspace.rename(target)
    c.workspace = target
    c.source = target / "source.py"
    candidate["plan"]["workspaces"] = [dict(run_id=c.run["id"], source=logical_workspace, target=str(target))]
    candidate["plan"]["verification_runtimes"] = [
        dict(
            run_id=c.run["id"],
            source="/fictional",
            target="/new/runtime",
            mode="verification_runtime_baseline",
            source_content_digest=None,
            target_content_digest="e" * 64,
            approved_baseline_sha256="f" * 64,
        )
    ]
    candidate["digest"] = digest({k: v for k, v in candidate.items() if k != "digest"})
    request["candidate_digest"] = candidate["digest"]
    history = events(case)
    record = dict(version=1, receipt_digest=history[-1]["digest"], events=history)
    c.intent["workspace"] = logical_workspace
    c.record = record
    c.case = case
    c.original = copy.deepcopy(c.run)
    c.live = True
    c.fences = 0
    c.discovery = []

    async def assert_current():
        c.fences += 1
        if not c.live:
            raise ValueError("Native location ownership changed")

    projection = SimpleNamespace(
        record=lambda: copy.deepcopy(c.record),
        project_argv=lambda run_id, argv: project_native_argv(c.record, run_id, argv),
        assert_current=assert_current,
    )

    async def current_location(store):
        assert store is c.store
        return projection if c.live else None

    def roots(checks, workspace, *args):
        c.discovery.append((copy.deepcopy(checks), workspace))
        return [c.dependencies]

    monkeypatch.setattr(scope, "current_native_location", current_location, raising=False)
    monkeypatch.setattr(scope, "_roots", roots)
    return c


async def test_new_scope_binds_logical_and_physical_commands_and_maintenance_receipt(relocated_scope):
    from firefly_forge.verification_scope_attestation import mapped_argv, prepare_build_scope, scope_record

    c = relocated_scope
    prepared = await prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    wire = prepared.witness.model_dump(mode="json")
    assert wire["version"] == 2
    assert wire["native_location"] == c.record
    assert wire["workspace"] == str(c.workspace)
    assert wire["checks"][0]["argv"] == c.run["commands"][0]["argv"]
    physical = ["/new/runtime/python", *c.run["commands"][0]["argv"][1:]]
    assert wire["checks"][0]["physical_argv"] == physical
    assert c.discovery[0][0][0].argv == physical and c.discovery[0][1] == c.workspace
    assert c.run == c.original
    handoff = dict(kind="verification_handoff_intent", payload=c.intent, digest="7" * 64, sequence=1)
    recorded = dict(kind="verification_build_scope", payload=wire, sequence=2)
    assert scope_record(c.run, c.intent, [handoff, recorded]) == prepared.witness
    assert mapped_argv(c.run, c.intent, [handoff, recorded], 0)[-len(physical) :] == physical
    await prepared.fence()
    assert c.fences > 0


@pytest.mark.parametrize("change", ["revoked", "changed_record"])
async def test_relocated_scope_fence_refuses_lost_or_replaced_live_authority(relocated_scope, change):
    from firefly_forge.verification_scope_attestation import prepare_build_scope

    c = relocated_scope
    prepared = await prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    if change == "revoked":
        c.live = False
    else:
        c.record["receipt_digest"] = "0" * 64
    with pytest.raises(ValueError, match="location|relocation"):
        await prepared.fence()


@pytest.mark.parametrize("change", ["physical_argv", "workspace", "raw_type"])
async def test_retained_scope_rejects_tampered_physical_mapping(relocated_scope, change):
    from firefly_forge.verification_scope_attestation import prepare_build_scope, scope_record

    c = relocated_scope
    prepared = await prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    wire = prepared.witness.model_dump(mode="json")
    if change == "physical_argv":
        wire["checks"][0]["physical_argv"] = ["/foreign/python"]
    elif change == "workspace":
        wire["workspace"] = "/foreign/workspace"
    else:
        wire["native_location"]["events"][0]["sequence"] = 1.0
    handoff = dict(kind="verification_handoff_intent", payload=c.intent, digest="7" * 64, sequence=1)
    with pytest.raises(ValueError):
        scope_record(c.run, c.intent, [handoff, dict(kind="verification_build_scope", payload=wire, sequence=2)])


async def test_no_maintenance_keeps_v1_wire_and_new_baseline_cannot_equal_old_scope(build_scope_case, monkeypatch):
    from firefly_forge import verification_scope_attestation as scope

    c = build_scope_case

    async def no_location(store):
        return None

    monkeypatch.setattr(scope, "current_native_location", no_location, raising=False)
    prepared = await scope.prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    wire = prepared.witness.model_dump(mode="json")
    assert wire["version"] == 1
    assert "native_location" not in wire and "workspace" not in wire
    assert "physical_argv" not in wire["checks"][0]


async def test_reuse_rejects_predecessor_scope_after_runtime_baseline_selection(relocated_scope):
    from firefly_forge import verification_scope_attestation as scope

    c = relocated_scope
    prepared = await scope.prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    current = prepared.witness.model_dump(mode="json")
    predecessor = {key: value for key, value in current.items() if key not in {"native_location", "workspace"}}
    predecessor["version"] = 1
    predecessor["checks"] = [
        {key: value for key, value in check.items() if key != "physical_argv"} for check in predecessor["checks"]
    ]
    old = scope.VerificationScopeV1.model_validate(predecessor)
    # Even otherwise identical byte/environment hashes cannot authorize a replaced runtime baseline.
    with pytest.raises(ValueError, match="predecessor"):
        await scope.assert_scope_location(c.store, old)


async def test_unscoped_database_command_projects_exact_executable_without_build_wrapper(relocated_scope):
    from firefly_forge.verification_scope_attestation import mapped_argv, prepare_build_scope

    c = relocated_scope
    database = dict(argv=["/fictional/python", "-B", "-m", "pytest", "backend/tests"], timeout=5)
    c.run["commands"].append(database)
    c.intent["commands_digest"] = digest(dict(commands=c.run["commands"]))
    frozen = copy.deepcopy(c.run["commands"])
    prepared = await prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    entries = [
        dict(kind="verification_handoff_intent", payload=c.intent, digest="7" * 64, sequence=1),
        dict(kind="verification_build_scope", payload=prepared.witness.model_dump(mode="json"), sequence=2),
    ]
    assert mapped_argv(c.run, c.intent, entries, 1) == ["/new/runtime/python", *database["argv"][1:]]
    assert c.run["commands"] == frozen
    assert [check.command_index for check in prepared.witness.checks] == [1]


async def test_new_declared_runtime_baseline_is_bound_even_when_paths_and_package_bytes_match(relocated_scope):
    from firefly_forge.verification_scope_attestation import prepare_build_scope

    c = relocated_scope
    original = await prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert original is not None
    candidate, request = c.case[3:]
    candidate["plan"]["verification_runtimes"][0]["approved_baseline_sha256"] = "0" * 64
    candidate["digest"] = digest({key: value for key, value in candidate.items() if key != "digest"})
    request["candidate_digest"] = candidate["digest"]
    history = events(c.case)
    c.record = dict(version=1, receipt_digest=history[-1]["digest"], events=history)
    replacement = await prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert replacement is not None
    assert original.witness.dependency_digest == replacement.witness.dependency_digest
    assert original.witness.environment_digest == replacement.witness.environment_digest
    assert original.witness.prerequisite_digest != replacement.witness.prerequisite_digest
    with pytest.raises(ValueError, match="location"):
        await original.fence()


async def test_new_physical_handoff_workspace_projection_is_idempotent(relocated_scope):
    from firefly_forge.verification_scope_attestation import prepare_build_scope, scope_record

    c = relocated_scope
    c.intent["workspace"] = str(c.workspace)
    prepared = await prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    wire = prepared.witness.model_dump(mode="json")
    assert wire["workspace"] == c.intent["workspace"]
    assert wire["checks"][0]["argv"] == c.original["commands"][0]["argv"]
    entries = [
        dict(kind="verification_handoff_intent", payload=c.intent, digest="7" * 64, sequence=1),
        dict(kind="verification_build_scope", payload=wire, sequence=2),
    ]
    assert scope_record(c.run, c.intent, entries) == prepared.witness


def test_explicit_native_handoff_without_reusable_scope_projects_frozen_command(relocated_scope):
    from firefly_forge.verification_scope_attestation import mapped_argv

    c = relocated_scope
    c.intent["native_location"] = copy.deepcopy(c.record)
    original = copy.deepcopy(c.run["commands"])
    assert mapped_argv(c.run, c.intent, [], 0) == ["/new/runtime/python", *original[0]["argv"][1:]]
    assert c.run["commands"] == original
    c.intent["native_location"]["events"][0]["sequence"] = 1.0
    with pytest.raises(ValueError):
        mapped_argv(c.run, c.intent, [], 0)


async def test_reusable_scope_must_match_its_exact_native_handoff_record(relocated_scope):
    from firefly_forge.verification_scope_attestation import mapped_argv, prepare_build_scope, scope_record

    c = relocated_scope
    prepared = await prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    c.intent["native_location"] = copy.deepcopy(c.record)
    entries = [
        dict(kind="verification_handoff_intent", payload=c.intent, digest="7" * 64, sequence=1),
        dict(kind="verification_build_scope", payload=prepared.witness.model_dump(mode="json"), sequence=2),
    ]
    assert scope_record(c.run, c.intent, entries) == prepared.witness
    c.intent["native_location"]["receipt_digest"] = "0" * 64
    with pytest.raises(ValueError, match="location"):
        scope_record(c.run, c.intent, entries)
    with pytest.raises(ValueError, match="location"):
        mapped_argv(c.run, c.intent, entries, 0)
