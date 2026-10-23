# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned Git/native staging controls; no SQL, model, CRM or real provider."""

# ruff: noqa: F401, F811
from types import SimpleNamespace

import pytest
from test_execution import seed_repository
from test_source_edit_continuations_host import controlled_metadata
from test_workspace_dependency_reconciliation import git, legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge.engine import Engine

CACHE = "frontend/.cache/vite/vitest/" + "a" * 40 + "/results.json"


@pytest.mark.parametrize(
    "fault", ["cache_before_add", "cache_after_add", "staged_during_admission", "source_during_admission"]
)
async def test_snapshot_refuses_unreviewed_index_or_source_before_write_tree(tmp_path, monkeypatch, fault):
    from firefly_forge import engine as module

    root = await seed_repository(tmp_path / "source")
    base = git(root, "rev-parse", "HEAD")
    (root / "implementation.py").write_text("VALUE = 2\n")
    writes = []
    checked = module.checked

    def cache():
        path = root / CACHE
        path.parent.mkdir(parents=True)
        path.write_text("public-generated-result\n")

    async def inspect(argv, *args, **kwargs):
        if argv[:2] == ["git", "write-tree"]:
            writes.append(True)
        if argv[:3] == ["git", "add", "--all"] and fault == "cache_before_add":
            cache()
        result = await checked(argv, *args, **kwargs)
        if argv[:3] == ["git", "add", "--all"] and fault == "cache_after_add":
            cache()
            git(root, "add", "--", CACHE)
        return result

    async def admitted(_patch):
        if fault == "staged_during_admission":
            foreign = tmp_path / "public-foreign-blob"
            foreign.write_text("UNREVIEWED = 3\n")
            blob = git(root, "hash-object", "-w", str(foreign))
            git(root, "update-index", "--cacheinfo", "100644," + blob + ",implementation.py")
        if fault == "source_during_admission":
            (root / "implementation.py").write_text("UNREVIEWED = 3\n")

    monkeypatch.setattr(module, "checked", inspect)
    store = SimpleNamespace(
        path=tmp_path / "fixture.db", vault_source_patch_admission=admitted, reject_credentials=lambda _value: None
    )
    with pytest.raises(ValueError):
        await Engine(store, tmp_path / "workspaces")._snapshot(root, base)
    assert writes == []


async def test_snapshot_never_newly_stages_known_untracked_vitest_cache(tmp_path):
    root = await seed_repository(tmp_path / "source")
    base = git(root, "rev-parse", "HEAD")
    cache = root / CACHE
    cache.parent.mkdir(parents=True)
    cache.write_text('{"public":"known-generated-result"}\n')
    before = cache.read_bytes()
    (root / "implementation.py").write_text("VALUE = 2\n")

    async def admitted(_patch):
        pass

    store = SimpleNamespace(
        path=tmp_path / "fixture.db", vault_source_patch_admission=admitted, reject_credentials=lambda _value: None
    )
    engine = Engine(store, tmp_path / "workspaces")
    files, patch, _tree = await engine._snapshot(root, base)
    assert "implementation.py" in files
    assert CACHE not in files and CACHE not in patch
    assert git(root, "ls-files", "--", CACHE) == ""
    assert cache.read_bytes() == before


@pytest.fixture
def indexed_cache(legacy_workspace):
    from firefly_forge.workspace_reconciliation import apply_dependency_plan, dependency_plan

    root, base, metadata, _target = legacy_workspace
    apply_dependency_plan(root, dependency_plan(root, base, metadata, component="."))
    cache = root / CACHE
    cache.parent.mkdir(parents=True)
    cache.write_text('{"public":"known-generated-result"}\n')
    git(root, "add", "--", CACHE)
    return root, base, cache


def test_reviewed_index_effect_preserves_physical_cache_source_and_metadata(indexed_cache):
    from firefly_forge.git_workspace import metadata_digest
    from firefly_forge.source_index_sanitation import apply_sanitation, sanitation_plan
    from firefly_forge.verification_models import verification_source

    root, base, cache = indexed_cache
    source, metadata, physical = verification_source(root), metadata_digest(root), cache.read_bytes()
    plan = sanitation_plan(root, base, component=".")
    assert [item["path"] for item in plan["artifacts"]] == [CACHE]
    assert plan["artifacts"][0]["mode"] == "100644"
    effect = apply_sanitation(root, plan)
    assert effect["index_before"] != effect["index_after"]
    assert git(root, "ls-files", "--", CACHE) == ""
    assert cache.read_bytes() == physical
    assert verification_source(root) == source and metadata_digest(root) == metadata
    with pytest.raises(ValueError):
        apply_sanitation(root, plan)


