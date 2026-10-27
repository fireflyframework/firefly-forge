# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Generation activation must be derived from canonical native validation evidence."""

import pytest


async def test_ready_index_alone_never_activates_a_new_project_default(tmp_path):
    from test_framework_knowledge_service_host import host_refresh_case

    manager, _ = await host_refresh_case(tmp_path)
    with pytest.raises(ValueError, match="activat|validated"):
        await manager.prepare_target_request({"backend": "pyfly", "operation": "build"})


def test_generation_proof_rejects_missing_native_run_receipts():
    from firefly_forge.framework_knowledge_generation import validate_generation_evidence

    with pytest.raises(ValueError, match="validation|canonical"):
        validate_generation_evidence({"id": "a" * 32, "status": "succeeded"}, [], [], {})


def generation_case(run_id="a" * 32):
    import copy
    import hashlib
    import json
    from types import SimpleNamespace
    from uuid import NAMESPACE_URL, UUID, uuid5

    from test_framework_knowledge_requirements_host import source

    from firefly_forge.framework_knowledge_conformance import conformance_commands, runtime_probe_command
    from firefly_forge.framework_knowledge_requirements import manifest_requirements
    from firefly_forge.journal import ZERO_HASH, digest
    from firefly_forge.sandbox import SandboxExecutor

    manifest = b'[project]\nname="pyfly"\nversion="26.10.11"\nrequires-python=">=3.12"\n'
    requirements = manifest_requirements("pyfly", "26.10.11", "a" * 40, source("pyfly", manifest), manifest)
    pin = {
        "corpus_ref": "b" * 32,
        "corpus_manifest_sha256": "c" * 64,
        "release_commit": "a" * 40,
        "runtime_requirements": requirements,
        "framework": "pyfly",
        "framework_version": "26.10.11",
    }
    pin["digest"] = digest(pin)
    corpus = {
        "public": {"corpus_ref": "b" * 32, "manifest_sha256": "c" * 64, "commit": "a" * 40},
        "runtime_requirements": requirements,
    }
    entries, rows = [], []

    def append(task, key, kind, payload):
        value = {
            "run_id": run_id,
            "sequence": len(entries) + 1,
            "task_id": task,
            "idempotency_key": key,
            "kind": kind,
            "payload": copy.deepcopy(payload),
            "created_at": "2026-10-26T01:00:00Z",
            "previous_hash": entries[-1]["digest"] if entries else ZERO_HASH,
        }
        value["digest"] = digest(value)
        entries.append(value)
        return value

    def command(task, ordinal, argv, output):
        identity = uuid5(NAMESPACE_URL, f"forge:sandbox:{run_id}:{task}:1:{ordinal}")
        inner = {"argv": argv, "cwd": ".", "workspace": "/private/fictional", "timeout": 60}
        request = {
            "parent_attempt": 1,
            "ordinal": ordinal,
            "container_name": "forge-sandbox-" + identity.hex,
            "snapshot_digest": "f" * 64,
            "request": inner,
            "request_digest": digest(inner),
        }
        effects = {}
        for effect, evidence in [
            ("create", {"created": True}),
            ("start", {"exit_code": 0, "output": output}),
            ("cleanup", {"removed": True}),
        ]:
            append(
                task,
                identity.hex + ":" + effect,
                "sandbox_intent",
                {"execution_id": identity.hex, "effect": effect, "request": request},
            )
            public = copy.deepcopy(evidence)
            if "output" in public:
                text = public.pop("output")
                public.update(
                    output_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    output_bytes=len(text.encode()),
                    output_excerpt=text[:2000],
                )
            append(
                task,
                identity.hex + ":" + effect,
                "sandbox_receipt",
                {"execution_id": identity.hex, "effect": effect, "evidence": public},
            )
            effects[effect] = {"status": "done", "evidence": evidence}
        rows.append(
            SimpleNamespace(
                id=identity,
                run_id=UUID(hex=run_id),
                task_id=task,
                parent_attempt=1,
                ordinal=ordinal,
                container_name=request["container_name"],
                snapshot_digest="f" * 64,
                status="removed",
                data={"request": request, "effects": effects},
            )
        )
        captured = {}
        SandboxExecutor._capture_diagnostic(captured.update, identity.hex, request)
        return captured

    implementation_key = uuid5(NAMESPACE_URL, f"forge:{run_id}:implement:1").hex
    append("implement", implementation_key, "intent", {})
    observed = {"runtime": "python", "version": "3.13.13"}
    argv = runtime_probe_command(requirements)
    receipt = command("implement", 1, argv, json.dumps(observed))
    attestation = {
        "version": 1,
        "run_id": run_id,
        "task_key": implementation_key,
        "pin_digest": pin["digest"],
        "requirements_digest": requirements["digest"],
        "observed": observed,
        "command": receipt,
        "argv_digest": hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest(),
    }
    event = append("implement", digest(attestation), "framework_runtime_attestation", attestation)
    attestation = {**attestation, "event_digest": event["digest"]}
    append("implement", implementation_key, "receipt", {"source_tree": "d" * 40})
    verify_key = uuid5(NAMESPACE_URL, f"forge:{run_id}:verify:1").hex
    append("verify", verify_key, "intent", {})
    checks = []
    for index, argv in enumerate(conformance_commands(pin), 1):
        command("verify", index, argv, "PASS")
        checks.append({"argv": argv, "exit_code": 0})
    append("verify", verify_key, "receipt", {"checks": checks, "failed_checks": [], "source_tree": "d" * 40})
    run = {
        "id": run_id,
        "status": "succeeded",
        "source_tree": "d" * 40,
        "framework_knowledge_pin": pin,
        "framework_runtime_attestation": attestation,
    }
    return run, entries, rows, corpus


