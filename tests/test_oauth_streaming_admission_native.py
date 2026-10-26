# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned SQL contracts for complete retained text with real configured OAuth history."""

import hashlib
import json
from uuid import uuid4

import pytest
from test_agentic_checkpoint_storage_host import checkpoint_for
from test_bridge_completion_native import prepared
from test_connection_oauth_ledger import actor, configuration, service

from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL


async def test_native_large_json_checks_current_and_retired_oauth_history(store):
    vault = service(store)
    user = actor()
    retired, current = "fictional-retired-" + uuid4().hex, "fictional-current-" + uuid4().hex
    await vault.configure("large-admission", configuration(client_secret=retired), user)
    await vault.configure("large-admission", configuration(client_secret=current), user)
    history = await vault.source_diff_history()
    assert len(history.packets) == 2
    assert vault.matcher.contains(retired, list(history.packets))
    values = ["Fictional public retained source. " * 550 for _ in range(130)]
    assert sum(len(value.encode()) for value in values) > 2097152
    await store.admit_credentials({"retained": values})
    for credential in (retired, current):
        with pytest.raises(ValueError, match="configured credentials"):
            await store.admit_credentials({"retained": [*values, credential]})


async def test_native_large_checkpoint_journal_with_configured_history_reads_normal_and_ambient(store, tmp_path):
    async with prepared(store, tmp_path) as (_, _, run, journal, completions, _, descriptor, body):
        vault = service(store)
        credential = "fictional-journal-private-" + uuid4().hex
        await vault.configure("large-journal", configuration(client_secret=credential), CURRENT_PRINCIPAL.get())
        history = await vault.source_diff_history()
        assert history.packets and vault.matcher.contains(credential, list(history.packets))
        initial = await checkpoint_for(body)
        record = initial.record.model_dump(mode="json")
        for message in record["history"]:
            for part in message["parts"]:
                if isinstance(part.get("content"), str):
                    part["content"] = "Fictional public source line.\n" * 45000
        record["request_history"] = record["history"]
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        assert len(canonical) > 1048576
        encoded = _checkpoint_payload(canonical)
        checkpoint = BoundaryCheckpoint.decode(encoded, hashlib.sha256(encoded).hexdigest())
        assert checkpoint.canonical_payload == canonical
        task = await journal.intent(run["id"], "implement")
        await AgentCheckpoints(completions).scope(run, task, capture=lambda _: checkpoint).admit(descriptor, body)
        normal = await journal.entries(run["id"])
        assert Journal.verify_chain(normal)
        retained = next(row for row in normal if row["kind"] == "agent_checkpoint")
        assert retained["payload"]["snapshot"].encode() == checkpoint.payload
        async with store._unit():
            assert await journal.entries(run["id"]) == normal
        # A credential configured after capture must withhold expanded history too.
        await vault.configure(
            "large-journal", configuration(client_secret="Fictional public source line."), CURRENT_PRINCIPAL.get()
        )
        with pytest.raises(ValueError, match="configured credentials"):
            await journal.entries(run["id"])
        async with store._unit():
            with pytest.raises(ValueError, match="configured credentials"):
                await journal.entries(run["id"])


async def test_native_credential_rotation_during_scan_refuses_without_admission_writes(store, monkeypatch):
    import asyncio

    from sqlalchemy import func, select

    from firefly_forge.connection_oauth_persistence import OAuthCredentialEvidenceEntity, OAuthEventEntity

    vault = service(store)
    user = actor()
    await vault.configure("rotating-admission", configuration(client_secret="fictional-before-rotation"), user)
    original = vault.matcher.contains_async
    entered, release = asyncio.Event(), asyncio.Event()

    async def at_final_admission(*args, **kwargs):
        verify = kwargs["verify_current"]

        async def waiting():
            entered.set()
            await release.wait()
            await verify()

        return await original(*args, **{**kwargs, "verify_current": waiting})

    async def counts():
        async with store._unit(autoflush=False) as session:
            return (
                await session.scalar(select(func.count()).select_from(OAuthCredentialEvidenceEntity)),
                await session.scalar(select(func.count()).select_from(OAuthEventEntity)),
            )

    monkeypatch.setattr(vault.matcher, "contains_async", at_final_admission)
    admission = asyncio.create_task(vault.admit_credentials({"retained": "Fictional public result " * 1000}))
    await entered.wait()
    try:
        # The concurrent control-plane operation is real; avoid its independent
        # admission waiting on this receiver's test barrier.
        monkeypatch.setattr(vault.matcher, "contains_async", original)
        await vault.configure("rotating-admission", configuration(client_secret="fictional-after-rotation"), user)
        before = await counts()
    except BaseException:
        admission.cancel()
        await asyncio.gather(admission, return_exceptions=True)
        raise
    finally:
        release.set()
    with pytest.raises(ValueError, match="changed during admission"):
        await admission
    assert await counts() == before
    assert await store.runs() == []