@pytest.mark.parametrize(
    "fault",
    ["baseline", "private", "foreign_blob", "symlink", "hardlink", "unknown", "oversize", "count", "case_alias"],
)
def test_sanitation_refuses_unproved_or_unbounded_index_entries(indexed_cache, tmp_path, monkeypatch, fault):
    from firefly_forge import source_index_sanitation as module

    root, base, cache = indexed_cache
    if fault == "baseline":
        git(root, "commit", "--quiet", "-m", "Existing public repository asset")
        base = git(root, "rev-parse", "HEAD")
    elif fault == "private":
        private = root / (".ssh/.cache/vite/vitest/" + "b" * 40 + "/results.json")
        private.parent.mkdir(parents=True)
        private.write_text("public-fixture\n")
        git(root, "add", "--", str(private.relative_to(root)))
    elif fault == "foreign_blob":
        foreign = tmp_path / "foreign.txt"
        foreign.write_text("unreviewed-index-content\n")
        blob = git(root, "hash-object", "-w", str(foreign))
        git(root, "update-index", "--cacheinfo", "100644," + blob + "," + CACHE)
    elif fault in {"symlink", "hardlink"}:
        import os

        cache.unlink()
        target = tmp_path / "public-target.txt"
        target.write_text("public-fixture\n")
        if fault == "symlink":
            cache.symlink_to(target)
        else:
            os.link(target, cache)
        git(root, "add", "--", CACHE)
    elif fault == "unknown":
        output = root / "frontend/dist/bundle.js"
        output.parent.mkdir(parents=True)
        output.write_text("public-fixture\n")
        git(root, "add", "--", str(output.relative_to(root)))
    elif fault == "oversize":
        cache.write_bytes(b"x" * (module.MAX_ARTIFACT_BYTES + 1))
        git(root, "add", "--", CACHE)
    elif fault == "count":
        for n in range(module.MAX_ARTIFACTS):
            output = root / ("frontend/.cache/vite/vitest/" + format(n, "040x") + "/results.json")
            output.parent.mkdir(parents=True)
            output.write_text("public-fixture\n")
            git(root, "add", "--", str(output.relative_to(root)))
    else:
        actual = module._descriptor_path

        def alias(fd):
            path = actual(fd)
            return path.with_name("RESULTS.JSON") if path == cache else path

        monkeypatch.setattr(module, "_descriptor_path", alias)
    before = (root / ".git/index").read_bytes()
    with pytest.raises((ValueError, OSError)):
        module.sanitation_plan(root, base, component=".")
    assert (root / ".git/index").read_bytes() == before


def test_changed_public_source_refuses_before_index_effect(indexed_cache):
    from firefly_forge.source_index_sanitation import apply_sanitation, sanitation_plan

    root, base, _cache = indexed_cache
    plan = sanitation_plan(root, base, component=".")
    (root / "frontend/app.js").write_text("export const value = 999;\n")
    before = (root / ".git/index").read_bytes()
    with pytest.raises(ValueError):
        apply_sanitation(root, plan)
    assert (root / ".git/index").read_bytes() == before


@pytest.mark.parametrize("baseline_asset", [False, True])
async def test_snapshot_preserves_baseline_assets_and_never_auto_removes_staged_cache(tmp_path, baseline_asset):
    root = await seed_repository(tmp_path / "source")
    cache = root / CACHE
    cache.parent.mkdir(parents=True)
    cache.write_text("public-baseline\n")
    git(root, "add", "--", CACHE)
    if baseline_asset:
        git(root, "commit", "--quiet", "-m", "Preserve established tracked asset")
    base = git(root, "rev-parse", "HEAD")
    cache.write_text("public-current\n")
    (root / "implementation.py").write_text("VALUE = 2\n")

    async def admitted(_patch):
        pass

    store = SimpleNamespace(
        path=tmp_path / "fixture.db", vault_source_patch_admission=admitted, reject_credentials=lambda _value: None
    )
    engine = Engine(store, tmp_path / "workspaces")
    before = (root / ".git/index").read_bytes()
    if baseline_asset:
        files, _patch, _tree = await engine._snapshot(root, base)
        assert CACHE in files and git(root, "ls-files", "--", CACHE) == CACHE
    else:
        with pytest.raises(ValueError, match="explicit native sanitation"):
            await engine._snapshot(root, base)
        assert (root / ".git/index").read_bytes() == before
        assert git(root, "ls-files", "--", CACHE) == CACHE


