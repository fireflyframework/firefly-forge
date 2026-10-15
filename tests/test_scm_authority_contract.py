# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pure contracts for explicit SCM delegation; no provider or database calls."""

import base64
import hashlib
import hmac

import pytest
from pydantic import ValidationError


def reference(**changes):
    return {
        "version": 1,
        "provider": "github",
        "web_origin": "https://github.com",
        "api_origin": "https://api.github.com",
        "connection_revision": "a" * 32,
        "connection_scope_digest": "b" * 64,
        "installation_id": "17",
        "remote_repository_id": "42",
        "external_actor_id": "7",
        "webhook_key_revision": "c" * 32,
        "verification_method": "raw_body_hmac_sha256",
        "payload_digest": "d" * 64,
        **changes,
    }


def test_verified_reference_requires_actual_provider_scope():
    from firefly_forge.scm_authority_models import VerifiedSCMReferenceV1

    assert VerifiedSCMReferenceV1.model_validate(reference()).external_actor_id == "7"
    with pytest.raises(ValidationError):
        VerifiedSCMReferenceV1.model_validate(reference(installation_id=None))
    with pytest.raises(ValidationError):
        VerifiedSCMReferenceV1.model_validate(reference(provider="gitlab"))


@pytest.mark.parametrize("value", [True, 7, "007", "0", "actor-name", "user@example.test"])
def test_external_actor_id_is_not_a_label_or_coerced_number(value):
    from firefly_forge.scm_authority_models import VerifiedSCMReferenceV1

    with pytest.raises(ValidationError):
        VerifiedSCMReferenceV1.model_validate(reference(external_actor_id=value))


@pytest.mark.parametrize(
    "origin", ["http://github.com", "https://github.com/path", "https://user@github.com", "https://github.com?x=1"]
)
def test_reference_origin_is_exact_https_origin(origin):
    from firefly_forge.scm_authority_models import VerifiedSCMReferenceV1

    with pytest.raises(ValidationError):
        VerifiedSCMReferenceV1.model_validate(reference(web_origin=origin))


def test_bitbucket_uuid_normalization_binds_one_identity():
    from firefly_forge.scm_authority_models import VerifiedSCMReferenceV1

    value = VerifiedSCMReferenceV1.model_validate(
        reference(
            provider="bitbucket",
            installation_id=None,
            external_actor_id="{AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA}",
            remote_repository_id="{BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB}",
        )
    )
    assert value.external_actor_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert value.remote_repository_id == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def test_legacy_gitlab_token_cannot_be_delegation_reference():
    from firefly_forge.scm_authority_models import VerifiedSCMReferenceV1

    with pytest.raises(ValidationError):
        VerifiedSCMReferenceV1.model_validate(
            reference(
                provider="gitlab",
                installation_id=None,
                verification_method="gitlab_shared_token",
            )
        )


@pytest.mark.parametrize("version", [True, False, "1", 1.0])
def test_version_discriminator_is_exact_integer(version):
    from firefly_forge.scm_authority_models import SCMActorBindingInputV1, VerifiedSCMReferenceV1

    for model, body in ((SCMActorBindingInputV1, binding()), (VerifiedSCMReferenceV1, reference())):
        with pytest.raises(ValidationError):
            model.model_validate({**body, "version": version})


@pytest.mark.parametrize(
    "changes",
    [
        {"api_origin": "not-an-origin"},
        {"installation_id": None},
        {"remote_repository_id": "repository-name"},
        {"external_actor_id": "actor-label"},
    ],
)
def test_v2_provenance_uses_same_origin_and_provider_identity_rules(changes):
    from firefly_forge.scm_authority_models import SCMRespondingActorV2

    retained = reference()
    provenance = {
        name: retained[name]
        for name in (
            "provider",
            "api_origin",
            "connection_revision",
            "installation_id",
            "remote_repository_id",
            "external_actor_id",
        )
    }
    provenance.update(binding_request_id="a" * 32, binding_digest="b" * 64, verified_receipt_id="c" * 32)
    body = {"version": 2, "user_id": "d" * 32, "authentication_method": "scm_mapping", "provenance": provenance}
    assert SCMRespondingActorV2.model_validate(body).provenance.external_actor_id == "7"
    with pytest.raises(ValidationError):
        SCMRespondingActorV2.model_validate({**body, "provenance": {**provenance, **changes}})


def binding(**changes):
    return {
        "version": 1,
        "request_id": "a" * 32,
        "expected_previous_digest": None,
        "verified_receipt_id": "b" * 32,
        "expected_verified_reference_digest": "c" * 64,
        "native_user_id": "d" * 32,
        "component_ids": ["api"],
        "allowed_actions": ["respond", "status", "cancel"],
        "active": True,
        "acknowledge_webhook_asserted_identity": True,
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"active": 1},
        {"acknowledge_webhook_asserted_identity": 1},
        {"acknowledge_webhook_asserted_identity": False},
        {"component_ids": ["api", "api"]},
        {"allowed_actions": ["run"]},
        {"allowed_actions": ["*"]},
        {"native_user_id": True},
        {"external_actor_id": "arbitrary"},
    ],
)
def test_admin_input_has_explicit_scope_no_caller_provenance(changes):
    from firefly_forge.scm_authority_models import SCMActorBindingInputV1

    assert SCMActorBindingInputV1.model_validate(binding()).allowed_actions == ["respond", "status", "cancel"]
    with pytest.raises(ValidationError):
        SCMActorBindingInputV1.model_validate(binding(**changes))


def gitlab_signature(raw=b'{ "note": "hello" }', timestamp="1700000000"):
    key = bytes(range(32))
    token = "whsec_" + base64.b64encode(key).decode()
    message = b"delivery-1." + timestamp.encode() + b"." + raw
    signature = base64.b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode()
    return token, {"webhook-id": "delivery-1", "webhook-timestamp": timestamp, "webhook-signature": "v1," + signature}


def test_gitlab_signing_binds_id_timestamp_raw_bytes_without_token_downgrade():
    from firefly_forge.scm_webhooks import verify_gitlab_signing_token

    raw = b'{ "note": "hello" }'
    token, headers = gitlab_signature(raw)
    assert verify_gitlab_signing_token(raw, headers, token, current_time=1700000000)
    assert not verify_gitlab_signing_token(b'{"note":"hello"}', headers, token, current_time=1700000000)
    assert not verify_gitlab_signing_token(raw, {**headers, "webhook-id": "other"}, token, current_time=1700000000)
    assert not verify_gitlab_signing_token(raw, {"x-gitlab-token": token}, token, current_time=1700000000)
    assert not verify_gitlab_signing_token(raw, headers, token, current_time=1700000301)
    assert not verify_gitlab_signing_token(raw, headers, token, current_time=1699999699)


@pytest.mark.parametrize("token", ["whsec_bad!", "whsec_" + base64.b64encode(b"short").decode(), "legacy-token"])
def test_gitlab_signing_key_is_exact_32_byte_key(token):
    from firefly_forge.scm_webhooks import verify_gitlab_signing_token

    _, headers = gitlab_signature()
    assert not verify_gitlab_signing_token(b"{}", headers, token, current_time=1700000000)
