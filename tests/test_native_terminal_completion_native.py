# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared PostgreSQL terminal acceptance; controlled SDK, no provider account."""

import asyncio

import pytest
from test_known_worker_completion_native import known_native

from firefly_forge.native_terminal_completion_models import validate_native_receipt


@pytest.mark.parametrize("normal", [False, True])
async def test_native_automatic_acceptance_and_observed_hold_commit_together(store, tmp_path, monkeypatch, normal):
    async with known_native(store, tmp_path, monkeypatch, automatic=True, normal=normal) as c:
        run = await store.get_run(c.run_id)
        entries = await c.journal.entries(c.run_id)
        assert not run["bridge_completion_hold"], run.get("error")
        receipts = [entry for entry in entries if entry["kind"] == "terminal_completion_receipt"]
        assert len(receipts) == 1
        receipt = validate_native_receipt(receipts[0], entries)
        assert receipt.producer == ("native_implementation_receipt" if normal else "native_worker_feedback")
        if not normal:
            assert run["status"] == "paused" and run["worker_request"]["status"] == "pending"
        assert len(c.posts) == 2


@pytest.mark.parametrize("normal", [False, True])
async def test_native_terminal_receipt_failure_rolls_back_real_acceptance_and_hold(
    store, tmp_path, monkeypatch, normal
):
    from firefly_forge.journal import Journal

    append = Journal._append

    async def refuse(journal, run, task, key, kind, payload):
        if kind == "terminal_completion_receipt":
            raise RuntimeError("Controlled native terminal insert rollback")
        return await append(journal, run, task, key, kind, payload)

    monkeypatch.setattr(Journal, "_append", refuse)
    async with known_native(store, tmp_path, monkeypatch, automatic=True, normal=normal) as c:
        run = await store.get_run(c.run_id)
        entries = await c.journal.entries(c.run_id)
        assert run["bridge_completion_hold"]
        assert not any(entry["kind"] in {"terminal_completion_receipt", "worker_feedback"} for entry in entries)
        assert not any(entry["kind"] == "receipt" and entry["task_id"] == "implement" for entry in entries)
        assert len(c.posts) == 2


async def test_native_competing_duplicate_acceptance_keeps_one_terminal_receipt(store, tmp_path, monkeypatch):
    async with known_native(store, tmp_path, monkeypatch, automatic=True, normal=True) as c:
        entries = await c.journal.entries(c.run_id)
        receipt = next(entry for entry in entries if entry["kind"] == "terminal_completion_receipt")
        accepted = next(
            entry for entry in entries if entry["digest"] == receipt["payload"]["proof"]["acceptance_record_digest"]
        )
        outcomes = await asyncio.gather(
            *(
                c.journal.receipt(
                    c.run_id,
                    accepted["idempotency_key"],
                    accepted["payload"],
                    checkpoint=receipt["payload"]["proof"]["source_checkpoint"],
                )
                for _ in range(2)
            )
        )
        assert outcomes == [accepted, accepted]
        assert (
            len(
                [entry for entry in await c.journal.entries(c.run_id) if entry["kind"] == "terminal_completion_receipt"]
            )
            == 1
        )
        assert len(c.posts) == 2