async def _retained_unknown_with_cache(env, tmp_path, monkeypatch):
    from test_source_edit_observed_reconciliation_host import existing_effect

    env, original_request, original, catalog = await existing_effect(env, tmp_path, monkeypatch)
    cache = env.root / CACHE
    cache.parent.mkdir(parents=True)
    cache.write_text('{"public":"known-generated-result"}\n')
    git(env.root, "add", "--", CACHE)
    return env, original_request, original, catalog, cache


def _sanitation_request(review, catalog):
    from uuid import uuid4

    from firefly_forge.source_index_sanitation import IndexSanitationInput

    return IndexSanitationInput(
        version=1,
        request_id=uuid4().hex,
        original_intent_digest=review["original_intent_digest"],
        candidate_digest=review["digest"],
        admission_contract_digest=review["admission_contract_digest"],
        previous_catalog_path=str(catalog),
        valid_for_seconds=900,
        acknowledge_index_only_generated_artifacts=True,
        acknowledge_preserve_files_source_and_budgets=True,
        acknowledge_original_scm_outcome_unknown=True,
        acknowledge_no_queue_or_provider_dispatch=True,
    )


async def test_native_sanitation_intent_precedes_effect_and_never_queues_existing_task(
    reconciliation_service, tmp_path, monkeypatch
):
    import copy

    from firefly_forge import source_index_sanitation as module
    from firefly_forge.source_edit_observed_reconciliation import ObservedSourceReconciliation

    env, old_request, original, catalog, cache = await _retained_unknown_with_cache(
        reconciliation_service, tmp_path, monkeypatch
    )
    service = ObservedSourceReconciliation(env.service)
    with pytest.raises(ValueError, match="foreign or private"):
        await service.candidate(env.root.name, old_request, str(catalog))
    review = await service.index_sanitation_candidate(env.root.name, old_request, str(catalog))
    body = _sanitation_request(review, catalog)
    before_wal = [copy.deepcopy(row.data) for row in env.journal.records.rows]
    before_run = copy.deepcopy(env.store.run.data)
    physical = cache.read_bytes(), cache.stat()
    effects = []
    apply = module.apply_sanitation

    def effect(root, plan):
        assert env.journal.records.rows[-1].data["kind"] == module.SANITATION_INTENT
        effects.append(plan)
        return apply(root, plan)

    monkeypatch.setattr(module, "apply_sanitation", effect)
    receipt = await service.sanitize_index(env.root.name, old_request, body)
    assert len(effects) == 1 and receipt["no_queue_or_provider_dispatch"] is True
    assert await service.index_sanitation_get(env.root.name, old_request, body.request_id) == receipt
    assert await service.sanitize_index(env.root.name, old_request, body) == receipt
    assert len(effects) == 1
    assert env.store.run.data == before_run and env.store.run.status == "failed" and env.task.attempt == 2
    assert [row.data for row in env.journal.records.rows][: len(before_wal)] == before_wal
    assert [row.data["kind"] for row in env.journal.records.rows][-2:] == [
        module.SANITATION_INTENT,
        module.SANITATION_RECEIPT,
    ]
    assert cache.read_bytes() == physical[0] and cache.stat() == physical[1]
    assert git(env.root, "ls-files", "--", CACHE) == ""
    assert (await env.service.get(env.root.name, old_request))["status"] == "needs_reconciliation"
    strict = await service.candidate(env.root.name, old_request, str(catalog))
    assert strict["original_intent_digest"] == original["digest"]


