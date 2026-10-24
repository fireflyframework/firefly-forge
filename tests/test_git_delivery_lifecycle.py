# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real local commits and bare pushes share one controlled provider draft lifecycle."""

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_local import git

from firefly_forge import scm
from firefly_forge.git_workspace import metadata_digest


@pytest.fixture
def remote_case(delivery_case, tmp_path, monkeypatch):
    c = delivery_case()
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    git(c.root, "remote", "add", "origin", str(remote))
    c.state["run"]["git_metadata_digest"] = metadata_digest(c.root)
    c.state["run"]["git_delivery_policy"]["remote"] = "draft_pr"
    repository = {
        "id": "2" * 32,
        "url": "https://example.test/team/product.git",
        "provider": "github",
        "connection_provider": "github",
        "default_branch": "main",
    }
    calls = []
    state = {"draft": None}

    async def repo(id):
        return dict(repository)

    async def credentials(repo):
        return "private-public-fixture", "https://example.test/api/v3"

    async def environment(repo):
        return {}

    async def wording(run, native, safe, **kwargs):
        return {"body": native, "communication": {"mode": "deterministic", "reason": "unconfigured"}}

    async def remote_revision(repository, branch, environment):
        text = git(remote, "ls-remote", str(remote), "refs/heads/" + branch)
        return text.split("\t")[0] if text else None

    real_push = scm.push

    async def push(workspace, branch, **kwargs):
        calls.append("push")
        lease = (
            {"expected_remote_revision": kwargs["expected_remote_revision"]}
            if "expected_remote_revision" in kwargs
            else {}
        )
        await real_push(workspace, branch, repository_url=str(remote), revision=kwargs["revision"], **lease)

    def record():
        sha = git(remote, "rev-parse", "refs/heads/forge/public-unit")
        return {
            "number": 7,
            "node_id": "PR_fixture",
            "html_url": "https://example.test/team/product/pull/7",
            "state": "open",
            "draft": state["draft"]["draft"],
            "body": state["draft"]["body"],
            "head": {"ref": "forge/public-unit", "sha": sha, "repo": {"full_name": "team/product"}},
            "base": {"ref": "main", "repo": {"full_name": "team/product"}},
        }

    async def provider(request):
        calls.append(request.method + " " + request.url.path)
        if request.url.path == "/api/graphql":
            state["draft"]["draft"] = False
            return httpx.Response(
                200,
                json={
                    "data": {"markPullRequestReadyForReview": {"pullRequest": {"id": "PR_fixture", "isDraft": False}}}
                },
            )
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[] if state["draft"] is None else [record()])
        if request.method in {"POST", "PATCH"}:
            state["draft"] = {**json.loads(request.content), "draft": True}
        return httpx.Response(201 if request.method == "POST" else 200, json=record())

    client = httpx.AsyncClient
    monkeypatch.setattr("firefly_forge.scm.push", push)
    c.service.http_client = lambda **kwargs: client(transport=httpx.MockTransport(provider), **kwargs)
    c.service._remote_revision = remote_revision
    c.service.store.repository = repo
    c.service.engine.connections = SimpleNamespace(
        authentication=credentials, git_environment=environment, assert_snapshot=lambda *args: None
    )
    c.service.engine.communication_writer = SimpleNamespace(write=wording, assert_current=lambda evidence: None)
    c.remote, c.calls, c.remote_state = remote, calls, state
    return c


