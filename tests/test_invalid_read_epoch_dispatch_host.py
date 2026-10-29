# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Independent epoch-dispatch regression, not worker/native acceptance proof."""

from types import SimpleNamespace

pytest_plugins = ["test_invalid_read_epochs_host"]


async def test_worker_dispatch_uses_exact_reviewed_retained_sdk_epoch(legacy_epoch_case):
    from firefly_forge.invalid_read_continuity import RETAINED_SDK_RUNTIME_CONTRACT
    from firefly_forge.invalid_read_epochs import epoch_snapshot
    from firefly_forge.invalid_read_tickets import InvalidReadTickets

    c = legacy_epoch_case
    epoch = epoch_snapshot(
        c.store,
        await c.store.get_run(c.run_id),
        await c.journal.entries(c.run_id),
        legacy_runtime_contract=RETAINED_SDK_RUNTIME_CONTRACT,
    )
    assert epoch["legacy_runtime_contract"] == RETAINED_SDK_RUNTIME_CONTRACT
    ticket = {"run_id": c.run_id, "policy_epoch": epoch}
    entered = []

    async def owned(value):
        entered.append(value)

    service = SimpleNamespace(store=c.store, journal=c.journal, _execute_owned=owned)
    await InvalidReadTickets._execute(service, ticket)
    assert entered == [ticket]
