# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Complete large JSON values retain authenticated credential admission."""

import asyncio
import json
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from firefly_forge.oauth_credential_fingerprints import OAuthCredentialMatcher


@pytest.fixture
def authenticated_history():
    matcher = OAuthCredentialMatcher(Fernet.generate_key().decode(), "forge_test_streaming_admission")
    secret = "fictional-oauth-private-" + uuid4().hex
    packet = matcher.packet(uuid4().hex, "registration", uuid4().hex, 1, [secret])
    assert matcher.contains(secret, [packet])
    return matcher, [packet], secret


def test_large_single_text_is_fully_scanned_with_authenticated_history(authenticated_history):
    matcher, packets, _ = authenticated_history
    text = "Fictional public source line.\n" * 40000
    assert len(text.encode()) > 1_048_576
    assert not matcher.contains({"canonical_history": text}, packets)


def test_many_small_values_above_two_megabytes_are_all_admitted(authenticated_history):
    matcher, packets, _ = authenticated_history
    values = ["Fictional public retained source. " * 550 for _ in range(130)]
    assert sum(len(value.encode()) for value in values) > 2_097_152
    assert not matcher.contains({"retained_responses": values}, packets)


def test_large_component_graph_is_not_rejected_by_a_node_count(authenticated_history):
    matcher, packets, _ = authenticated_history
    graph = [{"component": index, "name": f"Fictional service {index}"} for index in range(3500)]
    assert not matcher.contains({"components": graph}, packets)


async def test_actual_native_checkpoint_canonical_history_above_one_megabyte_is_admitted(authenticated_history):
    from fireflyframework_agentic.agents import FireflyAgent
    from pydantic_ai import models
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary, BoundaryCheckpoint

    matcher, packets, _ = authenticated_history
    captures = []
    boundary = AgenticBoundary(lambda: {"files_written": ["app.py"]})

    def controlled(messages, info):
        captures.append(boundary.capture(b"fictional-original-wire"))
        return ModelResponse(parts=[TextPart("Fictional retained answer")])

    with models.override_allow_model_requests(False), boundary.invocation():
        await FireflyAgent(
            name="credential-history-fixture",
            model=FunctionModel(controlled),
            auto_register=False,
            capabilities=[boundary],
        ).run("Fictional retained source line.\n" * 40000)
    checkpoint = BoundaryCheckpoint.decode(captures[0].payload, captures[0].digest)
    canonical = checkpoint.canonical_payload.decode()
    assert len(canonical.encode()) > 1_048_576
    assert not matcher.contains(canonical, packets)
    assert checkpoint.digest == captures[0].digest


@pytest.mark.parametrize("secret", ["fictional-boundary-credential", "秘密🔐café", 'line\\quote"value'])
@pytest.mark.parametrize("escaped", [False, True])
async def test_credential_across_unicode_and_json_chunk_boundaries(secret, escaped):
    from firefly_forge.credential_text_stream import TEXT_CHARS

    matcher = OAuthCredentialMatcher(Fernet.generate_key().decode(), "forge_test_streaming_admission")
    packets = [matcher.packet(uuid4().hex, "registration", uuid4().hex, 1, [secret])]
    represented = json.dumps(secret, ensure_ascii=True)[1:-1] if escaped else secret
    text = "文" * (TEXT_CHARS - 5) + represented + " public suffix"
    assert await matcher.contains_async({"retained": text}, packets)


@pytest.mark.parametrize(
    "secret,value",
    [
        ("123,456", {"observed_values": [123, 456]}),
        ('"observed_flag":false', {"observed_flag": False}),
        ('"observed_values":[[],[]]', {"observed_values": [[], []]}),
    ],
)
async def test_complete_json_framing_remains_scanned(secret, value):
    matcher = OAuthCredentialMatcher(Fernet.generate_key().decode(), "forge_test_streaming_admission")
    packets = [matcher.packet(uuid4().hex, "registration", uuid4().hex, 1, [secret])]
    assert await matcher.contains_async(value, packets)


