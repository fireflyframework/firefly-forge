# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import copy
import importlib

import pytest


def schema():
    try:
        return importlib.import_module("firefly_forge.sandbox_models")
    except ModuleNotFoundError as exc:
        raise AssertionError("The immutable runner configuration contract is missing") from exc


def test_runner_snapshot_identifies_trusted_host_without_claiming_isolation():
    snapshot = schema().capture_sandbox(None)
    assert snapshot["mode"] == "host"
    assert snapshot["effective_network"] == "host"
    assert "trusted" in snapshot["reason"].lower()
    assert snapshot["enforced_controls"] == ["timeout", "output"]
    assert len(snapshot["digest"]) == 64


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "docker"},
        {"mode": "docker", "image": "python:latest", "context": "colima-forge-tests"},
        {"mode": "docker", "image": "python@sha256:" + "1" * 64, "context": "--host=evil"},
        {"mode": "host", "image": "python@sha256:" + "1" * 64},
        {"mode": "host", "privileged": True},
        {"mode": "host", "timeout": 0},
        {"mode": "host", "max_output_bytes": 2_000_001},
    ],
)
def test_runner_configuration_rejects_unpinned_images_and_unbounded_controls(body):
    with pytest.raises(ValueError):
        schema().SandboxInput.model_validate(body)


def test_snapshot_is_copied_and_detects_configuration_tampering():
    body = {"mode": "docker", "image": "python@sha256:" + "1" * 64, "context": "colima-forge-tests"}
    snapshot = schema().capture_sandbox(body)
    body["image"] = "python:latest"
    assert snapshot["configuration"]["image"].endswith("1" * 64)
    assert snapshot["effective_network"] == "none"
    assert schema().validate_sandbox_snapshot(snapshot).mode == "docker"
    altered = copy.deepcopy(snapshot)
    altered["configuration"]["network"] = "egress"
    with pytest.raises(ValueError, match="digest"):
        schema().validate_sandbox_snapshot(altered)


@pytest.mark.parametrize(
    "origin",
    ["https://example.com", "http://user:password@127.0.0.1:8080", "http://127.0.0.1:80", "http://127.0.0.1:8080/path"],
)
def test_browser_profile_only_targets_declared_container_loopback_origin(origin):
    with pytest.raises(ValueError):
        schema().BrowserInput.model_validate(
            {"image": "browser@sha256:" + "2" * 64, "start_argv": ["node", "server.js"], "origin": origin}
        )


@pytest.mark.asyncio
async def test_sandbox_wal_unknown_create_never_reexecutes_and_receipt_is_idempotent(store):
    import uuid

    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput

    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Verify service"))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    execution_id = uuid.uuid4().hex
    payload = {
        "parent_attempt": 1,
        "ordinal": 1,
        "container_name": "forge-" + execution_id,
        "snapshot_digest": "a" * 64,
        "request_digest": "b" * 64,
    }
    first = await journal.sandbox_effect_intent(run["id"], "verify", execution_id, effect="create", payload=payload)
    assert first["disposition"] == "execute"
    again = await journal.sandbox_effect_intent(run["id"], "verify", execution_id, effect="create", payload=payload)
    assert again["disposition"] == "needs_reconciliation"
    await journal.sandbox_effect_receipt(
        run["id"], "verify", execution_id, effect="create", evidence={"container_id": "c"}
    )
    await journal.sandbox_effect_receipt(
        run["id"], "verify", execution_id, effect="create", evidence={"container_id": "different"}
    )
    assert (await journal.sandbox_effect_intent(run["id"], "verify", execution_id, effect="create", payload=payload))[
        "disposition"
    ] == "completed"
    entries = await journal.entries(run["id"])
    assert journal.verify_chain(entries)
    assert len([item for item in entries if item["kind"] == "sandbox_receipt"]) == 1
    assert (await journal.plan(run["id"]))["tasks"][2]["status"] == "running"


@pytest.mark.asyncio
async def test_sandbox_wal_rejects_changed_request_and_nonstage_effects(store):
    import uuid

    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput

    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Verify service"))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    execution_id = uuid.uuid4().hex
    payload = {
        "parent_attempt": 1,
        "ordinal": 1,
        "container_name": "forge-" + execution_id,
        "snapshot_digest": "a" * 64,
        "request_digest": "b" * 64,
    }
    await journal.sandbox_effect_intent(run["id"], "verify", execution_id, effect="create", payload=payload)
    with pytest.raises(ValueError, match="changed"):
        await journal.sandbox_effect_intent(
            run["id"], "verify", execution_id, effect="create", payload={**payload, "request_digest": "c" * 64}
        )
    with pytest.raises(ValueError, match="stage"):
        await journal.sandbox_effect_intent(run["id"], "deploy", uuid.uuid4().hex, effect="create", payload=payload)


