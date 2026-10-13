# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from firefly_forge.collaboration import Collaboration, ProductDefinition
from firefly_forge.models import RepositoryInput
from firefly_forge.product_branding import BrandInput, ProductBranding
from firefly_forge.product_design import DesignReview, ProductDesign, design_files


def definition():
    return ProductDefinition(
        customer="Support agents",
        problem="Customers repeat their history",
        journey=["Receive a ticket", "Find prior replies", "Resolve the ticket"],
        user_stories=["As an agent I can search my assigned accounts"],
        acceptance=["Search excludes other accounts", "Replies appear in chronological order"],
        security_requirements=["Check account access on the server"],
        owners=["support-owner"],
    )


async def repository(store):
    return await store.add_repository(RepositoryInput(name="team/support", url="https://github.com/team/support.git"))


def review(bundle):
    return DesignReview(
        digest=bundle["digest"],
        reviewed=True,
        screen_ids=[screen["id"] for screen in bundle["screens"]],
        viewports=["desktop", "mobile"],
        themes=["light", "dark"],
    )


async def test_preview_covers_every_journey_with_safe_four_variant_artifacts(store):
    repo = await repository(store)
    service = ProductDesign(store)
    bundle = await service.preview(repo["id"], definition())
    assert bundle["method"] == "wireframe" and bundle["coverage"] == {"journeys": 3, "screen_count": 3}
    assert bundle["font_family"] == "Manrope"
    files = design_files(bundle)
    assert len(files) == 4 * len(bundle["screens"]) + 5
    assert "docs/design/interaction-contract.json" in files
    for screen in bundle["screens"]:
        assert screen["interaction_contract"]["journey"] in definition().journey
        for key, width, height in [
            ("desktop_svg", 1440, 900),
            ("desktop_dark_svg", 1440, 900),
            ("mobile_svg", 390, 844),
            ("mobile_dark_svg", 390, 844),
        ]:
            root = ET.fromstring(screen[key])
            assert root.attrib["viewBox"] == f"0 0 {width} {height}"
            assert not any(
                element.tag.split("}")[-1] in {"script", "foreignObject", "image", "iframe"} for element in root.iter()
            )
    assert (await service.load(repo["id"], bundle["digest"])) == bundle
    assert len(json.dumps(bundle).encode()) <= 2_000_000


async def test_qa_reference_uses_only_frozen_reviewed_source_and_actual_raster_hash(store, tmp_path):
    import hashlib

    from firefly_forge.engine import Engine
    from firefly_forge.qa import BrowserJourney

    repo = await repository(store)
    bundle = await ProductDesign(store).preview(repo["id"], definition())
    run = {
        "id": "a" * 32,
        "design_snapshot": bundle,
        "frontend": "react",
        "product_definition": {"definition": {"design_review": review(bundle).model_dump()}},
    }
    image = b"\x89PNG\r\n\x1a\nfixture-raster"
    calls = []

    class Raster:
        async def rasterize_reference(self, *args, **kwargs):
            calls.append((args, kwargs))
            return image

    engine = Engine(store, tmp_path)
    engine.browser = Raster()
    plan = BrowserJourney(
        journey_id="journey-01",
        name="Receive a ticket",
        reference_id="journey-01-desktop-light",
        expected_states=[{"id": "journey-01-default", "expected": "Ticket form is visible"}],
    )
    reference, evidence = await engine._resolve_qa_reference(run, tmp_path, plan)
    assert reference.sha256 == hashlib.sha256(image).hexdigest()
    assert (
        evidence.metadata["source_sha256"] == hashlib.sha256(bundle["screens"][0]["desktop_svg"].encode()).hexdigest()
    )
    assert evidence.metadata["source_sha256"] != reference.sha256
    assert len(calls) == 1
    assert await engine._resolve_qa_reference(run, tmp_path, plan.model_copy(update={"width": 390})) is None
    assert (
        await engine._resolve_qa_reference(run, tmp_path, plan.model_copy(update={"reference_id": "unreviewed"}))
        is None
    )
    bundle["screens"][0]["desktop_svg"] = "<svg/>"
    with pytest.raises(ValueError, match="digest"):
        await engine._resolve_qa_reference(run, tmp_path, plan)


