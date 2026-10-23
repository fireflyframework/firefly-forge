# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The independent native observer uses the configured dedicated test database."""

from uuid import UUID

import pytest
from test_source_index_sanitation_native import _committed_index_intents

from firefly_forge.source_index_sanitation import SANITATION_INTENT


@pytest.mark.parametrize(
    "url,host,port",
    [
        ("postgresql+asyncpg://public:fixture@127.0.0.1:5432/forge_test", "127.0.0.1", 5432),
        ("postgresql+asyncpg://public:fixture@127.0.0.1:61432/forge_test", "127.0.0.1", 61432),
        ("postgresql+asyncpg://public:fixture@postgres:5432/forge_ci_test", "postgres", 5432),
        ("postgresql+asyncpg://public:fixture@postgres/forge_ci_test", "postgres", None),
    ],
)
async def test_committed_observer_uses_configured_dedicated_database(monkeypatch, url, host, port):
    monkeypatch.setenv("FORGE_TEST_DATABASE_URL", url)
    calls, closed = [], []

    class Connection:
        async def fetchval(self, sql, *args):
            assert sql == (
                'SELECT count(*) FROM "forge_test_public".forge_journal '
                "WHERE run_id=$1 AND idempotency_key=$2 AND kind=$3"
            )
            assert args == (UUID("a" * 32), "b" * 32, SANITATION_INTENT)
            return 1

        async def close(self):
            closed.append(True)

    async def connect(**kwargs):
        calls.append(kwargs)
        return Connection()

    monkeypatch.setattr("test_source_index_sanitation_native.asyncpg.connect", connect)
    assert await _committed_index_intents("forge_test_public", "a" * 32, "b" * 32) == 1
    assert calls == [
        {
            "host": host,
            "port": port,
            "user": "public",
            "password": "fixture",
            "database": url.rsplit("/", 1)[1],
            "timeout": 3,
        }
    ]
    assert closed == [True]


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://public:fixture@127.0.0.1:5432/production",
        "postgresql+asyncpg://public:fixture@127.0.0.1:61432/production",
        "postgresql+asyncpg://public:fixture@postgres:5432/",
        "mysql://public:fixture@127.0.0.1:61432/forge_test",
    ],
)
async def test_committed_observer_refuses_non_test_database_before_connect(monkeypatch, url):
    monkeypatch.setenv("FORGE_TEST_DATABASE_URL", url)

    async def forbidden(**kwargs):
        raise RuntimeError("A refused database must never be contacted")

    monkeypatch.setattr("test_source_index_sanitation_native.asyncpg.connect", forbidden)
    with pytest.raises(AssertionError):
        await _committed_index_intents("forge_test_public", "a" * 32, "b" * 32)


async def test_committed_observer_refuses_invalid_schema_before_connect(monkeypatch):
    monkeypatch.setenv("FORGE_TEST_DATABASE_URL", "postgresql+asyncpg://public:fixture@postgres:5432/forge_test")

    async def forbidden(**kwargs):
        raise RuntimeError("An invalid schema must never be queried")

    monkeypatch.setattr("test_source_index_sanitation_native.asyncpg.connect", forbidden)
    with pytest.raises(AssertionError):
        await _committed_index_intents('forge_test_public";SELECT 1', "a" * 32, "b" * 32)
