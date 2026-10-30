# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Signed repository observations share natural read intake, never execution authority."""

import copy
import hashlib
import hmac
import json
from uuid import uuid4

import pytest
from test_addressed_status_polling import configured_status
from test_addressed_status_signed import grant
from test_scm_polling import poll_case as poll_case  # noqa: PLC0414

from firefly_forge.collaboration import CollaborationSettings, RepositoryCommand

QUESTION = "@forge What is blocked, and what is already verified? This is a follow-up, not a request to rerun."


async def signed(c, *, retained=False, installation="absent", signature=True, number=90, body=QUESTION):
    if not c.state.get("repo_hook_settings"):
        await c.collaboration.configure(
            c.repository,
            CollaborationSettings(
                enabled=True,
                trusted_actors=["7"],
                allowed_commands=["status"],
                webhook_secret="owned-repository-hook-key",
            ),
        )
        c.state["repo_hook_settings"] = True
    comment = c.comment(number, body)
    if not any(row["id"] == number for row in c.state["comments"]):
        c.state["comments"].append(comment)
    payload = {
        "action": "created",
        "repository": {"id": 42},
        "sender": {"id": 7, "type": "User"},
        "issue": {"number": 3},
        "comment": comment,
    }
    if installation != "absent":
        payload["installation"] = installation
    raw = json.dumps(payload).encode()
    headers = {
        "x-github-event": "issue_comment",
        "x-github-delivery": uuid4().hex,
        "x-hub-signature-256": "sha256="
        + hmac.new(b"owned-repository-hook-key" if signature else b"wrong", raw, hashlib.sha256).hexdigest(),
    }
    if retained:
        prepared = await c.collaboration._prepare_receive("github", c.repository, raw, headers)
        if prepared["reason"] is None and prepared["event"].pop("api_observation", None) is not None:
            prepared["event"]["command"] = RepositoryCommand.parse(
                body, bot_addresses=c.scm.bot_addresses("github")
            ).model_dump(exclude_none=True)
        return await c.collaboration._retain_receive(prepared), raw, headers
    return await c.collaboration.receive("github", c.repository, raw, headers), raw, headers


async def ready(c, monkeypatch):
    await c.ready()
    await c.consent()
    previous = await c.new_command(action="status")
    await c.collaboration.process(previous["id"])
    await configured_status(c, monkeypatch)
    return previous


async def test_repository_hook_without_payload_installation_retains_a_separate_read_proof(poll_case):
    async with poll_case() as c:
        delivery, _, _ = await signed(c)
        assert delivery["status"] == "received"
        proof = delivery["verification"]
        assert proof["transport"] == "github_repository_webhook_v1"
        assert "installation_id" not in proof
        assert proof["connection"]["installation_id"] == "34"
        assert proof["connection"]["provenance"] == "configured_github_application_v1"
        assert proof["payload_digest"] == delivery["digest"]
        assert proof["source_digest"] == delivery["addressed_source"]["source_digest"]
        assert not c.db["journal"] and not c.state["posts"]


@pytest.mark.parametrize("installation", [None, {"id": 35}, {"id": "34"}, {}])
async def test_present_invalid_app_installation_never_falls_back_to_repository_hook(poll_case, installation):
    async with poll_case() as c:
        delivery, _, _ = await signed(c, installation=installation)
        assert delivery["status"] == "denied"
        assert not (delivery.get("verification") or {}).get("transport")
        assert not c.db["journal"] and not c.state["posts"]


@pytest.mark.parametrize("transport", ["repository", "application"])
async def test_retained_signed_natural_question_uses_same_classifier_and_current_read_scope_once(
    poll_case, monkeypatch, transport
):
    async with poll_case() as c:
        await ready(c, monkeypatch)
        delivery, raw, headers = await signed(
            c, retained=True, installation={"id": 34} if transport == "application" else "absent"
        )
        if transport == "application":
            await grant(c, delivery)
        before = copy.deepcopy(c.db["runs"][c.run_id].data)
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "processed" and result["run_id"] == c.run_id
        assert result["natural_turn"]["human_message"] == QUESTION.removeprefix("@forge ")
        assert result["verification"] == delivery["verification"]
        assert c.state["status_calls"] == 1
        assert await c.collaboration.process(delivery["id"]) == result
        assert (await c.collaboration.receive("github", c.repository, raw, headers))["id"] == delivery["id"]
        assert c.state["status_calls"] == 1 and c.db["runs"][c.run_id].data == before
        assert not c.db["journal"] and len(c.state["posts"]) == 2


