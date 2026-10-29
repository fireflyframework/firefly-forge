# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Local offline operator entry points; no HTTP admission or execution grant."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError

from firefly_forge.native_location_files import _owned_bytes, read_storage_identity
from firefly_forge.native_location_maintenance import NativeLocationMaintenance
from firefly_forge.native_location_models import (
    NativeLocationAcknowledgmentV1,
    NativeLocationCandidateV1,
    NativeLocationFilesV1,
    NativeLocationPlanV1,
)
from firefly_forge.source_catalog import SourceCatalogBudget, _json
from firefly_forge.store import Store

Data = dict[str, Any]


def _document(path: Path) -> Data:
    size = path.lstat().st_size
    value = json.loads(_owned_bytes(path, size))
    if not isinstance(value, dict):
        raise ValueError("Use an exact private local JSON object")
    return value


def _write_new(path: Path, value: Data) -> None:
    folder = path.parent.lstat()
    if (
        path.parent.resolve() != path.parent
        or not stat.S_ISDIR(folder.st_mode)
        or folder.st_uid != os.getuid()
        or stat.S_IMODE(folder.st_mode) != 0o700
    ):
        raise ValueError("Maintenance output requires an existing private owner-controlled directory")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(_json(value) + b"\n")
        output.flush()
        os.fsync(output.fileno())


def _run(data_dir: Path, operation: Callable[[NativeLocationMaintenance], Awaitable[Data]]) -> Data:
    async def execute() -> Data:
        read_storage_identity(data_dir)
        store = Store(data_dir / "factory.db", existing_only=True)
        try:
            await store.initialize(existing_only=True)
            return await operation(NativeLocationMaintenance(store))
        finally:
            await store.close()

    try:
        return asyncio.run(execute())
    except ValidationError:
        raise click.ClickException(
            "Offline native maintenance input is malformed; no execution is authorized"
        ) from None
    except (ValueError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from None
    except Exception:
        raise click.ClickException(
            "Offline native maintenance did not confirm. Inspect the same request status before any retry"
        ) from None


@click.group("native-locations")
def native_locations() -> None:
    """Maintain a stopped local combined factory offline, using its existing OS-owned identity."""


@native_locations.command("prepare")
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
@click.option("--postgres-data", type=click.Path(path_type=Path), required=True)
@click.option("--plan", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--files", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--scratch", type=click.Path(path_type=Path), required=True)
@click.option("--output", type=click.Path(path_type=Path), required=True)
def prepare(data_dir: Path, postgres_data: Path, plan: Path, files: Path, scratch: Path, output: Path) -> None:
    """Measure selected retained bytes and produce a private candidate; append no native records."""
    if output.exists() or output.is_symlink():
        raise click.ClickException("Maintenance output must be a fresh create-only file")

    async def operation(service: NativeLocationMaintenance) -> Data:
        prepared = await service.prepare(
            NativeLocationPlanV1.model_validate(_document(plan)),
            NativeLocationFilesV1.model_validate(_document(files)),
            postgres_data,
            scratch,
            SourceCatalogBudget(),
        )
        result = prepared.candidate.model_dump(mode="json")
        _write_new(output, result)
        return {"status": "prepared_no_execution", "candidate_digest": prepared.candidate.digest, "output": str(output)}

    click.echo(json.dumps(_run(data_dir, operation), indent=2))


@native_locations.command("commit")
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
@click.option("--candidate", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--files", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--acknowledgment", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--scratch", type=click.Path(path_type=Path), required=True)
def commit(data_dir: Path, candidate: Path, files: Path, acknowledgment: Path, scratch: Path) -> None:
    """Append one exact maintenance pair; a repeated confirmed request returns its saved receipt."""

    async def operation(service: NativeLocationMaintenance) -> Data:
        reviewed = NativeLocationCandidateV1.model_validate(_document(candidate))
        body = NativeLocationAcknowledgmentV1.model_validate(_document(acknowledgment))
        if body.candidate_digest != reviewed.digest:
            raise ValueError("Maintenance acknowledgment differs from its exact reviewed candidate")
        saved = await service.status(body.request_id)
        if saved["status"] == "unknown":
            raise ValueError("An unknown native maintenance outcome requires inspection without replay")
        if saved["status"] == "completed":
            receipt = saved["receipt"]
            if _json(receipt["candidate"]) != _json(reviewed.model_dump(mode="json")) or _json(
                receipt["request"]
            ) != _json(body.model_dump(mode="json")):
                raise ValueError("The original maintenance request belongs to another reviewed input")
            return saved
        prepared = await service.prepare(
            reviewed.plan,
            NativeLocationFilesV1.model_validate(_document(files)),
            Path(reviewed.postgres_data_directory),
            scratch,
            SourceCatalogBudget(),
        )
        if _json(prepared.candidate.model_dump(mode="json")) != _json(reviewed.model_dump(mode="json")):
            raise ValueError("The reviewed native candidate changed before offline admission")
        receipt = await service.commit(prepared, body)
        return {"status": "completed", "request_id": body.request_id, "receipt": receipt}

    click.echo(json.dumps(_run(data_dir, operation), indent=2))


@native_locations.command("status")
@click.argument("request_id")
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
def status(request_id: str, data_dir: Path) -> None:
    """Inspect the original native maintenance request without executing or re-admitting it."""

    async def operation(service: NativeLocationMaintenance) -> Data:
        return await service.status(request_id)

    click.echo(json.dumps(_run(data_dir, operation), indent=2))
