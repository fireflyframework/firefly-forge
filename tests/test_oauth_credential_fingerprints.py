# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Irreversible vault admission evidence with Unicode and bounded work."""

import copy
import json
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from firefly_forge.oauth_credential_fingerprints import OAuthCredentialMatcher


def matcher(key=None):
    return OAuthCredentialMatcher(key or Fernet.generate_key().decode(), "forge_test_fixture")


def packet(svc, secret):
    return svc.packet(uuid4().hex, "registration", uuid4().hex, 1, [secret])


@pytest.mark.parametrize("secret", ["a", "short", "ordinary-credential-1234", "秘密🔐café", 'line\\quote"value'])
@pytest.mark.parametrize("escaped", [False, True])
def test_full_secret_substring_and_json_representation_match(secret, escaped):
    svc = matcher()
    raw = json.dumps(secret, ensure_ascii=True)[1:-1] if escaped else secret
    history = packet(svc, secret)
    assert svc.contains({"instruction": "Prefix " + raw + " suffix"}, [history])
    if len(secret) >= 8:
        assert secret not in json.dumps(history)


def test_matching_prefix_is_only_a_candidate_and_never_authority():
    svc = matcher()
    history = packet(svc, "shared-prefix-123456789-real-secret")
    assert not svc.contains("shared-prefix-123456789-different", [history])


@pytest.mark.parametrize("field", ["source_id", "revision", "row_id", "representations"])
def test_packet_context_and_representations_are_authenticated(field):
    svc = matcher()
    history = copy.deepcopy(packet(svc, "ordinary-credential-1234"))
    history[field] = [] if field == "representations" else "tampered"
    with pytest.raises(ValueError, match="history"):
        svc.contains("An ordinary customer journey.", [history])


def test_different_key_or_schema_cannot_silently_admit_history():
    key = Fernet.generate_key().decode()
    svc = matcher(key)
    history = packet(svc, "ordinary-credential-1234")
    with pytest.raises(ValueError, match="history"):
        matcher().contains("A public statement.", [history])
    with pytest.raises(ValueError, match="history"):
        OAuthCredentialMatcher(key, "forge_test_other").contains("A public statement.", [history])


def test_no_secret_positive_and_bounded_oversized_admission():
    svc = matcher()
    assert not svc.contains({"instructions": "Use atomic design and explain the customer journey."}, [])
    with pytest.raises(ValueError, match="bound"):
        svc.contains("x" * (1_048_576 + 1), [])


def test_deep_or_cyclic_inputs_fail_closed():
    svc = matcher()
    value = []
    value.append(value)
    with pytest.raises(ValueError, match="bound"):
        svc.contains(value, [])


def test_surrogate_text_returns_a_bounded_admission_error():
    with pytest.raises(ValueError, match="valid Unicode"):
        matcher().contains("\ud800", [])


def test_history_and_scan_work_are_bounded_before_matching():
    svc = matcher()
    history = packet(svc, "one-credential-value")
    with pytest.raises(ValueError, match="history.*bound"):
        svc.contains("A public customer journey.", [history] * 1025)
    histories = [packet(svc, "a"), packet(svc, "bb"), packet(svc, "ccc")]
    with pytest.raises(ValueError, match="work bound"):
        svc.contains("x" * 1_000_000, histories)


@pytest.mark.parametrize(
    "secret,value",
    [
        ("12345678901234567890", 12345678901234567890),
        ("true", True),
        ("false", False),
        ("null", None),
        ("123.125", 123.125),
    ],
)
def test_json_scalar_spelling_is_checked_as_actual_serialized_data(secret, value):
    svc = matcher()
    assert svc.contains({"result": value}, [packet(svc, secret)])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 1 << 4097])
def test_invalid_or_oversized_json_scalars_fail_before_conversion(value):
    with pytest.raises(ValueError, match="bound|finite"):
        matcher().contains({"result": value}, [])


def test_valid_no_secret_json_scalars_are_accepted():
    assert not matcher().contains({"active": True, "count": 42, "ratio": 1.25, "optional": None}, [])
