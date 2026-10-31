# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline transfer rows retain deterministic native identity and original history."""

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_native_location_ledger_host import events
from test_native_location_ledger_host import ledger_case as _ledger_case

from firefly_forge.journal import digest
from firefly_forge.native_location_maintenance import native_location_rows, topology_base
from firefly_forge.native_location_runtime import location_record_id


@pytest.fixture(name="ledger_case")
def native_ledger(tmp_path):
    return _ledger_case.__wrapped__(tmp_path)


def test_native_rows_require_their_exact_durable_id_and_combined_mode(ledger_case):
    history = events(ledger_case)
    rows = [SimpleNamespace(id=location_record_id(v), mode="combined", data=v) for v in history]
    assert native_location_rows(rows) == history
    for change in ("id", "mode"):
        bad = copy.deepcopy(rows)
        setattr(bad[0], change, uuid4() if change == "id" else "split")
        with pytest.raises(ValueError, match="native record"):
            native_location_rows(bad)


def test_legacy_storage_attestation_is_bound_without_rewriting_base(ledger_case):
    from firefly_forge.runtime_roles import STORAGE_ATTESTATION_ID, TOPOLOGY_ID

    _, _, data, _, _ = ledger_case
    old = {k: v for k, v in data.items() if k != "storage_identity"}
    row = SimpleNamespace(id=TOPOLOGY_ID, mode="combined", data=old)
    attestation = SimpleNamespace(
        id=STORAGE_ATTESTATION_ID,
        mode="combined",
        data={**data, "topology_id": TOPOLOGY_ID.hex},
    )
    before = copy.deepcopy(old)
    result = topology_base(row, attestation)
    assert result["storage_identity"] == data["storage_identity"]
    assert result["storage_binding"] == data["storage_binding"]
    assert old == before
    attestation.data["storage_binding"] = "f" * 64
    with pytest.raises(ValueError, match="attestation"):
        topology_base(row, attestation)


async def test_database_fingerprint_binds_cluster_and_native_database_schema_not_restart(monkeypatch):
    from pathlib import Path

    import firefly_forge.native_location_maintenance as module
    from firefly_forge.native_location_files import NativePostgresIdentity

    value = NativePostgresIdentity(Path("/local/postgres"), "1234", 1, 1000, 18545)
    monkeypatch.setattr(module, "read_postgres_identity", lambda path: value)

    class Session:
        async def execute(self, query, params):
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    one=lambda: {
                        "database": "factory",
                        "database_oid": 42,
                        "schema_oid": 43,
                        "port": 18545,
                        "started": 1000,
                        "address": "127.0.0.1",
                    }
                )
            )

    first = await module.database_fingerprint(Session(), "forge", value.directory)
    value = NativePostgresIdentity(value.directory, "1234", 99, 1000, 18545)
    assert await module.database_fingerprint(Session(), "forge", value.directory) == first
    value = NativePostgresIdentity(value.directory, "5678", 99, 1000, 18545)
    assert await module.database_fingerprint(Session(), "forge", value.directory) != first
    value = NativePostgresIdentity(value.directory, "5678", 99, 1001, 18545)
    with pytest.raises(ValueError, match="postmaster"):
        await module.database_fingerprint(Session(), "forge", value.directory)


async def test_runtime_start_requires_the_completed_leaf_and_existing_identity(ledger_case, monkeypatch):
    from contextlib import asynccontextmanager
    from pathlib import Path

    from firefly_forge.runtime_roles import TOPOLOGY_ID, RuntimeRoles

    _, _, base, candidate, request = ledger_case
    base["revision"] = 1
    row = SimpleNamespace(id=TOPOLOGY_ID, mode="combined", data=base)
    bound = topology_base(row, None)
    candidate["base_topology_digest"] = digest(bound)
    candidate["digest"] = digest({k: v for k, v in candidate.items() if k != "digest"})
    request["candidate_digest"] = candidate["digest"]
    history = events(ledger_case)

    class Records:
        async def find_by_id(self, key, **kwargs):
            return row if key == TOPOLOGY_ID else None

        async def native_locations(self):
            return [SimpleNamespace(id=location_record_id(v), mode="combined", data=v) for v in history]

    class Session:
        async def execute(self, *args, **kwargs):
            return None

    @asynccontextmanager
    async def unit():
        yield Session()

    import firefly_forge.native_location_maintenance as module

    async def fingerprint(*args):
        return "b" * 64

    monkeypatch.setattr(module, "database_fingerprint", fingerprint)
    store = SimpleNamespace(path=Path(candidate["plan"]["target_storage"]) / "factory.db", schema="forge", _unit=unit)
    roles = RuntimeRoles(store, records=Records())
    await roles.start()
    assert roles.started
    assert store.native_location_state.receipt_digest == history[-1]["digest"]
    store.path = Path("/retained/storage/factory.db")
    with pytest.raises((ValueError, RuntimeError), match="identity|leaf|private storage"):
        await RuntimeRoles(store, records=Records()).start()


