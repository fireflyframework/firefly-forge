# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real immutable image storage with controlled native journal/authority boundaries."""

import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from firefly_forge.agent_roles import WorkerFeedback


def screenshot(path, color="white"):
    output = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(output, format="PNG")
    path.write_bytes(output.getvalue())
    return output.getvalue()


def feedback(paths):
    return WorkerFeedback(
        id="design-question",
        role_id="implementer",
        task_id="implement",
        attempt=1,
        disposition="clarification",
        summary="A product choice is required",
        evidence_paths=paths,
        request={
            "id": "ownership",
            "kind": "design",
            "question": "Which ownership model should be shown?",
            "context": "Both options use existing permissions.",
            "choices": ["One owner", "Shared owners"],
        },
    )


async def test_admitted_screenshot_survives_workspace_changes_as_hash_bound_private_bytes(tmp_path):
    from firefly_forge.worker_evidence import WorkerEvidence

    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = screenshot(workspace / "choice.png")
    store = SimpleNamespace(path=data / "forge.json", reject_credentials=lambda _: None, credential_registry=lambda: {})
    service = WorkerEvidence(store, SimpleNamespace())
    record = await service.freeze({"id": "a" * 32}, feedback(["choice.png"]), workspace, workspace)
    assert len(record["images"]) == 1
    image = record["images"][0]
    assert len(image["id"]) == 64 and image["mime_type"] == "image/png"
    assert set(image) == {"id", "filename", "mime_type", "bytes", "sha256"}
    assert b"<svg" not in service.read_blob("a" * 32, image)
    screenshot(workspace / "choice.png", "black")
    retained = service.read_blob("a" * 32, image)
    assert hashlib.sha256(retained).hexdigest() == image["sha256"]
    with Image.open(io.BytesIO(retained)) as decoded:
        assert decoded.getpixel((0, 0))[:3] == (255, 255, 255)
    assert retained != (workspace / "choice.png").read_bytes()
    assert original.startswith(b"\x89PNG")


@pytest.mark.parametrize("case", ["symlink", "parent_symlink", "invalid_png", "oversized"])
async def test_preview_refuses_untrusted_or_unbounded_workspace_image(tmp_path, case):
    from firefly_forge.worker_evidence import WorkerEvidence

    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    screenshot(outside / "choice.png")
    if case == "symlink":
        (workspace / "choice.png").symlink_to(outside / "choice.png")
    elif case == "parent_symlink":
        (workspace / "images").symlink_to(outside, target_is_directory=True)
    elif case == "invalid_png":
        (workspace / "choice.png").write_bytes(b"<svg>not a raster</svg>")
    else:
        (workspace / "choice.png").write_bytes(b"x" * 1_000_001)
    store = SimpleNamespace(path=data / "forge.json", reject_credentials=lambda _: None, credential_registry=lambda: {})
    with pytest.raises(ValueError):
        await WorkerEvidence(store, SimpleNamespace()).freeze(
            {"id": "a" * 32},
            feedback(["images/choice.png" if case == "parent_symlink" else "choice.png"]),
            workspace,
            workspace,
        )

@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_worker_manifest_requires_exact_integer_version(version):
    from pydantic import ValidationError
    from firefly_forge.worker_evidence import WorkerEvidenceManifest
    with pytest.raises(ValidationError):
        WorkerEvidenceManifest.model_validate({"version": version, "run_id": "a" * 32, "feedback_id": "feedback", "request_id": "decision", "images": [{"id": "b" * 64, "filename": "choice.png", "mime_type": "image/png", "bytes": 8, "sha256": "c" * 64}]})

async def test_retained_preview_read_refuses_relaxed_private_directory_permissions(tmp_path):
    from firefly_forge.worker_evidence import WorkerEvidence
    data = tmp_path / "data"; data.mkdir()
    workspace = tmp_path / "workspace"; workspace.mkdir()
    screenshot(workspace / "choice.png")
    store = SimpleNamespace(path=data / "forge.json", reject_credentials=lambda _: None, credential_registry=lambda: {})
    service = WorkerEvidence(store, SimpleNamespace())
    manifest = await service.freeze({"id": "a" * 32}, feedback(["choice.png"]), workspace, workspace)
    (service.directory / ("a" * 32)).chmod(0o755)
    with pytest.raises(ValueError, match="owner-private"):
        service.read_blob("a" * 32, manifest["images"][0])
