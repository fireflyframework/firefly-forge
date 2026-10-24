# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Streaming admission must protect complete source, including chunk boundaries."""

import asyncio
import base64
import importlib
import importlib.util

import pytest

from firefly_forge.oauth_credential_fingerprints import OAuthCredentialMatcher


def scanner(credentials=(), packets=(), guard=None):
    name = "firefly_forge.source_diff_admission"
    assert importlib.util.find_spec(name) is not None, "Source diff streaming credential admission is missing"
    module = importlib.import_module(name)
    matcher = OAuthCredentialMatcher(base64.urlsafe_b64encode(bytes(range(32))).decode(), "public_fixture")

    async def current():
        pass

    return module.SourceDiffAdmission(
        credentials=credentials, matcher=matcher, packets=packets, verify_current=guard or current
    )


def history(secret):
    matcher = OAuthCredentialMatcher(base64.urlsafe_b64encode(bytes(range(32))).decode(), "public_fixture")
    return (matcher.packet("1" * 32, "grant", "2" * 32, 1, [secret]),)


async def test_documents_use_one_cut_without_cross_file_concatenation_or_early_finalization():
    checks = []

    async def guard():
        checks.append("current")

    stream = scanner(("public-fixture-secret",), guard=guard)
    await stream(b"public-fixture-", False)
    stream.finish_document()
    await stream(b"secret", False)
    stream.finish_document()
    assert checks == []
    await stream(b"clean serialized patch", False)
    await stream(b"", True)
    assert checks == ["current"]
    with pytest.raises(ValueError, match="closed"):
        stream.finish_document()


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("boundary", [1, 7, 17, 19])
async def test_split_current_or_retired_credential_never_passes_final_admission(historical, boundary):
    token = 'public-fixture-🐉-token-"-never-publish'
    stream = scanner(() if historical else (token,), history(token) if historical else ())
    data = token.encode()
    with pytest.raises(ValueError, match="credential") as error:
        await stream(b"public header\n" + data[:boundary], False)
        await stream(data[boundary:] + b"\npublic suffix", False)
        await stream(b"", True)
    assert token not in str(error.value)


@pytest.mark.parametrize("historical", [False, True])
async def test_escaped_credential_representation_across_chunks_is_not_admitted(historical):
    token = 'public-fixture-"-\\-token'
    stream = scanner(() if historical else (token,), history(token) if historical else ())
    data = b'public-fixture-\\"-\\\\-token'
    with pytest.raises(ValueError, match="credential"):
        await stream(data[:18], False)
        await stream(data[18:], False)


async def test_clean_diff_larger_than_old_flat_limit_stays_bounded_and_finalizes():
    stream = scanner(("public-fixture-private-token",), history("retired-public-fixture-token"))
    chunk = b"+public implementation line\n" * 2048
    total = 0
    for _ in range(96):
        await stream(chunk, False)
        total += len(chunk)
    await stream(b"", True)
    assert total == 5_505_024
    with pytest.raises(ValueError, match="closed"):
        await stream(b"unexpected", False)


async def test_changed_credential_cut_at_final_wait_cannot_finalize_or_be_retried():
    entered, release = asyncio.Event(), asyncio.Event()
    changed = False

    async def guard():
        entered.set()
        await release.wait()
        if changed:
            raise ValueError("The credential sources changed")

    stream = scanner(guard=guard)
    await stream(b"public source\n", False)
    final = asyncio.create_task(stream(b"", True))
    await entered.wait()
    changed = True
    release.set()
    with pytest.raises(ValueError, match="changed"):
        await final
    with pytest.raises(ValueError, match="closed"):
        await stream(b"", True)


async def test_cancellation_during_final_revalidation_leaves_admission_closed():
    entered = asyncio.Event()

    async def guard():
        entered.set()
        await asyncio.Event().wait()

    stream = scanner(guard=guard)
    final = asyncio.create_task(stream(b"", True))
    await entered.wait()
    final.cancel()
    with pytest.raises(asyncio.CancelledError):
        await final
    with pytest.raises(ValueError, match="closed"):
        await stream(b"", True)


