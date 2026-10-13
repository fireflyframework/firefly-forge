# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read bounded untrusted files in a credential-free, short-lived parser process."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MAX_BYTES = 1_000_000
MAX_TEXT = 30_000
DOCUMENT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
IMAGE_TYPES = {
    ".png": ("PNG", "image/png"),
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".webp": ("WEBP", "image/webp"),
}


def _document(name: str, raw: bytes) -> dict[str, Any]:
    suffix = Path(name).suffix.lower()
    if suffix == ".docx":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > 200 or sum(item.file_size for item in members) > 2_000_000:
                raise ValueError("This Word document exceeds the extraction limit")
            if any("vbaproject" in item.filename.lower() or item.flag_bits & 1 for item in members):
                raise ValueError("Upload a Word document without macros or encryption")
            xml = archive.read("word/document.xml")
            if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                raise ValueError("Upload a Word document without external entities")
            document = ElementTree.fromstring(xml)
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            text = "\n".join("".join(node.itertext()) for node in document.iter(namespace + "p"))
    elif suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 40:
            raise ValueError("Upload an unencrypted PDF with at most 40 pages")
        text = ""
        for page in reader.pages:
            text += (page.extract_text() or "") + "\n"
            if len(text) > MAX_TEXT:
                break
    else:
        text = raw.decode("utf8")
    if not text.strip() or "\x00" in text:
        raise ValueError("This document has no readable text; scanned PDFs require OCR before upload")
    return {
        "kind": "document",
        "mime_type": DOCUMENT_TYPES[suffix],
        "text": text[:MAX_TEXT],
        "image_base64": None,
        "truncated": len(text) > MAX_TEXT,
    }


def _decode(name: str, raw: bytes) -> dict[str, Any]:
    if (
        not 1 <= len(name) <= 180
        or not name[0].isalnum()
        or any(character in name for character in ("/", "\\"))
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or not raw
        or len(raw) > MAX_BYTES
    ):
        raise ValueError("Upload a named, nonempty file of at most 1 MB")
    suffix = Path(name).suffix.lower()
    if suffix in DOCUMENT_TYPES:
        return _document(name, raw)
    if suffix not in IMAGE_TYPES:
        raise ValueError("Choose a supported document or PNG, JPEG or WebP image")
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 4_000_000
    expected, mime_type = IMAGE_TYPES[suffix]
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != expected or getattr(image, "n_frames", 1) != 1 or image.width * image.height > 4_000_000:
            raise ValueError("Upload a matching, single-frame image of at most four million pixels")
        image.verify()
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        from PIL import ImageOps

        clean = ImageOps.exif_transpose(image).convert("RGB" if expected == "JPEG" else "RGBA")
        clean.info.clear()
        output = io.BytesIO()
        clean.save(output, format=expected)
    if len(output.getvalue()) > MAX_BYTES:
        raise ValueError("The decoded image exceeds 1 MB; export a smaller image before uploading")
    return {
        "kind": "image",
        "mime_type": mime_type,
        "text": "",
        "image_base64": base64.b64encode(output.getvalue()).decode(),
        "truncated": False,
    }


async def decode_attachment(filename: str, content: bytes) -> dict[str, Any]:
    if not content or len(content) > MAX_BYTES:
        raise ValueError("Upload a nonempty file of at most 1 MB")
    with tempfile.TemporaryDirectory(prefix="forge-file-") as task_directory:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            filename,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=task_directory,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(content), timeout=10)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode != 0 or len(output) > 1_600_000:
            raise ValueError("This file could not be read within its processing limits")
        result = json.loads(output)
        if not result.get("ok"):
            raise ValueError(result.get("detail", "This file could not be read"))
        data = result["data"]
        image = data.pop("image_base64")
        data["image"] = base64.b64decode(image, validate=True) if image else None
        return dict(data)


def _main() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        if sys.platform != "darwin":
            resource.setrlimit(resource.RLIMIT_AS, (512_000_000, 512_000_000))
        data = _decode(sys.argv[1], sys.stdin.buffer.read(MAX_BYTES + 1))
        print(json.dumps({"ok": True, "data": data}))
    except ValueError as error:
        print(json.dumps({"ok": False, "detail": str(error)}))
    except Exception:
        print(
            json.dumps(
                {
                    "ok": False,
                    "detail": "This file could not be read; export a supported, unencrypted document or image",
                }
            )
        )


if __name__ == "__main__":
    _main()
