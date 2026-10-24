# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An exact checkpoint is source provenance, never a reused execution grant."""

import copy

import pytest
from pydantic import ValidationError

from firefly_forge.models import RunInput

REPO, PROJECT, PARENT, REQUEST = ("a" * 32, "b" * 32, "c" * 32, "d" * 32)
REVISION, TREE = "1" * 40, "2" * 40
REFERENCE = dict(
    version=1, run_id=PARENT, request_id=REQUEST, expected_checkpoint_digest="3" * 64, expected_revision=REVISION
)


def request(**changes):
    return dict(
        repository_id=REPO,
        project_id=PROJECT,
        component_id="api",
        brief="Check the retained checkpoint",
        operation="test",
        publish=False,
        source_checkpoint=copy.deepcopy(REFERENCE),
        **changes,
    )


def test_exact_checkpoint_is_a_typed_test_source_without_repository_alias():
    value = RunInput.model_validate(request())
    assert value.source_checkpoint.model_dump() == REFERENCE
    assert value.repository_id == REPO and value.project_id == PROJECT and value.component_id == "api"


@pytest.mark.parametrize(
    "change",
    [
        dict(operation="build"),
        dict(operation="maintain", publish=True),
        dict(publish=True),
        dict(project_id=None, component_id=None),
    ],
)
def test_checkpoint_source_never_becomes_build_publish_or_unscoped_authority(change):
    value = request()
    value.update(change)
    with pytest.raises(ValidationError):
        RunInput.model_validate(value)


@pytest.mark.parametrize(
    "field", ["source_ref", "source_branch", "source_revision", "base_revision", "campaign_context"]
)
def test_untyped_source_overrides_remain_forbidden(field):
    with pytest.raises(ValidationError):
        RunInput.model_validate(dict(repository_id=REPO, brief="Check", **{field: "arbitrary"}))


@pytest.mark.parametrize(
    "change",
    [dict(version=True), dict(expected_revision="main"), dict(request_id="not-native"), dict(branch="foreign")],
)
def test_checkpoint_reference_requires_exact_version_identity_and_revision(change):
    value = request()
    value["source_checkpoint"].update(change)
    with pytest.raises(ValidationError):
        RunInput.model_validate(value)


def test_absent_or_null_checkpoint_preserves_legacy_wire_and_digest():
    from firefly_forge.git_deliveries import delivery_digest

    fields = dict(repository_id=REPO, brief="Check", operation="test")
    old = RunInput.model_validate(fields)
    explicit = RunInput.model_validate({**fields, "source_checkpoint": None})
    assert "source_checkpoint" not in old.model_dump()
    assert old.model_dump(mode="json", exclude_unset=True) == fields
    assert explicit.model_dump(mode="json", exclude_unset=True) == fields
    assert delivery_digest({"version": 1, "input": fields}) == delivery_digest(
        {"version": 1, "input": explicit.model_dump(mode="json", exclude_unset=True)}
    )


