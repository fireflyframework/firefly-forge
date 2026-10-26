# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.oauth_credential_fingerprints import OAuthCredentialMatcher
from firefly_forge.source_patch_admission import SourcePatch
from firefly_forge.store import Store


def matcher():
    return OAuthCredentialMatcher(Fernet.generate_key().decode(), "forge_test_patch")


def history(service, secret):
    return service.packet(uuid4().hex, "registration", uuid4().hex, 1, [secret])


def test_large_public_patch_and_generic_documents_use_complete_streamed_matching():
    service = matcher()
    text = "public" * 190000
    assert not service.contains_source_patch(SourcePatch(text), [])
    assert not service.contains(text, [])
    secret = "historical-credential-at-public-tail"
    assert service.contains(text + secret, [history(service, secret)])


@pytest.mark.parametrize("offset", [0, 1048570, 1100000])
def test_historical_credential_is_detected_at_every_patch_position(offset):
    service = matcher()
    secret = "historical-credential-value"
    text = "x" * offset + secret + "x" * (1140000 - offset)
    assert service.contains_source_patch(SourcePatch(text), [history(service, secret)])


def test_patch_history_remains_authenticated_across_a_large_complete_scan():
    service = matcher()
    packet = history(service, "secret")
    tampered = copy.deepcopy(packet)
    tampered["revision"] += 1
    with pytest.raises(ValueError, match="authenticated"):
        service.contains_source_patch(SourcePatch("public"), [tampered])
    assert not service.contains_source_patch(
        SourcePatch("x" * 1140000), [history(service, "a"), history(service, "bb")]
    )
    assert service.contains_source_patch(SourcePatch("x" * 1140000 + "secret"), [packet])


@pytest.mark.parametrize("value", ["x" * (4 * 1024 * 1024 + 1), "\ud800", b"bytes", True])
def test_patch_requires_bounded_valid_unicode(value):
    with pytest.raises((TypeError, ValueError)):
        SourcePatch(value)


async def test_current_di_binds_typed_patch_to_authenticated_evidence_snapshot(tmp_path):
    calls = []

    async def execute(statement):
        calls.append(statement)
        return SimpleNamespace(mappings=lambda: [])

    @asynccontextmanager
    async def unit(**kwargs):
        yield SimpleNamespace(execute=execute)

    store = SimpleNamespace(
        schema="forge_test_patch",
        _unit=unit,
        configuration=ConfigurationStore(tmp_path),
        credential_registry=lambda: {},
    )
    service = ConnectionOAuth(store, encryption_key=Fernet.generate_key().decode(), browser_origin="")
    assert store.vault_source_patch_admission == service.admit_source_patch
    text = "public" * 190000
    await store.vault_source_patch_admission(SourcePatch(text))
    assert len(calls) == 2
    assert str(calls[0]) == str(calls[1])
    service._secrets["FORGE_CURRENT_SECRET"] = "public"
    with pytest.raises(ValueError, match="configured credentials"):
        await store.vault_source_patch_admission(SourcePatch(text))


async def test_store_does_not_skip_unavailable_vault_coverage():
    calls = []

    async def generic(value):
        calls.append(value)
        raise ValueError("OAuth credential admission service is unavailable")

    store = SimpleNamespace(vault_source_patch_admission=None, admit_credentials=generic)
    with pytest.raises(ValueError, match="unavailable"):
        await Store.admit_source_patch(store, SourcePatch("public patch"))
    assert calls == ["public patch"]
