# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The sealed source agent sees current lifetime capabilities without renewed authority."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from firefly_forge import source_edit_continuations as module
from firefly_forge.prompts import prompt_text


async def test_initial_capability_view_rechecks_admission_and_counts_existing_rows(monkeypatch):
    from firefly_forge import source_budget_allowances

    run = {"id": uuid4().hex, "tool_policy_snapshot": {"settings": {"calls_limit": 30, "per_tool_limit": 10}}}
    calls = []

    async def receipt(r, current, *, task_key):
        assert current is run and task_key == "existing-key"
        calls.append("admission")
        return {}

    async def get_run(identifier):
        assert identifier == run["id"]
        return run

    async def for_run(identifier):
        return [
            SimpleNamespace(tool=n, status="succeeded")
            for n in ["read_file"] * 10 + ["list_files"] * 2 + ["search_text"]
        ]

    async def limits(store, journal, current, rows):
        assert current is run and len(rows) == 13
        return {"list_files": 10, "read_file": 20, "search_text": 10}, {"protocol": "source-budget-continuation-v1"}

    @asynccontextmanager
    async def unit():
        yield None

    monkeypatch.setattr(module, "source_edit_receipt", receipt)
    monkeypatch.setattr(source_budget_allowances, "effective_source_read_limits", limits)
    r = SimpleNamespace(
        store=SimpleNamespace(get_run=get_run, _unit=unit),
        journal=object(),
        engine=SimpleNamespace(tool_broker=SimpleNamespace(records=SimpleNamespace(for_run=for_run))),
    )
    result = await module.SourceEditInvocation(r, run["id"], "existing-key").capabilities()
    assert result["total"] == {"used": 13, "limit": 30, "remaining": 17}
    assert result["tools"]["read_file"] == {"used": 10, "limit": 20, "remaining": 10}
    assert calls == ["admission", "admission"]


def test_new_prompt_explains_definite_limits_unknowns_and_sealed_guidance_boundary():
    text = prompt_text(
        "implementation/source-edit-only",
        version="1.0.2",
        calls_limit="30",
        per_tool_limit="10",
        command_limit="35",
        cli_commands="21",
        native_commands="7",
        final_checks="7",
        capabilities=json.dumps({"total": {"used": 13, "remaining": 17}}),
    )
    assert '"used": 13' in text and "smaller" in text
    assert "tool_budget_exhausted" in text and "Unknown" in text
    assert "durable" in text and "in-flight" in text
    assert "35" in text and "7" in text
