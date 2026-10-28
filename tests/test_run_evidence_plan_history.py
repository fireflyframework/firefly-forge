# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native-shaped immutable plan history under a read-only repeatable-read cut."""

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_run_evidence import REPO, RUN, add_entry, sample

from firefly_forge import run_evidence as m
from firefly_forge.journal import digest
from firefly_forge.persistence import (
    ForgeEventEntity,
    ForgeJournalEntity,
    ForgePlanEntity,
    ForgeRunEntity,
    ForgeTaskEntity,
)
from firefly_forge.tool_persistence import ForgeToolInvocationEntity


def native_cut():
    data = sample()
    plan = {k: v for k, v in data["plan"].items() if k != "digest"}
    plan.update(
        agent="bridge",
        config_revision="1" * 32,
        execution_config={"model": "fictional"},
        model_selection=None,
        review_model_selection=None,
    )
    first_digest = digest(plan)
    data["journal"][0]["payload"]["digest"] = first_digest
    data["journal"][0]["digest"] = digest({k: v for k, v in data["journal"][0].items() if k != "digest"})
    transition = {
        "mode": "legacy_provider",
        "previous_agent": "bridge",
        "agent": "bridge",
        "previous_config_revision": "1" * 32,
        "config_revision": "2" * 32,
        "previous_model_selection": None,
        "model_selection": None,
    }
    second = {
        **copy.deepcopy(plan),
        "revision": 2,
        "config_revision": "2" * 32,
        "execution_config": {"model": "new-fictional"},
        "selection_transition": transition,
    }
    add_entry(
        data,
        "handoff",
        "9" * 32,
        {
            **{k: v for k, v in transition.items() if k != "mode"},
            "selection_mode": transition["mode"],
            "automatic": False,
        },
    )
    data["journal"][-1]["task_id"] = "handoff"
    data["journal"][-1]["digest"] = digest({k: v for k, v in data["journal"][-1].items() if k != "digest"})
    data["run"].update(
        agent="bridge", config_revision="2" * 32, execution_config=second["execution_config"], model_selection=None
    )
    plans = [
        SimpleNamespace(id=UUID(int=n), run_id=UUID(RUN), revision=n, digest=digest(body), data=body)
        for n, body in ((1, plan), (2, second))
    ]
    tasks = [
        SimpleNamespace(
            id=UUID(int=10),
            run_id=UUID(RUN),
            plan_id=plans[0].id,
            task_id="prepare",
            attempt=1,
            status="completed",
            data={"idempotency_key": "3" * 32},
        )
    ]
    return data, plans, tasks


def rehash(data):
    previous = "0" * 64
    for index, row in enumerate(data["journal"], 1):
        row.update(sequence=index, previous_hash=previous)
        row["digest"] = digest({k: v for k, v in row.items() if k != "digest"})
        previous = row["digest"]


async def capture(monkeypatch, data, plans, tasks):
    observations = []

    class Transaction:
        def __init__(self, manager, **kwargs):
            observations.append(kwargs)

        @asynccontextmanager
        async def transaction(self):
            yield

    @asynccontextmanager
    async def unit():
        yield object()

    monkeypatch.setattr(m, "TransactionTemplate", Transaction)
    monkeypatch.setattr(m, "current_unit_of_work", lambda: SimpleNamespace(manager=object()))
    service = m.RunEvidence(SimpleNamespace(_unit=unit))
    table = {
        ForgeRunEntity: [
            SimpleNamespace(id=UUID(RUN), repository_id=UUID(REPO), status=data["run"]["status"], data=data["run"])
        ],
        ForgePlanEntity: plans,
        ForgeTaskEntity: tasks,
        ForgeToolInvocationEntity: [],
        ForgeEventEntity: [
            SimpleNamespace(id=UUID(row["id"]), sequence=row["sequence"], data=row["data"]) for row in data["activity"]
        ],
        ForgeJournalEntity: [SimpleNamespace(**row, data=row) for row in data["journal"]],
    }

    async def rows(session, statement, limit, remaining):
        entity = statement.column_descriptions[0]["entity"]
        result = table[entity]
        if entity is ForgePlanEntity:
            result = sorted(
                result, key=lambda row: row.revision, reverse=any("DESC" in str(x) for x in statement._order_by_clauses)
            )
            if statement._limit_clause is not None:
                result = result[: statement._limit_clause.value]
        if len(result) > limit:
            raise m.EvidenceUnavailable("evidence_limit")
        return result

    monkeypatch.setattr(service, "_rows", rows)
    result = await service._snapshot(RUN)
    assert len(observations) == 1
    assert observations[0]["read_only"] is True and observations[0]["timeout"] == 10
    assert observations[0]["isolation"] == m.Isolation.REPEATABLE_READ
    return result


async def test_handoff_revision_keeps_original_task_foreign_key_and_strict_plan_anchor(monkeypatch):
    data, plans, tasks = native_cut()
    before = copy.deepcopy((data, [r.data for r in plans], tasks[0].plan_id))
    snapshot = await capture(monkeypatch, data, plans, tasks)
    assert snapshot.manifest["plan_digest"] == plans[0].digest
    assert snapshot.manifest["plan_history"]["latest_observed"]["digest"] == plans[1].digest
    history = snapshot.members["plans.json"]
    assert history["task_plan_bindings"][0]["plan_id"] == plans[0].id.hex
    assert history["plans"][0]["provenance"] == "immutable_wal_plan"
    assert history["plans"][1]["provenance"] == "observed_native_plan_with_selection_handoff"
    assert history["plans"][1]["full_plan_digest_in_wal"] is False
    assert snapshot.members["journal.json"] == data["journal"]
    assert (data, [r.data for r in plans], tasks[0].plan_id) == before


