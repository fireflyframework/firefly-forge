# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Credential rotation is rescanned from retained objects, never a lost workspace."""

import copy

import pytest
from test_runner_object_artifacts_host import allowed, authority_fence
from test_source_archive_publisher_host import fixture


async def recorded(tmp_path):
    publisher, workspace, run, records = fixture(tmp_path)
    data = b"safe retained source\n" * 1000
    (workspace / "app.py").write_bytes(data)
    ref = await publisher.publish(run, workspace, "a" * 40, "b" * 40, authority_fence(), authorize=allowed)
    page, descriptors = await publisher.index.page(ref, 0)
    for path in workspace.iterdir():
        path.unlink()
    workspace.rmdir()
    publisher.content.helper.credential_registry = lambda: {"NEW_TOKEN": "rotated-private-token-8362892"}
    return publisher, ref, page.entries[0], descriptors[0], data, records


async def test_explicit_retained_scan_streams_all_chunks_without_workspace_or_reupload(tmp_path, monkeypatch):
    publisher, ref, entry, descriptor, data, records = await recorded(tmp_path)
    content = publisher.content
    before = copy.deepcopy(content.helper.ledger.rows)
    reads = []
    native_read = content.helper.objects.read_chunk

    async def read(scope, chunk, **kwargs):
        reads.append(chunk.bytes)
        return await native_read(scope, chunk, **kwargs)

    def no_write(*args, **kwargs):
        raise AssertionError("A credential scan cannot upload or recreate source bytes")

    monkeypatch.setattr(content.helper.objects, "read_chunk", read)
    monkeypatch.setattr(content.helper.objects, "write_chunks", no_write)
    monkeypatch.setattr(content.helper, "_spooled", no_write)
    proof = await content.scan(ref.content_binding(), entry, descriptor, authorize=allowed)
    assert sum(reads) == len(data) and max(reads) <= 4096
    assert proof == descriptor["identity_sha256"]
    assert content.helper.ledger.rows == before
    assert not list(content.helper.scratch.iterdir())


@pytest.mark.parametrize("mutation", ["secret", "registry", "revoked", "descriptor", "corrupt"])
async def test_retained_scan_cannot_confirm_secret_tampered_or_changed_authority_bytes(tmp_path, monkeypatch, mutation):
    publisher, ref, entry, descriptor, data, _ = await recorded(tmp_path)
    content = publisher.content
    read = content.helper.objects.read_chunk
    calls = 0

    async def reading(scope, chunk, **kwargs):
        nonlocal calls
        calls += 1
        result = await read(scope, chunk, **kwargs)
        if mutation == "registry":
            content.helper.credential_registry = lambda: {"NEW_TOKEN": "changed-again-private-token-8362892"}
        if mutation == "corrupt":
            return b"!" + result[1:]
        return result

    async def current():
        if mutation == "revoked" and calls:
            raise ValueError("revoked")

    if mutation == "secret":
        content.helper.credential_registry = lambda: {"NEW_TOKEN": "safe retained source"}
    elif mutation == "descriptor":
        descriptor = {**descriptor, "identity_sha256": "0" * 64}
    monkeypatch.setattr(content.helper.objects, "read_chunk", reading)
    with pytest.raises(ValueError):
        await content.scan(ref.content_binding(), entry, descriptor, authorize=current)
    assert not list(content.helper.scratch.iterdir())


class Scans:
    def __init__(self):
        self.rows = {}

    async def locked(self, key):
        return self.rows.get(key)

    async def save(self, row):
        self.rows[row.id.hex] = row

    async def completed(self, snapshot, fingerprint):
        return next(
            (
                row
                for row in self.rows.values()
                if row.snapshot_id.hex == snapshot and row.fingerprint == fingerprint and row.status == "scanned"
            ),
            None,
        )

    def _require_session(self):
        from types import SimpleNamespace

        async def flush():
            return None

        return SimpleNamespace(flush=flush)


