# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The actual immutable carrier selects the embedded versioned owner supervisor."""

import ast
import io
import json
import tarfile
from types import SimpleNamespace

from test_run_owned_authority_host import case

from firefly_forge.runner_pool_artifacts import RunnerPoolArtifacts, source_archive_digest
from firefly_forge.runner_pool_launcher import owned_launcher
from firefly_forge.runner_pool_models import RunnerPoolProfile, pool_digest
from firefly_forge.sandbox import pack_workspace


def test_actual_carrier_binds_the_complete_invocation_without_storing_renewal_keys(tmp_path):
    request, _, run, *_ = case()
    profile = RunnerPoolProfile.model_validate(run["sandbox_snapshot"]["configuration"]["profile"])
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir(mode=0o700)
    (workspace / "service.py").write_text("VALUE = 1\n")
    archive, _ = pack_workspace(workspace, profile.max_source_bytes)
    import hashlib

    request = request.model_copy(
        update={
            "source_bytes": len(archive),
            "archive_digest": hashlib.sha256(archive).hexdigest(),
            "source_digest": source_archive_digest(archive),
        }
    )
    artifacts = RunnerPoolArtifacts(private)
    artifacts.prepare(profile, request, archive)
    artifacts.retain(profile, request, "stdin", b"")
    with tarfile.open(fileobj=io.BytesIO(artifacts.input_archive(profile, request)), mode="r:") as incoming:
        config = json.load(incoming.extractfile("invocation.json"))
    assert config["run_liveness_sha256"] == pool_digest(request.model_dump(mode="json"))
    assert set(config) == {
        "argv",
        "cwd",
        "timeout",
        "execution_id",
        "work_duration",
        "owner_grace_seconds",
        "max_output_bytes",
        "run_liveness_sha256",
    }


def test_actual_embedded_owner_factory_selects_v3_only_from_the_frozen_binding():
    import hashlib
    import hmac
    import re
    import secrets
    import threading
    import time

    program = ast.parse(owned_launcher())
    names = {"_owner_body", "OwnerLiveness", "_run_owner_body", "RunOwnerLiveness", "create_owner"}
    definitions = [
        node for node in program.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ]
    assert {node.name for node in definitions} == names
    namespace = {
        "hashlib": hashlib,
        "hmac": hmac,
        "json": json,
        "re": re,
        "secrets": secrets,
        "threading": threading,
        "time": time,
        "signal": SimpleNamespace(alarm=lambda seconds: None),
    }
    future = ast.parse("from __future__ import annotations").body
    exec(compile(ast.Module(body=[*future, *definitions], type_ignores=[]), "actual-embedded-owner", "exec"), namespace)
    for tagged in [False, True]:
        config = {"execution_id": "a" * 32, "owner_grace_seconds": 30}
        if tagged:
            config["run_liveness_sha256"] = "b" * 64
        namespace["config"] = config
        owner = namespace["create_owner"]()
        frame = {
            "protocol": "forge-runner-owner-v3" if tagged else "forge-runner-owner-v2",
            "execution_id": "a" * 32,
            "operation": "claim",
        }
        if tagged:
            frame["binding_digest"] = "b" * 64
        result = owner.handle(frame)
        assert result["protocol"] == ("forge-runner-owner-status-v3" if tagged else "forge-runner-owner-status-v2")
