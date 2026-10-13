# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json

import pytest

from firefly_forge.models import RepositoryInput, RunInput


async def test_generated_and_imported_brand_tokens_are_frozen_for_run(store, tmp_path):
    from firefly_forge.product_branding import BrandInput, ProductBranding

    repository = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    branding = ProductBranding(store)
    first = await branding.configure(repository["id"], BrandInput(name="Inventory"))
    assert "Manrope" in first["files"]["logo.svg"]
    assert "--color-light-primary" in first["files"]["theme.css"]
    assert "logo-dark.svg" in first["files"]
    assert "--color-primary: var(--color-dark-primary)" in first["files"]["theme.css"]
    assert "atoms" in first["files"]["brand-guide.md"]
    assert "<path" in first["files"]["logo.svg"] and "<text" not in first["files"]["logo.svg"]
    assert "@font-face" in first["files"]["theme.css"] and "data:font/woff2;base64," in first["files"]["theme.css"]
    assert "SIL OPEN FONT LICENSE" in first["files"]["FONT-LICENSE.txt"]
    run = await store.create_run(RunInput(repository_id=repository["id"], brief="Build inventory"))
    imported = await branding.configure(repository["id"], BrandInput(name="Inventory", tokens=first["tokens"]))
    assert imported["tokens"] == first["tokens"]
    assert run["branding_snapshot"]["revision"] == first["revision"]
    assert (await store.get_run(run["id"]))["branding_snapshot"]["revision"] != imported["revision"]


async def test_brand_import_rejects_active_svg_invalid_tokens_and_bad_contrast(store, tmp_path):
    from firefly_forge.product_branding import BrandInput, ProductBranding

    repository = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    branding = ProductBranding(store)
    for svg in (
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://external.invalid/pixel"/></svg>',
    ):
        with pytest.raises(ValueError):
            await branding.configure(repository["id"], BrandInput(name="Inventory", logo_svg=svg))
    with pytest.raises(ValueError, match="contrast"):
        await branding.configure(repository["id"], BrandInput(name="Inventory", background="#ffffff", text="#ffffff"))
    valid = await branding.configure(repository["id"], BrandInput(name="Inventory"))
    invalid = json.loads(json.dumps(valid["tokens"]))
    invalid["color"]["light"]["primary"]["$value"] = "url(https://external.invalid)"
    with pytest.raises(ValueError):
        await branding.configure(repository["id"], BrandInput(name="Inventory", tokens=invalid))
