# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit logical/physical views; projection records alone do not grant execution."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from firefly_forge.native_location_files import read_storage_identity
from firefly_forge.native_location_maintenance import database_fingerprint, native_location_rows, topology_base
from firefly_forge.native_location_models import NativeLocationProjectionRecordV1
from firefly_forge.native_location_runtime import location_binding, resolve_native_location
from firefly_forge.runtime_role_persistence import ForgeRuntimeTopologyRepository
from firefly_forge.runtime_roles import STORAGE_ATTESTATION_ID, TOPOLOGY_ID
from firefly_forge.source_catalog import _json
from firefly_forge.store import Store

Data = dict[str, Any]


def _projection(value: Data) -> NativeLocationProjectionRecordV1:
    record = NativeLocationProjectionRecordV1.model_validate(value)
    if _json(value) != _json(record.model_dump(mode="json")):
        raise ValueError("Physical projection differs from its exact typed native record")
    return record


def _path(value: str, source: str, target: str) -> str:
    path = Path(value)
    if path.is_absolute() and path.is_relative_to(source):
        return str(Path(target) / path.relative_to(source))
    return value


def project_native_argv(value: Data, run_id: str, logical: list[str]) -> list[str]:
    record = _projection(value)
    if not logical or any(type(token) is not str for token in logical):
        raise ValueError("Physical execution projection requires exact logical argument tokens")
    physical = list(logical)
    for event in record.events:
        if event.kind != "native_location_receipt":
            continue
        plan = event.candidate.plan
        workspace = next((item for item in plan.workspaces if item.run_id == run_id), None)
        if workspace is None:
            raise ValueError("This native run was not selected for the completed location transfer")
        mappings = [(workspace.source, workspace.target)] + [
            (item.source, item.target) for item in plan.verification_runtimes if item.run_id == run_id
        ]
        for index, token in enumerate(physical):
            choices = [
                (source, target)
                for source, target in mappings
                if Path(token).is_absolute() and Path(token).is_relative_to(source)
            ]
            if len(choices) > 1:
                raise ValueError("Physical command path has ambiguous reviewed location mappings")
            if choices:
                physical[index] = _path(token, *choices[0])
    return physical


def workspace_matches(value: Data, run_id: str, actual: dict[str, str], expected: dict[str, str]) -> bool:
    record = _projection(value)
    paths: list[str] = []
    for event in record.events:
        if event.kind != "native_location_receipt":
            continue
        selected = next((item for item in event.candidate.plan.workspaces if item.run_id == run_id), None)
        if selected is None or (paths and selected.source != paths[-1]):
            return False
        if not paths:
            paths.append(selected.source)
        paths.append(selected.target)
    return bool(paths) and expected.get("path") in paths and _json(actual) == _json({**expected, "path": paths[-1]})


def native_projection_record(store: Store) -> Data | None:
    state = getattr(store, "native_location_state", None)
    if state is None:
        return None
    value = {"version": 1, "receipt_digest": state.receipt_digest, "events": list(state.history)}
    _projection(value)
    return copy.deepcopy(value)


async def assert_handoff_location(store: Store, run_id: str, intent: Data) -> None:
    recorded = intent.get("native_location")
    if getattr(store, "native_location_state", None) is None:
        if recorded is not None:
            raise ValueError("Verification handoff has no admitted native location authority")
        return
    current = await current_native_location(store)
    if current is None or recorded is None or _json(recorded) != _json(current.record()):
        raise ValueError("Verification handoff changed its exact owned native location context")
    selected = [value for value in _projection(recorded).events[-1].candidate.plan.workspaces if value.run_id == run_id]
    if (
        len(selected) != 1
        or selected[0].target != intent["workspace"]
        or current.project_argv(run_id, [intent["workspace"]])[0] != intent["workspace"]
    ):
        raise ValueError("A fresh verification handoff requires its current physical workspace")


@dataclass(frozen=True)
class NativeLocationProjection:
    _store: Store
    _record: Data

    @property
    def receipt_digest(self) -> str:
        return str(self._record["receipt_digest"])

    def record(self) -> Data:
        return copy.deepcopy(self._record)

    def project_argv(self, run_id: str, logical: list[str]) -> list[str]:
        return project_native_argv(self._record, run_id, logical)

    def workspace_matches(self, run_id: str, actual: dict[str, str], expected: dict[str, str]) -> bool:
        return workspace_matches(self._record, run_id, actual, expected)

    async def assert_current(self) -> None:
        current = await current_native_location(self._store)
        if current is None or _json(current.record()) != _json(self._record):
            raise ValueError("Native location ownership changed after physical execution preparation")


async def current_native_location(store: Store) -> NativeLocationProjection | None:
    records = ForgeRuntimeTopologyRepository()
    async with store._unit() as session:
        rows = await records.native_locations()
        if not rows:
            return None
        history = native_location_rows(rows)
        row = await records.find_by_id(TOPOLOGY_ID)
        if row is None:
            raise ValueError("Native physical location has no immutable topology")
        base = topology_base(row, await records.find_by_id(STORAGE_ATTESTATION_ID))
        identity = read_storage_identity(store.path.parent)
        fingerprint = await database_fingerprint(
            session, store.schema, Path(history[-1]["candidate"]["postgres_data_directory"])
        )
        state = resolve_native_location(base, history, identity, fingerprint)
        if state.storage_binding != location_binding(str(store.path.parent)) or state.receipt_digest is None:
            raise ValueError("Physical storage is not the current exclusive native location leaf")
        value = {"version": 1, "receipt_digest": state.receipt_digest, "events": history}
        _projection(value)
        return NativeLocationProjection(store, value)


async def source_workspace_matches(store: Store, run_id: str, actual: dict[str, str], expected: dict[str, str]) -> bool:
    if getattr(store, "native_location_state", None) is None:
        return _json(actual) == _json(expected)
    projection = await current_native_location(store)
    return projection is not None and projection.workspace_matches(run_id, actual, expected)


async def runner_location(
    store: Store, run_id: str, task_id: str, logical: list[str]
) -> tuple[list[str], Data | None, Callable[[], Awaitable[None]] | None]:
    if getattr(store, "native_location_state", None) is None:
        return logical, None, None
    projection = await current_native_location(store)
    if projection is None:
        raise ValueError("Relocated runner lost its native maintenance authority")
    physical = projection.project_argv(run_id, logical)
    if physical != logical and task_id != "verify":
        raise ValueError("A relocated verification runtime cannot rebind a coding invocation")
    return (
        physical,
        {
            "version": 1,
            "receipt_digest": projection.receipt_digest,
            "logical_argv": list(logical),
            "physical_argv": list(physical),
        },
        projection.assert_current,
    )
