# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from firefly_forge.collaboration import ProductDefinition
from firefly_forge.models import RepositoryInput
from firefly_forge.product_architecture import ProductArchitectureService
from firefly_forge.product_design import DesignReview, ProductDesign, design_files
from firefly_forge.visual_design import VisualDesign, VisualDesignService


def product(*, api_only=False):
    value = ProductDefinition(
        customer="Support service owners",
        problem="Ticket context is lost",
        journey=["Resolve ticket"],
        user_stories=["As an agent I resolve an assigned ticket"],
        acceptance=["Only assigned accounts are visible"],
        owners=["support"],
    )
    if api_only:
        proposal = ProductArchitectureService().propose(value.model_dump(), backend="pyfly", frontend="none")
        value = ProductDefinition.model_validate(
            {**value.model_dump(), "architecture": {**proposal["architecture"], "reviewed": True}}
        )
    return value


def test_product_grid_is_configurable_and_column_math_matches_viewports():
    spec = VisualDesign()
    assert spec.geometry_configurable is True
    service = VisualDesignService()
    for width, viewport in [(1440, "desktop"), (390, "mobile")]:
        grid = service.geometry(spec, viewport)
        assert grid["content_x"] == (248 + 32 if viewport == "desktop" else 20)
        assert grid["column_width"] * grid["columns"] + grid["gutter"] * (grid["columns"] - 1) == grid["content_width"]
        assert grid["content_x"] + grid["content_width"] == width - grid["margin"]
    with pytest.raises(ValidationError):
        VisualDesign.model_validate({"desktop": {"columns": 16, "gutter": 48}, "shell": {"sidebar_width": 360}})


async def test_wireframe_panels_use_grid_and_materialize_layout_contract(store):
    repo = await store.add_repository(RepositoryInput(name="team/support", url="https://github.com/team/support.git"))
    bundle = await ProductDesign(store).preview(repo["id"], product())
    assert bundle["requirements"]["grid"]["scope"] == "product_ui"
    files = design_files(bundle)
    assert "docs/design/layout-contract.json" in files and "docs/design/grid.svg" in files
    assert json.loads(files["docs/design/layout-contract.json"]) == bundle["requirements"]["grid"]
    rectangles = [
        node for node in ET.fromstring(bundle["screens"][0]["desktop_svg"]).iter() if node.tag.endswith("rect")
    ]
    assert any(node.get("x") == "280" and node.get("width") == "744" for node in rectangles)
    assert any(node.get("x") == "1048" and node.get("width") == "360" for node in rectangles)
    assert any(node.get("y") == "836" and node.get("height") == "64" for node in rectangles)


async def test_api_only_preview_is_a_pending_process_document_not_application_form(store):
    repo = await store.add_repository(RepositoryInput(name="team/support", url="https://github.com/team/support.git"))
    bundle = await ProductDesign(store).preview(repo["id"], product(api_only=True))
    assert bundle["method"] == "process_map"
    assert bundle["requirements"]["grid"]["scope"] == "diagram_document"
    assert bundle["functional_definition_status"] == "pending"
    for key in ["desktop_svg", "mobile_svg", "desktop_dark_svg", "mobile_dark_svg"]:
        svg = bundle["screens"][0][key]
        assert "Customer input" not in svg and "Enter required details" not in svg and "Continue" not in svg
        assert "Functional definition pending" in svg
    files = design_files(bundle)
    assert "docs/design/processes.md" in files and "docs/design/functional-contract.json" in files


