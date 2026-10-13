# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import pytest
from test_product_design import definition, review

from firefly_forge.collaboration import Collaboration
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.product_branding import BrandInput, ProductBranding
from firefly_forge.product_design import ProductDesign


async def test_known_credentials_are_rejected_before_run_admission(store, tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_TOKEN", "dummy-never-submit-provider-token")
    repo = await store.add_repository(RepositoryInput(name="Stock", url=str(tmp_path)))
    with pytest.raises(ValueError, match="credential"):
        await store.create_run(RunInput(repository_id=repo["id"], brief="Use dummy-never-submit-provider-token"))
    assert await store.runs() == []


async def test_brand_and_reviewed_design_must_freeze_the_same_revision(store, monkeypatch):
    repo = await store.add_repository(RepositoryInput(name="Product", url="https://github.com/team/product.git"))
    brand = ProductBranding(store)
    request = BrandInput(name="Product A", primary="#416eab")
    original = await brand.configure(repo["id"], request)
    product = definition()
    bundle = await ProductDesign(store).preview(repo["id"], product)
    await Collaboration(store).save_product(repo["id"], product.model_copy(update={"design_review": review(bundle)}))
    await brand.configure(repo["id"], BrandInput(name="Product B", primary="#9b5521"))
    validate = ProductDesign.validate_review

    async def restore_after_capture(self, repository_id, product):
        restored = await brand.configure(repository_id, request)
        assert restored["digest"] == original["digest"]
        return await validate(self, repository_id, product)

    monkeypatch.setattr(ProductDesign, "validate_review", restore_after_capture)
    with pytest.raises(ValueError, match="brand revision"):
        await store.create_run(RunInput(repository_id=repo["id"], brief="Build product"))
    assert await store.runs() == []


async def test_registered_role_credential_is_rejected_before_run_snapshot_admission(store, tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_TOKEN", "role-credential-test-value")
    document = store.roles.current()["document"]
    next(role for role in document["roles"] if role["id"] == "implementer")["instructions"] = (
        "role-credential-test-value"
    )
    with pytest.raises(ValueError, match="credentials"):
        store.roles.save(document)
    store.roles.private._write(store.roles.path, {"revision": "unsafe-legacy-fixture", "document": document})
    repository = await store.add_repository(RepositoryInput(name="Product", url=str(tmp_path)))
    with pytest.raises(ValueError, match="credentials"):
        await store.create_run(RunInput(repository_id=repository["id"], brief="Implement customer rules"))
    assert await store.runs() == []
