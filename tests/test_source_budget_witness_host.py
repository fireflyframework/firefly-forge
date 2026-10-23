# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Ordinary CI owns witness output without borrowing production qualification."""

import os
from pathlib import Path

import pytest
import test_source_budget_native as native
import test_source_budget_output_native as output


def test_output_native_cases_register_the_shared_controlled_witness_fixture():
    assert output.measured_witness_directory is native.measured_witness_directory


@pytest.mark.parametrize("supervised", [False, True])
def test_controlled_witness_directory_preserves_explicit_supervisor_or_owns_temporary_output(
    tmp_path, monkeypatch, supervised
):
    monkeypatch.delenv("NATIVE_SQL_FIXTURE", raising=False)
    existing = tmp_path / "supervisor"
    if supervised:
        (existing / "logs").mkdir(parents=True)
        (existing / "logs" / "retained.jsonl").write_text("retained public fixture\n")
        monkeypatch.setenv("NATIVE_SQL_FIXTURE", str(existing))
    native.measured_witness_directory.__wrapped__(tmp_path, monkeypatch)
    actual = Path(os.environ["NATIVE_SQL_FIXTURE"])
    assert actual == (existing if supervised else tmp_path / "native-sql-witnesses")
    assert (actual / "logs").is_dir()
    if supervised:
        assert (actual / "logs" / "retained.jsonl").read_text() == "retained public fixture\n"
    else:
        assert list((actual / "logs").iterdir()) == []
