# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Remaining product, channel and administration boundaries use current vault custody."""

from uuid import uuid4

import pytest
from test_connection_oauth_ledger import actor, configuration
from test_oauth_vault_admission import paired_services


@pytest.mark.parametrize("action", ["preview", "apply"])
async def test_feature_changes_reject_new_credentials_before_creating_product_revision(
    store, store_factory, tmp_path, action
):
    from test_product_lifecycle import change, setup

    writer, replica = await paired_services(store, store_factory, tmp_path)
    collaboration, repo, saved, lifecycle = await setup(replica)
    value = "owned-feature-credential-" + uuid4().hex
    await writer.configure("design", configuration(client_secret=value), actor())
    request = change(saved).model_copy(update={"reason": value})
    with pytest.raises(ValueError, match="credentials"):
        await getattr(lifecycle, action)(repo["id"], request)
    assert (await collaboration.product(repo["id"]))["revision"] == saved["revision"]


async def test_signed_repository_ingress_does_not_persist_new_credentials_as_commands(store, store_factory, tmp_path):
    from test_collaboration import configured, delivery

    writer, replica = await paired_services(store, store_factory, tmp_path)
    collaboration, repo = await configured(replica)
    value = "owned-channel-credential-" + uuid4().hex
    await writer.configure("design", configuration(client_secret=value), actor())
    raw, headers = delivery(repo, brief=value)
    receipt = await collaboration.receive("github", repo["id"], raw, headers)
    assert receipt["status"] == "denied"
    assert value not in str(receipt)
    assert receipt.get("command") is None


async def test_global_signed_ingress_rejects_new_credentials_before_registration(
    store, store_factory, tmp_path, monkeypatch
):
    from test_channel_actions import channel, signed

    writer, replica = await paired_services(store, store_factory, tmp_path)
    channels, _, _ = await channel(replica, monkeypatch)
    value = "owned-global-channel-credential-" + uuid4().hex
    await writer.configure("design", configuration(client_secret=value), actor())
    raw, headers = signed('/forge run {"brief":"' + value + '"}')
    receipt = await channels.receive("github", raw, headers)
    assert receipt["status"] == "denied"
    assert await replica.repositories() == []


async def test_identity_provisioning_rejects_current_credentials_before_any_provider_dispatch(
    store, store_factory, tmp_path
):
    from test_user_administration import setup

    from firefly_forge.user_administration import UserProvisionInput

    writer, replica = await paired_services(store, store_factory, tmp_path)
    administration, user, _, calls, _ = await setup(replica)
    value = "owned-identity-credential-" + uuid4().hex
    await writer.configure("design", configuration(client_secret=value), actor())
    request = UserProvisionInput(request_id=uuid4().hex, username="fixture-user", first_name=value)
    try:
        with pytest.raises(ValueError, match="credentials"):
            await administration.provision(request, user)
        assert calls == []
    finally:
        await administration.stop()
        await administration.identity.stop()


async def test_retained_attachment_text_is_rechecked_after_later_vault_publication(store, store_factory, tmp_path):
    from test_assistant_attachments import plain_decoder, setup, upload

    writer, replica = await paired_services(store, store_factory, tmp_path)
    attachments, repository, session = await setup(replica, plain_decoder)
    value = "owned-retained-document-credential-" + uuid4().hex
    receipt = await attachments.admit(repository, session, upload(value.encode()))
    await writer.configure("design", configuration(client_secret=value), actor())
    with pytest.raises(ValueError, match="credentials"):
        await attachments.freeze(repository, session, [receipt["id"]])


async def test_learning_invalidation_rejects_current_credentials_before_persisting_a_reason(
    store, store_factory, tmp_path
):
    from test_learning_ledger import completed

    from firefly_forge.learning import LearningLedger

    writer, replica = await paired_services(store, store_factory, tmp_path)
    engine, run = await completed(replica, tmp_path)
    learning = LearningLedger(replica, engine.journal)
    learning.engine = engine
    record = await learning.capture_outcome(run["id"])
    value = "owned-learning-credential-" + uuid4().hex
    await writer.configure("design", configuration(client_secret=value), actor())
    with pytest.raises(ValueError, match="credentials"):
        await learning.invalidate(record["id"], reason=value, request_id=uuid4().hex)
    assert (await learning.records(run["repository_id"]))[0]["state"] == "verified"


@pytest.mark.parametrize(
    "secret,result",
    [
        ("123,456", {"results": [], "observed_values": [123, 456]}),
        ('"observed_flag":false', {"results": [], "observed_flag": False}),
        ('"observed_values":[[],[]]', {"results": [], "observed_values": [[], []]}),
    ],
)
async def test_structured_tool_json_framing_cannot_reflect_a_current_credential(
    store, store_factory, tmp_path, secret, result
):
    from test_tool_broker import invoke, seed

    writer, replica = await paired_services(store, store_factory, tmp_path)
    module, broker, run, source = await seed(replica, tmp_path)
    await writer.configure("design", configuration(client_secret=secret), actor())
    calls = []

    async def external():
        calls.append(1)
        return result

    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, source, "d" * 32, external)
    assert calls == [1]
    assert (await broker.invocations(run["id"]))[0]["status"] == "unknown"


async def test_learning_outcome_rechecks_retained_question_before_publishing_local_knowledge(
    store, store_factory, tmp_path
):
    from test_learning_ledger import completed

    from firefly_forge.learning import LearningLedger

    writer, replica = await paired_services(store, store_factory, tmp_path)
    engine, run = await completed(replica, tmp_path)
    learning = LearningLedger(replica, engine.journal)
    learning.engine = engine
    await writer.configure("design", configuration(client_secret=run["brief"]), actor())
    with pytest.raises(ValueError, match="credentials"):
        await learning.capture_outcome(run["id"])
    assert await learning.records(run["repository_id"]) == []


async def test_execution_observation_rejects_provider_result_after_dispatch_without_replay(
    store, store_factory, tmp_path
):
    from test_execution_requests import queue_fixture

    from firefly_forge.project_access import CURRENT_PRINCIPAL

    writer, replica = await paired_services(store, store_factory, tmp_path)
    api, worker, run, _, owner, _ = await queue_fixture(replica, tmp_path)
    value = "owned-execution-result-credential-" + uuid4().hex
    await writer.configure("design", configuration(client_secret=value), actor())
    calls = []

    async def publish(*_):
        calls.append(1)
        return "https://github.com/team/product/pull/1?note=" + value

    worker.publish_effect = publish
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        request = await api.request_publish(run["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
    await replica.acquire_ownership()
    try:
        await worker.execute(request["id"])
        await worker.execute(request["id"])
    finally:
        await replica.release_ownership()
    receipt = await worker.get(request["id"])
    assert receipt["status"] == "needs_reconciliation"
    assert calls == [1]
    assert value not in str(await worker.events(request["id"]))