@pytest.fixture
def source_case(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import UUID

    from firefly_forge.git_deliveries import delivery_digest
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess

    user = Principal(UUID("e" * 32), "public-issuer", "public-subject")
    token = CURRENT_PRINCIPAL.set(user)
    state = dict(allowed=True, mutation=None, remote=True)
    run = dict(
        id=PARENT,
        repository_id=REPO,
        project_id=PROJECT,
        component_id="api",
        branch="forge/" + PARENT[:12],
        scm_snapshot={"revision": "original"},
        issue_context={"provider": "github", "receipt_id": "f" * 32, "actor": "owner", "kind": "issue", "number": 4},
    )
    checkpoint = dict(
        run_id=PARENT,
        request_id=REQUEST,
        checkpoint_digest="3" * 64,
        revision=REVISION,
        source_tree=TREE,
        branch=run["branch"],
    )
    remote = dict(
        request_id=REQUEST,
        revision=REVISION,
        remote_status="draft",
        reference=dict(url="https://example.invalid/pull/4", body_sha256="7" * 64),
        intent_digest="4" * 64,
    )
    entries = [
        dict(
            kind="git_delivery_remote_intent",
            sequence=1,
            digest="4" * 64,
            idempotency_key="remote",
            payload=dict(request_id=REQUEST, revision=REVISION, body_sha256="7" * 64),
        ),
        dict(kind="git_delivery_remote_receipt", sequence=2, digest="5" * 64, idempotency_key="remote", payload=remote),
    ]

    @asynccontextmanager
    async def unit(**kwargs):
        yield SimpleNamespace()

    async def current(_):
        return copy.deepcopy(run)

    async def repository(_):
        return dict(id=REPO, provider="github", url="https://example.invalid/factory.git", default_branch="main")

    async def authority(*args, **kwargs):
        if not state["allowed"]:
            raise PermissionError("Current execute permission revoked")

    async def history(_):
        return copy.deepcopy(entries)

    async def snapshot_head(_):
        return entries[-1]["digest"]

    async def fetch(*args):
        assert state["remote"], "Remote availability must be checked"
        if state["mutation"]:
            state["mutation"]()
        return REVISION

    store = SimpleNamespace(get_run=current, repository=repository, _unit=unit)
    from firefly_forge.git_deliveries import GitDeliveries

    deliveries = SimpleNamespace(
        store=store,
        _authorize=authority,
        _entries=history,
        _records=lambda value: [copy.deepcopy(checkpoint)],
        _remote_state=GitDeliveries._remote_state,
    )
    monkeypatch.setattr(ProjectAccess, "authorize_actor", authority)
    monkeypatch.setattr(ProjectAccess, "require_repository", authority)
    try:
        yield SimpleNamespace(
            user=user,
            state=state,
            run=run,
            checkpoint=checkpoint,
            entries=entries,
            store=store,
            deliveries=deliveries,
            fetch=fetch,
            head=snapshot_head,
            digest=delivery_digest,
        )
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def source_service(case):
    from firefly_forge.checkpoint_test_sources import CheckpointTestSources

    service = CheckpointTestSources(case.deliveries)
    service._observe_revision = case.fetch
    service._head = case.head
    return service


async def test_server_resolves_immutable_source_and_never_copies_parent_grant(source_case):
    c = source_case
    service = await source_service(c)
    value = await service.prepare(RunInput.model_validate(request()))
    assert value["revision"] == REVISION and value["source_tree"] == TREE
    assert value["reference"] == REFERENCE and value["branch"] == c.run["branch"]
    assert not {"execution_actor", "sandbox_snapshot", "provider_recovery", "commands"} & value.keys()
    assert value["digest"] == c.digest({k: v for k, v in value.items() if k != "digest"})


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_repo",
        "foreign_component",
        "changed_digest",
        "changed_revision",
        "no_remote",
        "unknown_remote",
        "revoked",
        "late_revoked",
        "late_parent_changed",
        "late_head_changed",
    ],
)
async def test_source_admission_refuses_foreign_unknown_stale_or_revoked_provenance(source_case, mutation):
    c = source_case
    service = await source_service(c)
    if mutation == "foreign_repo":
        c.run["repository_id"] = "9" * 32
    elif mutation == "foreign_component":
        c.run["component_id"] = "other"
    elif mutation == "changed_digest":
        c.checkpoint["checkpoint_digest"] = "8" * 64
    elif mutation == "changed_revision":
        c.checkpoint["revision"] = "8" * 40
    elif mutation == "no_remote":
        c.entries.clear()
    elif mutation == "unknown_remote":
        c.entries.pop()
    elif mutation == "revoked":
        c.state["allowed"] = False
    elif mutation == "late_revoked":
        c.state["mutation"] = lambda: c.state.update(allowed=False)
    elif mutation == "late_parent_changed":
        c.state["mutation"] = lambda: c.run.update(component_id="other")
    elif mutation == "late_head_changed":
        c.state["mutation"] = lambda: c.entries[-1].update(digest="8" * 64)
    with pytest.raises((ValueError, PermissionError)):
        await service.prepare(RunInput.model_validate(request()))


