# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import copy
import importlib
import uuid

import pytest
from pydantic import ValidationError


def module():
    return importlib.import_module("firefly_forge.product_lifecycle")


def feature(name, journey="Receive ticket", acceptance="acceptance-1"):
    return {
        "id": name,
        "name": name.title(),
        "journey_refs": [journey],
        "acceptance_refs": [acceptance],
        "behaviors": [
            {
                "id": "success",
                "given": "An assigned user",
                "when": "The user opens the ticket",
                "then": "The permitted ticket appears",
                "failure_paths": ["An unauthorized user is denied"],
            }
        ],
    }


async def setup(store):
    from test_collaboration import product

    from firefly_forge.collaboration import Collaboration
    from firefly_forge.models import RepositoryInput

    repo = await store.add_repository(RepositoryInput(name="team/product", url="https://github.com/team/product.git"))
    collaboration = Collaboration(store)
    definition = product().model_dump()
    definition["features"] = [feature("receive"), feature("resolve", "Resolve ticket")]
    definition["functional_reviewed"] = True
    from firefly_forge.collaboration import ProductDefinition

    saved = await collaboration.save_product(repo["id"], ProductDefinition.model_validate(definition))
    return collaboration, repo, saved, module().ProductLifecycle(collaboration)


def change(saved, **kwargs):
    return module().FeatureChangeInput(
        request_id=uuid.uuid4().hex,
        base_revision=saved["revision"],
        base_digest=saved["digest"],
        feature_upserts=[feature("receive") | {"name": "Receive with context"}],
        **kwargs,
    )


async def test_change_preserves_other_features_and_original_revision_without_claiming_code_success(store):
    collaboration, repo, saved, lifecycle = await setup(store)
    request = change(saved)
    preview = await lifecycle.preview(repo["id"], request)
    assert (await collaboration.product(repo["id"]))["revision"] == 1
    assert preview["implementation_status"] == "not_started"
    result = await lifecycle.apply(repo["id"], request)
    assert result["revision"] == 2 and result["change_receipt"]["base_digest"] == saved["digest"]
    assert result["definition"]["features"][1] == saved["definition"]["features"][1]
    assert not result["definition"]["functional_reviewed"] and result["definition"]["design_review"] is None
    assert result["change_receipt"]["impact"]["unchanged_feature_ids"] == ["resolve"]
    assert saved["definition"]["functional_reviewed"] is True
    assert len(await store.runs()) == 0


async def test_concurrent_same_request_reuses_revision_and_stale_or_conflicting_replay_is_denied(store):
    _, repo, saved, lifecycle = await setup(store)
    request = change(saved)
    one, two = await asyncio.gather(lifecycle.apply(repo["id"], request), lifecycle.apply(repo["id"], request))
    assert one == two and one["revision"] == 2
    with pytest.raises(ValueError, match="replay"):
        await lifecycle.apply(repo["id"], request.model_copy(update={"reason": "different change"}))
    with pytest.raises(ValueError, match="base"):
        await lifecycle.apply(repo["id"], change(saved))


async def test_append_journeys_and_acceptance_preserves_existing_reference_numbers(store):
    _, repo, saved, lifecycle = await setup(store)
    request = module().FeatureChangeInput(
        request_id=uuid.uuid4().hex,
        base_revision=1,
        base_digest=saved["digest"],
        append_journeys=["Escalate ticket"],
        append_acceptance=["Owner is notified"],
        feature_upserts=[feature("escalate", "Escalate ticket", "acceptance-2")],
    )
    result = await lifecycle.apply(repo["id"], request)
    assert result["definition"]["acceptance"] == [*saved["definition"]["acceptance"], "Owner is notified"]
    assert result["definition"]["features"][:2] == saved["definition"]["features"]


def integration():
    return {
        "id": "crm",
        "name": "CRM lookup",
        "kind": "third_party",
        "protocol": "http",
        "endpoint": "https://crm.example/v1/tickets",
        "authentication": {"method": "api_key", "secret_refs": ["secret://crm/API_KEY"]},
        "inputs": [{"name": "ticket_id", "type": "string"}],
        "outputs": [{"name": "history", "type": "array"}],
        "failure_paths": ["Return a bounded unavailable state"],
        "retry": {"max_attempts": 2, "backoff_seconds": 1, "retry_on": ["timeout"]},
        "idempotency": {"mode": "not_applicable"},
        "feature_refs": ["receive"],
        "journey_refs": ["Receive ticket"],
    }