def test_canonical_iterator_matches_exact_native_json_without_joining_in_production():
    from firefly_forge.credential_text_stream import canonical_chunks

    value = {"文": ["a" * 9000 + '\n"\\', None, True, -7, 1.25, {False: "data"}], 123: ()}
    assert "".join(canonical_chunks(value)) == json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def test_async_complete_scan_is_cancellable_by_the_receiver(authenticated_history):
    matcher, packets, _ = authenticated_history
    progressed = asyncio.Event()

    def observed(chunk):
        progressed.set()
        return False

    task = asyncio.create_task(matcher.contains_async("public line " * 100000, packets, check_chunk=observed))
    await progressed.wait()
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_receiver_deadline_stops_scan_without_a_project_size_ceiling(authenticated_history):
    matcher, packets, _ = authenticated_history
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):
            await matcher.contains_async("public line " * 100000, packets)


def test_deep_finite_graph_and_shared_subtrees_do_not_need_a_recursive_walker():
    matcher = OAuthCredentialMatcher(Fernet.generate_key().decode(), "forge_test_streaming_admission")
    graph = {"leaf": "public"}
    for _ in range(1500):
        graph = {"child": graph}
    assert not matcher.contains([graph, graph], [])
    graph["cycle"] = graph
    with pytest.raises(ValueError, match="cyclic"):
        matcher.contains(graph, [])


def test_raw_sibling_documents_are_not_falsely_joined(authenticated_history):
    matcher, packets, secret = authenticated_history
    assert not matcher.contains([secret[:20], secret[20:]], packets)


def test_streaming_iterator_and_scanner_do_not_copy_large_input(authenticated_history):
    import tracemalloc

    matcher, packets, _ = authenticated_history
    text = "public line " * 100000
    tracemalloc.start()
    try:
        assert not matcher.contains({"retained": text}, packets)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 524288


def isolated_service(monkeypatch, authenticated_history):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from firefly_forge.connection_oauth import ConnectionOAuth

    matcher, packets, _ = authenticated_history
    configuration = SimpleNamespace(write_generation=object(), resolve=lambda _: {})
    store = SimpleNamespace(configuration=configuration, credential_registry=lambda: {})
    vault = object.__new__(ConnectionOAuth)
    vault.store, vault.matcher, vault._secrets = store, matcher, {}
    observed = {"reads": 0, "packets": packets, "exit": lambda: None}

    @asynccontextmanager
    async def snapshot():
        observed["reads"] += 1
        yield observed["packets"]
        await asyncio.sleep(0)
        observed["exit"]()

    monkeypatch.setattr(vault, "_credential_packet_snapshot", snapshot)
    return vault, observed


@pytest.mark.parametrize("change", ["history", "secret", "environment", "configuration", "registry", "vault"])
async def test_original_credential_cut_is_rechecked_after_cooperative_scan(monkeypatch, authenticated_history, change):
    vault, observed = isolated_service(monkeypatch, authenticated_history)
    original = vault.matcher.contains_async

    async def changed(*args, **kwargs):
        check = kwargs["check_chunk"]
        changed_once = False

        def during_scan(chunk):
            nonlocal changed_once
            matched = check(chunk)
            if not changed_once:
                changed_once = True
                if change == "history":
                    observed["packets"] = [
                        vault.matcher.packet(uuid4().hex, "registration", uuid4().hex, 1, ["new-private"])
                    ]
                elif change == "secret":
                    vault._secrets["API_TOKEN"] = "new-private"
                elif change == "environment":
                    monkeypatch.setenv("FORGE_STREAMING_FIXTURE_TOKEN", "new-private")
                elif change == "configuration":
                    vault.store.configuration.write_generation = object()
                elif change == "registry":
                    vault.store.credential_registry = lambda: {"API_TOKEN": "new-private"}
                else:
                    vault.store.vault_credential_admission = vault.admit_credentials
            return matched

        return await original(*args, **{**kwargs, "check_chunk": during_scan})

    monkeypatch.setattr(vault.matcher, "contains_async", changed)
    with pytest.raises(ValueError, match="changed during admission"):
        await vault.admit_credentials({"retained": "Fictional clean text " * 500})
    assert observed["reads"] == 2


async def test_final_context_exit_await_cannot_change_raw_credential_cut(monkeypatch, authenticated_history):
    vault, observed = isolated_service(monkeypatch, authenticated_history)

    def final_exit():
        if observed["reads"] == 2:
            vault._secrets["API_TOKEN"] = "new-private"

    observed["exit"] = final_exit
    with pytest.raises(ValueError, match="changed during admission"):
        await vault.admit_credentials("Fictional clean text")