async def test_process_contract_is_explicit_reviewed_and_frozen_without_inventing_ui(store):
    value = product(api_only=True)
    workflow = {
        "id": "resolve",
        "name": "Resolve assigned ticket",
        "actor": "Support agent",
        "trigger": "Authenticated resolve request",
        "journey_refs": ["Resolve ticket"],
        "acceptance_refs": ["acceptance-1"],
        "steps": [
            {
                "id": "authorize",
                "action": "Check account access",
                "owner": "Ticket service",
                "inputs": ["Actor ID and ticket ID"],
                "outputs": ["Authorized ticket scope"],
                "failures": ["Return forbidden without disclosing ticket data"],
            },
            {
                "id": "resolve",
                "action": "Resolve ticket and record audit event",
                "owner": "Ticket service",
                "inputs": ["Authorized ticket scope"],
                "outputs": ["Resolved ticket version"],
                "failures": ["Return conflict and preserve current version"],
            },
        ],
    }
    value = ProductDefinition.model_validate({**value.model_dump(), "processes": [workflow]})
    repo = await store.add_repository(RepositoryInput(name="team/support", url="https://github.com/team/support.git"))
    service = ProductDesign(store)
    bundle = await service.preview(repo["id"], value)
    assert bundle["functional_definition_status"] == "ready"
    assert bundle["functional_contract"]["processes"][0]["steps"] == value.processes[0].model_dump()["steps"]
    files = design_files(bundle)
    assert "Return forbidden" in files["docs/design/processes.md"]
    review = DesignReview(
        digest=bundle["digest"],
        reviewed=True,
        method="process_map",
        screen_ids=["journey-01"],
        viewports=["desktop", "mobile"],
        themes=["light", "dark"],
    )
    reviewed = value.model_copy(update={"design_review": review})
    assert (await service.validate_review(repo["id"], reviewed))["method"] == "process_map"
    wrong = reviewed.model_copy(update={"design_review": review.model_copy(update={"method": "wireframe"})})
    with pytest.raises(ValueError, match="Review"):
        await service.validate_review(repo["id"], wrong)
    changed = reviewed.model_copy(update={"visual_design": VisualDesign(guidance=["Keep incident ownership visible"])})
    with pytest.raises(ValueError, match="definition"):
        await service.validate_review(repo["id"], changed)
    for key in ["desktop_svg", "mobile_svg", "desktop_dark_svg", "mobile_dark_svg"]:
        root = ET.fromstring(bundle["screens"][0][key])
        assert any("Check account access" in "".join(node.itertext()) for node in root.iter())
        assert "Customer input" not in bundle["screens"][0][key]


async def test_pending_process_review_is_rejected_and_foreign_references_fail_validation(store):
    value = product(api_only=True)
    repo = await store.add_repository(RepositoryInput(name="team/support", url="https://github.com/team/support.git"))
    service = ProductDesign(store)
    bundle = await service.preview(repo["id"], value)
    review = DesignReview(
        digest=bundle["digest"],
        reviewed=True,
        method="process_map",
        screen_ids=["journey-01"],
        viewports=["desktop", "mobile"],
        themes=["light", "dark"],
    )
    with pytest.raises(ValueError, match="pending"):
        await service.validate_review(repo["id"], value.model_copy(update={"design_review": review}))
    process = {
        "id": "resolve",
        "name": "Resolve",
        "actor": "Support",
        "trigger": "Request",
        "journey_refs": ["Another customer journey"],
        "acceptance_refs": ["acceptance-99"],
        "steps": [{"id": "check", "action": "Check", "owner": "Service"}],
    }
    with pytest.raises(ValidationError, match="references"):
        ProductDefinition.model_validate({**value.model_dump(), "processes": [process]})


async def test_process_labels_are_static_escaped_data_and_all_contract_files_are_publishable(store):
    from firefly_forge.channel_artifacts import reference_manifest

    value = product(api_only=True)
    value = ProductDefinition.model_validate(
        {
            **value.model_dump(),
            "processes": [
                {
                    "id": "resolve",
                    "name": "<script>unsafe</script>",
                    "actor": "<img onerror=bad>",
                    "trigger": "Request",
                    "journey_refs": ["Resolve ticket"],
                    "acceptance_refs": ["acceptance-1"],
                    "steps": [
                        {
                            "id": "check",
                            "action": "<script>unsafe</script>",
                            "owner": "Service",
                            "inputs": ["id"],
                            "outputs": ["ticket"],
                            "failures": ["denied"],
                        }
                    ],
                }
            ],
        }
    )
    repo = await store.add_repository(RepositoryInput(name="team/support", url="https://github.com/team/support.git"))
    bundle = await ProductDesign(store).preview(repo["id"], value)
    for key in ["desktop_svg", "mobile_svg", "desktop_dark_svg", "mobile_dark_svg"]:
        svg = bundle["screens"][0][key]
        ET.fromstring(svg)
        assert "<script>" not in svg and "<img" not in svg
    manifest, files = reference_manifest(repo["id"], bundle["digest"], "design", design_files(bundle))
    assert manifest["files"] and any(path.endswith("functional-contract.json") for path in files)


