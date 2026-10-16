# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Signed ingress uses V2 consent without enabling the legacy resume action."""

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from firefly_forge.collaboration import Collaboration, CollaborationSettings


@pytest.fixture
def ingress(tmp_path):
    reference = json.loads(Path("studio/tests/scm-delegation-contract-fixture.json").read_text())["receipt"][
        "verified_reference"
    ]
    repository = "6" * 32
    repo = {
        "id": repository,
        "name": "fixture/product",
        "url": "https://github.com/fixture/product.git",
        "connection_provider": "github",
        "remote_repository_id": reference["remote_repository_id"],
    }
    settings = CollaborationSettings(
        enabled=True, trusted_actors=[reference["external_actor_id"]], allowed_commands=["status"]
    ).model_dump(mode="json", exclude={"webhook_secret"})
    secret = "fictional-webhook-test-secret"

    async def repository_record(key):
        assert key == repository
        return repo

    async def configured(key):
        assert key.hex == repository
        return SimpleNamespace(data=settings)

    async def admit(value):
        pass

    @asynccontextmanager
    async def unit():
        yield None

    scope = {
        key: value
        for key, value in reference.items()
        if key not in {"version", "external_actor_id", "webhook_key_revision", "verification_method", "payload_digest"}
    }
    service = Collaboration(
        SimpleNamespace(
            repository=repository_record,
            _unit=unit,
            admit_credentials=admit,
            configuration=SimpleNamespace(
                directory=tmp_path,
                _read=lambda _: {repository: {"secret": secret, "revision": reference["webhook_key_revision"]}},
            ),
        ),
        scm=SimpleNamespace(delegation_scope=lambda _: scope, _host=lambda _: "https://github.com"),
        settings_records=SimpleNamespace(find_for_repository=configured),
        resume_authority=SimpleNamespace(),
    )

    async def prepare(command, *, registered_key=True, service_available=True):
        raw = json.dumps(
            {
                "action": "created",
                "repository": {"id": int(reference["remote_repository_id"])},
                "installation": {"id": int(reference["installation_id"])},
                "sender": {"id": int(reference["external_actor_id"])},
                "issue": {"number": 1},
                "comment": {"id": 1, "body": command},
            }
        ).encode()
        headers = {
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": "fictional-delivery-1",
            "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
        }
        if not registered_key:
            service.store.configuration._read = lambda _: {repository: secret}
        if not service_available:
            service.resume_authority = None
        return await service._prepare_receive("github", repository, raw, headers)

    return prepare, settings


def versioned_command(action="resume"):
    body = {"version": 2, "run_id": "1" * 32, "expected_grant_digest": "2" * 64}
    if action == "resume":
        body.update(expected_question_id="question-1", expected_response_digest="3" * 64, expected_cut_digest="4" * 64)
    return "/forge " + action + " " + json.dumps(body)


async def test_signed_v2_resume_can_reach_separate_admission_without_legacy_resume(ingress):
    prepare, settings = ingress
    prepared = await prepare(versioned_command())
    assert settings["channel_commands"] == []
    assert prepared["verified_reference"] is not None
    assert prepared["reason"] is None
    assert prepared["event"]["command"]["version"] == 2


async def test_unversioned_resume_is_not_enabled_by_the_v2_admission_path(ingress):
    prepare, _ = ingress
    prepared = await prepare('/forge resume {"run_id":"' + "1" * 32 + '"}')
    assert prepared["reason"] == "command_disabled"


@pytest.mark.parametrize("enabled", [True, False])
async def test_signed_v2_status_keeps_the_existing_status_policy(ingress, enabled):
    prepare, settings = ingress
    settings["allowed_commands"] = ["status"] if enabled else []
    prepared = await prepare(versioned_command("status"))
    assert prepared["reason"] == (None if enabled else "command_disabled")


async def test_unregistered_signature_key_does_not_admit_v2_resume(ingress):
    prepare, _ = ingress
    prepared = await prepare(versioned_command(), registered_key=False)
    assert prepared["verified_reference"] is None
    assert prepared["reason"] == "command_disabled"


async def test_unavailable_v2_service_does_not_fall_back_to_legacy_resume(ingress):
    prepare, _ = ingress
    prepared = await prepare(versioned_command(), service_available=False)
    assert prepared["reason"] == "command_disabled"