async def test_atomic_transfer_is_idempotent_and_rolls_back_after_append(ledger_case, monkeypatch):
    from contextlib import asynccontextmanager
    from pathlib import Path

    import firefly_forge.native_location_maintenance as module
    from firefly_forge.native_location_models import NativeLocationAcknowledgmentV1, NativeLocationCandidateV1
    from firefly_forge.runtime_roles import TOPOLOGY_ID

    _, identity, raw, candidate, request = ledger_case
    row = SimpleNamespace(id=TOPOLOGY_ID, mode="combined", data=raw)
    candidate["base_topology_digest"] = digest(topology_base(row, None))
    candidate["digest"] = digest({k: v for k, v in candidate.items() if k != "digest"})
    request["candidate_digest"] = candidate["digest"]
    appended = []
    fail = False
    checks = 0

    class Records:
        async def find_by_id(self, key, **kwargs):
            return row if key == TOPOLOGY_ID else None

        async def native_locations(self):
            return appended.copy()

        async def save(self, value):
            appended.append(value)

    class Session:
        async def execute(self, *args, **kwargs):
            return None

        async def scalar(self, *args, **kwargs):
            return True

        async def flush(self):
            if fail:
                raise RuntimeError("fictional post-append fault")

    @asynccontextmanager
    async def unit():
        old = appended.copy()
        try:
            yield Session()
        except BaseException:
            appended[:] = old
            raise

    async def fence():
        nonlocal checks
        checks += 1

    async def fingerprint(*args):
        return candidate["database_fingerprint"]

    async def preserved(*args, **kwargs):
        return candidate["preserved_native_digest"]

    monkeypatch.setattr(module, "database_fingerprint", fingerprint)
    monkeypatch.setattr(module, "preserved_native_digest", preserved)
    store = SimpleNamespace(path=Path(candidate["plan"]["target_storage"]) / "factory.db", schema="forge", _unit=unit)
    maintenance = module.NativeLocationMaintenance(store, Records())
    prepared = module.PreparedNativeLocation(NativeLocationCandidateV1.model_validate(candidate), identity, fence)
    acknowledgment = NativeLocationAcknowledgmentV1.model_validate(request)
    fail = True
    with pytest.raises(RuntimeError, match="post-append"):
        await maintenance.commit(prepared, acknowledgment)
    assert appended == []
    fail = False
    first = await maintenance.commit(prepared, acknowledgment)
    assert len(appended) == 2
    before = checks
    assert await maintenance.commit(prepared, acknowledgment) == first
    assert len(appended) == 2
    assert checks == before  # Saved result readback does not repeat filesystem work.
    assert raw == row.data


async def test_store_existing_only_initialization_never_migrates(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    import firefly_forge.store as module
    from firefly_forge.migrations import SCHEMA_REVISION

    calls = []

    class Session:
        async def execute(self, query, values=None):
            calls.append(str(query))

        async def scalar(self, query, values=None):
            calls.append(str(query))
            return SCHEMA_REVISION

    class Provider:
        @asynccontextmanager
        async def unit(self):
            yield Session()

    async def forbidden(*args):
        raise AssertionError("Offline maintenance attempted a schema migration")

    monkeypatch.setattr(module, "migrate", forbidden)
    store = module.Store(tmp_path / "state" / "factory.db", session_provider=Provider())
    await store.initialize(existing_only=True)
    assert store._initialized
    assert not any("CREATE" in query for query in calls)


def test_existing_only_store_construction_does_not_create_private_directories(tmp_path):
    from firefly_forge.store import Store

    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="existing"):
        Store(missing / "factory.db", existing_only=True)
    assert not missing.exists()
