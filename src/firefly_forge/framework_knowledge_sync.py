# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded text windows and complete, observation-only flycanon reconciliation."""

from __future__ import annotations

import codecs
from collections.abc import Awaitable, Callable, Iterable, Iterator
from typing import Any


class NonTextReferenceError(ValueError):
    """Reference bytes contain binary or invalid UTF-8 content."""


def utf8_parts(chunks: Iterable[bytes], *, window: int = 65536) -> Iterator[bytes]:
    if type(window) is not int or window < 4:
        raise ValueError("UTF-8 response windows must hold at least one complete code point")
    decoder = codecs.getincrementaldecoder("utf8")("strict")
    pending = bytearray()
    for chunk in chunks:
        if b"\0" in chunk:
            raise NonTextReferenceError("Official reference text cannot contain binary NUL bytes")
        try:
            pending.extend(decoder.decode(chunk).encode())
        except UnicodeDecodeError as exc:
            raise NonTextReferenceError("Official reference text must be complete UTF-8") from exc
        while len(pending) >= window:
            end = window
            while end and pending[end : end + 1] and pending[end] & 0xC0 == 0x80:
                end -= 1
            yield bytes(pending[:end])
            del pending[:end]
    try:
        pending.extend(decoder.decode(b"", final=True).encode())
    except UnicodeDecodeError as exc:
        raise NonTextReferenceError("Official reference text must be complete UTF-8") from exc
    if pending:
        yield bytes(pending)


def canonical_part_ref(remote: Any, part: dict[str, Any], canonical: dict[str, Any] | None) -> str:
    """Validate content deduplication against an already submitted native part."""
    extra = getattr(getattr(remote, "metadata", None), "extra", {})
    reference = extra.get("forge_framework_part") if isinstance(extra, dict) else None
    if (
        not isinstance(reference, str)
        or len(reference) != 32
        or any(c not in "0123456789abcdef" for c in reference)
        or not isinstance(getattr(remote, "id", None), str)
        or not remote.id
        or canonical is None
        or canonical.get("part_ref") != reference
        or canonical.get("canonical_part_ref", reference) != reference
        or canonical.get("corpus_ref") != part.get("corpus_ref")
        or canonical.get("content_sha256") != part.get("content_sha256")
        or getattr(remote, "content_sha256", None) != part.get("content_sha256")
        or getattr(remote, "filename", None) != canonical.get("filename")
        or canonical.get("state") not in {"submitting", "needs_reconciliation", "indexing", "ready"}
        or canonical.get("source_id") not in {None, getattr(remote, "id", None)}
        or (canonical.get("state") in {"indexing", "ready"} and canonical.get("source_id") != remote.id)
        or part.get("source_id") not in {None, getattr(remote, "id", None)}
        or part.get("canonical_part_ref", reference) != reference
        or (reference != part.get("part_ref") and extra.get("corpus_ref") != part.get("corpus_ref"))
    ):
        raise ValueError("Scoped canonical source content does not match its native submitted provenance")
    return reference


async def reconcile_part(
    client: Any,
    part: dict[str, Any],
    *,
    before_page: Callable[[], Awaitable[None]] | None = None,
    lookup_part: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
) -> str | None:
    """Never retry an uncertain POST; attach only one exact remote source."""
    offset = 0
    matches: list[str] = []
    while True:
        if before_page is not None:
            await before_page()
        page = await client.list_sources(limit=50, offset=offset)
        for source in page.items:
            metadata = getattr(source, "metadata", None)
            if getattr(source, "content_sha256", None) != part["content_sha256"]:
                continue
            if lookup_part is None:
                if (
                    getattr(source, "filename", None) == part["filename"]
                    and getattr(metadata, "extra", {}).get("forge_framework_part") == part["part_ref"]
                ):
                    matches.append(source.id)
            else:
                reference = getattr(metadata, "extra", {}).get("forge_framework_part")
                canonical = (
                    part
                    if reference == part["part_ref"]
                    else (await lookup_part(reference) if isinstance(reference, str) else None)
                )
                try:
                    canonical_part_ref(source, part, canonical)
                except ValueError:
                    continue
                matches.append(source.id)
        received = len(page.items)
        offset += received
        if offset >= page.total:
            break
        if not received:
            raise ValueError("Scoped source reconciliation returned an incomplete page")
    return matches[0] if len(matches) == 1 else None
