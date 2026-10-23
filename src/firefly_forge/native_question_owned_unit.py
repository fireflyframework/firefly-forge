# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PyFly units bound to the existing PostgreSQL execution lease."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pyfly.data.relational.sqlalchemy import SessionProvider
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import current_unit_of_work
from pyfly.data.transaction.registry import TransactionManagerRegistry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from firefly_forge.store import Store


class QuestionLeaseTransactionManager(SqlAlchemyTransactionManager):
    def __init__(self, engine: AsyncEngine, connection: AsyncConnection) -> None:
        if connection.engine is not engine:
            raise ValueError("The lease connection belongs to another native engine")
        self._lease_engine = engine
        super().__init__(
            sessionmaker=async_sessionmaker(bind=connection, expire_on_commit=False),
            name="primary",
        )

    @property
    def engine(self) -> AsyncEngine:
        # Native units need engine capabilities while their sessions stay on the existing lease.
        return self._lease_engine


@asynccontextmanager
async def question_owned_unit(
    store: Store, ownership: object, *, complete_during_drain: bool = False
) -> AsyncIterator[AsyncSession]:
    if current_unit_of_work() is not None:
        raise ValueError("Question notifications must run outside a caller transaction")
    async with store._ownership_lock:
        if ownership is not store.execution_ownership_token:
            raise RuntimeError("PostgreSQL execution ownership was lost")
        await store._assert_ownership()
        if ownership is not store.execution_ownership_token:
            raise RuntimeError("PostgreSQL execution ownership changed during admission")
        connection = store._ownership_connection
        engine = store._engine
        assert connection is not None and engine is not None
        registry = TransactionManagerRegistry()
        registry.register(QuestionLeaseTransactionManager(engine, connection), default=True)
        # The named native unit makes nested repositories join without changing the installed registry.
        async with SessionProvider(registry).unit() as session:
            actual = await session.connection()
            if actual.sync_connection is not connection.sync_connection:
                raise RuntimeError("The native effect unit changed its lease connection")
            async with store._unit() as nested:
                if nested is not session:
                    raise RuntimeError("The native repository unit changed its lease session")
            yield session
            current = store._ownership_connection if complete_during_drain else store.execution_ownership_token
            if (
                current is not ownership
                or connection.closed
                or connection.invalidated
                or not await session.scalar(
                    text(
                        "SELECT pg_backend_pid() = :pid AND EXISTS ("
                        "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
                        "AND pid = pg_backend_pid() AND objsubid = 1 "
                        "AND classid::bigint = ((hashtextextended(:key, 0) >> 32) & 4294967295) "
                        "AND objid::bigint = (hashtextextended(:key, 0) & 4294967295))"
                    ),
                    {"key": "forge:execution:" + store.schema, "pid": store._ownership_pid},
                )
            ):
                raise RuntimeError("PostgreSQL execution ownership was lost before effect custody committed")