async def test_integrations_use_references_only_and_block_removing_referenced_feature(store):
    _, repo, saved, lifecycle = await setup(store)
    request = change(saved, integration_upserts=[integration()])
    result = await lifecycle.apply(repo["id"], request)
    assert result["definition"]["integrations"][0]["authentication"]["secret_refs"] == ["secret://crm/API_KEY"]
    remove = module().FeatureChangeInput(
        request_id=uuid.uuid4().hex, base_revision=2, base_digest=result["digest"], remove_feature_ids=["receive"]
    )
    with pytest.raises(ValueError, match="references"):
        await lifecycle.apply(repo["id"], remove)
    removed = await lifecycle.apply(repo["id"], remove.model_copy(update={"remove_integration_ids": ["crm"]}))
    assert [entry["id"] for entry in removed["definition"]["features"]] == ["resolve"]


@pytest.mark.parametrize(
    "override",
    [
        {"endpoint": "https://secret@crm.example"},
        {"endpoint": "https://crm.example?api_key=secret"},
        {"authentication": {"method": "api_key", "token": "raw-secret"}},
        {"authentication": {"method": "api_key", "secret_refs": ["raw-secret"]}},
    ],
)
def test_integration_rejects_raw_credentials_and_credential_urls(override):
    with pytest.raises(ValidationError):
        module().IntegrationContract.model_validate(integration() | override)


async def test_noop_or_invalid_references_never_append_revision(store):
    collaboration, repo, saved, lifecycle = await setup(store)
    noop = module().FeatureChangeInput(
        request_id=uuid.uuid4().hex,
        base_revision=1,
        base_digest=saved["digest"],
        feature_upserts=[copy.deepcopy(saved["definition"]["features"][0])],
    )
    with pytest.raises(ValueError, match="no change"):
        await lifecycle.apply(repo["id"], noop)
    bad = change(saved, integration_upserts=[integration() | {"feature_refs": ["missing"]}])
    with pytest.raises(ValueError, match="references"):
        await lifecycle.apply(repo["id"], bad)
    assert (await collaboration.product(repo["id"]))["revision"] == 1


async def test_two_different_changes_on_same_base_cannot_both_create_revision(store):
    _, repo, saved, lifecycle = await setup(store)
    one, two = change(saved), change(saved)
    two = two.model_copy(update={"reason": "Another writer"})
    results = await asyncio.gather(
        lifecycle.apply(repo["id"], one), lifecycle.apply(repo["id"], two), return_exceptions=True
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert [result["revision"] for result in results if isinstance(result, dict)] == [2]


async def test_change_resets_architecture_review_and_audit_uses_server_identity(store):
    from firefly_forge.collaboration import ProductDefinition
    from firefly_forge.product_architecture import ProductArchitectureService

    collaboration, repo, saved, lifecycle = await setup(store)
    definition = copy.deepcopy(saved["definition"])
    architecture = ProductArchitectureService().propose(definition)["architecture"]
    architecture["reviewed"] = True
    definition["architecture"] = architecture
    saved = await collaboration.save_product(repo["id"], ProductDefinition.model_validate(definition))
    actor = uuid.uuid4()
    result = await lifecycle.apply(repo["id"], change(saved), actor_id=actor)
    assert result["definition"]["architecture"]["reviewed"] is False
    assert result["change_receipt"]["actor_id"] == actor.hex
    assert result["change_receipt"]["impact"]["reviews_required"] == ["functional", "design", "architecture"]


def test_mutating_retry_requires_idempotency_and_header_values_are_forbidden():
    for changes in ({"side_effects": True}, {"header_names": ["Authorization: private-token"]}):
        with pytest.raises(ValueError):
            module().IntegrationContract.model_validate(integration() | changes)
    value = module().IntegrationContract.model_validate(
        integration()
        | {
            "side_effects": True,
            "idempotency": {"mode": "required", "key_field": "Idempotency-Key", "key_location": "header"},
        }
    )
    assert value.idempotency.mode == "required"


async def test_controller_receipt_cannot_take_actor_id_from_client_and_revision_is_immutable(store):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from firefly_forge.identity import Principal
    from firefly_forge.product_lifecycle_controllers import ProductLifecycleController
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    _, repo, saved, lifecycle = await setup(store)
    controller = ProductLifecycleController(lifecycle)
    actor = Principal(uuid.uuid4(), "https://id.example", "owner-1")
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        result = await controller.apply(repo["id"], change(saved))
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert result["change_receipt"]["actor_id"] == actor.id.hex
    with pytest.raises(DBAPIError):
        async with store._unit() as session:
            await session.execute(text("UPDATE forge_product_definitions SET digest='tampered' WHERE revision=2"))
