# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Binary reference exclusions never grant authority to corrupt private cache bytes."""

import io
import os
import zipfile

import pytest
from test_framework_knowledge_service_host import host_refresh_case
from test_framework_knowledge_upstream_host import official_fixture


def reference_asset(kind):
    if kind == "text":
        body = b"Official reference content.\n" * 5000
        return "docs/cache-reference.md", {"docs": {"cache-reference.md": body}}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as book:
        book.writestr("mimetype", "application/epub+zip")
        book.writestr(
            "META-INF/container.xml",
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/package.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        book.writestr(
            "OEBPS/package.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="book">urn:fixture:reference</dc:identifier>'
            "<dc:title>Official reference fixture</dc:title><dc:language>en</dc:language>"
            '<meta property="dcterms:modified">2026-10-26T00:00:00Z</meta></metadata>'
            '<manifest><item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '</manifest><spine><itemref idref="body"/></spine></package>',
        )
        book.writestr(
            "OEBPS/nav.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            '<head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
            '<li><a href="body.xhtml">Reference</a></li></ol></nav></body></html>',
        )
        book.writestr(
            "OEBPS/body.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Reference</title></head><body>'
            + "<p>Official reference content.</p>" * 5000
            + "</body></html>",
        )
    return "book/dist/fixture.epub", {"book": {"dist": {"fixture.epub": archive.getvalue()}}}


async def test_verified_epub_is_excluded_without_submitting_binary_parts(tmp_path):
    from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker

    path, extra = reference_asset("epub")
    manager, remote = await host_refresh_case(tmp_path, transport=official_fixture(extra_files=extra)[0])
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    result = await manager.get_refresh("pyfly", "f" * 32)
    assert result["state"] == "ready" and result["counts"]["excluded_files"] == 1
    rows = [r for r in manager.records.rows.values() if r.kind == "source" and r.data["public"]["path"] == path]
    assert len(rows) == 1
    source = rows[0].data["public"]
    assert source["state"] == "excluded" and source["exclusion_reason"] == "non_utf8_reference_asset"
    assert source["sha256"] and source["parts"] == 0
    assert len(remote.posts) == 5


@pytest.mark.parametrize("kind", ["text", "epub"])
@pytest.mark.parametrize(
    "mutation", ["hash", "private_file", "private_directory", "ownership", "hard_link", "mid_read"]
)
async def test_cache_integrity_failure_holds_refresh_without_excluding_source(tmp_path, monkeypatch, kind, mutation):
    from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker

    path, extra = reference_asset(kind)
    manager, remote = await host_refresh_case(tmp_path, transport=official_fixture(extra_files=extra)[0])
    worker = FrameworkKnowledgeWorker(manager)
    fetch, chunks, fstat = worker._fetch, worker._chunks, os.fstat
    selected = {}

    async def tampered_fetch(upstream, release, entry):
        digest, name = await fetch(upstream, release, entry)
        if entry.path == path:
            cached = worker._cache() / name
            selected.update(name=name, path=cached, inode=cached.stat().st_ino)
            if mutation == "hash":
                with cached.open("r+b") as stream:
                    stream.seek(-1, os.SEEK_END)
                    stream.write(b"!")
            elif mutation == "private_file":
                cached.chmod(0o644)
            elif mutation == "private_directory":
                cached.parent.chmod(0o755)
            elif mutation == "hard_link":
                os.link(cached, tmp_path / "owned-cache-alias")
        return digest, name

    def observed_fstat(descriptor):
        result = fstat(descriptor)
        if mutation == "ownership" and result.st_ino == selected.get("inode"):
            fields = list(result)
            fields[4] += 1
            return os.stat_result(fields)
        return result

    def observed_chunks(name):
        for chunk in chunks(name):
            if mutation == "mid_read" and name == selected.get("name") and not selected.get("changed"):
                with selected["path"].open("r+b") as stream:
                    stream.seek(-1, os.SEEK_END)
                    stream.write(b"!")
                selected["changed"] = True
            yield chunk

    monkeypatch.setattr(worker, "_fetch", tampered_fetch)
    monkeypatch.setattr(worker, "_chunks", observed_chunks)
    monkeypatch.setattr(os, "fstat", observed_fstat)
    await worker.advance("f" * 32)
    result = await manager.get_refresh("pyfly", "f" * 32)
    assert result["state"] == "needs_reconciliation"
    rows = [r for r in manager.records.rows.values() if r.kind == "source" and r.data["public"]["path"] == path]
    assert len(rows) == 1 and rows[0].data["public"]["state"] == "pending"
    assert rows[0].data["public"]["exclusion_reason"] is None
    assert (await manager._get("operation", "f" * 32))["error_type"] == "ValueError"
    assert not remote.posts


@pytest.mark.parametrize("payload", [b"binary\0reference", b"invalid\xff", b"truncated\xf0\x9f"])
def test_nontext_bytes_have_a_distinct_content_error(payload):
    from firefly_forge.framework_knowledge_sync import NonTextReferenceError, utf8_parts

    with pytest.raises(NonTextReferenceError):
        list(utf8_parts([payload]))


def test_utf8_parts_preserves_iterator_integrity_and_window_errors():
    from firefly_forge.framework_knowledge_sync import NonTextReferenceError, utf8_parts

    failure = ValueError("Verified input changed")

    def chunks():
        yield b"reference"
        raise failure

    with pytest.raises(ValueError) as raised:
        list(utf8_parts(chunks()))
    assert raised.value is failure and not isinstance(raised.value, NonTextReferenceError)
    with pytest.raises(ValueError) as raised:
        list(utf8_parts([b"reference"], window=3))
    assert not isinstance(raised.value, NonTextReferenceError)