@pytest.mark.parametrize(
    "change", ["signature", "membership", "actor", "source", "permission", "webhook-key", "missing-consent"]
)
async def test_retained_repository_hook_does_not_classify_before_current_source_and_actor_authority(
    poll_case, monkeypatch, change
):
    async with poll_case() as c:
        if change != "missing-consent":
            await ready(c, monkeypatch)
        else:
            await configured_status(c, monkeypatch)
        before = len(c.state["posts"])
        delivery, _, _ = await signed(c, retained=True, signature=change != "signature")
        if change == "membership":
            c.db["memberships"]["member"].active = False
        if change == "actor":
            c.db["settings"][c.repository].data["trusted_actors"] = []
        if change == "source":
            c.state["edited"] = True
        if change == "permission":
            c.state["external_permission"] = "none"
        if change == "webhook-key":
            c.store.configuration._write(c.collaboration.secrets_path, {})
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "denied"
        assert c.state["status_calls"] == 0 and len(c.state["posts"]) == before
        assert not c.db["journal"]


async def test_retained_unknown_signed_classifier_outcome_is_not_replayed_or_replaced_by_basic_status(
    poll_case, monkeypatch
):
    async with poll_case() as c:
        await ready(c, monkeypatch)
        delivery, _, _ = await signed(c, retained=True)
        calls = []

        async def unknown(*args, **kwargs):
            calls.append(True)
            raise RuntimeError("Controlled unknown provider outcome")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", unknown)
        posts = len(c.state["posts"])
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "needs_reconciliation" and result["natural_turn"]["classifier_outcome"] == "unknown"
        assert await c.collaboration.process(delivery["id"]) == result
        assert calls == [True] and len(c.state["posts"]) == posts and not c.db["journal"]


@pytest.mark.parametrize("first", ["webhook", "poll"])
async def test_one_comment_has_one_operational_receipt_and_separate_signed_delivery(poll_case, monkeypatch, first):
    async with poll_case() as c:
        await ready(c, monkeypatch)
        if first == "poll":
            c.state["clock"] += 3600
            c.state["comments"].append(c.comment(90, QUESTION))
            for _ in range(3):
                await c.service.step(c.repository)
        signed_delivery, _, _ = await signed(c)
        await c.collaboration.process(signed_delivery["id"])
        for _ in range(3):
            await c.service.step(c.repository)
        rows = [row for row in c.db["inbox"].values() if row.source_key == "comment:90"]
        assert len(rows) == 1 and rows[0].id.hex != signed_delivery["id"]
        result = await c.collaboration.process(rows[0].id.hex)
        assert result["status"] == "processed" and c.state["status_calls"] == 1
        correlation = await c.collaboration.process(signed_delivery["id"])
        assert correlation["api_observation"]["receipt_id"] == result["id"]
        assert correlation["verification"] == signed_delivery["verification"]
        assert (await c.collaboration.process(rows[0].id.hex)) == result
        assert len(c.state["posts"]) == 2 and not c.db["journal"]


@pytest.mark.parametrize("family", ["answer", "product", "checkpoint"])
async def test_retained_signed_read_proof_never_admits_polling_effects_even_with_polling_grants(
    poll_case, monkeypatch, family
):
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.scm_natural_checkpoints import SCMNaturalCheckpoints
    from firefly_forge.scm_natural_effects import NaturalEffects
    from firefly_forge.scm_natural_product import NaturalProductContinuation
    from firefly_forge.scm_natural_tasks import SCMNaturalTasks

    async with poll_case() as c:
        await ready(c, monkeypatch)
        delivery, _, _ = await signed(c, retained=True)
        result = await c.collaboration.process(delivery["id"])
        natural = SCMNaturalTasks(c.collaboration)
        before = copy.deepcopy((c.db["runs"][c.run_id].data, c.db["journal"], len(c.db["inbox"])))
        if family == "answer":
            with pytest.raises(AccessDenied, match="read authority"):
                await NaturalEffects(natural).review(result["id"])
        elif family == "product":
            with pytest.raises(AccessDenied, match="read authority"):
                await NaturalProductContinuation(natural).review(result["id"])
        else:

            async def forbidden(*args, **kwargs):
                raise AssertionError("No signed read proof may request a native checkpoint actor")

            monkeypatch.setattr(c.service, "resolve_checkpoint", forbidden)
            held = await SCMNaturalCheckpoints(natural).apply(result["id"])
            assert held["natural_turn"]["state"] == "checkpoint_pending_permission"
            assert "natural_checkpoint" not in held
        assert (c.db["runs"][c.run_id].data, c.db["journal"], len(c.db["inbox"])) == before


def test_natural_policy_seals_repository_read_authority_delegates(monkeypatch):
    from pathlib import Path

    from firefly_forge.scm_natural_tasks import natural_policy_digest

    original = Path.read_bytes
    before = natural_policy_digest()
    for target in ["scm_actor_authority.py", "scm_authority_models.py", "scm_connections.py", "scm_comment_source.py"]:
        with monkeypatch.context() as mutation:

            def changed(path, *, target=target):
                content = original(path)
                return content + b"\n# controlled authority drift\n" if path.name == target else content

            mutation.setattr(Path, "read_bytes", changed)
            assert natural_policy_digest() != before
