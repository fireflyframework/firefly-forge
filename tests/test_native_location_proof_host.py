# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Selected inputs, unavailable history and physical source remain explicit."""

import pytest

from firefly_forge.native_location_proof import required_native_files


def test_current_immutable_references_are_required_without_activating_other_runs():
    run = {
        "config_revision": "a" * 32,
        "review_config_revision": "b" * 32,
        "model_selection": {"config_revision": "a" * 32, "profile_id": "chosen"},
    }
    names = required_native_files([run])
    assert "configuration/revisions/" + "a" * 32 + ".json" in names
    assert "configuration/revisions/" + "b" * 32 + ".json" in names
    assert "configuration/model-profile-revisions/" + "a" * 32 + ".json" in names
    with pytest.raises(ValueError):
        required_native_files([{**run, "config_revision": "../foreign"}])


async def test_selected_private_bytes_and_unavailable_manifest_are_fenced(tmp_path):
    import hashlib
    from types import SimpleNamespace

    from firefly_forge.native_location_models import NativeLocationFilesV1, NativeLocationPlanV1
    from firefly_forge.native_location_proof import prepare_location_files
    from firefly_forge.source_catalog import SourceCatalogBudget

    target = tmp_path / "target"
    (target / "configuration").mkdir(parents=True, mode=0o700)
    target.chmod(0o700)
    required = []
    for name, raw in (
        ("runtime-storage.identity", b"a" * 64),
        ("configuration/providers.json", b"{}"),
        ("configuration/source-budget-policy.json", b"{}"),
    ):
        path = target / name
        path.write_bytes(raw)
        path.chmod(0o600)
        required.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = NativeLocationFilesV1.model_validate(
        {
            "version": 1,
            "required_files": required,
            "unavailable_files": [{"path": "historical/config.json", "sha256": None}],
        }
    )
    plan = NativeLocationPlanV1.model_validate(
        {
            "version": 1,
            "source_storage": str(tmp_path / "old"),
            "target_storage": str(target),
            "workspaces": [],
            "verification_runtimes": [],
        }
    )
    proof = await prepare_location_files(
        SimpleNamespace(), plan, manifest, {}, {}, tmp_path / "scratch", SourceCatalogBudget(min_free_bytes=0)
    )
    await proof.assert_current()
    assert not (target / "historical/config.json").exists()
    (target / "configuration/providers.json").write_bytes(b'{"changed":true}')
    with pytest.raises(ValueError, match="changed"):
        await proof.assert_current()
    with pytest.raises(ValueError, match="reviewed retained"):
        await prepare_location_files(
            SimpleNamespace(), plan, manifest, {}, {}, tmp_path / "scratch", SourceCatalogBudget(min_free_bytes=0)
        )


async def test_review_digest_binds_native_file_identity_even_when_bytes_match(tmp_path):
    import hashlib
    from types import SimpleNamespace

    from firefly_forge.native_location_models import NativeLocationFilesV1, NativeLocationPlanV1
    from firefly_forge.native_location_proof import prepare_location_files
    from firefly_forge.source_catalog import SourceCatalogBudget

    root = tmp_path / "new"
    (root / "configuration").mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    declarations = []
    for name, data in (
        ("runtime-storage.identity", b"a" * 64),
        ("configuration/providers.json", b"{}"),
        ("configuration/source-budget-policy.json", b"{}"),
    ):
        path = root / name
        path.write_bytes(data)
        path.chmod(0o600)
        declarations.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
    manifest = NativeLocationFilesV1.model_validate(
        {"version": 1, "required_files": declarations, "unavailable_files": []}
    )
    plan = NativeLocationPlanV1.model_validate(
        {
            "version": 1,
            "source_storage": str(tmp_path / "old"),
            "target_storage": str(root),
            "workspaces": [],
            "verification_runtimes": [],
        }
    )
    args = (SimpleNamespace(), plan, manifest, {}, {}, tmp_path / "scratch", SourceCatalogBudget(min_free_bytes=0))
    first = await prepare_location_files(*args)
    path = root / "configuration/providers.json"
    replacement = root / "configuration/replacement"
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(path)
    second = await prepare_location_files(*args)
    assert first.digest != second.digest
