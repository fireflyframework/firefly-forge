# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real SQL reader receipts and budgets; Git/source only, no real coding provider."""

import hashlib
from uuid import uuid4

import pytest
from test_source_edit_native import activation, prepared, started
from worker_reply_fixtures import authenticated_operator

from firefly_forge.source_edit_continuations import SourceEditInvocation
from firefly_forge.source_edit_models import SourceReadRequest


async def reader(store, tmp_path, monkeypatch, files, *, deleted_tracked=False):
    import subprocess

    import test_workspace_reconciliation_native as reconciliation_fixture

    original_seed = reconciliation_fixture.seed_repository

    async def seed_with_catalog(path):
        repository = await original_seed(path)
        catalog = {**files, **({"catalog/deleted.py": b"obsolete = True\n"} if deleted_tracked else {})}
        for name, data in catalog.items():
            target = repository / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        subprocess.run(["git", "-C", str(repository), "add", "--", *catalog], check=True, timeout=10)
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "Seed source reader catalog",
            ],
            check=True,
            timeout=10,
        )
        return repository

    monkeypatch.setattr(reconciliation_fixture, "seed_repository", seed_with_catalog)
    if deleted_tracked:
        original_engine = reconciliation_fixture.Engine
        applied = False

        def engine_with_worker_effect(store, workspaces, provider_factory, **kwargs):
            def provider(settings):
                worker = provider_factory(settings)
                original_execute = worker.execute

                async def execute(workspace, *args):
                    nonlocal applied
                    if not applied:
                        # A modeled worker effect precedes the existing verification cut.
                        (workspace / "catalog/deleted.py").unlink()
                        applied = True
                    return await original_execute(workspace, *args)

                worker.execute = execute
                return worker

            return original_engine(store, workspaces, provider, **kwargs)

        monkeypatch.setattr(reconciliation_fixture, "Engine", engine_with_worker_effect)
    env = await prepared(store, tmp_path, monkeypatch)
    await env.service.activate_source_edit(env.run_id, activation(await env.service.source_candidate(env.run_id)))
    return env, SourceEditInvocation(env.service.recoveries, env.run_id, await started(env))


async def test_native_source_pages_keep_exact_receipts_and_spent_read_budget(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        files = {f"catalog/{i:04}.py": b"VALUE = 1\n" for i in range(2003)}
        env, runtime = await reader(store, tmp_path, monkeypatch, files, deleted_tracked=True)
        first_request = SourceReadRequest(tool="list_files", request_id=uuid4().hex, path="catalog", page_size=1000)
        first = await runtime.run(first_request)
        assert await runtime.run(first_request) == first
        second = await runtime.run(
            SourceReadRequest(
                tool="list_files", request_id=uuid4().hex, path="catalog", page_size=1000, cursor=first["next_cursor"]
            )
        )
        third = await runtime.run(
            SourceReadRequest(
                tool="list_files", request_id=uuid4().hex, path="catalog", page_size=1000, cursor=second["next_cursor"]
            )
        )
        assert len(first["files"] + second["files"] + third["files"]) == 2003 and third["complete"] is True
        assert "catalog/deleted.py" not in first["files"] + second["files"] + third["files"]
        rows = await env.engine.tool_broker.invocations(env.run_id)
        assert len(rows) == 3 and all(row["status"] == "succeeded" for row in rows)
        assert all(row["admission"]["tool"] == "list_files" for row in rows)
        assert first["source_digest"] == second["source_digest"] == third["source_digest"]
        assert len(env.calls) == 1 and len(env.checks) == 7


async def test_native_source_cursor_drift_has_no_new_tool_or_command_authority(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env, runtime = await reader(store, tmp_path, monkeypatch, {"catalog/a.py": b"A\n", "catalog/b.py": b"B\n"})
        request = SourceReadRequest(tool="list_files", request_id=uuid4().hex, path="catalog", page_size=1)
        first = await runtime.run(request)
        with pytest.raises(ValueError):
            await runtime.run(
                SourceReadRequest(
                    tool="list_files",
                    request_id=request.request_id,
                    path="catalog",
                    page_size=1,
                    cursor=first["next_cursor"],
                )
            )
        rows = await env.engine.tool_broker.invocations(env.run_id)
        assert len(rows) == 1 and rows[0]["status"] == "succeeded"
        assert await runtime.run(request) == first
        assert len(env.calls) == 1 and len(env.checks) == 7


async def test_native_large_text_search_pages_keep_scope_and_full_hash(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        data = b"x" * 1_100_000 + b"\nlarge-needle\nlarge-needle\n"
        env, runtime = await reader(
            store, tmp_path, monkeypatch, {"catalog/large.py": data, "catalog/image.bin": b"\x00\xff"}
        )
        first = await runtime.run(
            SourceReadRequest(
                tool="search_text", request_id=uuid4().hex, text="large-needle", path="catalog", max_results=1
            )
        )
        second = await runtime.run(
            SourceReadRequest(
                tool="search_text",
                request_id=uuid4().hex,
                text="large-needle",
                path="catalog",
                max_results=1,
                cursor=first["next_cursor"],
            )
        )
        assert first["matches"][0]["line"] == 2 and second["matches"][0]["line"] == 3
        assert first["matches"][0]["sha256"] == second["matches"][0]["sha256"] == hashlib.sha256(data).hexdigest()
        assert second["complete"] is True and second["classifications"]["binary"] == 1
        rows = await env.engine.tool_broker.invocations(env.run_id)
        assert len(rows) == 2 and all(row["status"] == "succeeded" for row in rows)
        assert len(env.calls) == 1 and len(env.checks) == 7
