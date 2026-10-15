# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PHP profiling is an explicit diagnostic capability, never a QA approval."""

import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from firefly_forge.workspace_tools import tool_catalog


def test_php_profiling_has_a_versioned_recipe_module():
    assert importlib.util.find_spec("firefly_forge.php_diagnostics") is not None, (
        "PHP profiling needs a native versioned recipe"
    )


def test_php_profile_capability_requires_both_explicit_grants():
    catalog = {item["id"]: item for item in tool_catalog()}
    assert "profile_php" in catalog, "The PHP profiling capability is missing"
    assert catalog["profile_php"]["availability"] == "not_configured"
    single = {item["id"]: item for item in tool_catalog({"profile_php": True})}
    assert single["profile_php"]["availability"] == "not_configured"
    ready = {item["id"]: item for item in tool_catalog({"profile_php": True, "execute_command": True})}
    assert ready["profile_php"]["availability"] == "available"
    assert set(ready["profile_php"]["requires"]) == {"profile_php", "execute_command"}


def recipes():
    if importlib.util.find_spec("firefly_forge.php_diagnostics") is None:
        pytest.fail("PHP profiling needs a callable native recipe")
    return importlib.import_module("firefly_forge.php_diagnostics")


def php_fixture():
    php = shutil.which(os.environ.get("FORGE_TEST_PHP_FIXTURE", "php"))
    assert php, "Prepare the PHP 8.3+ fixture with make php-fixture-check; no interpreter fallback is provided"
    return php


def test_explicit_php_fixture_does_not_fall_back_to_another_interpreter(monkeypatch):
    monkeypatch.setenv("FORGE_TEST_PHP_FIXTURE", "/missing-owned-fixture/php")
    with pytest.raises(AssertionError, match="make php-fixture-check"):
        php_fixture()


def run_recipe(tmp_path, source, *, script="workload.php", arguments=(), expected=None, recipe=None, timeout=30):
    php = php_fixture()
    module = recipes()
    target = tmp_path / script
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    fields = {"script": script, "args": list(arguments), "timeout": timeout}
    if recipe:
        fields["recipe"] = recipe
    request = module.PHPProfileRequest.model_validate(fields)
    argv = module.build_php_profile_argv(request, expected or hashlib.sha256(target.read_bytes()).hexdigest())
    assert argv[0] == "php"
    # This host interpreter is a test fixture only; product dispatch uses the frozen isolated runner.
    result = subprocess.run(
        [php, *argv[1:]],
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": str(scratch)},
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert list(scratch.iterdir()) == []
    return result


def summary(result):
    lines = result.stdout.splitlines()
    marker = lines.index("FORGE_PHP_PROFILE_V1")
    return json.loads(lines[marker + 1])


def test_runtime_recipe_preserves_native_entrypoint_globals_and_includes(tmp_path):
    (tmp_path / "helper.php").write_text("<?php function owned_workload() { return 42; }\n")
    source = """<?php
require __DIR__ . '/helper.php';
echo json_encode([__FILE__, __DIR__, $argv, $argc, $_SERVER['argv'], owned_workload()]) . "\\n";
"""
    result = run_recipe(tmp_path, source, arguments=["two words", "; literal argument"])
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout.splitlines()[0])
    assert actual == [
        str(tmp_path / "workload.php"),
        str(tmp_path),
        ["workload.php", "two words", "; literal argument"],
        3,
        ["workload.php", "two words", "; literal argument"],
        42,
    ]
    observed = summary(result)
    assert observed["recipe"] == "forge-php-runtime-v1"
    assert observed["observation_only"] is True and observed["best_effort"] is True
    assert observed["metrics"]["wall_ns"] >= 0
    assert observed["metrics"]["cpu_user_us"] >= 0 and observed["metrics"]["cpu_system_us"] >= 0
    assert observed["metrics"]["peak_memory_bytes"] > 0


def test_standard_artisan_entrypoint_is_not_rewritten_or_evaluated(tmp_path):
    source = "<?php echo basename(__FILE__) . ':' . $argv[1] . \"\\n\";\n"
    result = run_recipe(tmp_path, source, script="artisan", arguments=["list"])
    assert result.returncode == 0 and result.stdout.startswith("artisan:list\n")
    assert summary(result)["metrics"]["peak_memory_bytes"] > 0
    assert (tmp_path / "artisan").read_text() == source


