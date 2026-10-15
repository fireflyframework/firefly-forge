# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One real isolated PHP command reaches the normal persisted journal read path."""

import os

import pytest
from test_php_profile_native import owned_php, report
from test_profile_native import assert_drained

from firefly_forge.journal import Journal
from firefly_forge.php_diagnostics import PHPProfileRequest

pytestmark = pytest.mark.skipif(
    not os.environ.get("FORGE_TEST_PHP_IMAGE"), reason="Requires an explicitly prepared isolated PHP image"
)


async def test_actual_isolated_php_result_has_one_bound_direct_diagnostic_after_cleanup(store, tmp_path):
    workspace, run, journal, runner, engine = await owned_php(
        store, tmp_path, '<?php echo "owned retained PHP observation\\n"; exit(7);'
    )
    request = PHPProfileRequest(script="workload.php")
    result = await engine._profile_php(run["id"], run, workspace, request)
    assert result["exit_code"] == 7 and report(result)["workload_exit_code"] == 7
    assert result["retention"]["status"] == "recorded"
    entries = await journal.entries(run["id"])
    diagnostics = [entry for entry in entries if entry["kind"] == "diagnostic_observation"]
    assert len(diagnostics) == 1 and diagnostics[0] == entries[-1]
    payload = {key: value for key, value in result.items() if key != "retention"}
    assert diagnostics[0]["payload"] == payload
    assert Journal.verify_chain(entries)
    assert len([entry for entry in entries if entry["kind"] == "sandbox_intent"]) == 3
    assert len([entry for entry in entries if entry["kind"] == "sandbox_receipt"]) == 3
    assert await journal.php_diagnostic(run["id"], request, payload) == diagnostics[0]
    assert await journal.entries(run["id"]) == entries
    assert len(await assert_drained(workspace, run, journal, runner)) == 1