def test_generation_activation_requires_the_complete_runtime_to_product_qa_chain():
    from firefly_forge.framework_knowledge_generation import validate_generation_evidence

    run, entries, rows, corpus = generation_case()
    proof = validate_generation_evidence(run, entries, rows, corpus)
    assert proof["corpus_ref"] == corpus["public"]["corpus_ref"]
    assert proof["verification_receipt_digest"] == entries[-1]["digest"]


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "version",
        "unknown",
        "runtime",
        "missing_conformance",
        "failed_check",
        "late_implementation",
        "corrupt_chain",
    ],
)
def test_incomplete_or_different_native_validation_cannot_activate(mutation):
    from firefly_forge.framework_knowledge_generation import validate_generation_evidence
    from firefly_forge.journal import ZERO_HASH, digest

    run, entries, rows, corpus = generation_case()
    if mutation == "source":
        run["source_tree"] = "e" * 40
    if mutation == "version":
        corpus["runtime_requirements"]["framework_version"] = "26.10.12"
    if mutation == "unknown":
        rows[-1].data["effects"]["start"]["status"] = "intent"
    if mutation == "runtime":
        run["framework_runtime_attestation"]["observed"]["version"] = "3.11.9"
    if mutation == "missing_conformance":
        entries[-1]["payload"]["checks"][0]["argv"] = ["echo", "pretend pass"]
    if mutation == "failed_check":
        entries[-1]["payload"]["checks"][0]["exit_code"] = 1
    if mutation == "late_implementation":
        entries.append({**entries[-1], "kind": "intent", "task_id": "implement"})
    if mutation == "corrupt_chain":
        entries[0]["digest"] = "e" * 64
    if mutation not in {"corrupt_chain"}:
        previous = ZERO_HASH
        for sequence, entry in enumerate(entries, 1):
            entry.update(sequence=sequence, previous_hash=previous)
            entry["digest"] = digest({k: v for k, v in entry.items() if k != "digest"})
            previous = entry["digest"]
    with pytest.raises(ValueError):
        validate_generation_evidence(run, entries, rows, corpus)


