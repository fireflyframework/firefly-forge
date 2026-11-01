# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real canonical source/ignored outputs; explicit fictional owner and input-scope seams."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from linked_verification_fixtures import candidate
from test_checkpoint_source_identity_host import SourceStore, git, repository

from firefly_forge import linked_verifications as module
from firefly_forge.agentic_checkpoint_invocation import workspace_source_capture
from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.known_worker_completions import _task
from firefly_forge.linked_verification_models import LinkedVerificationCandidateV1
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.verification_scope_attestation import PreparedBuildScope, VerificationScopeV1

pytest_plugins = ["test_linked_verification_consumer_host"]


@pytest.fixture
async def source_case(tmp_path, monkeypatch):
    root = repository(tmp_path)
    (root / ".gitignore").write_text("generated/\nnode_modules/\ndist/\n.cache/\n")
    (root / "generated").mkdir()
    (root / "generated/tracked.txt").write_text("Tracked even though its directory is ignored\n")
    git(root, "add", ".gitignore")
    git(root, "add", "-f", "generated/tracked.txt")
    git(root, "commit", "-qm", "Freeze tracked ignored source and output rules")
    store = SourceStore()
    store.execution_ownership_token = object()
    store.execution_ownership_identity = {"backend_pid": 1, "backend_start": "fictional", "instance": "fixture"}
    store.configuration = SimpleNamespace(write_generation=object())
    run = {"id": "b" * 32, "repository_id": "c" * 32}
    task = SimpleNamespace(task_id="verify", status="queued", attempt=1, data={"idempotency_key": "d" * 32})

    async def owner():
        pass

    async def get_run(run_id):
        assert run_id == run["id"]
        return copy.deepcopy(run)

    @asynccontextmanager
    async def unit():
        yield

    async def entries(run_id):
        return []

    async def verify_task(run_id, kind):
        assert kind == "verify"
        return task

    store.assert_ownership, store.get_run, store._unit = owner, get_run, unit
    engine = SimpleNamespace(active={}, active_projects={})
    journal = SimpleNamespace(records=SimpleNamespace(find_for_run=entries), _task=verify_task)
    service = module.LinkedVerifications(store, journal, SimpleNamespace(), engine)
    monkeypatch.setattr(service, "_authority", lambda *args: owner())
    actor = Principal(UUID(int=1), "fixture", "owner", authentication_method="local_bootstrap")
    source = await workspace_source_capture(store, root, assert_current=owner)
    scope = VerificationScopeV1(
        version=1,
        run_id=run["id"],
        handoff_intent_digest="1" * 64,
        source_digest="2" * 64,
        commands_digest="3" * 64,
        scope_digest="4" * 64,
        runtime_contract="5" * 64,
        dependency_digest="6" * 64,
        environment_digest="7" * 64,
        credential_digest="8" * 64,
        prerequisite_digest="9" * 64,
        wrapper_digest="a" * 64,
        declaration_digest="b" * 64,
        dependency_roots=[str(root)],
        scratch=str(root.parent / "unused-scratch"),
        checks=[
            dict(
                command_index=1,
                kind="node_build",
                argv=["fictional-build"],
                wrapped_argv=["fictional-wrapper", "-p", "deny-network", "fictional-build"],
            )
        ],
    )

    async def measure():
        return scope

    inputs = PreparedBuildScope(scope, measure, owner, dict)
    value = candidate(1)
    value.update(
        actor_digest=module.actor_digest(actor),
        run_digest=digest(run),
        journal_digest=digest({"entries": []}),
        source_digest=digest(source.cut),
        input_scope_digest=digest(scope.model_dump(mode="json")),
        owner_digest=digest(
            {"owner": store.execution_ownership_identity, "generation": id(store.configuration.write_generation)}
        ),
    )
    value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
    proof = LinkedVerificationCandidateV1.model_validate(value)
    monkeypatch.setattr(module, "_runtime_material_digest", lambda: proof.runtime_contract)
    monkeypatch.setattr(module, "profile_digest", lambda: proof.profile_digest)
    monkeypatch.setattr(module, "_policy_material_digest", lambda: proof.policy_digest)
    prepared = module.PreparedLinkedVerification(
        proof,
        run,
        [],
        _task(task),
        root,
        source,
        inputs,
        actor,
        CURRENT_PRINCIPAL.get(),
        store.execution_ownership_token,
        store.configuration.write_generation,
        service,
        None,
    )
    try:
        yield SimpleNamespace(root=root, prepared=prepared, store=store)
    finally:
        prepared.close()


async def test_ignored_output_creation_does_not_change_selected_source_or_index(source_case):
    c = source_case
    await c.prepared.revalidate()
    for name in ("dist", ".cache"):
        (c.root / name).mkdir()
        (c.root / name / "output.txt").write_text("Small fictional QA output\n")
    current = await workspace_source_capture(c.store, c.root)
    try:
        assert current.cut == c.prepared.source.cut
        assert current.index == c.prepared.source.index
        assert current.physical_digest == c.prepared.source.physical_digest
    finally:
        current.fence.close()
    with pytest.raises(ValueError, match="metadata"):
        c.prepared.source.fence.revalidate()
    await c.prepared.after_result()
    await c.prepared.revalidate()