async def test_bearer_prefix_split_across_chunks_is_rejected_without_registered_tokens():
    stream = scanner()
    await stream(b"public\nAuthoriz", False)
    with pytest.raises(ValueError, match="credential"):
        await stream(b"ation: Bearer unregistered-public-fixture", False)


async def test_configured_megabyte_chunks_are_scanned_in_bounded_internal_windows():
    stream = scanner()
    await stream(b"+" * 1_048_576, False)
    await stream(b"", True)


async def prepared(store, settings=None):
    module = importlib.import_module("firefly_forge.source_diff_admission")
    prepare = getattr(module, "prepare_source_diff_admission", None)
    assert callable(prepare), "Authenticated streaming credential preparation is missing"

    async def owner():
        pass

    return await prepare(store, settings=settings or {}, assert_current=owner)


@pytest.fixture
def admission_store(tmp_path):
    from firefly_forge.store import Store

    store = Store(tmp_path / "state" / "factory.db")
    store.credential_registry = lambda: {"SCM_TOKEN": "configured-public-fixture-token"}
    return store


@pytest.mark.parametrize("source", ["registry", "settings", "environment", "ephemeral"])
async def test_prepared_scan_covers_each_live_credential_source(admission_store, monkeypatch, source):
    from contextlib import ExitStack

    from firefly_forge.secrets import ephemeral_credentials

    token = "source-specific-private-fixture-token"
    module = importlib.import_module("firefly_forge.source_diff_admission")
    snapshot_type = getattr(module, "CredentialHistorySnapshot", None)
    assert snapshot_type is not None, "Authenticated streaming history snapshot is missing"

    async def empty():
        return snapshot_type(matcher=None, packets=(), credentials=())

    admission_store.vault_source_diff_history = empty
    settings = {}
    with ExitStack() as stack:
        if source == "registry":
            admission_store.credential_registry = lambda: {"SCM_TOKEN": token}
        elif source == "settings":
            settings = {"FROZEN_API_KEY": token}
        elif source == "environment":
            monkeypatch.setenv("PROVIDER_TOKEN", token)
        else:
            stack.enter_context(ephemeral_credentials({"SCM_TOKEN": token}))
        stream = await prepared(admission_store, settings)
        with pytest.raises(ValueError, match="credential"):
            await stream(token[:12].encode(), False)
            await stream(token[12:].encode(), False)


async def test_history_added_after_scan_prevents_manifest_release(admission_store):
    module = importlib.import_module("firefly_forge.source_diff_admission")
    snapshot_type = getattr(module, "CredentialHistorySnapshot", None)
    assert snapshot_type is not None, "Authenticated streaming history snapshot is missing"
    matcher = OAuthCredentialMatcher(base64.urlsafe_b64encode(bytes(range(32))).decode(), "public_fixture")
    packets = ()

    async def current():
        return snapshot_type(matcher=matcher, packets=packets, credentials=())

    admission_store.vault_source_diff_history = current
    stream = await prepared(admission_store)
    await stream(b"+new historical public fixture token\n", False)
    packets = history("new historical public fixture token")
    with pytest.raises(ValueError, match="changed"):
        await stream(b"", True)


async def test_changed_then_restored_configuration_still_refuses_final_cut(admission_store):
    from firefly_forge.configuration import ProviderConfiguration

    module = importlib.import_module("firefly_forge.source_diff_admission")
    snapshot_type = getattr(module, "CredentialHistorySnapshot", None)
    assert snapshot_type is not None, "Authenticated streaming history snapshot is missing"

    async def empty():
        return snapshot_type(matcher=None, packets=(), credentials=())

    admission_store.vault_source_diff_history = empty
    admission_store.configuration.update("codex", ProviderConfiguration(values={"model": "initial"}))
    stream = await prepared(admission_store)
    await stream(b"public source\n", False)
    admission_store.configuration.update("codex", ProviderConfiguration(values={"model": "changed"}))
    admission_store.configuration.update("codex", ProviderConfiguration(values={"model": "initial"}))
    with pytest.raises(ValueError, match="changed"):
        await stream(b"", True)


