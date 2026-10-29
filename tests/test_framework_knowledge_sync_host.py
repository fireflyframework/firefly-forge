# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Complete deterministic UTF-8 parts and conservative remote reconciliation."""

import hashlib
from types import SimpleNamespace

import pytest


def test_utf8_parts_round_trip_and_keep_all_content():
    from firefly_forge.framework_knowledge_sync import utf8_parts

    payload = ("service λ🙂\n" * 100_001).encode()
    parts = list(utf8_parts([payload[i : i + 53] for i in range(0, len(payload), 53)], window=4096))
    assert b"".join(parts) == payload
    assert all(0 < len(part) <= 4096 and part.decode().encode() == part for part in parts)
    assert len(parts) > 100


def test_utf8_parts_refuse_binary_incomplete_or_empty_evidence():
    from firefly_forge.framework_knowledge_sync import utf8_parts

    for value in (b"a\0b", b"a\xffb", b"a\xf0\x9f"):
        with pytest.raises(ValueError):
            list(utf8_parts([value], window=32))
    assert list(utf8_parts([], window=32)) == []


async def test_unknown_submission_finds_exact_match_beyond_first_page_without_post():
    from firefly_forge.framework_knowledge_sync import reconcile_part

    part = {"filename": "file-2.txt", "content_sha256": hashlib.sha256(b"known").hexdigest(), "part_ref": "a" * 32}
    match = SimpleNamespace(
        id="source-current",
        filename=part["filename"],
        content_sha256=part["content_sha256"],
        metadata=SimpleNamespace(extra={"forge_framework_part": part["part_ref"]}),
    )

    class Client:
        def __init__(self):
            self.pages = []

        async def list_sources(self, *, limit, offset):
            self.pages.append(offset)
            return SimpleNamespace(items=[SimpleNamespace(filename="other")] * 50 if offset == 0 else [match], total=51)

        async def submit_source(self, payload):
            raise AssertionError("Unknown source must never be resubmitted")

    client = Client()
    assert await reconcile_part(client, part) == "source-current"
    assert client.pages == [0, 50]


async def test_zero_or_duplicate_remote_matches_remain_uncertain():
    from firefly_forge.framework_knowledge_sync import reconcile_part

    part = {"filename": "one.txt", "content_sha256": "b" * 64, "part_ref": "a" * 32}
    match = SimpleNamespace(
        id="source",
        filename="one.txt",
        content_sha256="b" * 64,
        metadata=SimpleNamespace(extra={"forge_framework_part": "a" * 32}),
    )

    class Client:
        async def list_sources(self, **kwargs):
            return SimpleNamespace(items=_values, total=len(_values))

    for _values in ([], [match, match]):
        assert await reconcile_part(Client(), part) is None
