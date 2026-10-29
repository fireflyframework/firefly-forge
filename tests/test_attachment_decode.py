# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Untrusted conversation files are bounded before they reach an AI provider."""

import importlib
import io
import json
import zipfile

import pytest


async def decode(name, content):
    return await importlib.import_module("firefly_forge.attachment_decode").decode_attachment(name, content)


async def test_utf8_document_is_read_and_truncation_is_explicit():
    result = await decode("journey.md", b"# Reserve stock\nReject overselling")
    assert result["kind"] == "document" and "Reject overselling" in result["text"]
    assert result["image"] is None and not result["truncated"]
    result = await decode("long.txt", b"a" * 40000)
    assert len(result["text"]) == 30000 and result["truncated"]


async def test_docx_extracts_paragraphs_without_accepting_external_entities_or_zip_bombs():
    def docx(body):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", body)
        return stream.getvalue()

    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Reserve inventory</w:t></w:r></w:p></w:body></w:document>"
    )
    assert (await decode("journey.docx", docx(xml)))["text"] == "Reserve inventory"
    with pytest.raises(ValueError):
        await decode("unsafe.docx", docx('<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><x>&x;</x>'))
    with pytest.raises(ValueError):
        await decode("large.docx", docx("a" * 2100000))


async def test_images_are_decoded_and_metadata_is_removed():
    from PIL import Image, PngImagePlugin

    stream = io.BytesIO()
    image = Image.new("RGB", (24, 24), "white")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-note", "strip this metadata")
    image.save(stream, format="PNG", pnginfo=metadata)
    result = await decode("wireframe.png", stream.getvalue())
    assert result["kind"] == "image" and result["mime_type"] == "image/png"
    assert b"strip this metadata" not in result["image"]
    with pytest.raises(ValueError):
        await decode("mismatch.jpg", stream.getvalue())
    with pytest.raises(ValueError):
        await decode("fake.png", b"not an image")


async def test_encrypted_or_empty_pdf_never_claims_readable_customer_content():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(ValueError):
        await decode("scan.pdf", stream.getvalue())
    writer.encrypt("password")
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(ValueError):
        await decode("encrypted.pdf", stream.getvalue())


@pytest.mark.parametrize(
    "name,content",
    [("bad.svg", b"<svg/>"), ("binary.txt", b"\x00"), ("huge.txt", b"x" * 1000001), ("invalid.txt", b"\xff")],
    ids=["unsupported", "null-bytes", "too-large", "invalid-utf8"],
)
async def test_unsupported_or_malformed_files_are_not_admitted(name, content):
    with pytest.raises(ValueError):
        await decode(name, content)


async def test_readable_pdf_supplies_actual_extracted_customer_text():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 250 Td (Reserve inventory) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    result = await decode("customer.pdf", output.getvalue())
    assert result["mime_type"] == "application/pdf" and "Reserve inventory" in result["text"]
    assert not result["truncated"] and result["image"] is None


async def test_isolated_real_image_decoder_disables_bytecode_writes(tmp_path, monkeypatch):
    from pathlib import Path

    from PIL import Image

    module = importlib.import_module("firefly_forge.attachment_decode")
    flag = tmp_path / "child-flags.json"
    cache = tmp_path / "owned-python-cache"
    probe = tmp_path / "probe.py"
    source = str(Path(module.__file__).resolve())
    probe.write_text(
        "# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0\n"
        "import json, pathlib, runpy, sys\n"
        + "pathlib.Path("
        + repr(str(flag))
        + ").write_text(json.dumps({"
        + "'dont_write_bytecode':sys.dont_write_bytecode,'isolated':sys.flags.isolated,"
        + "'pycache_prefix':sys.pycache_prefix}))\n"
        + "runpy.run_path("
        + repr(source)
        + ", run_name='__main__')\n"
    )
    create = module.asyncio.create_subprocess_exec

    async def observe(*argv, **kwargs):
        index = argv.index(source)
        # Redirect any RED-side cache writes into this test's owned directory.
        return await create(
            argv[0],
            "-X",
            "pycache_prefix=" + str(cache),
            *argv[1:index],
            str(probe),
            *argv[index + 1 :],
            **kwargs,
        )

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", observe)
    content = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(content, format="PNG")
    result = await module.decode_attachment("wireframe.png", content.getvalue())
    assert result["kind"] == "image" and result["mime_type"] == "image/png"
    with Image.open(io.BytesIO(result["image"])) as image:
        assert image.size == (16, 16)
    observed = json.loads(flag.read_text())
    assert observed["isolated"] == 1
    assert observed["pycache_prefix"] == str(cache)
    assert observed["dont_write_bytecode"] is True
    assert list(cache.rglob("*.pyc")) == []
