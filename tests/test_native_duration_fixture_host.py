# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Legacy native producers select their frozen runner before Store admission."""

import importlib
import importlib.util
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.work_duration_models import WorkDurationPolicyV2


def fixture_snapshot(**kwargs):
    assert importlib.util.find_spec("runner_duration_native_fixtures"), "Explicit native runner preparation is missing"
    return importlib.import_module("runner_duration_native_fixtures").runner_snapshot(**kwargs)


def test_legacy_native_runner_capture_keeps_absence_and_exact_bytes():
    prepared = fixture_snapshot()
    assert "work_duration" not in prepared
    assert prepared == {
        "sandbox_snapshot": capture_sandbox(SandboxInput(command_limit=22)),
        "runner_config_revision": None,
        "search_snapshot": None,
    }


def test_selected_uncapped_native_runner_binds_matching_frozen_policy():
    prepared = fixture_snapshot(policy=WorkDurationPolicyV2(), timeout=1)
    assert prepared["work_duration"] == {"protocol": "forge-work-duration-v2", "timeout_seconds": None}
    assert prepared["sandbox_snapshot"]["configuration"]["work_duration"] == prepared["work_duration"]
    assert prepared["sandbox_snapshot"]["configuration"]["timeout"] == 1
    assert "timeout" not in prepared["sandbox_snapshot"]["enforced_controls"]


def test_selected_native_runner_preserves_chosen_deadline():
    prepared = fixture_snapshot(policy=WorkDurationPolicyV2(timeout_seconds=17))
    assert prepared["work_duration"]["timeout_seconds"] == 17
    assert prepared["sandbox_snapshot"]["configuration"]["work_duration"] == prepared["work_duration"]
    assert "timeout" in prepared["sandbox_snapshot"]["enforced_controls"]


async def test_historical_continuity_fixture_selects_legacy_before_store_admission(tmp_path, monkeypatch):
    import test_invalid_read_continuity_native as module

    observed = {}

    class AdmissionObserved(Exception):
        pass

    class Store:
        configuration = SimpleNamespace(update=lambda *args: None)

        async def create_run(self, request, **kwargs):
            observed.update(kwargs)
            raise AdmissionObserved

    @asynccontextmanager
    async def prepared(*args, **kwargs):
        yield None, None, {"repository_id": str(uuid4()), "project_id": uuid4().hex}

    monkeypatch.setattr(module, "prepared", prepared)
    fixture = module.native_continuity.__wrapped__(Store(), tmp_path, monkeypatch)
    with pytest.raises(AdmissionObserved):
        await anext(fixture)
    assert observed.get("prepared_runner") is not None, "Legacy selection must precede Store.create_run"
    selected = observed["prepared_runner"]
    assert "work_duration" not in selected
    assert "work_duration" not in selected["sandbox_snapshot"]["configuration"]
    assert selected["sandbox_snapshot"] == capture_sandbox(SandboxInput(command_limit=22))
