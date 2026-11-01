# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native request history has a stable cursor without imposing a total-history limit."""

from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from firefly_forge.execution_request_persistence import ForgeExecutionRequestRepository


async def test_native_request_history_uses_an_explicit_uuid_cursor_without_a_total_cap(monkeypatch):
    statements = []

    class Session:
        async def scalars(self, statement):
            statements.append(statement)
            return []

    session = Session()
    repository = ForgeExecutionRequestRepository(session=session)
    monkeypatch.setattr(repository, "_require_session", lambda: session)
    run_id, after = UUID("1" * 32), UUID("2" * 32)
    assert await repository.for_run_page(run_id, "linked_verify", after=after, limit=64) == []
    compiled = statements[-1].compile(dialect=postgresql.dialect())
    assert "forge_execution_requests.id >" in str(compiled)
    assert "ORDER BY forge_execution_requests.id" in str(compiled)
    assert run_id in compiled.params.values() and after in compiled.params.values()
    assert "linked_verify" in compiled.params.values() and 64 in compiled.params.values()
    assert await repository.for_run_page(run_id, "linked_verify", after=None, limit=64) == []
    assert "forge_execution_requests.id >" not in str(statements[-1].compile(dialect=postgresql.dialect()))
    for limit in (True, 0, -1):
        with pytest.raises(ValueError):
            await repository.for_run_page(run_id, "linked_verify", after=after, limit=limit)
    assert len(statements) == 2
