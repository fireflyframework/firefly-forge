# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Scoped source readback retains native versions without inventing upload causality."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from koinon_sdk import KoinonClient
from koinon_sdk.models import CatalogEntry

from firefly_forge.knowledge_platform import KoinonKnowledge, KoinonScope
from firefly_forge.project_access import AccessDenied

Data = dict[str, Any]
Guard = Callable[[], Awaitable[None]]


async def catalog_children(
    client: KoinonClient, settings: Data, scope: KoinonScope, parent_id: str, kind: str, guard: Guard
) -> AsyncIterator[CatalogEntry]:
    page_number, total, observed = 1, None, set()
    while True:
        await guard()
        page = await client.catalog(settings["tenant_id"], parent_id=parent_id, kind=kind, page=page_number)
        await guard()
        size = page.page_size or page.size
        if (
            page.page != page_number
            or type(page.total) is not int
            or page.total < 0
            or type(size) is not int
            or size <= 0
            or total is not None
            and total != page.total
        ):
            raise ValueError("Native catalog pagination is incomplete or changed during readback")
        total = page.total
        pages = max(1, math.ceil(total / size))
        if page.pages is not None and page.pages != pages:
            raise ValueError("Native catalog page count changed")
        for item in page.items:
            KoinonKnowledge.scoped(item, settings, scope)
            if item.kind != kind or item.parent_id != parent_id or item.id in observed:
                raise AccessDenied("Native catalog page escaped its requested parent or repeated an identity")
            observed.add(item.id)
            yield item
        if page_number >= pages:
            if len(observed) != total:
                raise ValueError("Native catalog did not return its complete recorded membership")
            break
        if not page.items:
            raise ValueError("Native catalog stopped before its recorded final page")
        page_number += 1


async def observe_source(
    platform: KoinonKnowledge,
    client: KoinonClient,
    settings: Data,
    scope: KoinonScope,
    source_id: str,
    filename: str,
    content_sha256: str,
    guard: Guard,
    asset_ids: list[str] | None = None,
) -> Data:
    await guard()
    source = await client.get(settings["tenant_id"], source_id)
    await platform.ancestry(client, settings, scope, source)
    if (
        source.kind != "source"
        or source.name != filename
        or source.metadata.get("sha256") != content_sha256
        or source.parent_id != scope.collection_id
    ):
        raise AccessDenied("Native source differs from its immutable admitted filename, bytes or destination")
    documents = []
    if asset_ids:
        for identifier in asset_ids:
            await guard()
            document = await client.get(settings["tenant_id"], identifier)
            platform.scoped(document, settings, scope)
            if document.kind != "document" or document.parent_id != source_id:
                raise AccessDenied("Native source document changed its admitted parent")
            documents.append(document)
    else:
        async for document in catalog_children(client, settings, scope, source_id, "document", guard):
            documents.append(document)
    versions = []
    for document in documents:
        await guard()
        page = await client.document_chunks(settings["tenant_id"], document.id, offset=0, limit=1)
        if page.document_id != document.id or page.source_id != source_id:
            raise AccessDenied("Native source version belongs to another document or source")
        versions.append(
            {
                "asset_id": document.id,
                "native_source_id": page.native_source_id,
                "source_version_id": page.source_version_id,
                "extraction_id": page.extraction_id,
            }
        )
    await guard()
    result = {
        "source_id": source_id,
        "source_status": source.status,
        "asset_ids": [x.id for x in documents],
        "content_sha256": content_sha256,
        "source_versions": versions,
        "version_state": "recorded"
        if versions and all(x["native_source_id"] and x["source_version_id"] and x["extraction_id"] for x in versions)
        else "not_recorded",
        "creation_causality": "not_proved",
        "availability": "unverified",
        "origin": "external_proposal",
    }
    if platform.manager.safe(json.dumps(result), settings) != json.dumps(result):
        raise ValueError("Native source version identity contains a registered credential")
    return result
