# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pure host controls for journey references; no database or browser is activated."""

import json
import threading
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from firefly_forge import browser_runner
from firefly_forge.collaboration import ProductDefinition
from firefly_forge.product_branding import ProductBranding
from firefly_forge.product_design import ProductDesign, design_files
from firefly_forge.product_fonts import manrope_style

SVG = "{http://www.w3.org/2000/svg}"


def definition():
    return ProductDefinition.model_validate(
        {
            "customer": "Fictional sales team",
            "problem": "Keep durable records and resolve concurrent edits explicitly",
            "journey": ["Create a company", "Move an opportunity through the sales pipeline with current permissions"],
            "user_stories": ["Unrelated account administration story"],
            "acceptance": ["Company names are unique", "Stale stage writes preserve the unsent draft"],
            "owners": ["Owner"],
            "features": [
                {
                    "id": "pipeline",
                    "name": "Opportunity stages",
                    "journey_refs": ["Move an opportunity through the sales pipeline with current permissions"],
                    "acceptance_refs": ["acceptance-2"],
                    "behaviors": [
                        {
                            "id": "move",
                            "given": "An authorized salesperson and a saved opportunity",
                            "when": "The salesperson selects a sales stage",
                            "then": "The current version moves to the selected stage",
                            "failure_paths": ["A stale version preserves the draft without retry"],
                        }
                    ],
                }
            ],
        }
    )


@pytest.fixture
async def service(tmp_path, monkeypatch):
    async def repository(_):
        return {"id": "a" * 32}

    async def brand(*_):
        return None

    monkeypatch.setattr(ProductBranding, "current", brand)
    return ProductDesign(
        SimpleNamespace(
            configuration=SimpleNamespace(directory=tmp_path, lock=threading.RLock()), repository=repository
        )
    )


def visible(svg):
    return " ".join(node.text or "" for node in ET.fromstring(svg).iter(SVG + "tspan"))


async def test_journey_reference_uses_only_linked_behaviors_and_acceptance(service):
    bundle = await service.preview("a" * 32, definition())
    screen = bundle["screens"][1]
    assert screen["interaction_contract"]["acceptance_ids"] == ["acceptance-2"]
    assert screen["interaction_contract"]["acceptance_criteria"] == {"acceptance-2": definition().acceptance[1]}
    for key in ["desktop_svg", "desktop_dark_svg", "mobile_svg", "mobile_dark_svg"]:
        text = visible(screen[key])
        assert definition().journey[1] in text
        assert "Opportunity stages" in text and "The salesperson selects a sales stage" in text
        assert "Unrelated account administration story" not in text
        assert "Company names are unique" not in text
        assert "Customer input" not in text and "Enter required details" not in text and "Continue" not in text
        assert "…" not in text
    assert bundle["screens"][0]["interaction_contract"]["acceptance_ids"] == []
    assert "Feature mapping pending" in visible(bundle["screens"][0]["mobile_svg"])
    assert (await service.load("a" * 32, bundle["digest"])) == bundle


async def test_full_overflow_contract_is_explicit_and_saved_without_silent_ellipsis(service):
    value = definition().model_dump()
    value["features"][0]["behaviors"][0]["then"] = "Required immutable contract detail " * 55
    product = ProductDefinition.model_validate(value)
    bundle = await service.preview("a" * 32, product)
    for key in ["desktop_svg", "desktop_dark_svg", "mobile_svg", "mobile_dark_svg"]:
        text = visible(bundle["screens"][1][key])
        assert "interaction-contract.json" in text and "…" not in text
    saved = json.loads(design_files(bundle)["docs/design/interaction-contract.json"])
    contract = saved["screens"][1]["interaction_contract"]
    assert contract["expected_behaviors"][0]["then"] == product.features[0].behaviors[0].then
    assert contract["acceptance_criteria"] == {"acceptance-2": product.acceptance[1]}


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_raster_document_preserves_prefixed_logo_as_svg_xml(width, height):
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 390 844">'
        '<ns0:svg xmlns:ns0="http://www.w3.org/2000/svg" x="24" y="18" width="184" height="40">'
        '<ns0:path d="M0 0 H100 V40 H0 Z" fill="#0f766e"/></ns0:svg></svg>'
    )
    document = browser_runner._reference_document(source, manrope_style(), width, height)
    root = ET.fromstring(document)
    assert root.tag == SVG + "svg"
    assert root.get("width") == str(width) and root.get("height") == str(height)
    assert root.find(SVG + "svg/" + SVG + "path").get("fill") == "#0f766e"
    assert len(list(root.iter(SVG + "style"))) == 1
    assert "<ns0:svg" in source