def generation_listing_case(count=1):
    import copy
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import UUID

    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.product_provenance import content_digest

    packets = {letter * 32: generation_case(letter * 32) for letter in "abc"[:count]}
    settings = {"enabled": True, "endpoint": "http://fixture.invalid"}
    corpus = copy.deepcopy(next(iter(packets.values()))[-1])
    corpus["public"].update(framework="pyfly", state="ready")
    corpus["connection_digest"] = content_digest(settings)
    queries, reads = [], []

    class Records:
        async def by_key(self, kind, key, **kwargs):
            return SimpleNamespace(data=corpus) if kind == "corpus" and key == "b" * 32 else None

        async def generation_runs(self, framework, corpus_ref, cursor, size):
            queries.append((framework, corpus_ref, cursor, size))
            return [
                SimpleNamespace(id=UUID(hex=key), data=copy.deepcopy(packet[0]))
                for key, packet in sorted(packets.items())
                if cursor is None or key > cursor
            ][:size]

    class Session:
        async def scalars(self, query):
            run_id = query.compile().params["run_id_1"].hex
            return packets[run_id][2]

    class Tasks:
        async def find_for_run(self, run_id):
            return [SimpleNamespace(task_id="verify", status="done", attempt=1)]

    class Entries:
        async def find_for_run(self, run_id):
            return [SimpleNamespace(data=value) for value in packets[run_id.hex][1]]

    @asynccontextmanager
    async def unit():
        yield Session()

    store = SimpleNamespace(_unit=unit, configuration=SimpleNamespace(settings_generation=object()))
    knowledge = SimpleNamespace(settings=lambda: settings, journal=SimpleNamespace(tasks=Tasks(), records=Entries()))
    manager = FrameworkKnowledge(store, knowledge, records=Records())

    async def authorize(*args, **kwargs):
        assert kwargs.get("admin") is True
        reads.append(kwargs)
        return {"user_id": "f" * 32, "global_admin": True}

    async def assert_pin(run):
        if run["framework_knowledge_pin"]["corpus_ref"] != corpus["public"]["corpus_ref"]:
            raise ValueError("Canonical retained pin differs")
        return corpus

    manager._authorize, manager.assert_run_pin = authorize, assert_pin
    for run, *_ in packets.values():
        run.update(
            repository_name="Controlled service",
            project_id="e" * 32,
            component_id="api",
            created_at="2026-10-26T01:00:00Z",
        )
    return SimpleNamespace(
        manager=manager, packets=packets, corpus=corpus, queries=queries, reads=reads, settings=settings
    )


async def test_generation_candidates_return_only_canonical_proofs_and_human_labels():
    from firefly_forge.framework_knowledge_generation import validate_generation_evidence
    from firefly_forge.product_provenance import content_digest

    case = generation_listing_case()
    run, entries, rows, corpus = case.packets["a" * 32]
    proof = validate_generation_evidence(run, entries, rows, corpus)
    page = await case.manager.generation_candidates("pyfly", "b" * 32)
    assert page == {
        "version": 1,
        "framework": "pyfly",
        "corpus_ref": "b" * 32,
        "items": [
            {
                "validation_run_id": "a" * 32,
                "repository_name": "Controlled service",
                "project_id": "e" * 32,
                "component_id": "api",
                "created_at": "2026-10-26T01:00:00Z",
                "source_tree": "d" * 40,
                "runtime": {"runtime": "python", "version": "3.13.13"},
                "validation_digest": content_digest(proof),
            }
        ],
        "next_cursor": None,
    }
    assert len(case.reads) == 2


@pytest.mark.parametrize(
    "mutation", ["chain", "unknown", "failed_qa", "pin", "runtime", "source", "shape", "command_shape", "execution_id"]
)
async def test_generation_candidates_exclude_incomplete_or_different_native_evidence(mutation):
    case = generation_listing_case()
    run, entries, rows, _ = case.packets["a" * 32]
    if mutation == "chain":
        entries[0]["digest"] = "0" * 64
    elif mutation == "unknown":
        rows[-1].data["effects"]["start"]["status"] = "intent"
    elif mutation == "failed_qa":
        entries[-1]["payload"]["checks"][0]["exit_code"] = 1
    elif mutation == "pin":
        run["framework_knowledge_pin"]["corpus_ref"] = "f" * 32
    elif mutation == "runtime":
        run["framework_runtime_attestation"]["observed"]["version"] = "3.11.9"
    elif mutation == "source":
        run["source_tree"] = "f" * 40
    elif mutation == "command_shape":
        run["framework_runtime_attestation"]["command"] = []
    elif mutation == "execution_id":
        run["framework_runtime_attestation"]["command"]["execution_id"] = 42
    else:
        run["framework_runtime_attestation"] = None
    page = await case.manager.generation_candidates("pyfly", "b" * 32)
    assert page["items"] == [] and page["next_cursor"] is None