@pytest.mark.asyncio
async def test_docker_selected_runner_does_not_fall_back_to_host_when_unavailable(store, tmp_path, monkeypatch):
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox_models import SandboxInput

    try:
        from firefly_forge.sandbox import SandboxExecutor
    except ImportError as exc:
        raise AssertionError("Bounded Docker executor is missing") from exc
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Verify",
            sandbox=SandboxInput(mode="docker", context="missing", image="example/test@sha256:" + "a" * 64),
        )
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    await store.acquire_ownership()
    runner = SandboxExecutor(store, journal)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr(runner, "_docker", unavailable)
    with pytest.raises(RuntimeError, match="Docker unavailable"):
        await runner.execute(run["id"], tmp_path, tmp_path, ["touch", "host-fallback"], 10, "verify")
    assert not (tmp_path / "host-fallback").exists()


@pytest.mark.asyncio
async def test_host_command_budget_survives_stage_resume_and_handoff(store, tmp_path):
    import sys

    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.sandbox_models import SandboxInput

    await store.acquire_ownership()
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    run = await store.create_run(
        RunInput(repository_id=repo["id"], brief="Verify", sandbox=SandboxInput(command_limit=1))
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    stage = await journal.intent(run["id"], "verify")
    runner = SandboxExecutor(store, journal)
    result = await runner.execute(
        run["id"], tmp_path, tmp_path, [sys.executable, "-c", "print('real check')"], 10, "verify"
    )
    assert result.exit_code == 0 and result.output.strip() == "real check"
    await journal.fail(run["id"], stage["idempotency_key"], "quota", "credits exhausted")
    await journal.resume(run["id"], agent="claude")
    await journal.intent(run["id"], "verify", attempt=2)
    with pytest.raises(ValueError, match="total command budget"):
        await SandboxExecutor(store, journal).execute(
            run["id"], tmp_path, tmp_path, [sys.executable, "-c", "print('repeat')"], 10, "verify"
        )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("os").environ.get("FORGE_TEST_DOCKER_IMAGE"),
    reason="Set an explicitly prepared Docker image for native runner acceptance",
)
async def test_actual_docker_isolation_readonly_git_exit_and_cleanup(store, tmp_path, monkeypatch):
    import os

    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.sandbox_models import SandboxInput

    await store.acquire_ownership()
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("preserved")
    monkeypatch.setenv("FORGE_SECRET_SENTINEL", "must-never-enter-container")
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    config = SandboxInput(
        mode="docker",
        context=os.environ.get("FORGE_TEST_DOCKER_CONTEXT", "colima-forge-tests"),
        image=os.environ["FORGE_TEST_DOCKER_IMAGE"],
        workspace_transport=os.environ.get("FORGE_TEST_WORKSPACE_TRANSPORT", "bind"),
    )
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Verify", sandbox=config))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    runner = SandboxExecutor(store, journal)
    script = """import os,pathlib,socket,json
assert os.getuid()!=0
assert 'FORGE_SECRET_SENTINEL' not in os.environ
assert not pathlib.Path('/var/run/docker.sock').exists()
for path in ['/root/no-write','/workspace/.git/config']:
 try: pathlib.Path(path).write_text('modified')
 except OSError: pass
 else: raise AssertionError('read-only boundary failed')
try: socket.create_connection(('1.1.1.1',443),timeout=.5)
except OSError: pass
else: raise AssertionError('network boundary failed')
pathlib.Path('actual.py').write_text('VALUE=1\\n')
print(json.dumps({'uid':os.getuid(),'network':'none','git':'readonly','source_write':True}))
"""
    result = await runner.execute(
        run["id"], workspace, workspace, ["/app/.venv/bin/python", "-c", script], 20, "verify", git_root=workspace
    )
    assert result.exit_code == 0, result.output
    assert (workspace / "actual.py").read_text() == "VALUE=1\n"
    assert (workspace / ".git" / "config").read_text() == "preserved"
    failure = await runner.execute(
        run["id"],
        workspace,
        workspace,
        ["/app/.venv/bin/python", "-c", "raise SystemExit(7)"],
        20,
        "verify",
        git_root=workspace,
    )
    assert failure.exit_code == 7
    records = await journal.sandbox_executions(run["id"])
    assert all(row["status"] == "removed" for row in records)
    assert Journal.verify_chain(await journal.entries(run["id"]))
    found = await runner._docker(
        config, ["ps", "--all", "--filter", f"label=org.firefly.forge.run={run['id']}", "--format", "{{.Names}}"]
    )
    assert found.output.strip() == ""


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("os").environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Native Docker image not configured"
)
async def test_actual_docker_unknown_command_receipt_does_not_run_twice(store, tmp_path, monkeypatch):
    import os

    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor, SandboxReconciliationRequired
    from firefly_forge.sandbox_models import SandboxInput

    await store.acquire_ownership()
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    config = SandboxInput(
        mode="docker",
        context=os.environ.get("FORGE_TEST_DOCKER_CONTEXT", "colima-forge-tests"),
        image=os.environ["FORGE_TEST_DOCKER_IMAGE"],
        workspace_transport=os.environ.get("FORGE_TEST_WORKSPACE_TRANSPORT", "bind"),
    )
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Verify", sandbox=config))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    original = journal.sandbox_effect_receipt

    async def lost_receipt(*args, **kwargs):
        if kwargs["effect"] == "start":
            raise RuntimeError("simulated lost durable receipt")
        return await original(*args, **kwargs)

    monkeypatch.setattr(journal, "sandbox_effect_receipt", lost_receipt)
    argv = [
        "/app/.venv/bin/python",
        "-c",
        "import pathlib;p=pathlib.Path('counter');p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
    ]
    with pytest.raises(RuntimeError, match="lost durable receipt"):
        await SandboxExecutor(store, journal).execute(run["id"], tmp_path, tmp_path, argv, 10, "verify", ordinal=1)
    monkeypatch.setattr(journal, "sandbox_effect_receipt", original)
    with pytest.raises(SandboxReconciliationRequired):
        await SandboxExecutor(store, journal).execute(run["id"], tmp_path, tmp_path, argv, 10, "verify", ordinal=1)
    assert (tmp_path / "counter").read_text() == "1"
    assert Journal.verify_chain(await journal.entries(run["id"]))


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("os").environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Native Docker image not configured"
)
async def test_actual_docker_timeout_and_output_limits_remove_owned_container(store, tmp_path):
    import os

    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.sandbox_models import SandboxInput

    await store.acquire_ownership()
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    config = SandboxInput(
        mode="docker",
        context=os.environ.get("FORGE_TEST_DOCKER_CONTEXT", "colima-forge-tests"),
        image=os.environ["FORGE_TEST_DOCKER_IMAGE"],
        workspace_transport=os.environ.get("FORGE_TEST_WORKSPACE_TRANSPORT", "bind"),
        max_output_bytes=1024,
    )
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Verify", sandbox=config))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    runner = SandboxExecutor(store, journal)
    with pytest.raises(TimeoutError):
        await runner.execute(
            run["id"], tmp_path, tmp_path, ["/app/.venv/bin/python", "-c", "import time;time.sleep(30)"], 1, "verify"
        )
    with pytest.raises(RuntimeError, match="output"):
        await runner.execute(
            run["id"], tmp_path, tmp_path, ["/app/.venv/bin/python", "-c", "print('a'*5000)"], 10, "verify"
        )
    found = await runner._docker(
        config, ["ps", "--all", "--filter", f"label=org.firefly.forge.run={run['id']}", "--format", "{{.Names}}"]
    )
    assert found.output.strip() == ""


