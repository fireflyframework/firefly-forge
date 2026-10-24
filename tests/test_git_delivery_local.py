# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Factory checkpoints extend actual local Git history while retaining the original base."""

import asyncio
import copy
import hashlib
import importlib
import importlib.util
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from firefly_forge.git_workspace import metadata_digest


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=root, text=True, capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def delivery_case(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=main")
    git(root, "config", "user.name", "Public fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "product.py").write_text("value = 1\n")
    git(root, "add", "product.py")
    git(root, "commit", "-qm", "Original public baseline")
    base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-qb", "forge/public-unit")
    run = {
        "id": "1" * 32,
        "status": "paused",
        "repository_id": "2" * 32,
        "project_id": "3" * 32,
        "base_revision": base,
        "branch": "forge/public-unit",
        "execution_actor": {"user_id": "4" * 32},
        "git_delivery_policy": {
            "version": 1,
            "enabled": True,
            "every_units": 1,
            "on_pause": True,
            "remote": "local",
            "max_checkpoints": 50,
        },
        "active_task": "implement",
        "implementation_rounds": 1,
        "summary": "Implemented the public counter",
    }
    run["git_metadata_digest"] = metadata_digest(root)
    state = {"run": run, "entries": [], "allowed": True, "owned": True, "active": None}
    lock = asyncio.Lock()

    @asynccontextmanager
    async def unit(**_kwargs):
        async def execute(*args, **kwargs):
            return None

        yield SimpleNamespace(execute=execute)

    async def current(_run_id):
        return copy.deepcopy(state["run"])

    async def ownership():
        if not state["owned"]:
            raise RuntimeError("Controlled lease loss")

    async def admit(_value):
        return None

    async def authorize(_run, _actor, permission):
        if not state["allowed"]:
            raise PermissionError("Controlled permission revocation")

    async def append(_run_id, kind, key, payload):
        entry = {
            "run_id": _run_id,
            "sequence": len(state["entries"]) + 1,
            "task_id": "checkpoint",
            "kind": kind,
            "idempotency_key": key,
            "payload": copy.deepcopy(payload),
            "created_at": "2026-10-23T12:00:00Z",
            "previous_hash": state["entries"][-1]["digest"] if state["entries"] else "0" * 64,
        }
        entry["digest"] = hashlib.sha256(
            json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        state["entries"].append(entry)
        return entry

    async def entries(_run_id):
        return copy.deepcopy(state["entries"])

    async def update(_run_id, **changes):
        state["run"].update(changes)

    store = SimpleNamespace(
        get_run=current,
        assert_ownership=ownership,
        admit_credentials=admit,
        admit_source_patch=admit,
        reject_credentials=lambda _value: None,
        _unit=unit,
        source_budget_admission_unit=unit,
        update=update,
    )
    engine = SimpleNamespace(
        workspaces=tmp_path,
        active={},
        stopping=False,
        tool_broker=None,
        connections=None,
        component_roots={},
        git_metadata={},
        workspace_lock=lock,
    )

    class ControlledScanner:
        async def __call__(self, chunk, final):
            from firefly_forge.source_patch_admission import SourcePatch

            await ownership()
            if chunk:
                await store.admit_source_patch(SourcePatch(chunk.decode("utf-8", errors="replace")))

        def finish_document(self):
            return None

    async def scanner(**_kwargs):
        return ControlledScanner()

    store.source_patch_scanner = scanner

    async def snapshot(workspace, base, **kwargs):
        from firefly_forge.git_diff_artifacts import read_diff_page, retain_git_diff
        from firefly_forge.git_diff_models import GitDiffBudget
        from firefly_forge.source_snapshot import SourceDiffSnapshotV1, diff_artifact_root, scan_changed_source

        git(workspace, "add", "--all", "--", ".")
        budget = kwargs.get("budget") or GitDiffBudget.model_validate(
            (run.get("git_delivery_policy") or {}).get("diff_budget") or {}
        )
        scan = await store.source_patch_scanner(settings=kwargs.get("secrets"))
        files, _ = await scan_changed_source(workspace, base, None, scan, budget=budget, validate_path=lambda _: None)
        folder = diff_artifact_root(engine.workspaces.resolve(), run["id"])
        ref = await retain_git_diff(workspace, base, folder, budget=budget, scan_chunk=scan)
        preview = read_diff_page(folder, ref, 0, budget.preview_bytes) if budget.preview_bytes else None
        import base64

        text = base64.b64decode(preview.content_base64).decode("utf-8", errors="replace") if preview else ""
        preview_bytes = preview.byte_count if preview else 0
        tree = git(workspace, "write-tree")
        return (
            files,
            SourceDiffSnapshotV1(
                artifact=ref,
                preview=text,
                preview_bytes=preview_bytes,
                preview_truncated=preview_bytes < ref.byte_count,
            ),
            tree,
        )

    engine._snapshot = snapshot

    def verify(values):
        previous = "0" * 64
        for index, entry in enumerate(values, 1):
            signed = {k: v for k, v in entry.items() if k != "digest"}
            if (
                entry["previous_hash"] != previous
                or entry["sequence"] != index
                or hashlib.sha256(
                    json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                ).hexdigest()
                != entry["digest"]
            ):
                return False
            previous = entry["digest"]
        return True

    journal = SimpleNamespace(git_delivery_append=append, entries=entries, verify_chain=verify)

    def create():
        assert importlib.util.find_spec("firefly_forge.git_deliveries") is not None, (
            "Factory intermediate Git delivery service is missing"
        )
        module = importlib.import_module("firefly_forge.git_deliveries")
        service = module.GitDeliveries(store, engine, journal, workspace=lambda _run: root, authorize=authorize)
        return SimpleNamespace(root=root, base=base, state=state, service=service, module=module)

    return create


async def test_two_factory_checkpoints_preserve_original_base_and_resume_from_owned_head(delivery_case):
    c = delivery_case()
    (c.root / "product.py").write_text("value = 2\n")
    first = await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    assert first["parent_revision"] == c.base and first["original_base_revision"] == c.base
    assert git(c.root, "rev-parse", "HEAD") == first["revision"]
    assert git(c.root, "show", c.base + ":product.py") == "value = 1"
    (c.root / "product.py").write_text("value = 3\n")
    second = await c.service.checkpoint(
        "1" * 32, request_id="b" * 32, expected_head=first["revision"], actor={"user_id": "4" * 32}
    )
    assert second["sequence"] == 2 and second["parent_revision"] == first["revision"]
    assert git(c.root, "rev-parse", "HEAD^") == first["revision"]
    assert c.state["run"]["base_revision"] == c.base
    assert [e["kind"] for e in c.state["entries"]] == ["git_delivery_intent", "git_delivery_receipt"] * 2


async def test_foreign_head_is_refused_before_any_checkpoint_write(delivery_case):
    c = delivery_case()
    (c.root / "product.py").write_text("value = 8\n")
    git(c.root, "add", ".")
    git(c.root, "commit", "-qm", "Foreign commit")
    foreign = git(c.root, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="HEAD|head|baseline"):
        await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    assert git(c.root, "rev-parse", "HEAD") == foreign and not c.state["entries"]


async def test_revoked_operator_and_active_worker_cannot_checkpoint(delivery_case):
    c = delivery_case()
    (c.root / "product.py").write_text("value = 2\n")
    c.state["allowed"] = False
    with pytest.raises(PermissionError):
        await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    assert git(c.root, "rev-parse", "HEAD") == c.base and not c.state["entries"]


async def test_active_coding_task_cannot_be_checkpointed_by_an_operator(delivery_case):
    c = delivery_case()
    (c.root / "product.py").write_text("value = 2\n")
    waiting = asyncio.create_task(asyncio.Event().wait())
    c.service.engine.active["1" * 32] = waiting
    try:
        with pytest.raises(ValueError, match="worker|unit"):
            await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
        assert git(c.root, "rev-parse", "HEAD") == c.base and not c.state["entries"]
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_unknown_local_commit_outcome_blocks_same_and_new_request_after_restart(delivery_case):
    c = delivery_case()
    (c.root / "product.py").write_text("value = 2\n")
    append = c.service.journal.git_delivery_append

    async def fail_receipt(run_id, kind, key, payload):
        if kind == "git_delivery_receipt":
            raise RuntimeError("Controlled lost checkpoint receipt")
        return await append(run_id, kind, key, payload)

    c.service.journal.git_delivery_append = fail_receipt
    with pytest.raises(RuntimeError, match="lost checkpoint receipt"):
        await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    after = git(c.root, "rev-parse", "HEAD")
    assert after != c.base
    restarted = c.module.GitDeliveries(
        c.service.store,
        c.service.engine,
        c.service.journal,
        workspace=c.service.workspace,
        authorize=c.service.authorization,
    )
    for key in ["a" * 32, "b" * 32]:
        with pytest.raises(ValueError, match="unknown outcome"):
            await restarted.checkpoint("1" * 32, request_id=key, expected_head=c.base, actor={"user_id": "4" * 32})
    assert git(c.root, "rev-parse", "HEAD") == after
    assert len(c.state["entries"]) == 1


async def test_missing_journal_integrity_verifier_refuses_before_git_write(delivery_case):
    c = delivery_case()
    (c.root / "product.py").write_text("value = 2\n")
    del c.service.journal.verify_chain
    with pytest.raises(ValueError, match="integrity"):
        await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    assert git(c.root, "rev-parse", "HEAD") == c.base and not c.state["entries"]


async def test_missing_owned_git_metadata_baseline_refuses_checkpoint(delivery_case):
    c = delivery_case()
    (c.root / "product.py").write_text("value = 2\n")
    del c.state["run"]["git_metadata_digest"]
    with pytest.raises(ValueError, match="metadata"):
        await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    assert git(c.root, "rev-parse", "HEAD") == c.base and not c.state["entries"]


async def test_component_escape_and_lease_revocation_during_source_admission_refuse(delivery_case):
    c = delivery_case()
    c.state["run"]["component_snapshot"] = {"path": "api"}
    (c.root / "product.py").write_text("value = 2\n")
    with pytest.raises(ValueError, match="component"):
        await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    c.state["run"].pop("component_snapshot")

    async def lose(value):
        c.state["owned"] = False

    c.service.store.admit_source_patch = lose
    with pytest.raises(RuntimeError, match="lease"):
        await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    assert git(c.root, "rev-parse", "HEAD") == c.base and not c.state["entries"]


async def test_checkpoint_receipt_tampering_is_rejected_even_in_a_resigned_outer_wal(delivery_case):
    c = delivery_case()
    (c.root / "product.py").write_text("value = 2\n")
    await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    item = c.state["entries"][-1]
    item["payload"]["parent_revision"] = "f" * 40
    item["digest"] = c.module.delivery_digest({k: v for k, v in item.items() if k != "digest"})
    with pytest.raises(ValueError, match="digest|intent"):
        await c.service.view("1" * 32, actor={"user_id": "4" * 32})


async def test_maximum_reserves_verified_final_checkpoint_and_frequency_is_units(delivery_case):
    c = delivery_case()
    (c.root / "product.py").write_text("value = 2\n")
    c.state["run"]["git_delivery_policy"].update(every_units=2, max_checkpoints=2)
    assert await c.service.automatic("1" * 32) is None
    c.state["run"]["implementation_rounds"] = 2
    first = await c.service.automatic("1" * 32)
    assert first["sequence"] == 1
    (c.root / "product.py").write_text("value = 3\n")
    c.state["run"]["implementation_rounds"] = 4
    assert await c.service.automatic("1" * 32) is None
    final = await c.service.checkpoint(
        "1" * 32, request_id="f" * 32, expected_head=first["revision"], actor={"user_id": "4" * 32}, final=True
    )
    assert final["sequence"] == 2 and c.state["run"]["base_revision"] == c.base


async def test_new_default_schedule_keeps_old_frozen_policy_but_explicit_disable_revokes(delivery_case, monkeypatch):
    c = delivery_case()
    c.service.authorization = None
    saved = {"digest": "f" * 64, "policy": {**c.state["run"]["git_delivery_policy"], "every_units": 2}}

    async def snapshot(self, project_id):
        return saved

    async def authorize(*args):
        pass

    async def plan(run_id):
        return {"tasks": [{"id": "implement", "status": "done"}]}

    monkeypatch.setattr("firefly_forge.git_delivery_policy.GitDeliveryPolicies.snapshot", snapshot)
    c.service._authorize = authorize
    c.service.journal.plan = plan
    c.state["run"]["git_delivery_policy_digest"] = "e" * 64
    current = copy.deepcopy(c.state["run"])
    await c.service._guard("1" * 32, current, {"user_id": "4" * 32})
    assert c.state["run"]["git_delivery_policy"]["every_units"] == 1
    saved["policy"]["enabled"] = False
    with pytest.raises(ValueError, match="consent|disabled"):
        await c.service._guard("1" * 32, current, {"user_id": "4" * 32})
