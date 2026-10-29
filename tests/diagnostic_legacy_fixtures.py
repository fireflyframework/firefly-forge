# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Retained diagnostic producers freeze the original finite runner wire explicitly."""

import json

from firefly_forge.sandbox_models import capture_sandbox


async def create_retained_diagnostic_run(store, request, **options):
    sandbox = capture_sandbox(request.sandbox)
    assert "work_duration" not in sandbox["configuration"]
    run = await store.create_run(
        request,
        prepared_runner={"sandbox_snapshot": sandbox, "runner_config_revision": None, "search_snapshot": None},
        **options,
    )
    assert "work_duration" not in run and "work_duration" not in run["sandbox_snapshot"]["configuration"]
    assert json.dumps(run["sandbox_snapshot"], sort_keys=True) == json.dumps(sandbox, sort_keys=True)
    return run