@pytest.mark.parametrize("fault", ["revoke", "changed_source", "lost_effect", "expired", "receipt_failure"])
async def test_native_sanitation_custody_refuses_late_changes_and_never_replays(
    reconciliation_service, tmp_path, monkeypatch, fault
):
    from firefly_forge import source_edit_continuations
    from firefly_forge import source_index_sanitation as module
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.source_edit_observed_reconciliation import ObservedSourceReconciliation

    env, old_request, _original, catalog, cache = await _retained_unknown_with_cache(
        reconciliation_service, tmp_path, monkeypatch
    )
    service = ObservedSourceReconciliation(env.service)
    review = await service.index_sanitation_candidate(env.root.name, old_request, str(catalog))
    body = _sanitation_request(review, catalog)
    before = (env.root / ".git/index").read_bytes()
    effects = []
    apply = module.apply_sanitation
    now = source_edit_continuations.clock_ms()
    clock = [now]
    monkeypatch.setattr(source_edit_continuations, "clock_ms", lambda: clock[0])

    async def admitted(_patch):
        if fault == "revoke":
            env.identity.enabled = False
        if fault == "changed_source":
            (env.root / "frontend/app.js").write_text("export const value = 999;\n")

    env.store.vault_source_patch_admission = admitted

    def effect(root, plan):
        effects.append(1)
        result = apply(root, plan)
        if fault == "lost_effect":
            raise OSError("Controlled lost sanitation completion")
        if fault == "expired":
            clock[0] = now + 901000
        return result

    monkeypatch.setattr(module, "apply_sanitation", effect)
    append = env.journal._append

    async def write(*args):
        if fault == "receipt_failure" and args[3] == module.SANITATION_RECEIPT:
            raise RuntimeError("Controlled sanitation receipt SQL rollback")
        return await append(*args)

    monkeypatch.setattr(env.journal, "_append", write)
    error = (
        AccessDenied
        if fault == "revoke"
        else OSError
        if fault == "lost_effect"
        else RuntimeError
        if fault == "receipt_failure"
        else ValueError
    )
    with pytest.raises(error):
        await service.sanitize_index(env.root.name, old_request, body)
    assert env.store.run.status == "failed" and env.task.attempt == 2 and cache.is_file()
    if fault in {"revoke", "changed_source"}:
        assert not effects and (env.root / ".git/index").read_bytes() == before
        assert not any(row.data["kind"] == module.SANITATION_INTENT for row in env.journal.records.rows)
    else:
        assert effects == [1]
        assert (await service.index_sanitation_get(env.root.name, old_request, body.request_id))[
            "status"
        ] == "needs_reconciliation"
        with pytest.raises(ValueError, match="unknown|reconciliation"):
            await service.sanitize_index(env.root.name, old_request, body)
        with pytest.raises(ValueError, match="unknown|reconciliation"):
            await service.candidate(env.root.name, old_request, str(catalog))
        assert effects == [1]


def test_native_index_sanitation_kind_names_fit_actual_persistent_schema():
    from firefly_forge.persistence import ForgeJournalEntity
    from firefly_forge.source_index_sanitation import SANITATION_INTENT, SANITATION_RECEIPT

    bound = ForgeJournalEntity.__table__.c.kind.type.length
    assert bound is not None and all(len(name) <= bound for name in (SANITATION_INTENT, SANITATION_RECEIPT))


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", True),
        ("valid_for_seconds", True),
        ("valid_for_seconds", 0),
        ("valid_for_seconds", 1801),
        ("acknowledge_index_only_generated_artifacts", 1),
        ("acknowledge_preserve_files_source_and_budgets", False),
        ("acknowledge_original_scm_outcome_unknown", "true"),
        ("acknowledge_no_queue_or_provider_dispatch", None),
        ("unreviewed_option", True),
    ],
)
def test_index_sanitation_requires_typed_finite_explicit_acknowledgements(key, value):
    from pydantic import ValidationError

    from firefly_forge.source_index_sanitation import IndexSanitationInput

    body = _sanitation_request(
        {"original_intent_digest": "a" * 64, "digest": "b" * 64, "admission_contract_digest": "c" * 64},
        "/public/resources/native/codex-0.163.0/source-only-model.json",
    ).model_dump()
    with pytest.raises(ValidationError):
        IndexSanitationInput.model_validate(body | {key: value})


def test_sanitation_admission_contract_binds_the_unknown_effect_recovery_fence(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    from firefly_forge import source_edit_observed_reconciliation as module

    source = Path(module.__file__).parent
    copied = tmp_path / "package"
    copied.mkdir()
    for path in source.glob("*.py"):
        shutil.copyfile(path, copied / path.name)
    monkeypatch.setattr(module, "__file__", str(copied / "source_edit_observed_reconciliation.py"))
    before = module.admission_contract_digest()
    fence = copied / "provider_recoveries.py"
    fence.write_bytes(fence.read_bytes() + b"\n# Controlled custody-fence revision\n")
    assert module.admission_contract_digest() != before
