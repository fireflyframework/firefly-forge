# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real filesystem and scalar fences remain usable at the final native writer boundary."""

import sys
from types import SimpleNamespace

import pytest

from firefly_forge.journal import digest
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.source_catalog import SourceCatalogBudget

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Confined build scope requires Darwin host")


@pytest.fixture
def build_scope_case(tmp_path, monkeypatch):
    from firefly_forge import verification_scope_attestation as scope

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.py"
    source.write_text("VALUE = 1\n")
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    dependency = dependencies / "module.py"
    dependency.write_text("VALUE = 1\n")
    prerequisite = tmp_path / "config.private.json"
    prerequisite.write_text('{"version":1}\n')
    monkeypatch.setenv("QA_REUSE_INPUT_FILE", str(prerequisite))

    async def no_location(store):
        return None

    monkeypatch.setattr(scope, "current_native_location", no_location)
    monkeypatch.setattr(scope, "_roots", lambda *args: [dependencies])
    commands = [dict(argv=["/fictional/python", "-B", "-m", "ruff", "check", "source.py"], timeout=5)]
    declarations = [
        dict(command_digest=digest(commands[0]), purpose="source_build", independent_of_time_and_external_state=True)
    ]
    policy = dict(
        version=1,
        revision=None,
        settings=dict(
            automatic_verification=dict(enabled=True, max_continuations=1, reusable_build_checks=declarations)
        ),
    )
    policy["digest"] = digest(policy)
    run = dict(
        id="1" * 32,
        invalid_read_continuity=dict(request_id="2" * 32),
        commands=commands,
        sandbox_snapshot=capture_sandbox(SandboxInput()),
        tool_policy_snapshot=policy,
        config_revision="3" * 32,
    )
    intent = dict(
        id="4" * 32,
        workspace=str(workspace),
        source_digest="5" * 64,
        commands_digest=digest(dict(commands=commands)),
        scope_digest="6" * 64,
        reusable_build_declaration_digest=digest(dict(checks=declarations)),
    )
    state = tmp_path / "state"
    state.mkdir()
    configuration = SimpleNamespace(settings_generation=object(), load_snapshot=lambda _: dict(LOCAL="fixture"))
    store = SimpleNamespace(path=state / "factory.db", configuration=configuration)
    registry = {}
    engine = SimpleNamespace(sandbox=SimpleNamespace(credential_registry=lambda: registry))
    journal = SimpleNamespace(source_catalog_budget=SourceCatalogBudget(min_free_bytes=0))

    async def current():
        pass

    return SimpleNamespace(
        workspace=workspace,
        source=source,
        dependencies=dependencies,
        dependency=dependency,
        prerequisite=prerequisite,
        configuration=configuration,
        store=store,
        registry=registry,
        engine=engine,
        journal=journal,
        run=run,
        intent=intent,
        current=current,
    )


@pytest.mark.parametrize(
    "change", ["environment", "dependency", "prerequisite", "source", "credential", "configuration"]
)
async def test_prepared_build_scope_rejects_late_input_change_at_commit(build_scope_case, monkeypatch, change):
    from firefly_forge import verification_scope_attestation as scope

    c = build_scope_case
    prepared = await scope.prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    await prepared.revalidate()
    if change == "environment":
        monkeypatch.setenv("QA_REUSE_LATE_ENV", "changed")
    elif change == "dependency":
        c.dependency.write_text("VALUE = 2\n")
    elif change == "prerequisite":
        c.prerequisite.write_text('{"version":2}\n')
    elif change == "source":
        c.source.write_text("VALUE = 2\n")
    elif change == "credential":
        c.registry["API_KEY"] = "fictional-changed-key"
    else:
        c.configuration.settings_generation = object()
    with pytest.raises(ValueError):
        await prepared.fence()