@pytest.mark.asyncio
async def test_completed_host_rows_have_terminal_cleanup_receipts(store, tmp_path):
    import sys

    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor

    await store.acquire_ownership()
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Verify"))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    await SandboxExecutor(store, journal).execute(
        run["id"], tmp_path, tmp_path, [sys.executable, "-c", "print(1)"], 10, "verify"
    )
    assert (await journal.sandbox_executions(run["id"]))[0]["status"] == "removed"
    assert (await SandboxExecutor(store, journal).recover())["inspected"] == 0


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("os").environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Native Docker image not configured"
)
async def test_actual_recovery_recovers_exited_receipt_and_returns_cached_result_without_replay(
    store, tmp_path, monkeypatch
):
    import os

    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.sandbox_models import SandboxInput

    await store.acquire_ownership()
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    config = SandboxInput(
        mode="docker",
        context=os.environ.get("FORGE_TEST_DOCKER_CONTEXT", "colima-forge-tests"),
        image=os.environ["FORGE_TEST_DOCKER_IMAGE"],
        workspace_transport=os.environ.get("FORGE_TEST_WORKSPACE_TRANSPORT", "bind"),
    )
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Verify", sandbox=config))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    original = journal.sandbox_effect_receipt

    async def lost_receipt(*args, **kwargs):
        if kwargs["effect"] == "start":
            raise RuntimeError("lost receipt")
        return await original(*args, **kwargs)

    monkeypatch.setattr(journal, "sandbox_effect_receipt", lost_receipt)
    runner = SandboxExecutor(store, journal)

    async def crash_cleanup(*args, **kwargs):
        pass

    monkeypatch.setattr(runner, "_cleanup", crash_cleanup)
    script = (
        "import pathlib;p=pathlib.Path('counter');"
        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1');print('completed')"
    )
    argv = ["/app/.venv/bin/python", "-c", script]
    with pytest.raises(RuntimeError, match="lost receipt"):
        await runner.execute(run["id"], tmp_path, tmp_path, argv, 10, "verify", ordinal=1)
    monkeypatch.setattr(journal, "sandbox_effect_receipt", original)
    restored = SandboxExecutor(store, journal)
    assert (await restored.recover())["recovered_receipts"] == 1
    result = await restored.execute(run["id"], tmp_path, tmp_path, argv, 10, "verify", ordinal=1)
    assert result.exit_code == 0 and result.output.strip() == "completed"
    assert (tmp_path / "counter").read_text() == "1"
    assert (await journal.sandbox_executions(run["id"]))[0]["status"] == "removed"
    assert Journal.verify_chain(await journal.entries(run["id"]))


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("os").environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Native Docker image not configured"
)
async def test_actual_docker_execution_capacity_is_two_independent_runs(store, tmp_path, monkeypatch):
    import asyncio
    import os

    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.sandbox_models import SandboxInput

    await store.acquire_ownership()
    journal = Journal(store)
    config = SandboxInput(
        mode="docker",
        context=os.environ.get("FORGE_TEST_DOCKER_CONTEXT", "colima-forge-tests"),
        image=os.environ["FORGE_TEST_DOCKER_IMAGE"],
        workspace_transport=os.environ.get("FORGE_TEST_WORKSPACE_TRANSPORT", "bind"),
    )
    runs = []
    for index in range(3):
        repo = await store.add_repository(
            RepositoryInput(name=f"product-{index}", url=f"https://github.com/team/product-{index}.git")
        )
        run = await store.create_run(RunInput(repository_id=repo["id"], brief="Verify", sandbox=config))
        await journal.ensure_plan(run["id"])
        await journal.intent(run["id"], "verify")
        runs.append(run)
    runner = SandboxExecutor(store, journal)
    original = runner._docker
    active = maximum = 0

    async def measured(config, argv, *args, **kwargs):
        nonlocal active, maximum
        if argv[0] != "start":
            return await original(config, argv, *args, **kwargs)
        active += 1
        maximum = max(maximum, active)
        try:
            return await original(config, argv, *args, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(runner, "_docker", measured)
    argv = ["/app/.venv/bin/python", "-c", "import time;time.sleep(.5);print('real isolated job')"]
    results = await asyncio.gather(*(runner.execute(run["id"], tmp_path, tmp_path, argv, 10, "verify") for run in runs))
    assert all(result.exit_code == 0 and result.output.strip() == "real isolated job" for result in results)
    assert maximum == 2 and active == 0


def test_browser_budget_is_frozen_and_legacy_snapshots_keep_eight_calls():
    import hashlib
    import json

    from firefly_forge.sandbox_models import BrowserInput, SandboxInput, capture_sandbox, validate_sandbox_snapshot

    value = SandboxInput(
        mode="docker",
        context="isolated",
        image="example/project@sha256:" + "a" * 64,
        browser=BrowserInput(
            image="example/browser@sha256:" + "b" * 64,
            start_argv=["node", "server.js"],
            origin="http://127.0.0.1:18080",
        ),
    )
    frozen = capture_sandbox(value)
    assert frozen["configuration"]["browser"]["max_calls"] == 8
    legacy = copy.deepcopy(frozen)
    legacy["configuration"]["browser"].pop("max_calls")
    legacy["digest"] = hashlib.sha256(
        json.dumps(legacy["configuration"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert validate_sandbox_snapshot(legacy).browser.max_calls == 8


def test_browser_evidence_with_known_credentials_is_rejected_before_image_admission():
    from firefly_forge.process import ProcessResult
    from firefly_forge.sandbox import SandboxExecutor

    runner = object.__new__(SandboxExecutor)
    runner.credential_registry = lambda: {"FORGE_TOKEN": "operator-private-token"}
    raw = ProcessResult([], 0, '{"body_text":"operator-private-token","image":"PNG"}')
    with pytest.raises(ValueError, match="cannot be admitted"):
        runner._safe_result(raw, browser=True)
    assert "operator-private-token" not in runner._safe_result(raw).output
