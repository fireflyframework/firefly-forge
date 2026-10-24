# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""SDK and CLI read exact diff pages without fetching an entire patch."""

import base64
import hashlib
import json

import httpx
import pytest
from click.testing import CliRunner
from test_original_recovery_sdk import connection

from firefly_forge import cli
from firefly_forge.git_diff_models import GitDiffIndexPageV1, GitDiffPageV1

RUN, REQUEST, ARTIFACT = (c * 32 for c in "abc")
MANIFEST = "d" * 64
RAW = b"diff --git a/a b/a\n"
INDEX = {
    "version": 1,
    "artifact_id": ARTIFACT,
    "manifest_sha256": MANIFEST,
    "page": 2,
    "next_page": None,
    "files": [
        {
            "file_index": 7,
            "path": "src/product.py",
            "offset": 64,
            "byte_count": len(RAW),
            "sha256": hashlib.sha256(RAW).hexdigest(),
        }
    ],
}
FILE = {
    "version": 1,
    "artifact_id": ARTIFACT,
    "manifest_sha256": MANIFEST,
    "file_index": 7,
    "offset": 0,
    "byte_count": len(RAW),
    "total_bytes": len(RAW),
    "next_offset": None,
    "content_base64": base64.b64encode(RAW).decode(),
    "content_sha256": hashlib.sha256(RAW).hexdigest(),
    "starts_mid_line": False,
    "ends_mid_line": False,
    "utf8_complete": True,
}


def test_sdk_returns_typed_exact_run_and_checkpoint_pages():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=FILE if "/files/" in request.url.path else INDEX)

    with connection(respond) as client:
        assert isinstance(client.run_diff_index(RUN, MANIFEST, page=2), GitDiffIndexPageV1)
        assert isinstance(client.run_diff_file(RUN, MANIFEST, file_index=7, limit=32), GitDiffPageV1)
        client.run_diff_index(RUN, MANIFEST, page=2, request_id=REQUEST)
        client.run_diff_file(RUN, MANIFEST, file_index=7, limit=32, request_id=REQUEST)
    assert [(r.method, r.url.path) for r in calls] == [
        ("GET", f"/api/v1/runs/{RUN}/diff/index"),
        ("GET", f"/api/v1/runs/{RUN}/diff/files/7"),
        ("GET", f"/api/v1/runs/{RUN}/git-deliveries/requests/{REQUEST}/diff"),
        ("GET", f"/api/v1/runs/{RUN}/git-deliveries/requests/{REQUEST}/diff/files/7"),
    ]
    assert dict(calls[0].url.params) == {"expected_manifest": MANIFEST, "page": "2"}
    assert dict(calls[1].url.params) == {"expected_manifest": MANIFEST, "offset": "0", "limit": "32"}
    assert all(not request.content for request in calls)


@pytest.mark.parametrize(
    "query",
    [
        {"expected_manifest": "../private"},
        {"page": True},
        {"page": -1},
        {"request_id": "../../other"},
        {"file_index": True},
        {"file_index": -1},
        {"file_index": 7, "offset": -1},
        {"file_index": 7, "limit": 1048577},
    ],
)
def test_invalid_queries_are_rejected_before_any_http(query):
    calls = []
    selected = {"expected_manifest": MANIFEST, **query}
    with (
        connection(lambda request: calls.append(request) or httpx.Response(200, json=FILE)) as client,
        pytest.raises(ValueError),
    ):
        if "file_index" in selected:
            client.run_diff_file(RUN, **selected)
        else:
            client.run_diff_index(RUN, **selected)
    assert calls == []


@pytest.mark.parametrize(
    "change", [{"manifest_sha256": "e" * 64}, {"file_index": 8}, {"offset": 1}, {"byte_count": 33}]
)
def test_mismatched_file_page_is_not_accepted_as_the_requested_window(change):
    with connection(lambda request: httpx.Response(200, json={**FILE, **change})) as client, pytest.raises(ValueError):
        client.run_diff_file(RUN, MANIFEST, file_index=7, limit=32)


def test_failed_page_read_is_not_retried_or_followed_by_a_write():
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("Controlled diff page timeout", request=request)

    with connection(fail) as client, pytest.raises(httpx.ReadTimeout):
        client.run_diff_index(RUN, MANIFEST)
    assert len(calls) == 1 and calls[0].method == "GET"


def test_cli_uses_exact_bounded_typed_diff_reads(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=FILE if "/files/" in request.url.path else INDEX)

    monkeypatch.setattr(cli, "client", lambda: connection(respond))
    for args in [
        ["diff-index", RUN, MANIFEST, "--page", "2"],
        ["diff-file", RUN, MANIFEST, "7", "--limit", "32", "--request-id", REQUEST],
    ]:
        result = CliRunner().invoke(cli.main, ["git-deliveries", *args])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["manifest_sha256"] == MANIFEST
    assert len(calls) == 2 and all(request.method == "GET" for request in calls)
    assert calls[1].url.path.endswith(f"/requests/{REQUEST}/diff/files/7")
    assert dict(calls[1].url.params) == {"expected_manifest": MANIFEST, "offset": "0", "limit": "32"}


@pytest.mark.parametrize("change", [{"page": 3}, {"manifest_sha256": "e" * 64}, {"artifact_id": "../../foreign"}])
def test_mismatched_index_is_not_accepted_as_the_requested_page(change):
    with connection(lambda request: httpx.Response(200, json={**INDEX, **change})) as client, pytest.raises(ValueError):
        client.run_diff_index(RUN, MANIFEST, page=2)


@pytest.mark.parametrize("change", [{"content_base64": "invalid!"}, {"content_sha256": "e" * 64}, {"next_offset": 99}])
def test_corrupt_file_content_is_not_exposed_as_verified(change):
    with connection(lambda request: httpx.Response(200, json={**FILE, **change})) as client, pytest.raises(ValueError):
        client.run_diff_file(RUN, MANIFEST, file_index=7, limit=32)


def test_file_page_cannot_return_zero_progress_before_eof():
    stalled = {
        **FILE,
        "byte_count": 0,
        "content_base64": "",
        "content_sha256": hashlib.sha256(b"").hexdigest(),
        "next_offset": 0,
    }
    with connection(lambda request: httpx.Response(200, json=stalled)) as client, pytest.raises(ValueError):
        client.run_diff_file(RUN, MANIFEST, file_index=7, limit=32)