@pytest.mark.parametrize("width,margin", [(320, 40), (390, 20), (600, 12)])
async def test_wrapped_guidance_stays_within_configured_mobile_canvas(service, width, margin):
    from firefly_forge.product_fonts import manrope_text_width
    from firefly_forge.visual_design import VisualDesign

    product = definition().model_copy(
        update={"visual_design": VisualDesign.model_validate({"mobile": {"viewport_width": width, "margin": margin}})}
    )
    bundle = await service.preview("a" * 32, product)
    for field in ["mobile_svg", "mobile_dark_svg"]:
        root = ET.fromstring(bundle["screens"][1][field])
        for text in root.iter(SVG + "text"):
            size, weight = int(text.get("font-size")), int(text.get("font-weight"))
            for span in text:
                x, y = int(span.get("x")), int(span.get("y"))
                assert x >= 0 and y - size >= 0 and y + 4 <= 844
                assert x + manrope_text_width(span.text or "", size, weight) <= width
        assert "…" not in visible(bundle["screens"][1][field])


async def test_linked_acceptance_union_is_deduplicated_and_unrelated_criteria_stay_out(service):
    value = definition().model_dump()
    additional = json.loads(json.dumps(value["features"][0]))
    additional.update(id="persisted-pipeline", name="Persisted pipeline")
    value["features"].append(additional)
    bundle = await service.preview("a" * 32, ProductDefinition.model_validate(value))
    contract = bundle["screens"][1]["interaction_contract"]
    assert contract["feature_ids"] == ["pipeline", "persisted-pipeline"]
    assert contract["acceptance_ids"] == ["acceptance-2"]
    assert len(contract["expected_behaviors"]) == 2
    assert {item["feature_id"] for item in contract["expected_behaviors"]} == {"pipeline", "persisted-pipeline"}


@pytest.mark.parametrize(
    "source",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com"/></svg>',
        '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',
    ],
)
def test_xml_raster_document_still_refuses_active_or_external_content(source):
    with pytest.raises(ValueError):
        browser_runner._reference_document(source, manrope_style(), 390, 844)


