# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Execute the stdlib recipe; timing values are observations, never pass thresholds."""

import hashlib
import importlib
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError


def recipes():
    try:
        return importlib.import_module("firefly_forge.diagnostic_recipes")
    except ModuleNotFoundError:
        pytest.fail("The callable Python profiling recipe is not implemented")


def run_recipe(tmp_path, source, *, arguments=(), expected=None):
    module = recipes()
    target = tmp_path / "workload.py"
    target.write_text(source)
    request = module.PythonProfileRequest(script="workload.py", args=list(arguments))
    argv = module.build_profile_argv(request, expected or hashlib.sha256(target.read_bytes()).hexdigest())
    assert argv[0] == "python3"
    return subprocess.run([sys.executable, *argv[1:]], cwd=tmp_path, capture_output=True, text=True, timeout=5)


def test_cpu_recipe_executes_owned_source_and_emits_a_plain_text_report(tmp_path):
    result = run_recipe(tmp_path, "def owned_workload():\n    return sum(range(2000))\nprint(owned_workload())\n")
    assert result.returncode == 0
    assert "1999000" in result.stdout
    assert "FORGE_CPROFILE_V1" in result.stdout
    assert "owned_workload" in result.stdout
    assert "cumtime" in result.stdout
    assert sorted(path.name for path in tmp_path.iterdir()) == ["workload.py"]


@pytest.mark.parametrize(
    "ending,code,output",
    [("raise SystemExit(7)", 7, ""), ("raise ValueError('owned failure')", 1, "ValueError: owned failure")],
)
def test_recipe_keeps_the_workload_exit_and_exception(tmp_path, ending, code, output):
    result = run_recipe(tmp_path, "print('before failure')\n" + ending + "\n")
    assert result.returncode == code
    assert "before failure" in result.stdout
    assert "FORGE_CPROFILE_V1" in result.stdout
    assert output in result.stderr


def test_recipe_refuses_changed_source_before_the_workload_can_run(tmp_path):
    result = run_recipe(tmp_path, "open('unexpected-write', 'w').write('changed')\n", expected="a" * 64)
    assert result.returncode != 0
    assert "source changed" in result.stderr.lower()
    assert not (tmp_path / "unexpected-write").exists()


def test_recipe_passes_arguments_as_data_without_shell_interpretation(tmp_path):
    args = ["two words", "; touch unexpected-write"]
    result = run_recipe(tmp_path, "import json, sys\nprint(json.dumps(sys.argv))\n", arguments=args)
    assert json.loads(result.stdout.splitlines()[0]) == ["workload.py", *args]
    assert not (tmp_path / "unexpected-write").exists()


@pytest.mark.parametrize(
    "script", ["../outside.py", "/outside.py", ".git/tool.py", ".env/private.py", "a\\b.py", "data.json"]
)
def test_recipe_inputs_reject_non_source_and_private_paths(script):
    with pytest.raises(ValidationError):
        recipes().PythonProfileRequest(script=script)


@pytest.mark.parametrize(
    "fields",
    [
        {"rows": True},
        {"rows": 101},
        {"timeout": False},
        {"timeout": 121},
        {"image": "untrusted"},
        {"recipe": "other"},
        {"args": [""]},
    ],
)
def test_recipe_inputs_reject_policy_overrides_and_unbounded_values(fields):
    with pytest.raises(ValidationError):
        recipes().PythonProfileRequest(script="workload.py", **fields)


def test_recipe_launcher_rejects_a_symlink_even_with_the_matching_digest(tmp_path):
    module = recipes()
    source = "print('should not execute')\n"
    (tmp_path / "real.py").write_text(source)
    (tmp_path / "workload.py").symlink_to(tmp_path / "real.py")
    argv = module.build_profile_argv(
        module.PythonProfileRequest(script="workload.py"), hashlib.sha256(source.encode()).hexdigest()
    )
    result = subprocess.run([sys.executable, *argv[1:]], cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "should not execute" not in result.stdout


def test_mutating_a_request_argument_cannot_bypass_dispatch_bounds():
    module = recipes()
    request = module.PythonProfileRequest(script="workload.py")
    request.args.append("\x00")
    with pytest.raises(ValidationError):
        module.build_profile_argv(request, "a" * 64)


def test_recipe_imports_stdlib_before_any_project_module(tmp_path):
    (tmp_path / "cProfile.py").write_text("raise RuntimeError('project shadowed profiler before source admission')")
    result = run_recipe(tmp_path, "print('actual workload')\n")
    assert result.returncode == 0, result.stderr
    assert "actual workload" in result.stdout and "FORGE_CPROFILE_V1" in result.stdout


def test_early_workload_exit_stays_an_observation_without_a_profile_report(tmp_path):
    result = run_recipe(tmp_path, "import os\nos._exit(23)\n")
    assert result.returncode == 23 and "FORGE_CPROFILE_V1" not in result.stdout


def test_profiler_output_failure_cannot_replace_the_workload_exit(tmp_path):
    result = run_recipe(tmp_path, "import sys\nsys.stdout.close()\nraise SystemExit(7)\n")
    assert result.returncode == 7


def test_recipe_does_not_create_project_import_caches(tmp_path):
    (tmp_path / "owned_helper.py").write_text("VALUE=4\n")
    result = run_recipe(tmp_path, "from owned_helper import VALUE\nprint(VALUE)\n")
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in tmp_path.iterdir()) == ["owned_helper.py", "workload.py"]