async def test_complete_service_admission_scans_real_retired_packet(monkeypatch, authenticated_history):
    vault, observed = isolated_service(monkeypatch, authenticated_history)
    await vault.admit_credentials({"retained": "public text " * 90000})
    assert observed["reads"] == 2
    secret = authenticated_history[2]
    with pytest.raises(ValueError, match="configured credentials"):
        await vault.admit_credentials({"retained": "public text " * 90000 + secret})


async def test_mutable_value_changed_after_last_chunk_cannot_be_admitted(authenticated_history):
    matcher, packets, secret = authenticated_history
    value = {"result": "public"}
    ends = 0

    def change_after_canonical(chunk):
        nonlocal ends
        if chunk is None:
            ends += 1
            if ends == 3:
                value["result"] = secret
        return False

    with pytest.raises(ValueError, match="data changed during admission"):
        await matcher.contains_async(
            value, packets, check_chunk=change_after_canonical, check_canonical_chunk=change_after_canonical
        )


async def test_final_history_exit_cannot_mutate_the_already_scanned_result(monkeypatch, authenticated_history):
    vault, observed = isolated_service(monkeypatch, authenticated_history)
    value = {"result": "public"}

    def final_exit():
        if observed["reads"] == 2:
            value["result"] = authenticated_history[2]

    observed["exit"] = final_exit
    with pytest.raises(ValueError, match="data changed during admission"):
        await vault.admit_credentials(value)


@pytest.mark.parametrize(
    "suffix,matched", [("[redacted]", False), ("[redacted]suffix", True), ("[redacte", True), ("private", True)]
)
def test_bearer_detection_keeps_complete_token_semantics_at_every_split(suffix, matched):
    from firefly_forge.secrets import KnownCredentialScan

    value = "authorization: bearer " + suffix
    for split in range(1, len(value)):
        scanner = KnownCredentialScan(())
        assert (scanner.feed(value[:split]) or scanner.feed(value[split:]) or scanner.feed(None)) is matched


async def test_long_matching_prefix_candidates_remain_cooperatively_cancellable(monkeypatch):
    matcher = OAuthCredentialMatcher(Fernet.generate_key().decode(), "forge_test_streaming_admission")
    packets = [matcher.packet(uuid4().hex, "registration", uuid4().hex, 1, ["x" * 20000 + "private-end"])]
    digest = matcher._digest
    calls = 0

    def observed(purpose, value):
        nonlocal calls
        if purpose == b"match":
            calls += 1
            if calls == 1:
                asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        return digest(purpose, value)

    monkeypatch.setattr(matcher, "_digest", observed)
    task = asyncio.create_task(matcher.contains_async("x" * 40000, packets))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert 1 <= calls < 16


async def test_deep_empty_structure_yields_to_receiver_cancellation():
    matcher = OAuthCredentialMatcher(Fernet.generate_key().decode(), "forge_test_streaming_admission")
    graph = []
    for _ in range(10000):
        graph = [graph]
    task = asyncio.create_task(matcher.contains_async(graph, []))
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancelled_service_admission_never_completes_or_publishes_a_new_cut(monkeypatch, authenticated_history):
    vault, observed = isolated_service(monkeypatch, authenticated_history)
    original = vault.matcher.contains_async
    entered = asyncio.Event()

    async def waiting(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(vault.matcher, "contains_async", waiting)
    task = asyncio.create_task(vault.admit_credentials("Fictional public retained text"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed["reads"] == 1
    assert vault._secrets == {}


async def test_public_redacted_bearer_scalar_keeps_existing_admission_semantics(monkeypatch, authenticated_history):
    vault, observed = isolated_service(monkeypatch, authenticated_history)
    await vault.admit_credentials({"public_header": "Authorization: Bearer [redacted]"})
    assert observed["reads"] == 2


async def test_current_plaintext_credential_still_matches_exact_canonical_framing(monkeypatch, authenticated_history):
    vault, _ = isolated_service(monkeypatch, authenticated_history)
    vault._secrets["FIXTURE_API_KEY"] = "123,456"
    with pytest.raises(ValueError, match="configured credentials"):
        await vault.admit_credentials({"values": [123, 456]})