async def test_available_legacy_vault_without_streaming_snapshot_cannot_default_allow(admission_store):
    async def legacy(_value):
        pass

    admission_store.vault_source_patch_admission = legacy
    with pytest.raises(ValueError, match="unavailable"):
        await prepared(admission_store)


@pytest.fixture
def oauth_snapshot(admission_store, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import UUID

    from cryptography.fernet import Fernet

    from firefly_forge.connection_oauth import ConnectionOAuth

    records = []

    async def execute(_statement):
        return SimpleNamespace(mappings=lambda: records)

    @asynccontextmanager
    async def unit(**_kwargs):
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(admission_store, "_unit", unit)
    service = ConnectionOAuth(admission_store, encryption_key=Fernet.generate_key().decode(), browser_origin="")

    def add(secret):
        packet = service.matcher.packet("1" * 32, "grant", "2" * 32, 1, [secret])
        evidence = dict(
            record_kind="evidence",
            row_id=UUID("1" * 32),
            connection_id=UUID("2" * 32),
            source_kind="grant",
            source_id=UUID("2" * 32),
            revision=1,
            data=packet,
        )
        grant = dict(
            record_kind="grant",
            row_id=UUID("2" * 32),
            connection_id=UUID("2" * 32),
            source_kind="grant",
            source_id=UUID("2" * 32),
            revision=1,
            data=None,
        )
        records.extend((evidence, grant))
        return evidence, grant

    return service, records, add


async def test_real_store_and_oauth_preparation_reject_retired_tokens_across_chunks(admission_store, oauth_snapshot):
    service, _, add = oauth_snapshot
    token = "retired-oauth-public-fixture-token"
    add(token)
    stream = await prepared(admission_store)
    with pytest.raises(ValueError, match="credential"):
        await stream(token[:15].encode(), False)
        await stream(token[15:].encode(), False)
    snapshot = await admission_store.source_diff_history()
    assert token not in repr(snapshot)
    assert service._secrets["FORGE_CONNECTION_OAUTH_SECRET_ENCRYPTION_KEY"] not in repr(snapshot)


@pytest.mark.parametrize("broken", ["missing_registration", "missing_revision", "signature", "source_context"])
async def test_actual_snapshot_refuses_incomplete_or_forged_oauth_coverage(admission_store, oauth_snapshot, broken):
    from uuid import UUID

    _, records, add = oauth_snapshot
    evidence, grant = add("retired-oauth-public-fixture-token")
    if broken == "missing_registration":
        records[:] = [{**grant, "record_kind": "registration"}]
    elif broken == "missing_revision":
        grant["revision"] = 2
    elif broken == "signature":
        evidence["data"]["signature"] = "0" * 64
    else:
        evidence["connection_id"] = UUID("3" * 32)
    with pytest.raises(ValueError, match="history|authenticated"):
        await prepared(admission_store)


async def test_actual_oauth_history_published_during_scan_prevents_final_admission(admission_store, oauth_snapshot):
    _, _, add = oauth_snapshot
    stream = await prepared(admission_store)
    await stream(b"+a newly registered fixture token\n", False)
    add("newly registered fixture token")
    with pytest.raises(ValueError, match="changed"):
        await stream(b"", True)


@pytest.mark.parametrize("draining", [False, True])
async def test_default_store_factory_never_replaces_lost_or_draining_ownership_with_a_noop(
    admission_store, oauth_snapshot, draining
):
    admission_store.execution_stopping = draining
    with pytest.raises(RuntimeError, match="lost|drains"):
        await admission_store.source_patch_scanner()


async def test_concurrent_finalization_cannot_release_two_successful_admissions():
    entered, release = asyncio.Event(), asyncio.Event()

    async def guard():
        entered.set()
        await release.wait()

    stream = scanner(guard=guard)
    first = asyncio.create_task(stream(b"", True))
    await entered.wait()
    with pytest.raises(ValueError, match="closed"):
        await stream(b"", True)
    release.set()
    with pytest.raises(ValueError, match="closed"):
        await first
