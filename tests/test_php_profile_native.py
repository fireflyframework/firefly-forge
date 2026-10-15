# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Opt-in isolated PHP observations; never installs a runtime or uses a host fallback."""

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from test_profile_native import assert_drained

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.php_diagnostics import PHPProfileRequest, build_php_profile_argv
from firefly_forge.sandbox import SandboxExecutor, SandboxReconciliationRequired
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.workspace_tools import tool_catalog

pytestmark = pytest.mark.skipif(
    not os.environ.get("FORGE_TEST_PHP_IMAGE"), reason="Requires an explicitly prepared isolated PHP image"
)


async def owned_php(store, tmp_path, source, *, script="workload.php", output=16000, backend="pyfly"):
    await store.acquire_ownership()
    workspace = tmp_path / "owned-source"
    workspace.mkdir()
    (workspace / script).write_text(source)
    repository = await store.add_repository(
        RepositoryInput(name="Owned PHP observation", url="https://github.com/test/owned.git")
    )
    config = SandboxInput(
        mode="docker",
        context=os.environ["FORGE_TEST_DOCKER_CONTEXT"],
        image=os.environ["FORGE_TEST_PHP_IMAGE"],
        workspace_transport="archive",
        max_output_bytes=output,
        memory_mb=128,
        timeout=10,
        command_limit=5,
    )
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            brief="Observe an explicitly owned PHP fixture",
            agent="agentic",
            backend=backend,
            sandbox=config,
            roles=ProjectRoles(allowed_tools={"implementer": ["execute_command", "profile_php"]}),
        ),
        prepared_tools=tool_catalog({"execute_command": True, "profile_php": True}),
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "implement")
    runner = SandboxExecutor(store, journal)
    engine = Engine(store, tmp_path / "jobs", journal=journal)
    engine.sandbox = runner
    return workspace, run, journal, runner, engine


def report(result):
    marker = "\nFORGE_PHP_PROFILE_V1\n"
    assert result["output"].count(marker) == 1
    return json.loads(result["output"].split(marker)[1])


@pytest.mark.parametrize("script", ["workload.php", "artisan", '-recho "WRONG_OPTION";#entry.php'])
async def test_actual_php_file_arguments_metrics_and_receipt_are_bound(store, tmp_path, script):
    source = """<?php
require __DIR__ . '/helper.php';
if (file_exists('/var/run/docker.sock') || getenv('XDEBUG_MODE') !== 'off') { exit(91); }
echo json_encode([basename(__FILE__), $argv, $argc, $_SERVER['argv'], owned_value()]) . "\\n";
"""
    workspace, run, journal, runner, engine = await owned_php(store, tmp_path, source, script=script)
    (workspace / "helper.php").write_text("<?php function owned_value() { return 42; }")
    originals = {p.name: p.read_bytes() for p in workspace.iterdir()}
    arguments = ["two words", "; literal argument"]
    result = await engine._profile_php(run["id"], run, workspace, PHPProfileRequest(script=script, args=arguments))
    assert result["exit_code"] == 0
    actual = json.loads(result["output"].splitlines()[0])
    assert actual == [script, [script, *arguments], 3, [script, *arguments], 42]
    observed = report(result)
    assert observed["complete"] is True and observed["workload_exit_code"] == 0
    assert observed["metrics"]["wall_ns"] >= 0 and observed["metrics"]["peak_memory_bytes"] > 0
    assert observed["metrics"]["cpu_user_us"] >= 0 and observed["metrics"]["cpu_system_us"] >= 0
    assert {p.name: p.read_bytes() for p in workspace.iterdir()} == originals
    assert result["source_sha256"] == hashlib.sha256(originals[script]).hexdigest()
    assert result["image"] == os.environ["FORGE_TEST_PHP_IMAGE"]
    rows = await assert_drained(workspace, run, journal, runner)
    assert len(rows) == 1 and result["receipt"]["execution_id"] == rows[0]["execution_id"]
    assert result["receipt"]["request_digest"] == rows[0]["request"]["request_digest"]
    assert result["argv"] == rows[0]["request"]["request"]["argv"]


@pytest.mark.parametrize("ending,code", [("exit(7);", 7), ("owned_missing_function();", 255)])
async def test_actual_php_workload_exit_is_not_replaced_by_diagnostics(store, tmp_path, ending, code):
    source = '<?php echo "before failure\\n"; ' + ending
    workspace, run, journal, runner, engine = await owned_php(store, tmp_path, source)
    result = await engine._profile_php(run["id"], run, workspace, PHPProfileRequest(script="workload.php"))
    assert result["exit_code"] == code and "before failure\n" in result["output"]
    assert report(result)["workload_exit_code"] == code
    assert (workspace / "workload.php").read_text() == source
    assert len(await assert_drained(workspace, run, journal, runner)) == 1