async def current_reader(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import UUID

    from test_source_explorer_host import prepared

    from firefly_forge.project_access import AccessDenied, ProjectAccess

    explorer, ref, state, publisher, workspace = await prepared(tmp_path)
    scans = Scans()
    publisher.index.scans = scans
    state.owner = True
    pools = publisher.index.pools
    pools.schema = "controlled_source"
    unit = pools._unit

    async def execute(*args, **kwargs):
        return None

    @asynccontextmanager
    async def native_unit():
        prior = copy.deepcopy(scans.rows)
        try:
            async with unit():
                yield SimpleNamespace(execute=execute)
        except BaseException:
            scans.rows = prior
            raise

    monkeypatch.setattr(pools, "_unit", native_unit)

    async def authorize(self, actor, project_id, permission):
        assert actor["user_id"] == UUID(int=7).hex and project_id == ref.project_id and permission == "members"
        if not state.owner or not state.valid:
            raise AccessDenied("Current project owner authority is unavailable")
        return True

    monkeypatch.setattr(ProjectAccess, "authorize_actor", authorize)

    async def commit_authority(run, reader):
        assert all(value == state.run.get(key) for key, value in run.items()) and reader.id == UUID(int=7)
        await explorer._scan_authority(run, reader)

    monkeypatch.setattr(explorer, "_scan_commit_authority", commit_authority)
    publisher.content.helper.credential_registry = lambda: {"NEW_TOKEN": "rotated-private-token-8362892"}
    for path in workspace.iterdir():
        path.unlink()
    workspace.rmdir()
    return explorer, ref, state, publisher, scans


def scan_input(candidate):
    from firefly_forge.source_archive_scan_models import SourceCredentialScanInputV1

    return SourceCredentialScanInputV1(
        expected_manifest=candidate["expected_manifest"],
        expected_credential_fingerprint=candidate["expected_credential_fingerprint"],
        acknowledge_retained_bytes_only=True,
        acknowledge_no_source_change=True,
    )


async def test_rotation_holds_then_explicit_retained_scan_restores_access_without_changing_archive(
    tmp_path, monkeypatch
):
    from uuid import UUID

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    explorer, ref, state, publisher, scans = await current_reader(tmp_path, monkeypatch)
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    before = copy.deepcopy(publisher.content.helper.ledger.rows)
    root = copy.deepcopy(publisher.index.roots.rows[ref.snapshot_id].data)
    try:
        with pytest.raises(ValueError, match="credentials"):
            await explorer.file(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=0, entry_index=0)
        candidate = await explorer.scan_candidate(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256)
        assert scans.rows == {}
        body = scan_input(candidate)
        receipt = await explorer.rescan(ref.run_id, ref.snapshot_id, "e" * 32, body)
        assert receipt["snapshot_id"] == ref.snapshot_id and receipt["no_execution_grant"] is True
        assert receipt["source_digest"] == ref.catalog.source_digest and receipt["file_count"] == 2
        restored = await explorer.file(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=0, entry_index=0)
        assert restored["text"] == "print(1)\n"
        observed = await explorer.scan_receipt(ref.run_id, ref.snapshot_id, "e" * 32, ref.catalog.manifest_sha256)
        assert observed["status"] == "scanned" and observed["receipt"] == receipt
        reads = publisher.content.helper.objects.read_chunk

        async def no_read(*args, **kwargs):
            raise AssertionError("Exact receipt replay cannot rescan objects")

        monkeypatch.setattr(publisher.content.helper.objects, "read_chunk", no_read)
        assert await explorer.rescan(ref.run_id, ref.snapshot_id, "e" * 32, body) == receipt
        monkeypatch.setattr(publisher.content.helper.objects, "read_chunk", reads)
        assert publisher.content.helper.ledger.rows == before
        assert publisher.index.roots.rows[ref.snapshot_id].data == root
        state.valid = False
        with pytest.raises(Exception, match="revoked"):
            await explorer.file(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=0, entry_index=0)
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("mutation", ["owner", "registry", "secret", "revoked_during_read", "lost_reply"])
async def test_retained_scan_has_no_implicit_retry_or_authority_extension(tmp_path, monkeypatch, mutation):
    from uuid import UUID

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

    explorer, ref, state, publisher, scans = await current_reader(tmp_path, monkeypatch)
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    try:
        body = scan_input(await explorer.scan_candidate(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256))
        read = publisher.content.helper.objects.read_chunk

        async def reading(*args, **kwargs):
            data = await read(*args, **kwargs)
            if mutation == "registry":
                publisher.content.helper.credential_registry = lambda: {
                    "NEW_TOKEN": "changed-again-private-token-8362892"
                }
            elif mutation == "revoked_during_read":
                state.valid = False
            return data

        monkeypatch.setattr(publisher.content.helper.objects, "read_chunk", reading)
        if mutation == "owner":
            state.owner = False
        elif mutation == "secret":
            publisher.content.helper.credential_registry = lambda: {"NEW_TOKEN": "print(1)"}
            body = scan_input(await explorer.scan_candidate(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256))
        elif mutation == "lost_reply":
            complete = publisher.index.complete_scan

            async def lost(*args, **kwargs):
                await complete(*args, **kwargs)
                raise TimeoutError("Controlled lost response after commit")

            monkeypatch.setattr(publisher.index, "complete_scan", lost)
        with pytest.raises((ValueError, AccessDenied, TimeoutError)):
            await explorer.rescan(ref.run_id, ref.snapshot_id, "e" * 32, body)
        if mutation == "owner":
            assert not scans.rows
        else:
            row = scans.rows["e" * 32]
            assert row.status == ("scanned" if mutation == "lost_reply" else "pending")
            if mutation == "lost_reply":
                observed = await explorer.scan_receipt(
                    ref.run_id, ref.snapshot_id, "e" * 32, ref.catalog.manifest_sha256
                )
                assert observed["receipt"]["request_id"] == "e" * 32
            elif mutation == "secret":
                with pytest.raises(ValueError, match="pending"):
                    await explorer.rescan(ref.run_id, ref.snapshot_id, "e" * 32, body)
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("mutation", ["source", "snapshot", "tenant", "actor", "count", "request", "fingerprint"])
async def test_rehashed_foreign_credential_proof_never_relabels_a_retained_source(tmp_path, monkeypatch, mutation):
    from uuid import UUID

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL
    from firefly_forge.runner_pool_models import pool_digest

    explorer, ref, state, publisher, scans = await current_reader(tmp_path, monkeypatch)
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    try:
        body = scan_input(await explorer.scan_candidate(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256))
        await explorer.rescan(ref.run_id, ref.snapshot_id, "e" * 32, body)
        row = scans.rows["e" * 32]
        receipt = row.data["receipt"]
        field, value = {
            "source": ("source_digest", "f" * 64),
            "snapshot": ("snapshot_id", "f" * 32),
            "tenant": ("tenant_id", "f" * 32),
            "actor": ("actor_id", "f" * 32),
            "count": ("file_count", 0),
            "request": ("request_sha256", "f" * 64),
            "fingerprint": ("credential_fingerprint", "f" * 64),
        }[mutation]
        receipt[field] = value
        receipt["digest"] = pool_digest({k: v for k, v in receipt.items() if k != "digest"})
        with pytest.raises(ValueError):
            await explorer.file(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=0, entry_index=0)
        with pytest.raises(ValueError):
            await explorer.scan_receipt(ref.run_id, ref.snapshot_id, "e" * 32, ref.catalog.manifest_sha256)
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_paged_rescan_is_linear_and_midscan_catalog_tamper_never_confirms(tmp_path, monkeypatch):
    from uuid import UUID

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    explorer, ref, state, publisher, scans = await current_reader(tmp_path, monkeypatch)
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    reads = []
    at = publisher.index.pages.at

    async def page(*args):
        reads.append(args)
        return await at(*args)

    monkeypatch.setattr(publisher.index.pages, "at", page)
    try:
        body = scan_input(await explorer.scan_candidate(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256))
        await explorer.rescan(ref.run_id, ref.snapshot_id, "e" * 32, body)
        assert len(reads) == 2 * ref.catalog.page_count
        # Use a distinct current registry/request; an earlier successful scan cannot cover this change.
        publisher.content.helper.credential_registry = lambda: {"NEW_TOKEN": "another-rotation-private-token-996291"}
        body = scan_input(await explorer.scan_candidate(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256))
        scan = publisher.content.scan

        async def tamper(*args, **kwargs):
            value = await scan(*args, **kwargs)
            publisher.index.pages.parts[(ref.snapshot_id, 1)].data["content"][0]["identity_sha256"] = "0" * 64
            return value

        monkeypatch.setattr(publisher.content, "scan", tamper)
        with pytest.raises(ValueError):
            await explorer.rescan(ref.run_id, ref.snapshot_id, "f" * 32, body)
        assert scans.rows["f" * 32].status == "pending"
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_sdk_uses_one_typed_post_and_exact_readback_after_lost_reply(tmp_path, monkeypatch):
    from uuid import UUID

    import httpx

    from firefly_forge.client import ForgeClient
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL
    from firefly_forge.runner_pool_models import pool_digest
    from firefly_forge.source_explorer import SourceExplorerController

    explorer, ref, state, publisher, scans = await current_reader(tmp_path, monkeypatch)
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    try:
        candidate = await explorer.scan_candidate(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256)
        body = scan_input(candidate)
        receipt = await explorer.rescan(ref.run_id, ref.snapshot_id, "e" * 32, body)
        view = await explorer.scan_receipt(ref.run_id, ref.snapshot_id, "e" * 32, ref.catalog.manifest_sha256)
        response = await SourceExplorerController(explorer).scan_receipt(
            ref.run_id, ref.snapshot_id, "e" * 32, ref.catalog.manifest_sha256
        )
        assert response.headers["Cache-Control"] == "private, no-store"
    finally:
        CURRENT_PRINCIPAL.reset(token)
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Controlled lost response", request=request)
        return httpx.Response(200, json=candidate if request.url.path.endswith("candidate") else view)

    with ForgeClient("https://factory.invalid", "private-client-token") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://factory.invalid/api/v1/", transport=httpx.MockTransport(handle))
        assert client.run_source_scan_candidate(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256) == candidate
        with pytest.raises(httpx.ReadTimeout):
            client.run_source_rescan(ref.run_id, ref.snapshot_id, "e" * 32, body)
        observed = client.run_source_scan_receipt(
            ref.run_id,
            ref.snapshot_id,
            "e" * 32,
            ref.catalog.manifest_sha256,
            expected_request_sha256=pool_digest(body.model_dump(mode="json")),
        )
        assert observed.receipt.model_dump(mode="json") == receipt
    assert [request.method for request in requests] == ["GET", "POST", "GET"]


@pytest.mark.parametrize(
    "field,value", [("version", True), ("acknowledge_retained_bytes_only", 1), ("acknowledge_no_source_change", False)]
)
def test_scan_inputs_never_coerce_acknowledgments_or_version(field, value):
    from firefly_forge.source_archive_scan_models import SourceCredentialScanInputV1

    body = {
        "version": 1,
        "expected_manifest": "a" * 64,
        "expected_credential_fingerprint": "b" * 64,
        "acknowledge_retained_bytes_only": True,
        "acknowledge_no_source_change": True,
    }
    body[field] = value
    with pytest.raises(ValueError):
        SourceCredentialScanInputV1.model_validate(body)


async def test_scan_native_units_do_not_recursively_open_source_or_leak_factory_search_path(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import UUID

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    explorer, ref, state, publisher, scans = await current_reader(tmp_path, monkeypatch)
    pools = publisher.index.pools
    original = pools._unit
    active = False
    pools.sessions.current = lambda: object() if active else None

    async def execute(*args, **kwargs):
        return None

    @asynccontextmanager
    async def unit():
        nonlocal active
        assert not active
        active = True
        try:
            async with original():
                yield SimpleNamespace(execute=execute)
        finally:
            active = False

    monkeypatch.setattr(pools, "_unit", unit)
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    try:
        body = scan_input(await explorer.scan_candidate(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256))
        result = await explorer.rescan(ref.run_id, ref.snapshot_id, "e" * 32, body)
        assert result["no_execution_grant"] and not active
    finally:
        CURRENT_PRINCIPAL.reset(token)


def test_constructed_or_copied_scan_models_cannot_skip_wire_acknowledgments():
    from firefly_forge.source_archive_scan_models import SourceCredentialScanInputV1

    body = SourceCredentialScanInputV1(
        expected_manifest="a" * 64,
        expected_credential_fingerprint="b" * 64,
        acknowledge_retained_bytes_only=True,
        acknowledge_no_source_change=True,
    )
    with pytest.raises(ValueError):
        SourceCredentialScanInputV1.model_validate(body.model_copy(update={"acknowledge_no_source_change": False}))
