# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recorded diagnostic tails remain bounded, explicit and privacy filtered."""

import copy

import pytest

from firefly_forge.communication_diagnostics import check_diagnostics, render_excerpt


def recorded(output):
    return {
        "attempt": 1,
        "final_verification": [{"argv": ["python", "-m", "pytest"], "exit_code": 1, "attempt": 1, "output": output}],
    }


@pytest.mark.parametrize("output", ["\x00\x01", "\u202e \u2066"], ids=["control-bytes", "bidi-controls"])
def test_filtered_empty_output_is_not_offered_as_an_empty_code_fence(output):
    run = recorded(output)
    before = copy.deepcopy(run)
    diagnostics = check_diagnostics(run)
    assert "check-1-output" not in diagnostics
    assert diagnostics["check-1-command"]["text"] == "python -m pytest"
    assert run == before


@pytest.mark.parametrize("padding", ["", "\n" * 2000], ids=["ordinary-ending", "trailing-empty-lines"])
def test_long_output_preserves_its_final_recorded_failure_line(padding):
    output = "fixture setup banner\n" * 150 + "E assert 500 == 422\n" + padding
    run = recorded(output)
    before = copy.deepcopy(run)
    excerpt = check_diagnostics(run)["check-1-output"]
    assert excerpt["text"].endswith("E assert 500 == 422")
    assert excerpt["text"].splitlines()[0] == "fixture setup banner"
    assert excerpt["text"] in output and len(excerpt["text"]) <= 1800
    assert excerpt["truncated"] is True
    assert render_excerpt(excerpt) == "[excerpt truncated]\n\n```text\n" + excerpt["text"] + "\n```"
    assert run == before


def test_overlong_single_line_keeps_only_recorded_tail_bytes_inside_the_fence():
    output = "x" * 2000 + " final recorded failure"
    excerpt = check_diagnostics(recorded(output))["check-1-output"]
    assert excerpt["text"] == output[-1800:]
    assert excerpt["truncated"] is True
    assert render_excerpt(excerpt).startswith("[excerpt truncated]\n\n```text\n")


def test_short_output_retains_its_recorded_bytes_without_a_truncation_notice():
    output = "E assert 500 == 422\n"
    excerpt = check_diagnostics(recorded(output))["check-1-output"]
    assert excerpt["text"] == output and excerpt["truncated"] is False
    assert render_excerpt(excerpt) == "```text\n" + output + "\n```"


def test_a_long_command_still_identifies_its_recorded_executable():
    run = recorded("E final failure")
    run["final_verification"][0]["argv"].append("x" * 2000)
    before = copy.deepcopy(run)
    excerpt = check_diagnostics(run)["check-1-command"]
    assert excerpt["text"].startswith("python -m pytest ")
    assert len(excerpt["text"]) <= 1800 and excerpt["truncated"] is True
    assert render_excerpt(excerpt).startswith("[excerpt truncated]\n\n```text\npython -m pytest ")
    assert run == before


def test_privacy_checks_run_before_any_tail_selection():
    run = recorded("api_key=unregistered-private-fixture\n" + "banner\n" * 400 + "E final failure")
    diagnostics = check_diagnostics(run)
    assert "check-1-output" not in diagnostics
    assert "check-1-command" in diagnostics


def test_an_api_looking_path_in_unstructured_output_is_not_proof_that_it_is_public():
    excerpt = check_diagnostics(recorded("GET /api/v1/items returned 500"))["check-1-output"]
    assert excerpt["text"] == "GET [local path omitted] returned 500"
