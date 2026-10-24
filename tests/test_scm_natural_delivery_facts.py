# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Natural status must include verified checkpoint facts without borrowing write authority."""

import copy
from types import SimpleNamespace

import pytest
from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_local import git
from test_scm_natural_worker_context_host import fixture

from firefly_forge.journal import Journal, digest
from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_natural_delivery import delivery_facts


async def saved(case):
    (case.root / "product.py").write_text("value = 2\n")
    git(case.root, "add", "product.py")
    return await case.service.checkpoint(
        "1" * 32,
        request_id="a" * 32,
        expected_head=case.base,
        expected_source_tree=git(case.root, "write-tree"),
        actor=case.state["run"]["execution_actor"],
    )


def remote_input(case, receipt):
    return {
        "request_id": receipt["request_id"],
        "revision": receipt["revision"],
        "checkpoint_digest": receipt["checkpoint_digest"],
        "branch": receipt["branch"],
        "repository_id": case.state["run"]["repository_id"],
        "body_sha256": "b" * 64,
    }


async def test_status_reads_real_checkpoint_receipt_without_claiming_verified_product(delivery_case):
    case = delivery_case()
    receipt = await saved(case)
    facts = delivery_facts(case.state["run"], case.state["entries"])
    assert facts["checkpoint"]["revision"] == receipt["revision"]
    assert facts["checkpoint"]["changed_file_count"] == 1
    assert facts["remote"] == {"status": "not_requested", "pr_url": None}
    assert facts["product_acceptance"] == "not_established_by_checkpoint"
    assert "product.py" not in str(facts) and "value =" not in str(facts)


async def test_natural_context_includes_current_native_receipt_and_fences_later_changes(delivery_case):
    case = delivery_case()
    receipt = await saved(case)
    natural, message, scopes, _, runs = await fixture()
    runs["1" * 32].update(case.state["run"])

    async def records(run_id):
        return [SimpleNamespace(data=copy.deepcopy(e), digest=e["digest"]) for e in case.state["entries"]]

    journal = natural.collaboration.deployments.engine.journal
    journal.records.find_for_run = records
    before = await natural._context(message, scopes)
    candidate = next(c for c in before["candidates"] if c["run_id"] == "1" * 32)
    assert candidate["delivery"]["checkpoint"]["revision"] == receipt["revision"]
    assert candidate["delivery"]["remote"]["status"] == "not_requested"
    await case.service._append(
        "1" * 32,
        "git_delivery_remote_intent",
        "git-remote:" + "a" * 32,
        remote_input(case, receipt),
    )
    after = await natural._context(message, scopes)
    assert after != before
    assert after["candidates"][0]["delivery"]["remote"] == {"status": "unknown", "pr_url": None}


@pytest.mark.parametrize("tamper", ["chain", "run_id", "revision", "source_tree", "checkpoint_digest"])
async def test_invalid_receipts_never_become_natural_status_facts(delivery_case, tamper):
    case = delivery_case()
    await saved(case)
    run, entries = copy.deepcopy(case.state["run"]), copy.deepcopy(case.state["entries"])
    if tamper == "chain":
        entries[-1]["digest"] = "f" * 64
    elif tamper == "run_id":
        run["id"] = "9" * 32
    elif tamper == "revision":
        run["checkpoint_revision"] = "f" * 40
    else:
        entries[-1]["payload"][tamper] = "f" * (40 if tamper == "source_tree" else 64)
        entries[-1]["digest"] = digest({k: v for k, v in entries[-1].items() if k != "digest"})
    with pytest.raises((ValueError, AccessDenied)):
        delivery_facts(run, entries)


async def test_confirmed_remote_receipt_has_exact_link_and_unverified_scope(delivery_case):
    case = delivery_case()
    receipt = await saved(case)
    request = "a" * 32
    intent = await case.service._append(
        "1" * 32,
        "git_delivery_remote_intent",
        "git-remote:" + request,
        remote_input(case, receipt),
    )
    await case.service._append(
        "1" * 32,
        "git_delivery_remote_receipt",
        "git-remote:" + request,
        {
            "request_id": request,
            "revision": receipt["revision"],
            "remote_status": "draft",
            "intent_digest": intent["digest"],
            "reference": {
                "url": "https://github.com/team/product/pull/2",
                "body_sha256": "b" * 64,
                "revision": receipt["revision"],
            },
        },
    )
    facts = delivery_facts(case.state["run"], case.state["entries"])
    assert facts["remote"] == {"status": "draft", "pr_url": "https://github.com/team/product/pull/2"}
    assert facts["product_acceptance"] == "not_established_by_checkpoint"
    assert Journal.verify_chain(case.state["entries"])
    confirmed = copy.deepcopy(case.state["entries"])
    await case.service._append(
        "1" * 32,
        "git_delivery_remote_intent",
        "git-ready:" + request,
        {
            **remote_input(case, receipt),
            "purpose": "verified_ready_for_review",
        },
    )
    assert delivery_facts(case.state["run"], case.state["entries"])["remote"]["status"] == "unknown"
    assert case.state["entries"][: len(confirmed)] == confirmed


async def test_orphan_remote_receipt_never_manufactures_confirmed_status(delivery_case):
    case = delivery_case()
    receipt = await saved(case)
    await case.service._append(
        "1" * 32,
        "git_delivery_remote_receipt",
        "orphan",
        {
            "request_id": "a" * 32,
            "revision": receipt["revision"],
            "remote_status": "draft",
            "intent_digest": "f" * 64,
            "reference": {
                "url": "https://github.com/team/product/pull/999",
                "body_sha256": "b" * 64,
                "revision": receipt["revision"],
            },
        },
    )
    with pytest.raises((ValueError, AccessDenied)):
        delivery_facts(case.state["run"], case.state["entries"])


@pytest.mark.parametrize("field", ["revision", "checkpoint_digest"])
async def test_remote_pair_must_belong_to_the_selected_local_checkpoint(delivery_case, field):
    case = delivery_case()
    receipt = await saved(case)
    incoming = {**remote_input(case, receipt), field: "f" * (40 if field == "revision" else 64)}
    intent = await case.service._append("1" * 32, "git_delivery_remote_intent", "misbound", incoming)
    await case.service._append(
        "1" * 32,
        "git_delivery_remote_receipt",
        "misbound",
        {
            "request_id": receipt["request_id"],
            "revision": incoming["revision"],
            "remote_status": "draft",
            "intent_digest": intent["digest"],
            "reference": {
                "url": "https://github.com/team/product/pull/2",
                "body_sha256": "b" * 64,
                "revision": incoming["revision"],
            },
        },
    )
    assert Journal.verify_chain(case.state["entries"])
    with pytest.raises((ValueError, AccessDenied)):
        delivery_facts(case.state["run"], case.state["entries"])
