# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio

from firefly_forge import github_projects_worker as module


async def test_business_sync_has_no_inherited_ninety_second_work_deadline(monkeypatch):
    worker = object.__new__(module.GitHubProjectsWorker)
    calls = []

    async def blocked(request_id):
        calls.append(("dispatch", request_id))
        await asyncio.sleep(0.02)
        calls.append(("completed", request_id))

    async def finish(request_id, status, result):
        calls.append((status, request_id, result))

    monkeypatch.setattr(module, "REQUEST_TIMEOUT", 0.001, raising=False)
    worker._execute = blocked
    worker.finish = finish
    await worker.execute("1" * 32)
    assert [call[0] for call in calls] == ["dispatch", "completed"]
