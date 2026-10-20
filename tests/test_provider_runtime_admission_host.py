# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""CLI runtime admission uses controlled loop metadata and memory persistence."""

from __future__ import annotations

import asyncio
import os
import signal

import pytest
from click.testing import CliRunner

from firefly_forge import providers
from firefly_forge.cli import main


@pytest.mark.parametrize("role", [None, "combined", "api"])
def test_serve_selects_stdlib_asyncio_for_each_runtime_role(monkeypatch, role):
    import uvicorn

    app, observed = object(), []
    monkeypatch.setattr("firefly_forge.api.create_application", lambda *args, **kwargs: app)
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: observed.append((application, kwargs)))
    result = CliRunner().invoke(main, ["serve", "--port", "18791", *(["--role", role] if role else [])])
    assert result.exit_code == 0, result.output
    assert observed == [(app, {"host": "127.0.0.1", "port": 18791, "loop": "asyncio"})]


@pytest.fixture
def incompatible_loop(monkeypatch):
    if os.name != "posix" or not hasattr(signal, "SIGXFSZ"):
        pytest.skip("POSIX file-size limits are required")
    import resource

    uvloop = pytest.importorskip("uvloop")
    original = resource.getrlimit
    monkeypatch.setattr(
        resource, "getrlimit", lambda kind: (16_384, 16_384) if kind == resource.RLIMIT_FSIZE else original(kind)
    )
    return uvloop.new_event_loop


@pytest.mark.parametrize("ignored", [False, True])
def test_finite_uvloop_refuses_before_launch_guard_or_child(monkeypatch, tmp_path, incompatible_loop, ignored):
    calls = []
    monkeypatch.setattr(signal, "getsignal", lambda kind: signal.SIG_IGN if ignored else signal.SIG_DFL)

    async def guard():
        calls.append("dispatch")

    async def forbidden(*args, **kwargs):
        calls.append("child")
        raise AssertionError("An incompatible loop must not create a CLI process")

    async def discard(*args):
        pass

    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", forbidden)

    async def invoke():
        with providers.cli_dispatch_scope(guard):
            return await providers.run_process(["controlled-cli"], tmp_path, discard, timeout=1)

    with (
        asyncio.Runner(loop_factory=incompatible_loop) as runner,
        pytest.raises(providers.ProviderError, match="asyncio") as caught,
    ):
        runner.run(invoke())
    assert isinstance(caught.value, providers.ProviderRuntimeAdmissionError)
    assert caught.value.process_outcome is None and calls == []


@pytest.mark.parametrize("configured", [False, True])
def test_engine_runtime_fence_precedes_native_dispatch_but_preserves_custom_providers(
    monkeypatch, tmp_path, incompatible_loop, configured
):
    from test_bridge_inflight_cancel import RUN, MemoryStore, Rows

    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.engine import Engine
    from firefly_forge.journal import Journal

    store = MemoryStore(tmp_path / "memory")
    store.schema = "host_runtime_admission"
    store.configuration = ConfigurationStore(tmp_path / "configuration")
    store.credential_registry = dict
    store.run.data.update(store.configuration.snapshot("codex"), agent="codex")
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=Rows())
    calls = []

    class Worker:
        async def execute(self, *args):
            calls.append("provider")
            raise providers.ProviderError("Controlled custom provider reached")

    class Dispatch:
        async def begin(self, *args):
            return None

        async def normal_dispatch(self, *args):
            calls.append("dispatch")

        async def record_process(self, *args):
            return True

    engine = Engine(store, tmp_path / "workspaces", None if configured else lambda _: Worker(), journal=journal)
    engine.provider_recoveries = Dispatch()
    monkeypatch.setattr(providers, "get_provider", lambda *args: Worker())
    workspace = engine.workspaces / RUN
    workspace.mkdir()
    (workspace / "main.py").write_text("VALUE = 1\n")

    async def inspect(*args):
        return {"base_revision": "b" * 40, "head": "b" * 40, "source_tree": "c" * 40}

    async def checked(argv, *args, **kwargs):
        assert argv == ["git", "ls-files", "-z"]
        return ""

    monkeypatch.setattr(engine, "_inspect_workspace", inspect)
    monkeypatch.setattr("firefly_forge.engine.checked", checked)
    with asyncio.Runner(loop_factory=incompatible_loop) as runner:
        runner.run(engine.execute_run(RUN))
    result = store.run.data
    assert result["status"] == "failed"
    if configured:
        assert calls == [] and "asyncio" in result["error"], result
    else:
        assert calls == ["dispatch", "provider"], result
    assert result["repair_budget"] == 0
    assert Journal.verify_chain([row.data for row in journal.records.rows])