async def test_actual_missing_xdebug_refuses_before_application_and_does_not_fallback(store, tmp_path):
    source = "<?php file_put_contents('application-ran', 'bad'); echo 'APPLICATION_RAN';"
    workspace, run, journal, runner, engine = await owned_php(store, tmp_path, source)
    result = await engine._profile_php(
        run["id"], run, workspace, PHPProfileRequest(script="workload.php", recipe="forge-php-xdebug-v1")
    )
    assert result["exit_code"] == 78 and "preinstalled Xdebug" in result["output"]
    assert "APPLICATION_RAN" not in result["output"] and not result["report_marker_observed"]
    assert sorted(p.name for p in workspace.iterdir()) == ["workload.php"]
    assert len(await assert_drained(workspace, run, journal, runner)) == 1


@pytest.mark.parametrize(
    "source,error", [("<?php sleep(10);", TimeoutError), ("<?php echo str_repeat('x', 50000);", RuntimeError)]
)
async def test_actual_php_runner_limits_preserve_unknown_reconciliation(store, tmp_path, source, error):
    workspace, run, journal, runner, _ = await owned_php(store, tmp_path, source, output=1024)
    request = PHPProfileRequest(script="workload.php", timeout=10)
    argv = build_php_profile_argv(request, hashlib.sha256(source.encode()).hexdigest())
    with pytest.raises(error):
        await runner.execute(
            run["id"],
            workspace,
            workspace,
            argv,
            1,
            "implement",
            ordinal=1,
            _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
        )
    before = await journal.entries(run["id"])
    original = await journal.sandbox_executions(run["id"])
    with pytest.raises(SandboxReconciliationRequired):
        await runner.execute(
            run["id"],
            workspace,
            workspace,
            argv,
            1,
            "implement",
            ordinal=1,
            _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
        )
    after = await journal.entries(run["id"])
    assert after[: len(before)] == before and len(after) == len(before) + 1
    assert after[-1]["kind"] == "sandbox_reconcile"
    assert after[-1]["payload"] == {
        "execution_id": original[0]["execution_id"],
        "effect": "start",
        "evidence": {"outcome": "interrupted", "observed_state": "absent"},
    }
    rows = await assert_drained(workspace, run, journal, runner)
    assert len(rows) == len(original) == 1 and rows[0]["request"] == original[0]["request"]
    assert (workspace / "workload.php").read_text() == source


async def test_actual_php_optional_sink_failure_retains_exact_original_result_without_replay(store, tmp_path):
    source = "<?php fclose(STDOUT); exit(7);"
    workspace, run, journal, runner, _ = await owned_php(store, tmp_path, source)
    argv = build_php_profile_argv(PHPProfileRequest(script="workload.php"), hashlib.sha256(source.encode()).hexdigest())
    receipts = []

    def refuse(value):
        receipts.append(value)
        raise RuntimeError("Owned optional collector unavailable")

    result = await runner.execute(
        run["id"],
        workspace,
        workspace,
        argv,
        10,
        "implement",
        ordinal=1,
        _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
        _receipt_sink=refuse,
    )
    before = await journal.entries(run["id"])
    retained = await runner.execute(
        run["id"],
        workspace,
        workspace,
        argv,
        10,
        "implement",
        ordinal=1,
        _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
        _receipt_sink=refuse,
    )
    assert retained == result and result.exit_code == 7
    assert len(receipts) == 2 and receipts[0] == receipts[1]
    assert await journal.entries(run["id"]) == before
    assert len(await assert_drained(workspace, run, journal, runner)) == 1


@pytest.mark.skipif(not os.environ.get("FORGE_TEST_LARAFLY_FIXTURE"), reason="Requires a source-bound larafly fixture")
@pytest.mark.parametrize("backend", ["pyfly", "larafly"])
async def test_actual_larafly_metrics_module_runs_without_vendor_downloads(store, tmp_path, backend):
    fixture = Path(os.environ["FORGE_TEST_LARAFLY_FIXTURE"])
    provenance = json.loads((fixture / "PROVENANCE.json").read_text())
    assert provenance["all9module_files_byte_identical_to_stable_release"] is True
    source = (fixture / "larafly-workload.php").read_text()
    assert hashlib.sha256(source.encode()).hexdigest() == provenance["workload_sha256"]
    workspace, run, journal, runner, engine = await owned_php(store, tmp_path, source, backend=backend)
    assert run["backend"] == backend and (await store.get_run(run["id"]))["backend"] == backend
    for relative, identity in provenance["original_files"].items():
        original = fixture / relative
        assert original.is_file() and not original.is_symlink()
        assert hashlib.sha256(original.read_bytes()).hexdigest() == identity["sha256"]
        destination = workspace / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
    shutil.copyfile(fixture / "LICENSE", workspace / "LICENSE")
    originals = {p.relative_to(workspace).as_posix(): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    result = await engine._profile_php(
        run["id"], run, workspace, PHPProfileRequest(script="workload.php", args=["measure"])
    )
    assert result["exit_code"] == 0 and report(result)["complete"] is True
    actual = json.loads(result["output"].splitlines()[0])
    assert actual == {
        "framework": "larafly",
        "module": "observability",
        "counter": 10000,
        "timer_count": 10000,
        "histogram_counts": [10000, 10000],
        "meters": 3,
    }
    assert {
        p.relative_to(workspace).as_posix(): p.read_bytes() for p in workspace.rglob("*") if p.is_file()
    } == originals
    assert len(await assert_drained(workspace, run, journal, runner)) == 1
