# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Currently authorized, manifest-pinned reads of immutable Git diff pages."""

import asyncio
import copy
import re
from pathlib import Path
from typing import Any

from pyfly.container import service

from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.git_diff_artifacts import read_diff_file_page, read_diff_index_page
from firefly_forge.git_diff_models import GitDiffIndexPageV1, GitDiffPageV1, GitDiffRefV1
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import identifier

Data = dict[str, Any]


@service
class GitDiffArchive:
    def __init__(self, deliveries: GitDeliveries) -> None:
        self.deliveries = deliveries

    async def _capture(
        self, run_id: str, expected_manifest: str, reader: Principal, request_id: str | None
    ) -> tuple[Path, GitDiffRefV1, Data]:
        if CURRENT_PRINCIPAL.get() != reader:
            raise AccessDenied("The current diff reader changed")
        actor = ProjectAccess.actor_snapshot(reader)
        run = await self.deliveries.store.get_run(run_id)
        await self.deliveries._authorize(run, actor, "read")
        if request_id is None:
            value = run.get("diff_artifact")
        else:
            view = await self.deliveries.view(run_id, actor=actor)
            items = [item for item in view["items"] if item["request_id"] == request_id]
            if len(items) != 1:
                raise ValueError("The requested checkpoint diff is unavailable")
            value = (items[0].get("source_evidence") or {}).get("diff_artifact")
        ref = GitDiffRefV1.model_validate(value)
        if ref.manifest_sha256 != expected_manifest:
            raise ValueError("The requested diff manifest changed")
        scope = copy.deepcopy(
            {key: run.get(key) for key in ("id", "repository_id", "project_id", "component_id", "execution_actor")}
        )
        root = self.deliveries.engine.workspaces.resolve() / ".diff-artifacts" / run_id
        current = await self.deliveries.store.get_run(run_id)
        if any(current.get(key) != value for key, value in scope.items()) or (
            request_id is None and current.get("diff_artifact") != ref.model_dump(mode="json")
        ):
            raise ValueError("The current diff scope or reference changed")
        if CURRENT_PRINCIPAL.get() != reader:
            raise AccessDenied("The current diff reader changed")
        return root, ref, scope

    async def _read(
        self,
        run_id: str,
        expected_manifest: str,
        *,
        page: int | None,
        file_index: int | None,
        offset: int,
        limit: int,
        request_id: str | None,
    ) -> Data:
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("Diff content requires a current native reader")
        run_id = identifier(run_id).hex
        if not isinstance(expected_manifest, str) or re.fullmatch(r"[a-f0-9]{64}", expected_manifest) is None:
            raise ValueError("An exact diff manifest digest is required")
        if request_id is not None:
            request_id = identifier(request_id).hex
        before = await self._capture(run_id, expected_manifest, reader, request_id)
        root, ref, _ = before
        result: GitDiffIndexPageV1 | GitDiffPageV1
        if page is not None:
            result = await asyncio.to_thread(read_diff_index_page, root, ref, page)
        else:
            assert file_index is not None
            result = await asyncio.to_thread(read_diff_file_page, root, ref, file_index, offset, limit)
        after = await self._capture(run_id, expected_manifest, reader, request_id)
        if after != before:
            raise ValueError("The authorized diff scope or reference changed")
        return result.model_dump(mode="json")

    async def index(self, run_id: str, expected_manifest: str, *, page: int = 0, request_id: str | None = None) -> Data:
        if type(page) is not int or page < 0:
            raise ValueError("A nonnegative diff index page is required")
        return await self._read(
            run_id, expected_manifest, page=page, file_index=None, offset=0, limit=32768, request_id=request_id
        )

    async def file(
        self,
        run_id: str,
        expected_manifest: str,
        *,
        file_index: int,
        offset: int = 0,
        limit: int = 32768,
        request_id: str | None = None,
    ) -> Data:
        if (
            type(file_index) is not int
            or file_index < 0
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 1048576
        ):
            raise ValueError("The requested file window is outside its bounded extent")
        return await self._read(
            run_id,
            expected_manifest,
            page=None,
            file_index=file_index,
            offset=offset,
            limit=limit,
            request_id=request_id,
        )