async def test_raster_transport_uses_xml_and_keeps_original_source_identity(tmp_path):
    import base64
    import hashlib
    import struct

    from firefly_forge.sandbox_models import BrowserInput, SandboxInput, capture_sandbox

    source = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 390 844">'
        '<ns0:svg xmlns:ns0="http://www.w3.org/2000/svg"><ns0:path d="M0 0 H10 V10 Z"/></ns0:svg></svg>'
    )
    image = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 390, 844) + b"\0" * 9
    frozen = capture_sandbox(
        SandboxInput(
            mode="docker",
            context="host-transport-fixture",
            image="test/browser@sha256:" + "a" * 64,
            browser=BrowserInput(
                image="test/browser@sha256:" + "a" * 64,
                start_argv=["node", "server.js"],
                origin="http://127.0.0.1:18080",
            ),
        )
    )
    calls = []

    async def ownership():
        pass

    async def get_run(_):
        return {"sandbox_snapshot": frozen}

    async def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            exit_code=0, output=json.dumps({"width": 390, "height": 844, "image": base64.b64encode(image).decode()})
        )

    runner = browser_runner.BrowserRunner(
        SimpleNamespace(store=SimpleNamespace(assert_ownership=ownership, get_run=get_run), execute=execute), tmp_path
    )
    assert await runner.rasterize_reference("b" * 32, tmp_path, source, 390, 844) == image
    assert await runner.rasterize_reference("b" * 32, tmp_path, source, 390, 844) == image
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs["_browser"] is True and args[4] == 60
    payload = json.loads(kwargs["_browser_input"])
    assert ET.fromstring(payload["document"]).find(SVG + "svg/" + SVG + "path") is not None
    assert "data:image/svg+xml;base64," in args[3][2] and "setContent" not in args[3][2]
    assert "javaScriptEnabled:false" in args[3][2] and "context.route" in args[3][2]
    metadata = json.loads(next(runner.directory.glob("*.json")).read_text())
    assert metadata["identity"]["source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert metadata["identity"]["renderer"] == 2


async def test_process_only_reference_uses_linked_workflow_acceptance_without_features(service):
    value = definition().model_dump()
    value.update(
        architecture={
            "backend": "pyfly",
            "frontend": "none",
            "reviewed": True,
            "components": [{"name": "Sales API", "layer": "web", "responsibility": "Authorized sales operations"}],
        },
        acceptance=[
            "Assigned actor can resolve",
            "Resolution audit is retained",
            "Unrelated export criterion",
            "Unlinked global criterion",
        ],
        features=[],
    )
    workflow = {
        "id": "resolve",
        "name": "Resolve sales task",
        "actor": "Assigned operator",
        "trigger": "Resolve request",
        "journey_refs": [value["journey"][1]],
        "acceptance_refs": ["acceptance-1"],
        "steps": [
            {
                "id": "authorize",
                "action": "Resolve assigned task",
                "owner": "Sales API",
                "inputs": ["Actor and task"],
                "outputs": ["Updated task"],
                "failures": ["Deny other accounts"],
            }
        ],
    }
    audit = json.loads(json.dumps(workflow))
    audit.update(id="audit", acceptance_refs=["acceptance-1", "acceptance-2"])
    unrelated = json.loads(json.dumps(workflow))
    unrelated.update(id="export", journey_refs=[value["journey"][0]], acceptance_refs=["acceptance-3"])
    value["processes"] = [workflow, audit, unrelated]
    product = ProductDefinition.model_validate(value)
    bundle = await service.preview("a" * 32, product)
    assert product.features == [] and bundle["functional_definition_status"] == "ready"
    assert bundle["method"] == "process_map"
    frozen = json.loads(design_files(bundle)["docs/design/interaction-contract.json"])
    contract = frozen["screens"][1]["interaction_contract"]
    assert contract["acceptance_ids"] == ["acceptance-1", "acceptance-2"]
    assert contract["acceptance_criteria"] == {
        "acceptance-1": value["acceptance"][0],
        "acceptance-2": value["acceptance"][1],
    }
    assert contract["feature_ids"] == [] and contract["expected_behaviors"] == []
    assert frozen["screens"][0]["interaction_contract"]["acceptance_ids"] == ["acceptance-3"]
    assert bundle["functional_contract"]["processes"] == [item.model_dump(mode="json") for item in product.processes]
    for key in ["desktop_svg", "desktop_dark_svg"]:
        assert "Assigned actor can resolve" in visible(bundle["screens"][1][key])

    wireframe = ProductDefinition.model_validate(
        {
            **product.model_dump(),
            "architecture": None,
            "features": [item.model_dump() for item in definition().features],
        }
    )
    wire_bundle = await service.preview("a" * 32, wireframe)
    assert wire_bundle["method"] == "wireframe"
    wire_contract = wire_bundle["screens"][1]["interaction_contract"]
    assert wire_contract["acceptance_ids"] == ["acceptance-2"]
    assert wire_contract["feature_ids"] == ["pipeline"]