async def test_two_early_pushes_update_one_draft_and_verified_final_promotes_it(remote_case):
    c = remote_case
    actor = {"user_id": "4" * 32}
    (c.root / "product.py").write_text("value = 2\n")
    first = await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor=actor)
    (c.root / "product.py").write_text("value = 3\n")
    second = await c.service.checkpoint("1" * 32, request_id="b" * 32, expected_head=first["revision"], actor=actor)
    assert first["remote_status"] == second["remote_status"] == "draft"
    assert sum(v == "POST /api/v3/repos/team/product/pulls" for v in c.calls) == 1
    assert sum(v.startswith("PATCH") for v in c.calls) == 1 and c.calls.count("push") == 2
    assert git(c.remote, "rev-parse", "refs/heads/forge/public-unit") == second["revision"]
    c.state["run"].update(status="succeeded", source_tree=second["source_tree"], final_verification=[{"exit_code": 0}])

    async def exact(*args):
        pass

    c.service.engine._assert_delivery_tree = exact
    c.service.engine._assert_publication_current = exact
    run = await c.service.store.get_run("1" * 32)
    draft = {
        "title": "Verified public unit",
        "body": "The public counter passed the recorded checks.",
        "body_sha256": hashlib.sha256(b"The public counter passed the recorded checks.").hexdigest(),
    }
    prepared = await c.service.final_start(run, actor, draft)
    url = await c.service.final_ready(prepared, draft)
    assert url == first["pr_url"] and c.remote_state["draft"]["draft"] is False
    assert sum(v == "POST /api/v3/repos/team/product/pulls" for v in c.calls) == 1
    assert (await c.service.view("1" * 32, actor=actor))["items"][-1]["remote_status"] == "ready"


async def test_selected_ai_hold_commits_locally_but_never_pushes_or_creates_draft(remote_case):
    c = remote_case

    async def held(*args, **kwargs):
        return {"body": "", "communication": {"mode": "held", "reason": "unavailable"}}

    c.service.engine.communication_writer.write = held
    (c.root / "product.py").write_text("value = 2\n")
    with pytest.raises(ValueError, match="communication is held"):
        await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    assert git(c.root, "rev-parse", "HEAD") != c.base and c.calls == []


async def test_unknown_push_outcome_survives_restart_and_blocks_a_new_key(remote_case, monkeypatch):
    c = remote_case

    async def unknown(*args, **kwargs):
        raise RuntimeError("Controlled unknown push")

    monkeypatch.setattr("firefly_forge.scm.push", unknown)
    (c.root / "product.py").write_text("value = 2\n")
    with pytest.raises(RuntimeError, match="unknown push"):
        await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    retained = git(c.root, "rev-parse", "HEAD")
    (c.root / "product.py").write_text("value = 3\n")
    with pytest.raises(ValueError, match="unknown"):
        await c.service.checkpoint("1" * 32, request_id="b" * 32, expected_head=retained, actor={"user_id": "4" * 32})
    assert git(c.root, "rev-parse", "HEAD") == retained and c.remote_state["draft"] is None


async def test_foreign_ancestor_branch_created_after_observation_cannot_be_adopted(remote_case, monkeypatch):
    c = remote_case
    push = scm.push

    async def race(workspace, branch, **kwargs):
        git(workspace, "push", str(c.remote), c.base + ":refs/heads/" + branch)
        await push(workspace, branch, **kwargs)

    monkeypatch.setattr(scm, "push", race)
    (c.root / "product.py").write_text("value = 2\n")
    with pytest.raises((ValueError, RuntimeError)):
        await c.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=c.base, actor={"user_id": "4" * 32})
    assert git(c.remote, "rev-parse", "refs/heads/forge/public-unit") == c.base
    assert c.remote_state["draft"] is None


async def test_exact_ref_lease_never_authorizes_divergent_history(remote_case):
    c = remote_case
    git(c.root, "checkout", "-qb", "foreign-history", c.base)
    (c.root / "product.py").write_text("value = 99\n")
    git(c.root, "add", "product.py")
    git(c.root, "commit", "-qm", "Foreign branch history")
    foreign = git(c.root, "rev-parse", "HEAD")
    git(c.root, "push", str(c.remote), foreign + ":refs/heads/forge/public-unit")
    git(c.root, "checkout", "forge/public-unit")
    (c.root / "product.py").write_text("value = 2\n")
    git(c.root, "add", "product.py")
    git(c.root, "commit", "-qm", "Different local history")
    with pytest.raises(ValueError, match="divergent"):
        await scm.push(
            c.root, "forge/public-unit", revision=git(c.root, "rev-parse", "HEAD"), expected_remote_revision=foreign
        )
    assert git(c.remote, "rev-parse", "refs/heads/forge/public-unit") == foreign
