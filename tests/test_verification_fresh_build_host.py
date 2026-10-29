# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Builds that can inspect mutable output metadata always execute fresh."""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_verification_scope_commit_host import build_scope_case as build_scope_case

from firefly_forge.journal import digest
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.verification_handoffs import VerificationHandoffs
from firefly_forge.verification_models import VerificationAdoption, verification_build_declarations
from firefly_forge.verification_result_reuse import retained_check
from firefly_forge.verification_scope import eligible_build_checks
from firefly_forge.verification_scope_attestation import prepare_build_scope, scope_record


def build_command():
    return {"argv": ["/tools/npm", "--prefix", "frontend", "run", "build"], "timeout": 120}


def declaration():
    return {
        "command_digest": digest(build_command()),
        "purpose": "source_build",
        "independent_of_time_and_external_state": True,
    }


def adoption():
    return VerificationAdoption(
        request_id="1" * 32,
        pending_request_digest="2" * 64,
        source_digest="3" * 64,
        commands_digest="4" * 64,
        policy_digest="5" * 64,
        provider_digest="6" * 64,
        agent="agentic",
        reusable_build_checks=[declaration()],
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="Reusable candidates require Darwin host")
def test_candidate_lists_five_pure_checks_and_excludes_vite_build_and_database(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "node node_modules/vitest/vitest.mjs run --configLoader runner",
                    "lint": "node node_modules/eslint/bin/eslint.js .",
                    "typecheck": "node node_modules/typescript/bin/tsc --noEmit",
                    "build": "node node_modules/vite/bin/vite.js build --configLoader runner",
                }
            }
        )
    )
    commands = [
        {"argv": ["/tools/python", "-B", "-m", "ruff", "check", "backend"], "timeout": 120},
        {"argv": ["/tools/python", "-B", "-m", "mypy", "backend"], "timeout": 120},
        *[
            {"argv": ["/tools/npm", "--prefix", "frontend", "run", name], "timeout": 120}
            for name in ("test", "lint", "typecheck", "build")
        ],
        {"argv": ["/tools/python", "-B", "-m", "pytest"], "timeout": 120},
    ]
    run = {"commands": commands, "sandbox_snapshot": capture_sandbox(SandboxInput())}
    assert [row["command_index"] for row in eligible_build_checks(run, tmp_path)] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("source", ["policy", "saved_adoption"])
def test_build_reuse_declaration_is_refused_from_both_authority_sources(source):
    run = {"commands": [build_command()]}
    if source == "policy":
        frozen = {
            "version": 1,
            "revision": None,
            "settings": {"automatic_verification": {"enabled": True, "reusable_build_checks": [declaration()]}},
        }
        frozen["digest"] = digest(frozen)
        run["tool_policy_snapshot"] = frozen
    else:
        run["verification_adoption"] = {"request": adoption().model_dump()}
    with pytest.raises(ValueError, match="fresh"):
        verification_build_declarations(run)


async def test_saved_adoption_replay_cannot_reintroduce_build_reuse():
    body = adoption()

    async def entries(_):
        return [
            {"kind": "verification_handoff_adoption", "payload": {"request": body.model_dump(), "policy_snapshot": {}}}
        ]

    async def replay(*args, **kwargs):
        return {"status": "accepted_replay"}

    service = VerificationHandoffs(None, SimpleNamespace(entries=entries, verification_adopt=replay), None)

    async def operator(_):
        return {"commands": [build_command()]}

    service._operator = operator
    with pytest.raises(ValueError, match="fresh"):
        await service._adopt("7" * 32, body)


def test_even_successful_historical_native_receipt_cannot_become_a_reused_build():
    record = {"digest": "1" * 64, "created_at": "2026-10-29T00:00:00Z", "payload": {"execution_id": "2" * 32}}
    value = {"effects": {"start": {"status": "done", "evidence": {"exit_code": 0, "output": "passed"}}}}
    with pytest.raises(ValueError, match="fresh"):
        retained_check(build_command()["argv"], 6, 2, record, value)


@pytest.mark.skipif(sys.platform != "darwin", reason="Confined scope requires Darwin host")
async def test_mixed_scope_wraps_fresh_build_without_making_its_metadata_available_to_reusable_checks(
    build_scope_case, monkeypatch
):
    c = build_scope_case
    executable = os.environ.get("FORGE_TEST_NODE") or shutil.which("node")
    if executable is None:
        pytest.skip("This native scope fixture requires Node")
    node = Path(executable).resolve()
    prefix = node.parent.parent
    npm = prefix / "bin" / "npm"
    roots = [c.dependencies, prefix, node, npm, npm.resolve()]
    from firefly_forge import verification_scope_attestation as scope

    monkeypatch.setattr(scope, "_roots", lambda *args: roots)
    frontend = c.workspace / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps({"scripts": {"build": "node node_modules/vite/bin/vite.js build"}})
    )
    c.run["commands"].append(build_command())
    c.intent["commands_digest"] = digest({"commands": c.run["commands"]})
    prepared = await prepare_build_scope(c.store, c.journal, c.engine, c.run, c.intent, {"digest": "7" * 64}, c.current)
    assert prepared is not None
    assert [check.command_index for check in prepared.witness.checks] == [1, 2]
    first, fresh = prepared.witness.checks
    dist = frontend / "dist"
    dist.mkdir()
    (dist / "passed-verification.flag").write_text("unbound mutable output\n")
    for selected, allowed in ((first, False), (fresh, True)):
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/sandbox-exec",
            "-p",
            selected.wrapped_argv[2],
            str(node),
            "-e",
            "const fs=require('node:fs');console.log(fs.readdirSync(process.argv[1]).length)",
            str(dist),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if allowed:
            assert process.returncode == 0, stderr.decode()
            assert stdout == b"1\n"
        else:
            assert process.returncode != 0
            assert b"EPERM" in stderr
    await prepared.revalidate()
    entries = [
        {"kind": "verification_handoff_intent", "payload": c.intent, "digest": "7" * 64, "sequence": 1},
        {"kind": "verification_build_scope", "payload": prepared.witness.model_dump(mode="json"), "sequence": 2},
    ]
    assert scope_record(c.run, c.intent, entries) is not None
    entries[1]["payload"]["checks"][0]["wrapped_argv"][2] = fresh.wrapped_argv[2]
    with pytest.raises(ValueError, match="wrapper"):
        scope_record(c.run, c.intent, entries)