@pytest.mark.parametrize("primary", ["#747474", "#767676", "#808080"])
def test_custom_primary_text_meets_normal_text_contrast(primary):
    from firefly_forge.product_branding import BrandInput, generated_tokens
    from firefly_forge.product_design import _palette

    def luminance(value):
        components = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [part / 12.92 if part <= 0.04045 else ((part + 0.055) / 1.055) ** 2.4 for part in components]
        return sum(part * weight for part, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    palette = _palette(generated_tokens(BrandInput(name="Support", primary=primary)), "light", True)
    dark, light = sorted([luminance(primary), luminance(palette["primary_ink"])])
    assert (light + 0.05) / (dark + 0.05) >= 4.5


def test_legacy_frozen_reference_materialization_does_not_invent_a_new_grid():
    old = {
        "method": "wireframe",
        "requirements": {"acceptance": ["Existing criterion"]},
        "screens": [
            {
                "id": "journey-01",
                "title": "Existing journey",
                "interaction_contract": {},
                "desktop_svg": "<svg/>",
                "desktop_dark_svg": "<svg/>",
                "mobile_svg": "<svg/>",
                "mobile_dark_svg": "<svg/>",
            }
        ],
    }
    files = design_files(old)
    assert len(files) == 5
    assert "docs/design/layout-contract.json" not in files
    assert files["docs/design/reference-journey-01-desktop-light.svg"] == "<svg/>"


async def test_product_grid_drives_both_renderers_without_shared_mutation(store):
    repo = await store.add_repository(RepositoryInput(name="team/support", url="https://github.com/team/support.git"))
    custom = VisualDesign.model_validate(
        {
            "desktop": {"viewport_width": 1280, "columns": 8, "gutter": 16, "margin": 24},
            "mobile": {"viewport_width": 360, "columns": 3, "gutter": 12, "margin": 16},
            "shell": {"header_height": 96, "sidebar_width": 0, "footer_height": 0, "max_content_width": 1100},
        }
    )
    service = ProductDesign(store)
    for api_only in (False, True):
        value = product(api_only=api_only).model_copy(update={"visual_design": custom})
        bundle = await service.preview(repo["id"], value)
        assert bundle["requirements"]["grid"]["desktop"]["columns"] == 8
        for viewport, width in [("desktop", 1280), ("mobile", 360)]:
            svg = ET.fromstring(bundle["screens"][0][viewport + "_svg"])
            assert svg.attrib["viewBox"].split()[2] == str(width)
            rectangles = [node for node in svg.iter() if node.tag.endswith("rect")]
            assert any(node.get("height") == "96" and node.get("width") == str(width) for node in rectangles)
            assert not any(node.get("width") == "248" for node in rectangles)
            assert not any(node.get("height") == "64" and node.get("width") == str(width) for node in rectangles)
        default = await service.preview(repo["id"], product(api_only=api_only))
        assert default["requirements"]["grid"]["desktop"]["columns"] == 12
        assert default["digest"] != bundle["digest"]
    geometry = VisualDesignService().geometry(custom, "desktop")
    assert geometry["content_width"] == 1100 and geometry["content_x"] == 90
    assert VisualDesign().shell.sidebar_width == 248


@pytest.mark.parametrize(
    "settings",
    [
        {"desktop": {"columns": 17}},
        {"mobile": {"margin": 41}},
        {"shell": {"header_height": 20}},
        {"shell": {"footer_height": 15}},
        {"shell": {"sidebar_width": 30}},
        {"baseline": 3},
        {"desktop": {"viewport_width": 1024, "columns": 16, "gutter": 48, "margin": 64}},
    ],
)
def test_invalid_product_geometry_is_rejected(settings):
    with pytest.raises(ValidationError):
        VisualDesign.model_validate(settings)


async def test_journey_feature_behaviors_are_retained_in_all_reference_methods(store):
    repo = await store.add_repository(RepositoryInput(name="team/support", url="https://github.com/team/support.git"))
    for api_only in (False, True):
        value = product(api_only=api_only)
        value = ProductDefinition.model_validate(
            {
                **value.model_dump(),
                "functional_reviewed": True,
                "features": [
                    {
                        "id": "resolve-ticket",
                        "name": "Resolve assigned ticket",
                        "journey_refs": ["Resolve ticket"],
                        "acceptance_refs": ["acceptance-1"],
                        "behaviors": [
                            {
                                "id": "resolve",
                                "given": "An assigned ticket",
                                "when": "The agent resolves the ticket",
                                "then": "The ticket status changes with an audit event",
                                "failure_paths": ["Other accounts remain forbidden"],
                            }
                        ],
                    }
                ],
            }
        )
        bundle = await ProductDesign(store).preview(repo["id"], value)
        assert bundle["feature_contract"]["reviewed"] is True
        contract = bundle["screens"][0]["interaction_contract"]
        assert contract["feature_ids"] == ["resolve-ticket"]
        assert contract["expected_behaviors"][0]["then"] == "The ticket status changes with an audit event"
        frozen = json.loads(design_files(bundle)["docs/design/functional-contract.json"])
        assert frozen["feature_contract"] == bundle["feature_contract"]
        assert frozen["status"] == "pending"
        assert frozen["feature_contract"]["status"] == "ready"