async def test_checkpoint_clone_uses_server_branch_sha_and_refuses_snapshot_tampering(source_case):
    from firefly_forge.checkpoint_test_models import frozen_checkpoint_source

    c = source_case
    service = await source_service(c)
    snapshot = await service.prepare(RunInput.model_validate(request()))
    run = {**request(), "source_checkpoint_snapshot": snapshot}
    repository = {"id": REPO, "default_branch": "main", "url": "retained"}
    selected, revision, tree = frozen_checkpoint_source(run, repository)
    assert repository["default_branch"] == "main"
    assert selected == {**repository, "default_branch": c.run["branch"]}
    assert revision == REVISION and tree == TREE
    for key, value in [
        ("operation", "build"),
        ("publish", True),
        ("repository_id", "9" * 32),
        ("source_checkpoint_snapshot", None),
    ]:
        with pytest.raises(ValueError):
            frozen_checkpoint_source({**run, key: value}, repository)
    damaged = copy.deepcopy(snapshot)
    damaged["revision"] = "9" * 40
    with pytest.raises(ValueError):
        frozen_checkpoint_source({**run, "source_checkpoint_snapshot": damaged}, repository)
    assert frozen_checkpoint_source({"operation": "test"}, repository) == (repository, None, None)


async def test_exact_checkpoint_fetch_and_clone_use_real_git_without_changing_registered_default(tmp_path):
    from types import SimpleNamespace

    from test_git_delivery_local import git

    from firefly_forge.checkpoint_test_sources import CheckpointTestSources
    from firefly_forge.scm import clone

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "--initial-branch=main")
    git(origin, "config", "user.name", "Public fixture")
    git(origin, "config", "user.email", "fixture@example.invalid")
    (origin / "source.py").write_text("original = True\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "Original public source")
    baseline = git(origin, "rev-parse", "HEAD")
    branch = "forge/" + PARENT[:12]
    git(origin, "checkout", "-qb", branch)
    (origin / "source.py").write_text("verified_source = True\n")
    git(origin, "commit", "-qam", "Retained public checkpoint")
    revision = git(origin, "rev-parse", "HEAD")
    tree = git(origin, "rev-parse", "HEAD^{tree}")

    async def environment(repository):
        return None

    calls = []
    connections = SimpleNamespace(assert_snapshot=lambda *args: calls.append("scope"), git_environment=environment)
    service = CheckpointTestSources(SimpleNamespace(store=None, engine=SimpleNamespace(connections=connections)))
    repository = dict(id=REPO, url=str(origin), default_branch="main")
    receipt = dict(branch=branch, revision=revision, source_tree=tree)
    assert await service._observe_revision(repository, {}, receipt) == revision
    assert calls == ["scope", "scope"]
    target = tmp_path / "test-run"
    assert (
        await clone({**repository, "default_branch": branch}, target, "forge/new-test", revision=revision) == revision
    )
    assert git(target, "rev-parse", "HEAD^{tree}") == tree
    assert git(target, "merge-base", "--is-ancestor", baseline, revision) == ""
    assert repository["default_branch"] == "main"
    (origin / "source.py").write_text("later_source = True\n")
    git(origin, "commit", "-qam", "A later public checkpoint")
    with pytest.raises(ValueError, match="remote checkpoint source changed"):
        await service._observe_revision(repository, {}, receipt)


async def test_ordinary_test_verification_keeps_exact_commit_without_implementation_checkpoint(tmp_path):
    from types import SimpleNamespace

    from test_git_delivery_local import git

    from firefly_forge.engine import Engine

    root = tmp_path / "verified-source"
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=main")
    git(root, "config", "user.name", "Public fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "source.py").write_text("checked = True\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Retained public source")
    revision = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")
    updates = []

    async def current(_):
        return dict(
            operation="test",
            publish=False,
            source_tree=tree,
            base_revision=revision,
            changed_files=[],
            git_delivery_policy={"enabled": True},
        )

    async def exact(*args):
        assert git(root, "rev-parse", "HEAD^{tree}") == tree

    async def checkpoint(*args, **kwargs):
        raise AssertionError("A test run has no completed implementation unit or new publish grant")

    async def update(run_id, **fields):
        updates.append(fields)

    fake = SimpleNamespace(
        _assert_delivery_tree=exact,
        store=SimpleNamespace(get_run=current, update=update),
        git_deliveries=SimpleNamespace(checkpoint=checkpoint),
    )
    await Engine._checkpoint_commit(fake, "9" * 32, root)
    assert updates == [{"checkpoint_revision": revision}]
    assert git(root, "rev-parse", "HEAD") == revision


async def test_locked_source_cut_refuses_head_scope_actor_and_snapshot_drift(source_case, monkeypatch):
    from types import SimpleNamespace

    import firefly_forge.checkpoint_test_sources as module

    c = source_case
    service = await source_service(c)
    body = RunInput.model_validate(request())
    snapshot = await service.prepare(body)

    async def row(*args, **kwargs):
        return SimpleNamespace(data=copy.deepcopy(c.run))

    c.store.run_records = SimpleNamespace(find_by_id=row)
    monkeypatch.setattr(module, "current_unit_of_work", lambda: object(), raising=False)
    await service.revalidate_locked(body, snapshot)
    c.entries[-1]["digest"] = "0" * 64
    with pytest.raises(ValueError, match="provenance changed"):
        await service.revalidate_locked(body, snapshot)
    c.entries[-1]["digest"] = snapshot["journal_head"]
    c.run["component_id"] = "other"
    with pytest.raises(ValueError, match="original repository"):
        await service.revalidate_locked(body, snapshot)
    c.run["component_id"] = "api"
    c.state["allowed"] = False
    with pytest.raises(PermissionError):
        await service.revalidate_locked(body, snapshot)
    c.state["allowed"] = True
    bad = {**snapshot, "repository_id": "9" * 32}
    bad["digest"] = c.digest({k: v for k, v in bad.items() if k != "digest"})
    with pytest.raises(ValueError):
        await service.revalidate_locked(body, bad)


async def test_source_prepare_refuses_ambient_unit_and_locked_recheck_refuses_no_unit(source_case, monkeypatch):
    from types import SimpleNamespace

    import firefly_forge.checkpoint_test_sources as module

    service = await source_service(source_case)
    body = RunInput.model_validate(request())
    snapshot = await service.prepare(body)

    async def row(*args, **kwargs):
        return SimpleNamespace(data=copy.deepcopy(source_case.run))

    source_case.store.run_records = SimpleNamespace(find_by_id=row)
    with pytest.raises(ValueError, match="native transaction"):
        await service.revalidate_locked(body, snapshot)
    monkeypatch.setattr(module, "current_unit_of_work", lambda: object(), raising=False)
    with pytest.raises(ValueError, match="outside"):
        await service.prepare(body)


async def test_large_logical_source_preflight_only_observes_ref_without_fetch_or_object_database(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from test_git_delivery_local import git

    import firefly_forge.checkpoint_test_sources as module
    from firefly_forge import scm

    origin = tmp_path / "large-logical-origin"
    origin.mkdir()
    git(origin, "init", "--bare", "-q")
    git(origin, "config", "user.name", "Public fixture")
    git(origin, "config", "user.email", "fixture@example.invalid")

    def input_git(argv, content):
        result = subprocess.run(["git", *argv], cwd=origin, input=content, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    blob = input_git(["hash-object", "-w", "--stdin"], "x" * 65536)
    tree = input_git(["mktree"], "".join(f"100644 blob {blob}\tfile-{index:04}.txt\n" for index in range(2000)))
    revision = git(origin, "commit-tree", tree, "-m", "A 125 MiB logical source with shared blob storage")
    branch = "forge/" + PARENT[:12]
    git(origin, "update-ref", "refs/heads/" + branch, revision)
    calls = []
    checked = scm.checked

    async def observe_only(argv, cwd, timeout=300, *, env=None):
        calls.append(argv)
        assert argv[1] == "ls-remote", "Source admission must not fetch any repository objects"
        assert not (cwd / "objects").exists() and not (cwd / ".git").exists()
        return await checked(argv, cwd, timeout, env=env)

    monkeypatch.setattr(scm, "checked", observe_only)
    monkeypatch.setattr(module, "checked", observe_only, raising=False)

    async def credentials(repository):
        return None

    connections = SimpleNamespace(assert_snapshot=lambda *args: None, git_environment=credentials)
    service = module.CheckpointTestSources(SimpleNamespace(store=None, engine=SimpleNamespace(connections=connections)))
    value = await service._observe_revision(
        dict(url=str(origin), default_branch="main"), {}, dict(branch=branch, revision=revision, source_tree=tree)
    )
    assert value == revision
    assert len(calls) == 1 and calls[0][-1] == "refs/heads/" + branch
