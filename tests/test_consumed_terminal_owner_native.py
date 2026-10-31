# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reconciliation reviews the physical PostgreSQL lease, including backend birth."""

import pytest
from sqlalchemy import text


async def test_native_owner_identity_and_revoked_advisory_lease(store):
    await store.acquire_ownership()
    identity = store.execution_ownership_identity
    assert identity["backend_pid"] == store._ownership_pid
    assert isinstance(identity["backend_start"], str) and identity["backend_start"]
    await store.assert_ownership()
    connection = store.execution_ownership_token
    await connection.scalar(
        text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"), {"key": "forge:execution:" + store.schema}
    )
    await connection.commit()
    with pytest.raises(RuntimeError, match="ownership was lost"):
        await store.assert_ownership()


@pytest.mark.parametrize("inside_owned_unit", [False, True])
async def test_native_owner_backend_birth_is_fenced(store, inside_owned_unit):
    await store.acquire_ownership()
    original = store.execution_ownership_identity
    try:
        if inside_owned_unit:
            async with store.source_budget_admission_unit():
                store._ownership_backend_start = "1900-01-01 00:00:00+00"
                with pytest.raises(RuntimeError, match="execution lease"):
                    await store.assert_ownership()
                store._ownership_backend_start = original["backend_start"]
        else:
            store._ownership_backend_start = "1900-01-01 00:00:00+00"
            with pytest.raises(RuntimeError, match="ownership was lost"):
                await store.assert_ownership()
    finally:
        store._ownership_backend_start = original["backend_start"]