async def test_candidate_pagination_advances_across_empty_ineligible_pages():
    case = generation_listing_case(3)
    case.packets["a" * 32][0]["framework_runtime_attestation"] = None
    first = await case.manager.generation_candidates("pyfly", "b" * 32, page_size=1)
    assert first["items"] == [] and first["next_cursor"] == "a" * 32
    second = await case.manager.generation_candidates("pyfly", "b" * 32, cursor=first["next_cursor"], page_size=1)
    assert [item["validation_run_id"] for item in second["items"]] == ["b" * 32]
    assert second["next_cursor"] == "b" * 32
    third = await case.manager.generation_candidates("pyfly", "b" * 32, cursor=second["next_cursor"], page_size=1)
    assert [item["validation_run_id"] for item in third["items"]] == ["c" * 32]
    assert third["next_cursor"] is None


@pytest.mark.parametrize("choices", [{"page_size": 0}, {"page_size": 201}, {"page_size": True}, {"cursor": "bad"}])
async def test_candidate_windows_refuse_invalid_choices_before_scanning(choices):
    case = generation_listing_case()
    with pytest.raises(ValueError, match="cursor|window|identifier"):
        await case.manager.generation_candidates("pyfly", "b" * 32, **choices)
    assert not case.queries


@pytest.mark.parametrize("when", ["initial", "after_read"])
async def test_candidate_listing_requires_current_native_administrator_before_and_after_reads(when):
    from firefly_forge.project_access import AccessDenied

    case = generation_listing_case()
    authorize = case.manager._authorize

    async def revoked(*args, **kwargs):
        if when == "initial" or case.queries:
            raise AccessDenied("Current native administrator revoked")
        return await authorize(*args, **kwargs)

    case.manager._authorize = revoked
    with pytest.raises(AccessDenied, match="revoked"):
        await case.manager.generation_candidates("pyfly", "b" * 32)
    if when == "initial":
        assert not case.queries


@pytest.mark.parametrize("mutation", ["unready", "connection", "generation"])
async def test_candidate_listing_refuses_unready_or_changed_corpus_connection(mutation):
    case = generation_listing_case()
    if mutation == "unready":
        case.corpus["public"]["state"] = "indexing"
    elif mutation == "connection":
        case.settings["endpoint"] = "http://changed.invalid"
    else:
        read = case.manager.records.generation_runs

        async def changed(*args):
            result = await read(*args)
            case.manager.store.configuration.settings_generation = object()
            return result

        case.manager.records.generation_runs = changed
    with pytest.raises(ValueError, match="ready|connection|changed"):
        await case.manager.generation_candidates("pyfly", "b" * 32)


def test_candidate_query_uses_exact_scoped_succeeded_runs_and_uuid_keyset():
    from sqlalchemy.dialects import postgresql

    from firefly_forge.framework_knowledge_persistence import FrameworkKnowledgeRecords

    query = FrameworkKnowledgeRecords.generation_runs_query("pyfly", "b" * 32, "a" * 32, 51)
    compiled = query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "succeeded" in sql and "framework_knowledge_pin" in sql and "corpus_ref" in sql and "pyfly" in sql
    assert "forge_runs.id >" in sql and "ORDER BY forge_runs.id" in sql and "LIMIT 51" in sql and "OFFSET" not in sql


@pytest.mark.parametrize("mutation", ["effect_order", "runtime_command_refs"])
def test_rehashed_native_records_still_require_exact_effect_order_and_receipt_references(mutation):
    import copy

    from firefly_forge.framework_knowledge_generation import validate_generation_evidence
    from firefly_forge.journal import ZERO_HASH, digest

    run, entries, rows, corpus = generation_case()
    if mutation == "effect_order":
        entries[2], entries[3] = entries[3], entries[2]
    else:
        run["framework_runtime_attestation"]["command"]["cleanup_key"] = "f" * 32 + ":cleanup"
        runtime = next(e for e in entries if e["kind"] == "framework_runtime_attestation")
        runtime["payload"] = {
            k: copy.deepcopy(v) for k, v in run["framework_runtime_attestation"].items() if k != "event_digest"
        }
        runtime["idempotency_key"] = digest(runtime["payload"])
    previous = ZERO_HASH
    for sequence, entry in enumerate(entries, 1):
        entry.update(sequence=sequence, previous_hash=previous)
        entry["digest"] = digest({k: v for k, v in entry.items() if k != "digest"})
        previous = entry["digest"]
        if entry["kind"] == "framework_runtime_attestation":
            run["framework_runtime_attestation"]["event_digest"] = entry["digest"]
    with pytest.raises(ValueError):
        validate_generation_evidence(run, entries, rows, corpus)
