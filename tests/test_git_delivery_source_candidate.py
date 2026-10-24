# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A manual checkpoint reviews the current working source without staging it."""

import asyncio
import hashlib
from uuid import UUID

import pytest
from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_local import git
from test_git_delivery_operational import operational as operational

from firefly_forge.git_delivery_models import GitDeliveryInputV1


async def test_current_source_candidate_includes_untracked_files_and_does_not_write_the_index(delivery_case):
    case = delivery_case()
    (case.root / "product.py").write_text("value = 2\n")
    (case.root / "migration.py").write_text("def migrate(): return 2\n")
    index = hashlib.sha256((case.root / ".git/index").read_bytes()).hexdigest()
    candidate = await case.service.candidate("1" * 32, actor={"user_id": "4" * 32})
    assert candidate["expected_head"] == case.base
    assert len(candidate["expected_source_digest"]) == 64
    assert hashlib.sha256((case.root / ".git/index").read_bytes()).hexdigest() == index
    assert git(case.root, "rev-parse", "HEAD") == case.base and not case.state["entries"]
    before = candidate["expected_source_digest"]
    (case.root / "migration.py").write_text("def migrate(): return 3\n")
    assert (await case.service.candidate("1" * 32, actor={"user_id": "4" * 32}))["expected_source_digest"] != before


async def test_current_source_admission_queues_then_commits_real_git_without_a_prior_tree(operational):
    case = operational
    (case.root / "product.py").write_text("value = 2\n")
    (case.root / "migration.py").write_text("def migrate(): return 2\n")
    assert not case.state["run"].get("source_tree")
    candidate = await case.controller.candidate("1" * 32)
    body = GitDeliveryInputV1.model_validate(
        {
            **{key: candidate[key] for key in ("version", "expected_head", "expected_source_digest")},
            "request_id": "a" * 32,
        }
    )
    admitted = await case.controller.request("1" * 32, body)
    assert admitted["status"] == "queued" and git(case.root, "rev-parse", "HEAD") == case.base
    poller = asyncio.create_task(case.requests._poll())
    try:
        async with asyncio.timeout(10):
            while case.rows[UUID("a" * 32)].status == "queued" or case.requests.active:
                await asyncio.sleep(0.02)
                if case.rows[UUID("a" * 32)].status == "completed":
                    break
    finally:
        poller.cancel()
        await asyncio.gather(poller, *case.requests.active.values(), return_exceptions=True)
    completed = await case.controller.ticket("1" * 32, body.request_id)
    assert completed["status"] == "completed"
    assert git(case.root, "show", "HEAD:product.py") == "value = 2"
    assert git(case.root, "show", "HEAD:migration.py") == "def migrate(): return 2"
    assert git(case.root, "rev-parse", "HEAD^") == case.base


def test_current_source_digest_is_a_strict_alternative_to_a_previously_recorded_tree():
    value = GitDeliveryInputV1(version=1, request_id="a" * 32, expected_head="b" * 40, expected_source_digest="c" * 64)
    assert value.expected_source_tree is None
    with pytest.raises(ValueError):
        GitDeliveryInputV1(version=1, request_id="a" * 32, expected_head="b" * 40)


async def test_changed_current_source_is_refused_before_git_or_durable_delivery_intent(delivery_case):
    case = delivery_case()
    (case.root / "product.py").write_text("value = 2\n")
    candidate = await case.service.candidate("1" * 32, actor={"user_id": "4" * 32})
    (case.root / "product.py").write_text("value = 3\n")
    with pytest.raises(ValueError, match="source"):
        await case.service.checkpoint(
            "1" * 32,
            request_id="a" * 32,
            actor={"user_id": "4" * 32},
            **{key: candidate[key] for key in ("expected_head", "expected_source_digest")},
        )
    assert git(case.root, "rev-parse", "HEAD") == case.base and not case.state["entries"]
