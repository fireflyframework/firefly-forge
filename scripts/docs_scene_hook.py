# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
# https://www.apache.org/licenses/LICENSE-2.0
"""Build local illustration assets and version local CSS/JS by their content."""

import hashlib
import html
import json
import math
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
    "studio/tests/agent-lifecycle-fixture.ts",
    "studio/tests/learning-record-fixture.ts",
    "studio/tests/campaign-report-fixture.ts",
    "studio/tests/campaign-report-history-fixture.ts",
    "studio/tests/campaign-report-archive-fixture.json",
    "studio/tests/campaign-delivery-plan-fixture.ts",
    "studio/tests/campaign-delivery-browser-fixture.ts",
    "studio/tests/worker-dialogue-contract-fixture.json",
    "studio/tests/worker-decision-evidence-fixture.ts",
    "studio/tests/worker-evidence-raster-fixture.json",
    "studio/tests/agent-checkpoint-recovery-fixture.ts",
    "studio/tests/run-creation-cancellation-fixture.ts",
    "studio/tests/provider-activity-guide-fixture.json",
    "studio/tests/provider-mcp-activity-guide-fixture.json",
    "studio/tests/scm-delegation-fixture.ts",
    "studio/tests/scm-delegation-contract-fixture.json",
    "studio/tests/scm-poll-resume-contract-fixture.json",
    "studio/tests/provider-recovery-allowance-fixture.ts",
    "studio/tests/provider-recovery-allowance-browser-fixture.ts",
    "studio/tests/coding-check-grant-fixture.json",
    "studio/tests/coding-check-recovery-browser-fixture.ts",
    "studio/tests/git-delivery-contract-fixture.json",
    "studio/tests/framework-knowledge-fixture.ts",
}
CAPTURE_PRODUCERS = {
    "studio/tests/docs-walkthrough.spec.ts",
    "studio/tests/external-tools.spec.ts",
    "studio/tests/docs-provider-allowance.spec.ts",
    "studio/tests/docs-coding-check-recovery.spec.ts",
    "studio/tests/development-workflow.spec.ts",
    "studio/tests/framework-knowledge.spec.ts",
    "studio/tests/invalid-read-recovery.spec.ts",
    "studio/tests/runner-setup.spec.ts",
}


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
        allowance_stages = {
            "provider-allowance-failure",
            "provider-allowance-mitigation",
            "provider-allowance-review",
            "provider-allowance-reserved",
        }
        if stage.startswith("provider-allowance-") and stage not in allowance_stages:
            raise ValueError("Unknown final allowance capture stage: " + name)
        coding_stages = {
            "coding-check-scope",
            "coding-check-grant",
            "coding-check-mitigation",
            "coding-check-review",
            "coding-check-reserved",
        }
        if stage.startswith("coding-check-") and stage not in coding_stages:
            raise ValueError("Unknown configured-check capture stage: " + name)
        workflow_stages = {
            "development-workflow-settings",
            "development-workflow-actions",
            "development-workflow-history",
        }
        if stage.startswith("development-workflow-") and stage not in workflow_stages:
            raise ValueError("Unknown development workflow capture stage: " + name)
        framework_stages = {
            "framework-generation-review": "desktop",
            "framework-generation-candidates": "mobile",
            "framework-generation-confirmation": "mobile",
        }
        if stage.startswith("framework-generation-") and framework_stages.get(stage) != device:
            raise ValueError("Unknown framework generation capture stage: " + name)
        recovery_stages = {"continuity-recovery-intro", "continuity-recovery-review", "continuity-recovery-queued"}
        if stage.startswith("continuity-recovery-") and stage not in recovery_stages:
            raise ValueError("Unknown continuity recovery capture stage: " + name)
        checkpoint_stages = {
            "checkpoint-continuation-policy",
            "checkpoint-continuation-review",
            "checkpoint-continuation-status",
        }
        if stage.startswith("checkpoint-continuation-") and stage not in checkpoint_stages:
            raise ValueError("Unknown checkpoint continuation capture stage: " + name)
        runner_stages = {"runner-setup-execution", "runner-setup-processes", "runner-setup-review"}
        if stage.startswith("runner-setup-") and stage not in runner_stages:
            raise ValueError("Unknown runner setup capture stage: " + name)
        expected_producer = (
            "studio/tests/runner-setup.spec.ts"
            if stage in runner_stages
            else "studio/tests/invalid-read-recovery.spec.ts"
            if stage in recovery_stages | checkpoint_stages
            else "studio/tests/framework-knowledge.spec.ts"
            if stage in framework_stages
            else "studio/tests/development-workflow.spec.ts"
            if stage in workflow_stages
            else "studio/tests/external-tools.spec.ts"
            if stage.startswith("external-")
            else "studio/tests/docs-coding-check-recovery.spec.ts"
            if stage in coding_stages
            else "studio/tests/docs-provider-allowance.spec.ts"
            if stage in allowance_stages
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
        section = record.get("section_capture")
        if stage in framework_stages or section is not None:
            selectors = {
                "framework-generation-review": ".framework-generation-review",
                "framework-generation-candidates": ".framework-generation-review",
                "framework-generation-confirmation": ".framework-activation-confirm",
            }
            if stage not in selectors or not isinstance(section, dict) or section.get("selector") != selectors[stage]:
                raise ValueError("Invalid walkthrough section capture: " + name)
            bounds, viewport = section.get("bounds"), record.get("viewport")
            if not isinstance(bounds, dict) or not isinstance(viewport, dict):
                raise ValueError("Missing walkthrough section bounds: " + name)
            numbers = [bounds.get(key) for key in ("x", "y", "width", "height")]
            numbers += [viewport.get(key) for key in ("width", "height")]
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in numbers
            ):
                raise ValueError("Invalid walkthrough section bounds: " + name)
            # Element screenshots include the enclosing whole-pixel rectangle.
            raster_width = math.ceil(bounds["x"] + bounds["width"]) - math.floor(bounds["x"])
            raster_height = math.ceil(bounds["y"] + bounds["height"]) - math.floor(bounds["y"])
            if (
                bounds["x"] < 0
                or bounds["y"] < 0
                or bounds["width"] <= 0
                or bounds["height"] <= 0
                or bounds["x"] + bounds["width"] > viewport["width"]
                or bounds["y"] + bounds["height"] > viewport["height"]
                or width != raster_width
                or height != raster_height
                or section.get("image_size") != {"width": width, "height": height}
            ):
                raise ValueError("Walkthrough section raster differs from its bounds: " + name)
        elif {"width": width, "height": height} != record["viewport"]:
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
            size = record.get("section_capture", {}).get("image_size", record["viewport"])
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
