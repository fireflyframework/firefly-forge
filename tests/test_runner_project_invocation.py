# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit framed requests preserve legacy dictionaries and bind every transport identity."""

from uuid import uuid4

import pytest

from firefly_forge import runner_pool_models as models
from firefly_forge.runner_pool_bundle import pack_project_bundle


def test_project_invocation_requires_git_and_exact_bundle_identity(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print(1)\n")
    git = tmp_path / "git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/forge-snapshot\n")
    data, identity = pack_project_bundle(source, git, 1_048_576)
    base = models.RunnerInvocation(
        execution_id=uuid4().hex,
        profile_id="owned",
        profile_revision="a" * 64,
        repository_id=uuid4(),
        run_id=uuid4(),
        image="example/worker@sha256:" + "b" * 64,
        argv=("git", "diff"),
        source_digest=identity.source_digest,
        archive_digest=identity.archive_digest,
        source_bytes=len(data),
    )
    legacy = base.model_dump(mode="json")
    assert "protocol" not in legacy and models.parse_invocation(legacy).model_dump(mode="json") == legacy
    request = models.RunnerProjectInvocation(**legacy, bundle=identity)
    assert isinstance(models.parse_invocation(request.model_dump(mode="json")), models.RunnerProjectInvocation)
    with pytest.raises(ValueError, match="bundle"):
        models.RunnerProjectInvocation(**{**legacy, "archive_digest": "c" * 64}, bundle=identity)
    _, absent = pack_project_bundle(source, None, 1_048_576)
    with pytest.raises(ValueError, match="Git"):
        models.RunnerProjectInvocation(
            **{**legacy, "source_bytes": absent.archive_bytes, "archive_digest": absent.archive_digest}, bundle=absent
        )