def test_option_like_filename_is_executed_as_the_bound_source_not_php_code(tmp_path):
    script = "-rfile_put_contents('escaped', 'bad');#entry.php"
    result = run_recipe(tmp_path, '<?php echo "bound source\\n";', script=script)
    assert not (tmp_path / "escaped").exists()
    assert result.returncode == 0 and result.stdout.startswith("bound source\n")
    assert summary(result)["complete"] is True


@pytest.mark.parametrize("ending,code", [("exit(7);", 7), ("owned_missing_function();", 255)])
def test_runtime_recipe_keeps_original_exit_and_fatal_error(tmp_path, ending, code):
    result = run_recipe(tmp_path, '<?php echo "before failure\\n"; ' + ending)
    assert result.returncode == code
    assert result.stdout.startswith("before failure\n")
    assert summary(result)["observation_only"] is True
    if code == 255:
        assert "owned_missing_function" in result.stderr


def test_closed_workload_stdout_does_not_replace_exit_or_shutdown_metrics(tmp_path):
    result = run_recipe(tmp_path, "<?php fclose(STDOUT); exit(7);")
    assert result.returncode == 7
    assert summary(result)["workload_exit_code"] == 7


def test_missing_shutdown_report_keeps_workload_exit_and_marks_incomplete(tmp_path):
    source = """<?php
register_shutdown_function(static function() {
    foreach (glob(getenv('TMPDIR') . '/forge-php-profile-*/*') as $file) { unlink($file); }
});
exit(7);
"""
    result = run_recipe(tmp_path, source)
    assert result.returncode == 7
    observed = summary(result)
    assert observed["workload_exit_code"] == 7
    assert observed["complete"] is False and observed["metrics"] is None


def test_recipe_deadline_stops_child_and_does_not_claim_original_success(tmp_path):
    result = run_recipe(tmp_path, "<?php sleep(4); file_put_contents('unexpected-write', 'bad');", timeout=1)
    assert result.returncode != 0 and not (tmp_path / "unexpected-write").exists()
    observed = summary(result)
    assert observed["complete"] is False and observed["workload_exit_code"] is None


def test_source_byte_bound_precedes_application_execution(tmp_path):
    source = "<?php file_put_contents('unexpected-write', 'bad'); /*" + "x" * 1_000_000 + "*/"
    result = run_recipe(tmp_path, source)
    assert result.returncode != 0 and not (tmp_path / "unexpected-write").exists()


def test_runtime_child_explicitly_disables_inherited_xdebug_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDEBUG_MODE", "debug,profile,trace")
    monkeypatch.setenv("XDEBUG_CONFIG", "client_host=untrusted.invalid client_port=9999")
    result = run_recipe(tmp_path, "<?php echo getenv('XDEBUG_MODE') . ':' . getenv('XDEBUG_CONFIG') . \"\\n\";")
    assert result.returncode == 0 and result.stdout.startswith("off:\n")


def test_changed_source_is_refused_before_application_execution(tmp_path):
    result = run_recipe(tmp_path, "<?php file_put_contents('unexpected-write', 'bad');", expected="a" * 64)
    assert result.returncode != 0
    assert not (tmp_path / "unexpected-write").exists()
    assert "FORGE_PHP_PROFILE_V1" not in result.stdout


@pytest.mark.parametrize("link", ["symlink", "parent_symlink", "hardlink"])
def test_launcher_refuses_linked_sources_even_with_matching_digest(tmp_path, link):
    module = recipes()
    source = b"<?php file_put_contents('unexpected-write', 'bad');"
    real = tmp_path / "real.php"
    real.write_bytes(source)
    target = tmp_path / "workload.php"
    name = "workload.php"
    if link == "symlink":
        target.symlink_to(real)
    elif link == "hardlink":
        target.hardlink_to(real)
    else:
        directory = tmp_path / "real-dir"
        directory.mkdir()
        (directory / "workload.php").write_bytes(source)
        (tmp_path / "linked").symlink_to(directory, target_is_directory=True)
        name = "linked/workload.php"
    argv = module.build_php_profile_argv(module.PHPProfileRequest(script=name), hashlib.sha256(source).hexdigest())
    result = subprocess.run([php_fixture(), *argv[1:]], cwd=tmp_path, capture_output=True, timeout=5)
    assert result.returncode != 0 and not (tmp_path / "unexpected-write").exists()


