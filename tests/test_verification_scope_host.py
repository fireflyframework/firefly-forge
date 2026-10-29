# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Build verification scopes preserve logical commands and refuse external freshness."""

import json

import pytest


def scope_module():
    import firefly_forge.verification_scope as scope

    return scope


def test_local_build_classifier_keeps_postgres_integration_fresh(tmp_path):
    scope = scope_module()
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "node node_modules/vitest/vitest.mjs run --configLoader runner",
                    "build": "node node_modules/vite/bin/vite.js build --configLoader runner",
                    "deploy": "node deploy.js",
                }
            }
        )
    )
    assert (
        scope.build_check_kind(["/tools/python", "-B", "-m", "ruff", "check", "backend"], tmp_path) == "python_static"
    )
    assert (
        scope.build_check_kind(["/tools/python", "-B", "-m", "mypy", "--strict", "backend"], tmp_path)
        == "python_static"
    )
    assert scope.build_check_kind(["/tools/python", "-B", "-m", "pytest", "backend/tests"], tmp_path) is None
    assert scope.build_check_kind(["/tools/npm", "--prefix", "frontend", "run", "test"], tmp_path) == "node_build"
    assert scope.build_check_kind(["/tools/npm", "--prefix", "frontend", "run", "build"], tmp_path) == "node_build"
    assert scope.build_check_kind(["/tools/npm", "--prefix", "frontend", "run", "deploy"], tmp_path) is None
    (frontend / "package.json").write_text(
        json.dumps(
            {"scripts": {"test": "node node_modules/vitest/vitest.mjs run", "pretest": "curl https://service.invalid"}}
        )
    )
    assert scope.build_check_kind(["/tools/npm", "--prefix", "frontend", "run", "test"], tmp_path) is None


def test_network_wrapper_mapping_is_exact_and_never_rewrites_original_input():
    scope = scope_module()
    argv = ["/tools/python", "-B", "-m", "ruff", "check", "backend"]
    original = list(argv)
    wrapped = scope.wrap_build_argv(argv)
    assert argv == original and wrapped == ["/usr/bin/sandbox-exec", "-p", scope.DENY_NETWORK_PROFILE, *original]


async def test_dependency_fingerprint_detects_file_symlink_and_membership_drift(tmp_path):
    from firefly_forge.source_catalog import SourceCatalogBudget
    from firefly_forge.verification_dependencies import dependency_fingerprint

    root = tmp_path / "packages"
    root.mkdir()
    (root / "module.py").write_text("VALUE = 1\n")
    (root / "alias.py").symlink_to("module.py")
    checks = []

    async def current():
        checks.append(True)

    budget = SourceCatalogBudget(min_free_bytes=0)
    first = await dependency_fingerprint([root], tmp_path / "catalog", budget, current)
    (root / "module.py").write_text("VALUE = 2\n")
    second = await dependency_fingerprint([root], tmp_path / "catalog", budget, current)
    assert first != second
    (root / "new.py").write_text("VALUE = 3\n")
    third = await dependency_fingerprint([root], tmp_path / "catalog", budget, current)
    assert third != second and checks


@pytest.mark.parametrize(
    "field", ["environment_digest", "dependency_digest", "credential_digest", "prerequisite_digest"]
)
def test_scope_attestation_rejects_unknown_or_coerced_custody(field):
    from pydantic import ValidationError

    from firefly_forge.verification_scope_attestation import VerificationScopeV1

    body = dict(
        version=1,
        kind="verification_build_scope",
        run_id="1" * 32,
        handoff_intent_digest="2" * 64,
        source_digest="3" * 64,
        commands_digest="4" * 64,
        scope_digest="5" * 64,
        runtime_contract="6" * 64,
        dependency_digest="7" * 64,
        environment_digest="8" * 64,
        credential_digest="9" * 64,
        prerequisite_digest="a" * 64,
        wrapper_digest="b" * 64,
        declaration_digest="c" * 64,
        dependency_roots=["/usr/lib"],
        scratch="/private/tmp/fictional-build",
        checks=[
            dict(
                command_index=2,
                kind="python_static",
                argv=["python", "-m", "ruff"],
                wrapped_argv=["/usr/bin/sandbox-exec", "-p", "deny", "python", "-m", "ruff"],
            )
        ],
    )
    assert VerificationScopeV1.model_validate(body).version == 1
    with pytest.raises(ValidationError):
        VerificationScopeV1.model_validate({**body, field: True})
    with pytest.raises(ValidationError):
        VerificationScopeV1.model_validate({**body, "version": 1.0})


