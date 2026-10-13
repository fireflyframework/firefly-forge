# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Each database test owns a fresh schema on an explicitly configured test database."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def postgres_schema(monkeypatch):
    url = os.environ.get("FORGE_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Set FORGE_TEST_DATABASE_URL to an isolated PostgreSQL test database")
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").endswith("_test"):
        pytest.fail("FORGE_TEST_DATABASE_URL must name a dedicated PostgreSQL database ending in _test")
    schema = "forge_test_" + uuid.uuid4().hex
    monkeypatch.setenv("FORGE_DATABASE_URL", url)
    monkeypatch.setenv("FORGE_DATABASE_SCHEMA", schema)
    yield schema
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    assert re.fullmatch(r"forge_test_[a-f0-9]{32}", schema)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def store_factory(postgres_schema):
    from firefly_forge.store import Store

    stores = []

    async def create(path: Path):
        store = Store(path)
        await store.initialize()
        stores.append(store)
        return store

    yield create
    for store in reversed(stores):
        await store.close()


@pytest_asyncio.fixture
async def store(tmp_path, store_factory):
    return await store_factory(tmp_path / "state" / "factory.db")
