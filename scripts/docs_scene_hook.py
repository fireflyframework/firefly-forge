# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
# https://www.apache.org/licenses/LICENSE-2.0
"""Build local illustration assets and version local CSS/JS by their content."""

import hashlib
import html
import json
import re
import struct
import subprocess
from pathlib import Path

from mkdocs.structure.files import File
from mkdocs.utils import get_relative_url

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "build/docs-scene"
_hashes = {}
_walkthrough = {}
CAPTURE_HELPERS = {
    "studio/tests/walkthrough-capture.ts",
    "studio/tests/navigation.ts",
    "studio/tests/auth-fixture.ts",
    "studio/tests/learning-record-fixture.ts",
    "studio/tests/campaign-report-fixture.ts",
    "studio/tests/campaign-report-history-fixture.ts",
    "studio/tests/campaign-report-archive-fixture.json",
}
CAPTURE_PRODUCERS = {"studio/tests/docs-walkthrough.spec.ts", "studio/tests/external-tools.spec.ts"}


def _load_walkthrough(config, root=ROOT):
    path = Path(config["docs_dir"]) / "assets/screenshots/walkthrough-manifest.json"
    if not path.exists():
        return {}
    manifest = json.loads(path.read_text())
    if manifest.get("schema_version") != 2 or not manifest.get("fixture_only"):
        raise ValueError("Unsupported walkthrough capture manifest")
    inventory = {
        str(path.relative_to(root))
        for folder in ("studio/src", "studio/public")
        for path in (root / folder).rglob("*")
        if path.is_file()
    }
    inventory.update(("studio/index.html", "studio/package.json"))
    if inventory != set(manifest["frontend"]):
        raise ValueError("Walkthrough frontend inventory changed; recapture all inputs")
    for source, expected in manifest["frontend"].items():
        if hashlib.sha256((root / source).read_bytes()).hexdigest() != expected:
            raise ValueError("Walkthrough frontend changed; recapture " + source)
    for group, label, required in (
        ("capture_helpers", "capture helper", CAPTURE_HELPERS),
        ("capture_producers", "capture producer", CAPTURE_PRODUCERS),
    ):
        if not isinstance(manifest.get(group), dict) or set(manifest[group]) != required:
            raise ValueError("Missing walkthrough " + label + " registry")
        for source, expected in manifest[group].items():
            if not source.startswith("studio/tests/") or ".." in Path(source).parts:
                raise ValueError("Invalid walkthrough " + label + " path")
            if hashlib.sha256((root / source).read_bytes()).hexdigest() != expected:
                raise ValueError("Walkthrough " + label + " changed; recapture its source")
    capture_config = manifest.get("capture_config", {})
    if capture_config.get("path") != "studio/playwright.docs.config.ts":
        raise ValueError("Invalid walkthrough capture config")
    if hashlib.sha256((root / capture_config["path"]).read_bytes()).hexdigest() != capture_config.get("sha256"):
        raise ValueError("Walkthrough capture config changed; recapture its source")
    helper_digest = hashlib.sha256(
        json.dumps(manifest["capture_helpers"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    source_digest = hashlib.sha256(
        json.dumps(manifest["frontend"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for name, record in manifest["captures"].items():
        producer = record.get("producer")
        if producer not in manifest["capture_producers"]:
            raise ValueError("Walkthrough screenshot has unknown producer: " + name)
        if record.get("producer_sha256") != manifest["capture_producers"][producer]:
            raise ValueError("Walkthrough screenshot has stale producer: " + name)
        if record.get("capture_helpers_digest") != helper_digest:
            raise ValueError("Walkthrough screenshot has stale capture helpers: " + name)
        if record.get("capture_config_sha256") != capture_config["sha256"]:
            raise ValueError("Walkthrough screenshot has stale capture config: " + name)
        if record.get("frontend_digest") != source_digest:
            raise ValueError("Walkthrough screenshot has stale frontend: " + name)
        if record.get("studio_version") != manifest.get("studio_version"):
            raise ValueError("Walkthrough screenshot has stale Studio version: " + name)
        if record.get("fixture") is not True:
            raise ValueError("Walkthrough screenshot is missing its fixture declaration: " + name)
        stage, device, theme = record.get("stage"), record.get("device"), record.get("theme")
        if (
            not isinstance(stage, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", stage)
            or device not in ("desktop", "mobile")
            or theme not in ("light", "dark")
            or name != f"{stage}-{device}-{theme}.png"
        ):
            raise ValueError("Walkthrough screenshot stage differs from its path: " + name)
        expected_producer = (
            "studio/tests/external-tools.spec.ts"
            if stage.startswith("external-")
            else "studio/tests/docs-walkthrough.spec.ts"
        )
        if producer != expected_producer:
            raise ValueError("Walkthrough screenshot stage differs from its producer: " + name)
        if record.get("path") != "assets/screenshots/" + name or ".." in Path(name).parts:
            raise ValueError("Invalid walkthrough screenshot path: " + name)
        png = (Path(config["docs_dir"]) / record["path"]).read_bytes()
        if hashlib.sha256(png).hexdigest() != record["sha256"]:
            raise ValueError("Walkthrough screenshot changed: " + name)
        width, height = struct.unpack(">II", png[16:24])
        if {"width": width, "height": height} != record["viewport"]:
            raise ValueError("Walkthrough screenshot viewport differs: " + name)
    if manifest.get("studio_version") != json.loads((root / "studio/package.json").read_text())["version"]:
        raise ValueError("Walkthrough has stale Studio version")
    return manifest


def on_page_markdown(markdown, page, config, files=None):
    del config, files
    captures = _walkthrough.get("captures", {})

    def image(match):
        alt, path = match.groups()
        name = Path(path).name
        if name not in captures:
            return match.group(0)
        suffix = re.fullmatch(r"([a-z0-9][a-z0-9-]*)-(desktop|mobile)-(?:light|dark)\.png", name)
        if suffix is None:
            return match.group(0)
        base = f"{suffix[1]}-{suffix[2]}"
        light, dark = f"{base}-light.png", f"{base}-dark.png"
        if light not in captures or dark not in captures:
            raise ValueError("Walkthrough screenshot requires captured appearance counterparts: " + name)
        links = []
        for filename, theme in [(light, "light"), (dark, "dark")]:
            record = captures[filename]
            url = html.escape(get_relative_url(record["path"], page.url), quote=True)
            label = html.escape(alt, quote=True)
            size = record["viewport"]
            links.append(
                f'<a class="forge-shot__link theme-{theme}" href="{url}" '
                f'aria-label="Open full screenshot: {label}"><img src="{url}" alt="{label}" '
                f'width="{size["width"]}" height="{size["height"]}" loading="lazy"></a>'
            )
        return "".join(links)

    # Keep theme pairs inline so screenshot expansion also preserves table cells.
    return re.sub(r"!\[([^\]]*)\]\((assets/screenshots/[^)]+)\)", image, markdown)


def on_config(config):
    global _walkthrough
    _walkthrough = _load_walkthrough(config)

    subprocess.run(["node", str(ROOT / "scripts/build-docs-scene.mjs")], check=True, cwd=ROOT)
    config["extra_css"] = list(config["extra_css"]) + ["assets/docs-scene/scene.css"]
    config["extra_javascript"] = list(config["extra_javascript"]) + ["assets/docs-scene/scene.js"]
    _hashes.clear()
    for url in config["extra_css"] + config["extra_javascript"]:
        if not isinstance(url, str) or "://" in url:
            continue
        path = OUTPUT / Path(url).name if url.startswith("assets/docs-scene/") else Path(config["docs_dir"]) / url
        if path.is_file():
            _hashes[url] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return config


def on_files(files, config):
    for name in json.loads((OUTPUT / "manifest.json").read_text())["files"]:
        path = OUTPUT / name
        file = File("assets/docs-scene/" + path.name, str(OUTPUT), config["site_dir"], config["use_directory_urls"])
        file.abs_src_path = str(path)
        files.append(file)
    return files


def on_post_page(output, page, config):
    del page, config
    for url, digest in _hashes.items():
        output = re.sub(r'((?:href|src)="[^"?]*' + re.escape(url) + r')"', r"\1?v=" + digest + '"', output)
    return output
