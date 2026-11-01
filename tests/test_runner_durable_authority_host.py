# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real local custody must obey the same callback as durable object custody."""

import hashlib
from types import SimpleNamespace

import pytest
from test_run_owned_controller_host import initialized
from test_runner_pool_artifacts import artifact_service, fixture_request


async def allowed():
    pass


@pytest.mark.parametrize("operation", ["retain", "read", "bundle"])
async def test_disabled_object_storage_never_bypasses_authority_or_exposes_local_bytes(tmp_path, operation):
    profile, invocation, archive, root = fixture_request(tmp_path)
    artifacts = artifact_service(root)
    receipt = artifacts.retain(profile, invocation, "output", b"private output")
    artifacts.prepare(profile, invocation, archive)
    artifacts.retain(profile, invocation, "stdin", b"")
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    checks = 0

    async def revoked():
        nonlocal checks
        checks += 1
        raise PermissionError("Current artifact reader or writer revoked")

    with pytest.raises(PermissionError, match="revoked"):
        if operation == "retain":
            await artifacts.retain_durable(profile, invocation, "result", b"new result", object(), authorize=revoked)
        elif operation == "read":
            await artifacts.read_durable(profile, invocation, "output", receipt, authorize=revoked)
        else:
            await artifacts.input_archive_durable(profile, invocation, authorize=revoked)
    assert checks == 1
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


async def test_real_controller_revocation_during_daemon_check_prevents_local_retention_and_submission(
    tmp_path, monkeypatch
):
    executor, invocation, fence, _, permission, records, effects, *_ = await initialized(monkeypatch)
    _, prepared, archive, root = fixture_request(tmp_path)
    invocation = invocation.model_copy(
        update={
            "source_digest": prepared.source_digest,
            "archive_digest": hashlib.sha256(archive).hexdigest(),
            "source_bytes": len(archive),
        }
    )
    executor.artifacts = artifact_service(root)

    async def revoke():
        effects.append("daemon")
        permission["active"] = False

    monkeypatch.setattr(executor.dispatcher, "verify_daemon", revoke)
    with pytest.raises(ValueError, match="revoked"):
        await executor.accept(invocation, fence, archive, b"")
    assert not list(root.iterdir())
    assert records == {} and effects == ["daemon"] and executor.run_leases == {}


@pytest.mark.parametrize("operation", ["retain", "read"])
async def test_remote_completion_cannot_carry_stale_authority_into_local_write_or_return(tmp_path, operation):
    profile, invocation, _, root = fixture_request(tmp_path)
    artifacts = artifact_service(root)
    state = SimpleNamespace(valid=True, uploads=0, reads=0)

    async def current():
        if not state.valid:
            raise PermissionError("Artifact authority changed at remote return")

    async def upload(*args, **kwargs):
        await kwargs["authorize"]()
        state.uploads += 1
        state.valid = False

    async def read(*args, **kwargs):
        await kwargs["authorize"]()
        state.reads += 1
        yield b"retained"
        state.valid = False

    artifacts.objects = SimpleNamespace(
        objects=SimpleNamespace(settings=SimpleNamespace(chunk_bytes=4)), upload=upload, read=read
    )
    receipt = {"kind": "output", "bytes": 8, "sha256": hashlib.sha256(b"retained").hexdigest()}
    with pytest.raises(PermissionError, match="changed"):
        if operation == "retain":
            await artifacts.retain_durable(profile, invocation, "output", b"retained", object(), authorize=current)
        else:
            await artifacts.read_durable(profile, invocation, "output", receipt, authorize=current)
    assert not list(root.iterdir())
    assert (state.uploads, state.reads) == ((1, 0) if operation == "retain" else (0, 1))


async def test_authorized_local_read_and_bundle_preserve_exact_existing_receipts(tmp_path):
    import io
    import tarfile

    profile, invocation, archive, root = fixture_request(tmp_path)
    artifacts = artifact_service(root)
    receipt = await artifacts.retain_durable(profile, invocation, "output", b"retained", object(), authorize=allowed)
    assert receipt == {"kind": "output", "bytes": 8, "sha256": hashlib.sha256(b"retained").hexdigest()}
    assert await artifacts.read_durable(profile, invocation, "output", receipt, authorize=allowed) == b"retained"
    artifacts.prepare(profile, invocation, archive)
    artifacts.retain(profile, invocation, "stdin", b"")
    bundle = await artifacts.input_archive_durable(profile, invocation, authorize=allowed)
    with tarfile.open(fileobj=io.BytesIO(bundle)) as stream:
        assert stream.extractfile("source/main.py").read() == b'print("safe")\n'
        assert stream.extractfile("stdin.bin").read() == b""


async def test_unknown_native_upload_keeps_readback_intent_and_never_retries_provider(tmp_path):
    from test_runner_object_artifacts_host import authority_fence, fixture

    from firefly_forge.object_storage import ObjectWriteUncertain
    from firefly_forge.runner_pool_artifacts import RunnerPoolArtifacts

    objects, profile, invocation, ledger = fixture(tmp_path)
    artifacts = RunnerPoolArtifacts(objects.scratch, objects)
    native = objects.objects.adapter
    writes = 0

    class LostReply:
        name = "controlled-lost-native-reply"

        async def upload(self, document, content):
            nonlocal writes
            writes += 1
            await native.upload(document, content)
            raise TimeoutError("Controlled acknowledgement lost")

        async def download(self, document, version=None):
            return await native.download(document, version)

    objects.objects.adapter = LostReply()
    fence = authority_fence()
    with pytest.raises(ObjectWriteUncertain):
        await artifacts.retain_durable(profile, invocation, "output", b"retained", fence, authorize=allowed)
    assert writes == 1 and all(row["status"] == "pending" for row in ledger.rows.values())
    assert not list(objects.scratch.iterdir())
    with pytest.raises(RuntimeError, match="readback"):
        await artifacts.retain_durable(profile, invocation, "output", b"retained", fence, authorize=allowed)
    assert writes == 1
    receipt = {"kind": "output", "bytes": 8, "sha256": hashlib.sha256(b"retained").hexdigest()}
    assert await objects.reconcile(profile, invocation, "output", receipt, fence, authorize=allowed) == receipt
    assert all(row["status"] == "published" for row in ledger.rows.values())
    assert await artifacts.read_durable(profile, invocation, "output", receipt, authorize=allowed) == b"retained"
    assert writes == 1