def test_scope_mapping_requires_anchored_witness_and_exact_original_argv():
    from firefly_forge.verification_scope_attestation import mapped_argv

    run = dict(id="1" * 32, commands=[dict(argv=["python", "-m", "ruff"], timeout=5)])
    intent = dict(id="2" * 32, commands_digest="3" * 64, source_digest="4" * 64)
    handoff = dict(kind="verification_handoff_intent", payload=intent, digest="5" * 64, sequence=10)
    assert mapped_argv(run, intent, [], 0) == run["commands"][0]["argv"]
    # An unanchored or mismatched scope cannot authorize a stronger-looking foreign command.
    fake = dict(kind="verification_build_scope", sequence=11, payload=dict(handoff_intent_digest="f" * 64))
    with pytest.raises(ValueError):
        mapped_argv(run, intent, [handoff, fake], 0)


def test_reuse_policy_requires_explicit_scope_and_preserves_absent_legacy_bytes():
    from pydantic import ValidationError

    from firefly_forge.verification_models import AutomaticVerificationPolicy

    assert AutomaticVerificationPolicy().model_dump(mode="json") == dict(enabled=True, max_continuations=1)
    declaration = dict(command_digest="1" * 64, purpose="source_build", independent_of_time_and_external_state=True)
    assert AutomaticVerificationPolicy(reusable_build_checks=[declaration]).reusable_build_checks
    with pytest.raises(ValidationError):
        AutomaticVerificationPolicy(
            reusable_build_checks=[{**declaration, "independent_of_time_and_external_state": 1}]
        )


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="Darwin confinement is a scoped transport")
async def test_real_build_scope_blocks_external_file_reads_writes_and_inherits_in_children(tmp_path):
    import sys
    from pathlib import Path

    from firefly_forge.execution_environment import build_execution_environment
    from firefly_forge.runner_backends import HostRunnerBackend
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.verification_scope import build_profile, wrap_build_argv

    workspace, scratch = tmp_path / "workspace", tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    for part in ("home", "tmp"):
        (scratch / part).mkdir()
    foreign = tmp_path / "outside.txt"
    foreign.write_text("private external prerequisite")
    private = workspace / ".ENV"
    private.write_text("private workspace configuration")
    alias = workspace / "public-alias.txt"
    alias.symlink_to(foreign)
    python = Path(sys.executable)
    roots = [python, python.resolve().parent.parent, Path(sys.prefix)]
    profile = build_profile(workspace.resolve(), roots, scratch.resolve())
    code = """import errno,sys,subprocess
from pathlib import Path
p=Path(sys.argv[1])
for name in sys.argv[2:]:
    try: Path(name).read_text()
    except PermissionError: pass
    else: raise AssertionError('Private workspace input escaped')
for operation in (p.read_text,lambda:p.write_text('changed')):
    try: operation()
    except PermissionError: pass
    else: raise AssertionError('External file access escaped')
childcode="from pathlib import Path; Path(__import__('sys').argv[1]).read_text()"
child=subprocess.run([sys.executable,'-B','-c',childcode,str(p)],capture_output=True)
assert child.returncode != 0 and b'PermissionError' in child.stderr, child.stderr
print('SCOPED_CHILDREN_DENIED')
"""
    with build_execution_environment(scratch.resolve()):
        result = await HostRunnerBackend().run(
            SandboxInput(),
            wrap_build_argv([str(python), "-B", "-c", code, str(foreign.resolve()), str(private), str(alias)], profile),
            workspace.resolve(),
            10,
            100000,
        )
    assert result.exit_code == 0, result.output
    assert "SCOPED_CHILDREN_DENIED" in result.output
    assert foreign.read_text() == "private external prerequisite"