async def test_single_plan_output_is_exact_legacy_projection(monkeypatch):
    data, plans, tasks = native_cut()
    data["journal"] = data["journal"][:1]
    plans = plans[:1]
    snapshot = await capture(monkeypatch, data, plans, tasks)
    expected = m.build_evidence(
        run=data["run"],
        plan={**plans[0].data, "digest": plans[0].digest},
        journal=data["journal"],
        tasks=[{"id": "prepare", "attempt": 1, "observed_status": "completed", "data": tasks[0].data}],
        tools=[],
        activity=data["activity"],
    )
    assert snapshot == expected


@pytest.mark.parametrize(
    "change",
    [
        "missing_root",
        "duplicate",
        "native_digest",
        "native_revision",
        "foreign_plan_run",
        "foreign_task_plan",
        "foreign_task_run",
        "changed_contract",
        "changed_transition",
        "missing_handoff",
        "changed_handoff",
        "ambiguous_handoff",
    ],
)
async def test_missing_changed_foreign_or_ambiguous_native_lineage_is_held(monkeypatch, change):
    data, plans, tasks = native_cut()
    if change == "missing_root":
        plans = plans[1:]
    elif change == "duplicate":
        plans.append(copy.deepcopy(plans[-1]))
    elif change == "native_digest":
        plans[-1].digest = "f" * 64
    elif change == "native_revision":
        plans[-1].revision = 3
    elif change == "foreign_plan_run":
        plans[-1].run_id = UUID(int=99)
    elif change == "foreign_task_plan":
        tasks[0].plan_id = UUID(int=99)
    elif change == "foreign_task_run":
        tasks[0].run_id = UUID(int=99)
    elif change == "changed_contract":
        plans[-1].data["brief"] = "unrecorded task change"
        plans[-1].digest = digest(plans[-1].data)
    elif change == "changed_transition":
        plans[-1].data["selection_transition"]["previous_config_revision"] = "f" * 32
        plans[-1].digest = digest(plans[-1].data)
    elif change == "missing_handoff":
        data["journal"][-1]["kind"] = "unrelated"
        rehash(data)
    elif change == "changed_handoff":
        data["journal"][-1]["payload"]["config_revision"] = "f" * 32
        rehash(data)
    else:
        data["journal"].append(copy.deepcopy(data["journal"][-1]))
        rehash(data)
    with pytest.raises(m.EvidenceUnavailable, match="evidence_integrity"):
        await capture(monkeypatch, data, plans, tasks)


async def test_observed_plan_history_never_exports_raw_execution_configuration(monkeypatch):
    data, plans, tasks = native_cut()
    plans[-1].data["execution_config"]["credential"] = "NEVER-EXPORT-THIS-CONFIGURATION"
    plans[-1].digest = digest(plans[-1].data)
    snapshot = await capture(monkeypatch, data, plans, tasks)
    assert "NEVER-EXPORT-THIS-CONFIGURATION" not in json.dumps(snapshot.manifest)
    assert "NEVER-EXPORT-THIS-CONFIGURATION" not in json.dumps(snapshot.members)


@pytest.mark.parametrize("count", [4, 129])
async def test_repeated_identical_selection_changes_have_one_ordered_history(monkeypatch, count):
    data, plans, tasks = native_cut()
    for revision in range(3, count + 1):
        previous = plans[-1]
        transition = {
            **previous.data["selection_transition"],
            "previous_config_revision": previous.data["config_revision"],
        }
        body = {**copy.deepcopy(previous.data), "revision": revision, "selection_transition": transition}
        plans.append(
            SimpleNamespace(id=UUID(int=revision), run_id=UUID(RUN), revision=revision, digest=digest(body), data=body)
        )
        add_entry(
            data,
            "handoff",
            UUID(int=1000 + revision).hex,
            {
                **{k: v for k, v in transition.items() if k != "mode"},
                "selection_mode": transition["mode"],
                "automatic": False,
            },
        )
        data["journal"][-1]["task_id"] = "handoff"
        rehash(data)
    snapshot = await capture(monkeypatch, data, plans, tasks)
    assert snapshot.manifest["plan_history"]["count"] == count
    assert [row["sequence"] for row in snapshot.members["plans.json"]["plans"]] == list(range(1, count + 1))
    assert snapshot.manifest["limits"]["plans"] == snapshot.manifest["limits"]["journal"]


@pytest.mark.parametrize("source", ["runtime", "admission"])
def test_plan_history_security_helper_is_in_both_source_custody_closures(monkeypatch, source):
    from pathlib import Path

    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.source_budget_allowances import admission_contract_digest

    current = runtime_contract if source == "runtime" else admission_contract_digest
    before = current()
    original = Path.read_bytes

    def changed(path):
        raw = original(path)
        return raw + b"\n# controlled mutation\n" if path.name == "run_evidence_plans.py" else raw

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert current() != before
