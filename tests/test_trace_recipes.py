# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual target execution proves bounded observations, not trusted coverage."""

import hashlib
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from firefly_forge import diagnostic_recipes as recipes


def request(**fields):
    assert hasattr(recipes, "PythonTraceRequest"), "The optional line-tracing recipe is missing"
    return recipes.PythonTraceRequest(script="workload.py", **fields)


def launch(tmp_path, source, **fields):
    target = tmp_path / "workload.py"
    target.write_text(source)
    value = request(**fields)
    argv = recipes.build_trace_argv(value, hashlib.sha256(target.read_bytes()).hexdigest())
    return subprocess.run([sys.executable, *argv[1:]], cwd=tmp_path, capture_output=True, text=True, timeout=5)


def report(result):
    return json.loads(result.stdout.split("FORGE_PYTHON_TRACE_V1\n", 1)[1])


def test_trace_observes_executed_lines_and_loop_hits_without_report_files(tmp_path):
    source = (
        "value = 0\nfor i in range(3):\n    value += i\n"
        "if value < 0:\n    raise AssertionError('not executed')\nprint(value)\n"
    )
    result = launch(tmp_path, source)
    assert result.returncode == 0 and result.stdout.startswith("3\n")
    counts = {row["line"]: row["hits"] for row in report(result)["lines"]}
    assert counts[3] == 3 and counts[6] == 1 and 5 not in counts
    assert report(result)["best_effort"] is True
    assert sorted(path.name for path in tmp_path.iterdir()) == ["workload.py"]


def test_trace_filters_imported_code_even_with_a_matching_filename(tmp_path):
    (tmp_path / "helper.py").write_text("exec(compile('x=1\\ny=2\\n','workload.py','exec'))\n")
    result = launch(tmp_path, "import helper\ndef owned():\n    return 7\nprint(owned())\n")
    counts = {row["line"]: row["hits"] for row in report(result)["lines"]}
    assert result.returncode == 0 and counts[1] == 1 and counts[2] == 1 and counts[3] == 1


@pytest.mark.parametrize("ending,code", [("raise SystemExit(7)", 7), ("raise ValueError('owned failure')", 1)])
def test_trace_preserves_workload_failure_and_still_observes_lines(tmp_path, ending, code):
    result = launch(tmp_path, "print('before failure')\n" + ending + "\n")
    assert result.returncode == code and result.stdout.startswith("before failure\n")
    assert report(result)["lines"] == [{"line": 1, "hits": 1}, {"line": 2, "hits": 1}]


def test_trace_caps_distinct_rows_and_marks_the_partial_observation(tmp_path):
    result = launch(tmp_path, "a=1\nb=2\nc=3\n", max_rows=2)
    assert report(result)["lines"] == [{"line": 1, "hits": 1}, {"line": 2, "hits": 1}]
    assert report(result)["rows_truncated"] is True


def test_trace_is_best_effort_when_the_target_disables_tracing(tmp_path):
    result = launch(tmp_path, "import sys\nsys.settrace(None)\na=1\n")
    assert result.returncode == 0 and report(result)["best_effort"] is True
    assert 3 not in {row["line"] for row in report(result)["lines"]}


def test_trace_passes_literal_arguments_without_a_shell(tmp_path):
    result = launch(
        tmp_path, "import sys,json\nprint(json.dumps(sys.argv[1:]))\n", args=["two words", "; touch unexpected"]
    )
    assert json.loads(result.stdout.splitlines()[0]) == ["two words", "; touch unexpected"]
    assert not (tmp_path / "unexpected").exists()


@pytest.mark.parametrize(
    "fields",
    [
        {"max_rows": True},
        {"timeout": False},
        {"max_rows": 101},
        {"timeout": 121},
        {"max_rows": 0},
        {"image": "untrusted"},
        {"recipe": "other"},
        {"args": [""]},
    ],
)
def test_trace_rejects_overrides_and_non_strict_bounds(fields):
    with pytest.raises(ValidationError):
        request(**fields)


@pytest.mark.parametrize("path", ["../outside.py", "/outside.py", ".git/run.py", ".env/a.py", "a\\b.py", "a.json"])
def test_trace_rejects_private_and_unconfined_targets(path):
    request()
    with pytest.raises(ValidationError):
        recipes.PythonTraceRequest(script=path)


@pytest.mark.parametrize("unsafe", ["changed", "symlink", "hardlink"])
def test_trace_rechecks_source_identity_before_execution(tmp_path, unsafe):
    target = tmp_path / "workload.py"
    source = "open('unexpected','w').write('executed')\n"
    target.write_text(source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    value = request()
    argv = recipes.build_trace_argv(value, digest)
    if unsafe == "changed":
        target.write_text(source + "# changed\n")
    elif unsafe == "symlink":
        target.rename(tmp_path / "real.py")
        target.symlink_to(tmp_path / "real.py")
    else:
        (tmp_path / "other.py").hardlink_to(target)
    result = subprocess.run([sys.executable, *argv[1:]], cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and not (tmp_path / "unexpected").exists()


def test_trace_imports_stdlib_before_admitting_project_imports(tmp_path):
    (tmp_path / "json.py").write_text("raise AssertionError('shadowed stdlib')\n")
    result = launch(tmp_path, "print('owned')\n")
    assert result.returncode == 0 and report(result)["lines"] == [{"line": 1, "hits": 1}]


@pytest.mark.parametrize("ending", ["replacement", "audit"])
def test_best_effort_trace_cleanup_cannot_mask_a_known_workload_exit(tmp_path, ending):
    source = "import sys\ndef refuse(*args):\n    raise RuntimeError('owned trace cleanup refusal')\n"
    source += (
        "sys.settrace=refuse\n"
        if ending == "replacement"
        else ("def audit(event,args):\n    if event == 'sys.settrace':\n        refuse()\nsys.addaudithook(audit)\n")
    )
    result = launch(tmp_path, source + "raise SystemExit(7)\n")
    assert result.returncode == 7
    assert report(result)["best_effort"] is True


def test_best_effort_report_cannot_replace_the_workload_exit(tmp_path):
    result = launch(
        tmp_path,
        "import builtins\ndef refuse(*args,**kwargs):\n    raise SystemExit(9)\n"
        "builtins.print=refuse\nraise SystemExit(7)\n",
    )
    assert result.returncode == 7