async def test_preexisting_unignored_vitest_results_are_not_recoverable_as_ignored_outputs(source_case):
    """The public source excludes this cache; its selected Git bytes still require a hold."""
    from firefly_forge.source_catalog import SourceCatalogBudget
    from firefly_forge.verification_dependencies import prepare_public_source_cut

    c = source_case
    ignore = c.root / ".gitignore"
    ignore.write_text(ignore.read_text().replace(".cache/\n", ""))
    result = c.root / ".cache/vite/vitest/fixture-project/results.json"
    result.parent.mkdir(parents=True)
    result.write_text('{"files":[],"passed":3}\n')
    before = await workspace_source_capture(c.store, c.root)
    c.prepared.source.fence.close()
    c.prepared.source = before
    fields = c.prepared.candidate.model_dump(mode="json")
    fields["source_digest"] = digest(before.cut)
    fields["digest"] = digest({key: value for key, value in fields.items() if key != "digest"})
    c.prepared.candidate = LinkedVerificationCandidateV1.model_validate(fields)
    public = await prepare_public_source_cut(c.root, c.root.parent / "public-cut", SourceCatalogBudget())
    await c.prepared.revalidate()
    assert result.relative_to(c.root).as_posix() in git(c.root, "ls-files", "--others", "--exclude-standard")
    result.write_text('{"files":[],"passed":4}\n')
    await public.assert_current()
    # The real worker reaches this only after retaining its exact exit-zero result.
    with pytest.raises(module._PostResultGuardFailure) as held:
        await c.prepared.after_result()
    assert held.value.diagnostic.model_dump() == {
        "phase": "post_result_source",
        "cause_code": "selected_source_changed",
        "exception_type": "ValueError",
    }


@pytest.mark.parametrize("change", ["bytes", "tracked_ignored", "index", "symlink", "new_source"])
async def test_output_rebinding_cannot_authorize_selected_source_or_index_changes(source_case, change):
    c = source_case
    (c.root / "dist").mkdir()
    (c.root / "dist/output.txt").write_text("Ignored QA output")
    if change == "bytes":
        (c.root / "package.json").write_text('{"name":"foreign-source"}\n')
    elif change == "tracked_ignored":
        (c.root / "generated/tracked.txt").write_text("Changed tracked ignored asset")
    elif change == "index":
        git(c.root, "update-index", "--chmod=+x", "package.json")
    elif change == "symlink":
        (c.root / "generated/tracked.txt").unlink()
        (c.root / "generated/tracked.txt").symlink_to(c.root / "package.json")
    else:
        (c.root / "new-source.py").write_text("VALUE = 2\n")
    with pytest.raises(ValueError, match="source|index|metadata"):
        await c.prepared.after_result()


@pytest.fixture(autouse=True)
def output_rules_before_history(monkeypatch):
    import test_invalid_read_native_protocol_host as protocol

    activate = protocol.activated

    async def source_before_frozen_history(*args):
        env = await activate(*args)
        (env.workspace / ".gitignore").write_text("dist/\n.cache/\n")
        return env

    monkeypatch.setattr(protocol, "activated", source_before_frozen_history)


async def test_actual_worker_known_result_rebinds_outputs_before_next_check_and_parent_settlement(
    linked_case, monkeypatch
):
    from test_linked_verification_consumer_host import admit_and_execute

    from firefly_forge import engine as engine_module
    from firefly_forge.models import Command

    c = linked_case
    original = copy.deepcopy(await c.env.store.get_run(c.env.run_id))
    posts = len(c.env.posts)
    monkeypatch.setattr(
        engine_module,
        "policy_commands",
        lambda snapshot, backend: [Command(argv=["controlled-next-check"], timeout=10)],
    )
    backend = c.env.engine.sandbox.backends.select(None)

    class OutputBackend:
        async def run(self, config, argv, cwd, timeout, maximum):
            result = await backend.run(config, argv, cwd, timeout, maximum)
            if len(c.calls) == 1:
                for name in ("dist", ".cache"):
                    (c.env.workspace / name).mkdir()
                    (c.env.workspace / name / "output.txt").write_text("Controlled fresh QA output\n")
            else:
                assert argv == ["controlled-next-check"]
                assert (c.env.workspace / "dist/output.txt").is_file()
            return result

    monkeypatch.setattr(c.env.engine.sandbox.backends, "select", lambda config: OutputBackend())
    result = await admit_and_execute(c)
    assert result["status"] == "completed", result
    assert result["progress"]["started"] == result["progress"]["completed"] == 2
    assert c.calls == [c.wrapped, ["controlled-next-check"]]
    current = await c.env.store.get_run(c.env.run_id)
    assert current["usage"] == original["usage"] and current["attempt"] == original["attempt"]
    assert current["verification_handoff"] == original["verification_handoff"]
    assert len(c.env.posts) == posts
    task = next(task for task in (await c.env.journal.plan(c.env.run_id))["tasks"] if task["id"] == "verify")
    assert task["status"] == "done" and len(task["evidence"]["checks"]) == 2