@pytest.mark.parametrize(
    "script", ["../outside.php", "/outside.php", ".git/tool.php", ".env/a.php", "a\\b.php", "a.py"]
)
def test_php_request_refuses_private_and_non_php_entrypoints(script):
    with pytest.raises(ValidationError):
        recipes().PHPProfileRequest(script=script)


@pytest.mark.parametrize(
    "fields", [{"timeout": True}, {"timeout": 121}, {"rows": 101}, {"recipe": "other"}, {"args": [""]}]
)
def test_php_request_refuses_unbounded_or_overridden_policy(fields):
    with pytest.raises(ValidationError):
        recipes().PHPProfileRequest(script="workload.php", **fields)


def test_mutated_request_arguments_are_revalidated_before_recipe_build():
    module = recipes()
    request = module.PHPProfileRequest(script="workload.php")
    request.args.append("\x00")
    with pytest.raises(ValidationError):
        module.build_php_profile_argv(request, "a" * 64)


def parse_cachegrind(tmp_path, text, *, rows=1):
    module = recipes()
    profile = tmp_path / "cachegrind.out"
    profile.write_text(text)
    code = module._CACHEGRIND_PARSER + "echo json_encode(forge_cachegrind($argv[1], (int)$argv[2]));"
    return subprocess.run(
        [php_fixture(), "-n", "-r", code, "--", str(profile), str(rows)],
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_cachegrind_summary_uses_exclusive_costs_and_resolved_function_references(tmp_path):
    result = parse_cachegrind(
        tmp_path,
        """version: 1
events: Time_(10ns) Memory_(bytes)
fl=(1) /workspace/workload.php
fn=(1) owned_workload
4 100 32
cfl=(1)
cfn=(2) helper
calls=2 9
4 900 64
fl=(1)
fn=(2)
9 400 64
summary: 500 96
""",
        rows=2,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["events"] == ["Time_(10ns)", "Memory_(bytes)"]
    assert [(row["function"], row["self_time"], row["calls"]) for row in summary["rows"]] == [
        ("helper", 400, 2),
        ("owned_workload", 100, 0),
    ]
    assert summary["rows_truncated"] is False


@pytest.mark.parametrize(
    "text", ["events: Unrecognized\nfn=(1) bad\n1 2 3\n", "x" * 8_388_609], ids=["unknown-units", "oversized"]
)
def test_cachegrind_parser_rejects_unknown_units_and_oversized_profiles(tmp_path, text):
    result = parse_cachegrind(tmp_path, text)
    assert result.returncode != 0


def test_cachegrind_summary_is_bounded_without_reading_project_code(tmp_path):
    result = parse_cachegrind(
        tmp_path,
        "events: Time_(10ns) Memory_(bytes)\nfn=(1) first\n1 20 2\nfn=(2) second\n1 10 1\nsummary: 30 3\n",
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert len(summary["rows"]) == 1 and summary["rows_truncated"] is True


@pytest.mark.parametrize("footer", ["", "summary: 20 2", "summary: 20 2\n1 1 1\n"])
def test_cachegrind_profile_without_final_shutdown_summary_is_incomplete(tmp_path, footer):
    result = parse_cachegrind(tmp_path, "events: Time_(10ns) Memory_(bytes)\nfn=(1) unfinished\n1 20 2\n" + footer)
    assert result.returncode != 0


def test_php_launcher_respects_existing_command_argument_bounds():
    from firefly_forge.sandbox_models import bounded_argv

    module = recipes()
    argv = module.build_php_profile_argv(module.PHPProfileRequest(script="workload.php"), "a" * 64)
    assert bounded_argv(argv) == argv