async def test_untrusted_labels_are_escaped_and_long_text_is_wrapped(store):
    repo = await repository(store)
    product = definition().model_copy(
        update={
            "customer": '<script>alert("bad")</script>',
            "journey": ["<img onerror=bad> " + "VeryLongCustomerIdentifier" * 100],
        }
    )
    bundle = await ProductDesign(store).preview(repo["id"], product)
    for key in ["desktop_svg", "mobile_svg", "desktop_dark_svg", "mobile_dark_svg"]:
        svg = bundle["screens"][0][key]
        ET.fromstring(svg)
        assert "<script>" not in svg and "<img" not in svg
        assert "&lt;script&gt;" in svg or "&lt;script&gt;" in bundle["screens"][0]["desktop_svg"]
        visible = [element.text for element in ET.fromstring(svg).iter() if element.tag.endswith("tspan")]
        assert all(len(line or "") < 110 for line in visible)


async def test_review_requires_all_screens_viewports_and_matching_definition_and_brand(store):
    repo = await repository(store)
    service = ProductDesign(store)
    bundle = await service.preview(repo["id"], definition())
    product = definition().model_copy(update={"design_review": review(bundle)})
    assert (await service.validate_review(repo["id"], product))["digest"] == bundle["digest"]
    incomplete = product.model_copy(
        update={"design_review": review(bundle).model_copy(update={"screen_ids": ["journey-01"]})}
    )
    with pytest.raises(ValueError, match="every"):
        await service.validate_review(repo["id"], incomplete)
    duplicated = product.model_copy(
        update={
            "design_review": review(bundle).model_copy(
                update={"screen_ids": ["journey-01"] * 2 + ["journey-02", "journey-03"]}
            )
        }
    )
    with pytest.raises(ValueError, match="every"):
        await service.validate_review(repo["id"], duplicated)
    changed = product.model_copy(update={"problem": "A changed customer problem"})
    with pytest.raises(ValueError, match="definition"):
        await service.validate_review(repo["id"], changed)
    await ProductBranding(store).configure(repo["id"], BrandInput(name="Support"))
    with pytest.raises(ValueError, match="brand"):
        await service.validate_review(repo["id"], product)
    regenerated = await service.preview(repo["id"], definition())
    assert regenerated["brand_digest"] is not None and regenerated["digest"] != bundle["digest"]


async def test_bundle_is_repository_bound_immutable_and_digest_verified(store):
    first, second = await repository(store), await repository(store)
    service = ProductDesign(store)
    bundle = await service.preview(first["id"], definition())
    assert (await service.preview(first["id"], definition()))["digest"] == bundle["digest"]
    with pytest.raises(KeyError):
        await service.load(second["id"], bundle["digest"])
    with pytest.raises(ValueError):
        await service.load(first["id"], "../../owner.token")
    path = service.directory / first["id"] / (bundle["digest"] + ".json")
    data = json.loads(path.read_text())
    data["screens"][0]["title"] = "Changed after review"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="digest"):
        await service.load(first["id"], bundle["digest"])


async def test_fifty_journeys_stay_bounded_and_saved_product_validates_actual_bundle(store):
    repo = await repository(store)
    product = definition().model_copy(update={"journey": ["Customer step " + str(index) for index in range(50)]})
    service = ProductDesign(store)
    bundle = await service.preview(repo["id"], product)
    assert len(bundle["screens"]) == 50 and len(json.dumps(bundle).encode()) <= 2_000_000
    reviewed = product.model_copy(update={"design_review": review(bundle)})
    saved = await Collaboration(store).save_product(repo["id"], reviewed)
    assert saved["definition"]["design_review"]["digest"] == bundle["digest"]
    fake = reviewed.model_copy(update={"design_review": review(bundle).model_copy(update={"digest": "a" * 64})})
    with pytest.raises((KeyError, ValueError)):
        await Collaboration(store).save_product(repo["id"], fake)
    with pytest.raises(ValidationError):
        DesignReview(
            digest=bundle["digest"], reviewed=False, screen_ids=["journey-01"], viewports=["desktop"], themes=["light"]
        )


async def test_custom_brand_surface_and_action_ink_follow_actual_contrast(store):
    repo = await repository(store)
    await ProductBranding(store).configure(
        repo["id"], BrandInput(name="Support", primary="#001144", background="#101020", text="#ffffff")
    )
    bundle = await ProductDesign(store).preview(repo["id"], definition())
    root = ET.fromstring(bundle["screens"][0]["mobile_svg"])
    rectangles = [item for item in root.iter() if item.tag.endswith("rect")]
    assert rectangles[1].get("fill") == rectangles[0].get("fill")
    button_text = next(
        item for item in root.iter() if item.tag.endswith("text") and "Continue" in "".join(item.itertext())
    )
    assert button_text.get("fill") == "#ffffff"
